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
    _geometry_to_lines,
    _geometry_to_polygons,
    _hershey_lines_for_text,
    _raster_pil_image_to_shape_geometries,
    _resolve_visibility,
)


logger = logging.getLogger(__name__)


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


def _drawing_items_to_lines(items, sampling: SamplingConfig) -> List[LineString]:
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


def _drawing_to_shapes(drawing, sampling: SamplingConfig) -> List[ShapeGeometry]:
    shapes: List[ShapeGeometry] = []
    lines = _drawing_items_to_lines(drawing.get("items", []), sampling)
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


def _text_block_to_shapes(block) -> List[ShapeGeometry]:
    shapes: List[ShapeGeometry] = []
    for line in block.get("lines", []):
        direction = line.get("dir", (1.0, 0.0))
        angle = math.atan2(float(direction[1]), float(direction[0]))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        for span in line.get("spans", []):
            text = "".join(char.get("c", "") for char in span.get("chars", []))
            if not text:
                continue
            origin = span.get("origin") or line.get("origin") or (0.0, 0.0)
            size = float(span.get("size") or 12.0)
            rgb = _pdf_rgb_to_tuple(span.get("color")) or (0, 0, 0)
            raw_lines = _hershey_lines_for_text(
                text,
                x_base=0.0,
                y_base=0.0,
                font_size=size,
            )
            transform = [cos_a, sin_a, -sin_a, cos_a, float(origin[0]), float(origin[1])]
            for hershey_line in raw_lines:
                geom = shapely_affine_transform(hershey_line, transform)
                if geom.is_empty or geom.length <= 0:
                    continue
                shapes.append(
                    ShapeGeometry(
                        geometry=geom,
                        brightness=_rgb_to_brightness(rgb),
                        stroke_width=0.0,
                        color=rgb,
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
    return _raster_pil_image_to_shape_geometries(
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
            shapes.extend(_drawing_to_shapes(drawing, sampling))

        text_dict = page.get_text("rawdict")
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:
                shapes.extend(_text_block_to_shapes(block))

        image_dict = page.get_text("dict")
        for block in image_dict.get("blocks", []):
            if block.get("type") == 1:
                shapes.extend(_image_block_to_shapes(block, sampling))

        return _resolve_visibility(shapes)
    finally:
        doc.close()
