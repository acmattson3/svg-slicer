from __future__ import annotations

import io
import logging
import math
from typing import List, Tuple

from shapely.affinity import affine_transform as shapely_affine_transform
from shapely.geometry import LineString, Polygon

from .config import SamplingConfig
from .svg_parser import (
    ShapeGeometry,
    _color_to_brightness,
    _fit_lines_to_bounds,
    _fit_polygons_to_bounds,
    _glyph_data_for_character,
    _geometry_to_lines,
    _geometry_to_polygons,
    _hershey_grouped_lines_for_text,
    _merge_connected_ordered_lines,
    _normalize_hershey_text,
    _raster_pil_image_to_shape_geometries,
    _resolve_visibility,
    _text_string_to_polygons,
    _text_supported_by_hershey,
    _vectorize_pil_image_to_shape_geometries,
)


logger = logging.getLogger(__name__)

_PAGE_BORDER_TOLERANCE = 0.5
_PDF_LINE_MERGE_TOLERANCE = 1e-6


def _pdf_rgb_to_tuple(value) -> tuple[int, int, int] | None:
    if value is None:
        return None
    if isinstance(value, int):
        r = (value >> 16) & 0xFF
        g = (value >> 8) & 0xFF
        b = value & 0xFF
        return (r, g, b)
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(max(0, min(255, int(round(float(channel) * 255.0)))) for channel in value[:3])
    return None


def _rgb_to_brightness(rgb: tuple[int, int, int] | None) -> float:
    if rgb is None:
        return 0.0
    r, g, b = rgb
    return max(0.0, min(1.0, (0.299 * r + 0.587 * g + 0.114 * b) / 255.0))


def _rect_matches_page(rect, page_rect, *, tolerance: float = _PAGE_BORDER_TOLERANCE) -> bool:
    if rect is None or page_rect is None:
        return False
    return (
        abs(float(rect.x0) - float(page_rect.x0)) <= tolerance
        and abs(float(rect.y0) - float(page_rect.y0)) <= tolerance
        and abs(float(rect.x1) - float(page_rect.x1)) <= tolerance
        and abs(float(rect.y1) - float(page_rect.y1)) <= tolerance
    )


def _sample_cubic(p0, p1, p2, p3, tolerance: float, detail_scale: float) -> List[Tuple[float, float]]:
    def point(t: float) -> Tuple[float, float]:
        inv = 1.0 - t
        x = (
            inv * inv * inv * p0.x
            + 3.0 * inv * inv * t * p1.x
            + 3.0 * inv * t * t * p2.x
            + t * t * t * p3.x
        )
        y = (
            inv * inv * inv * p0.y
            + 3.0 * inv * inv * t * p1.y
            + 3.0 * inv * t * t * p2.y
            + t * t * t * p3.y
        )
        return (float(x), float(y))

    chord = math.hypot(p3.x - p0.x, p3.y - p0.y)
    control = (
        math.hypot(p1.x - p0.x, p1.y - p0.y)
        + math.hypot(p2.x - p1.x, p2.y - p1.y)
        + math.hypot(p3.x - p2.x, p3.y - p2.y)
    )
    detail = detail_scale if detail_scale and detail_scale > 0 else 1.0
    steps = max(1, int(math.ceil(max(chord, control) / max(tolerance, 0.1) * detail)))
    return [point(i / steps) for i in range(1, steps + 1)]


def _drawing_items_to_lines(items, sampling: SamplingConfig, page_rect=None) -> List[LineString]:
    lines: List[LineString] = []
    current: List[Tuple[float, float]] = []

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            line = LineString(current)
            if not line.is_empty and line.length > 0:
                lines.append(line)
        current = []

    for item in items:
        op = item[0]
        if op == "l":
            start, end = item[1], item[2]
            start_xy = (float(start.x), float(start.y))
            end_xy = (float(end.x), float(end.y))
            if not current or current[-1] != start_xy:
                flush()
                current = [start_xy]
            current.append(end_xy)
        elif op == "c":
            start, c1, c2, end = item[1], item[2], item[3], item[4]
            start_xy = (float(start.x), float(start.y))
            if not current or current[-1] != start_xy:
                flush()
                current = [start_xy]
            current.extend(_sample_cubic(start, c1, c2, end, sampling.segment_tolerance, sampling.curve_detail_scale))
        elif op == "re":
            flush()
            rect = item[1]
            if _rect_matches_page(rect, page_rect):
                logger.debug("Skipping PDF rectangle that matches the full page bounds.")
                continue
            coords = [
                (float(rect.x0), float(rect.y0)),
                (float(rect.x1), float(rect.y0)),
                (float(rect.x1), float(rect.y1)),
                (float(rect.x0), float(rect.y1)),
                (float(rect.x0), float(rect.y0)),
            ]
            lines.append(LineString(coords))
        else:
            logger.debug("Skipping unsupported PDF drawing operator: %s", op)
            flush()
    flush()
    return lines


def _drawing_to_shapes(drawing, sampling: SamplingConfig, page_rect=None) -> List[ShapeGeometry]:
    shapes: List[ShapeGeometry] = []
    lines = _drawing_items_to_lines(drawing.get("items", []), sampling, page_rect=page_rect)
    if not lines:
        return shapes

    fill_rgb = _pdf_rgb_to_tuple(drawing.get("fill"))
    stroke_rgb = _pdf_rgb_to_tuple(drawing.get("color"))
    width = float(drawing.get("width") or 0.0)
    fill_opacity = float(drawing.get("fill_opacity") or 0.0)
    stroke_opacity = float(drawing.get("stroke_opacity") or 0.0)

    if fill_rgb is not None and fill_opacity > 0:
        polygons: List[Polygon] = []
        for line in lines:
            coords = list(line.coords)
            if len(coords) >= 4 and coords[0] == coords[-1]:
                polygon = Polygon(coords).buffer(0)
                polygons.extend(poly for poly in _geometry_to_polygons(polygon) if poly.area > 0)
        for polygon in polygons:
            shapes.append(
                ShapeGeometry(
                    geometry=polygon,
                    brightness=_rgb_to_brightness(fill_rgb),
                    stroke_width=None,
                    color=fill_rgb,
                )
            )

    if stroke_rgb is not None and stroke_opacity > 0 and width >= 0:
        for line in lines:
            if line.is_empty or line.length <= 0:
                continue
            shapes.append(
                ShapeGeometry(
                    geometry=line,
                    brightness=_rgb_to_brightness(stroke_rgb),
                    stroke_width=width,
                    color=stroke_rgb,
                )
            )
    return shapes


def _merge_pdf_stroke_shapes(shapes: List[ShapeGeometry]) -> List[ShapeGeometry]:
    merged_shapes: List[ShapeGeometry] = []
    pending_lines: List[LineString] = []
    pending_style: tuple[
        float | None,
        float,
        tuple[int, int, int] | None,
        str | None,
        str | None,
    ] | None = None

    def flush_pending() -> None:
        nonlocal pending_lines, pending_style
        if not pending_lines or pending_style is None:
            pending_lines = []
            pending_style = None
            return
        stroke_width, brightness, color, toolpath_tag, toolpath_group = pending_style
        for line in _merge_connected_ordered_lines(pending_lines, tolerance=_PDF_LINE_MERGE_TOLERANCE):
            if line.is_empty or line.length <= 0:
                continue
            merged_shapes.append(
                ShapeGeometry(
                    geometry=line,
                    brightness=brightness,
                    stroke_width=stroke_width,
                    color=color,
                    toolpath_tag=toolpath_tag,
                    toolpath_group=toolpath_group,
                )
            )
        pending_lines = []
        pending_style = None

    for shape in shapes:
        if (
            shape.stroke_width is not None
            and isinstance(shape.geometry, LineString)
            and not shape.geometry.is_empty
            and shape.geometry.length > 0
        ):
            style = (
                shape.stroke_width,
                shape.brightness,
                shape.color,
                shape.toolpath_tag,
                shape.toolpath_group,
            )
            if pending_style is None or pending_style == style:
                pending_style = style
                pending_lines.append(shape.geometry)
                continue
        flush_pending()
        merged_shapes.append(shape)

    flush_pending()
    return merged_shapes


def _text_block_to_shapes(block) -> List[ShapeGeometry]:
    shapes: List[ShapeGeometry] = []
    for line in block.get("lines", []):
        direction = line.get("dir", (1.0, 0.0))
        angle = math.atan2(float(direction[1]), float(direction[0]))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        spans = line.get("spans", [])
        if not spans:
            continue

        text_parts = [
            "".join(char.get("c", "") for char in span.get("chars", []))
            for span in spans
        ]
        text = _normalize_hershey_text(" ".join(part for part in text_parts if part))
        if not text:
            continue

        first_span = spans[0]
        origin = line.get("origin") or first_span.get("origin") or (0.0, 0.0)
        size = float(first_span.get("size") or 12.0)
        rgb = _pdf_rgb_to_tuple(first_span.get("color")) or (0, 0, 0)
        transform = [cos_a, sin_a, -sin_a, cos_a, float(origin[0]), float(origin[1])]

        span_bounds = [span.get("bbox") for span in spans if span.get("bbox") and len(span.get("bbox")) >= 4]
        nonspace_chars = [
            char
            for span in spans
            for char in span.get("chars", [])
            if not str(char.get("c", "")).isspace() and char.get("bbox") and len(char.get("bbox")) >= 4
        ]
        target_bounds = None
        if span_bounds and nonspace_chars:
            try:
                import fitz

                first_bbox = nonspace_chars[0]["bbox"]
                text_width = fitz.get_text_length(
                    text,
                    fontname=first_span.get("font") or "helv",
                    fontsize=size,
                )
                if abs(sin_a) < 1e-7:
                    target_bounds = (
                        float(first_bbox[0]),
                        min(float(bbox[1]) for bbox in span_bounds),
                        float(first_bbox[0]) + text_width,
                        max(float(bbox[3]) for bbox in span_bounds),
                    )
            except Exception:
                target_bounds = None
        if target_bounds is None and span_bounds:
            target_bounds = (
                min(float(bbox[0]) for bbox in span_bounds),
                min(float(bbox[1]) for bbox in span_bounds),
                max(float(bbox[2]) for bbox in span_bounds),
                max(float(bbox[3]) for bbox in span_bounds),
            )
        if not _text_supported_by_hershey(text, size):
            grouped_raw_lines = []
            current_x = 0.0
            for span_index, span in enumerate(spans):
                span_size = float(span.get("size") or size)
                for char_index, char in enumerate(span.get("chars", [])):
                    char_text = str(char.get("c", "") or "")
                    if not char_text:
                        continue
                    char_bbox = char.get("bbox")
                    char_width = 0.0
                    if char_bbox and len(char_bbox) >= 4:
                        char_width = max(0.0, float(char_bbox[2]) - float(char_bbox[0]))
                    if char_text.isspace():
                        current_x += char_width
                        continue
                    glyph_lines, advance_x = _glyph_data_for_character(char_text, span_size)
                    if glyph_lines:
                        char_group = f"pdf-text:{origin[0]:.3f}:{origin[1]:.3f}:{span_index}:{char_index}"
                        for line in glyph_lines:
                            grouped_raw_lines.append(
                                (
                                    char_group,
                                    LineString(
                                        [
                                            (current_x + float(px), -float(py))
                                            for px, py in line
                                        ]
                                    ),
                                )
                            )
                        current_x += advance_x if advance_x > 0 else char_width
                    else:
                        if char_bbox and len(char_bbox) >= 4:
                            char_bounds = (
                                float(char_bbox[0]),
                                float(char_bbox[1]),
                                float(char_bbox[2]),
                                float(char_bbox[3]),
                            )
                            polygons = _text_string_to_polygons(
                                char_text,
                                font_size=span_size,
                                font_family=span.get("font") or first_span.get("font"),
                            )
                            polygons = _fit_polygons_to_bounds(
                                polygons,
                                char_bounds,
                                anchor_x="left",
                                anchor_y="top",
                            )
                            char_width = char_bounds[2] - char_bounds[0]
                            if char_width > 0:
                                polygons = [
                                    shapely_affine_transform(
                                        polygon,
                                        [1.0, 0.0, 0.0, 1.0, -(char_width * 0.25), 0.0],
                                    )
                                    for polygon in polygons
                                ]
                            for polygon in polygons:
                                if polygon.is_empty or polygon.area <= 0:
                                    continue
                                shapes.append(
                                    ShapeGeometry(
                                        geometry=polygon,
                                        brightness=_rgb_to_brightness(rgb),
                                        stroke_width=None,
                                        color=rgb,
                                    )
                                )
                        current_x += char_width
            transformed_lines = [
                (group, shapely_affine_transform(hershey_line, transform))
                for group, hershey_line in grouped_raw_lines
            ]
            fitted_lines = _fit_lines_to_bounds([line for _, line in transformed_lines], target_bounds)
            for index, geom in enumerate(fitted_lines):
                if geom.is_empty or geom.length <= 0:
                    continue
                shapes.append(
                    ShapeGeometry(
                        geometry=geom,
                        brightness=_rgb_to_brightness(rgb),
                        stroke_width=0.0,
                        color=rgb,
                        toolpath_group=transformed_lines[min(index, len(transformed_lines) - 1)][0] if transformed_lines else None,
                    )
                )
        else:
            grouped_raw_lines = _hershey_grouped_lines_for_text(
                text,
                x_base=0.0,
                y_base=0.0,
                font_size=size,
                group_prefix=f"pdf-text:{origin[0]:.3f}:{origin[1]:.3f}:{text}",
            )
            transformed_lines = [
                (group, shapely_affine_transform(hershey_line, transform))
                for group, hershey_line in grouped_raw_lines
            ]
            fitted_lines = _fit_lines_to_bounds([line for _, line in transformed_lines], target_bounds)
            for index, geom in enumerate(fitted_lines):
                if geom.is_empty or geom.length <= 0:
                    continue
                shapes.append(
                    ShapeGeometry(
                        geometry=geom,
                        brightness=_rgb_to_brightness(rgb),
                        stroke_width=0.0,
                        color=rgb,
                        toolpath_group=transformed_lines[min(index, len(transformed_lines) - 1)][0] if transformed_lines else None,
                    )
                )
    return shapes


def _image_block_to_shapes(block, sampling: SamplingConfig) -> List[ShapeGeometry]:
    image_bytes = block.get("image")
    bbox = block.get("bbox")
    if not image_bytes or not bbox:
        return []
    try:
        from PIL import Image as PILImage

        pil_image = PILImage.open(io.BytesIO(image_bytes))
        pil_image.load()
    except Exception as exc:  # pragma: no cover - bad image data
        logger.debug("Skipping PDF image block that cannot be loaded: %s", exc)
        return []
    helper = (
        _vectorize_pil_image_to_shape_geometries
        if getattr(sampling, "image_mode", "raster") == "vectorize"
        else _raster_pil_image_to_shape_geometries
    )
    return helper(
        pil_image,
        (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        sampling,
        clip_geom=None,
    )


def parse_pdf(
    pdf_path: str,
    sampling: SamplingConfig,
    *,
    page_number: int = 1,
    force_hershey_text: bool = False,
) -> List[ShapeGeometry]:
    try:
        import fitz
    except Exception as exc:  # pragma: no cover - dependency import issues
        raise RuntimeError("PDF support requires PyMuPDF. Install it with `pip install PyMuPDF`.") from exc

    if page_number < 1:
        raise ValueError("PDF page number must be 1 or greater.")

    doc = fitz.open(pdf_path)
    try:
        if page_number > doc.page_count:
            raise ValueError(f"PDF page {page_number} is out of range; document has {doc.page_count} pages.")
        page = doc[page_number - 1]
        shapes: List[ShapeGeometry] = []

        for drawing in page.get_drawings():
            shapes.extend(_drawing_to_shapes(drawing, sampling, page_rect=page.rect))

        text_dict = page.get_text("rawdict")
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:
                shapes.extend(_text_block_to_shapes(block))

        image_dict = page.get_text("dict")
        for block in image_dict.get("blocks", []):
            if block.get("type") == 1:
                shapes.extend(_image_block_to_shapes(block, sampling))

        return _resolve_visibility(_merge_pdf_stroke_shapes(shapes))
    finally:
        doc.close()
