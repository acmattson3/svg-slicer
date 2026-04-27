from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from shapely.affinity import affine_transform as shapely_affine_transform
from shapely.affinity import scale as shapely_scale
from shapely.affinity import translate as shapely_translate
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from svgelements import Arc, ClipPath, Color, Image, Line, Move, Path, SVG, Text

from .config import PrinterConfig, SamplingConfig


logger = logging.getLogger(__name__)

VALID_ALIGNMENTS = (
    "top-left",
    "top-middle",
    "top-right",
    "center-left",
    "center",
    "center-right",
    "bottom-left",
    "bottom-middle",
    "bottom-right",
)

_ALIGNMENT_FACTORS = {
    "top-left": (0.0, 0.0),
    "top-middle": (0.5, 0.0),
    "top-right": (1.0, 0.0),
    "center-left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "center-right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0),
    "bottom-middle": (0.5, 1.0),
    "bottom-right": (1.0, 1.0),
}

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ShapeGeometry:
    geometry: BaseGeometry
    brightness: float
    stroke_width: float | None = None
    color: tuple[int, int, int] | None = None
    centerline_geometry: BaseGeometry | None = None


def normalize_alignment(alignment: str | None) -> str:
    if alignment is None:
        return "center"

    normalized = str(alignment).strip().lower()
    aliases = {
        "top-center": "top-middle",
        "middle-left": "center-left",
        "middle": "center",
        "middle-center": "center",
        "middle-right": "center-right",
        "bottom-center": "bottom-middle",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _ALIGNMENT_FACTORS:
        choices = ", ".join(VALID_ALIGNMENTS)
        raise ValueError(f"Alignment must be one of: {choices}")
    return normalized


def _shoelace_area(points: Sequence[Tuple[float, float]]) -> float:
    area = 0.0
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        area += (x1 * y2) - (x2 * y1)
    return area / 2.0


def _path_to_polygons(path: Path, tolerance: float, detail_scale: float) -> List[Polygon]:
    # Ensure arcs are approximated by cubic curves for sampling stability.
    approximated = Path(path)
    detail_multiplier = detail_scale if detail_scale and detail_scale > 0 else 1.0
    arc_error_base = tolerance / 10.0 if tolerance else 0.1
    approximated.approximate_arcs_with_cubics(error=arc_error_base / detail_multiplier)

    rings: List[List[Tuple[float, float]]] = []
    current: List[Tuple[float, float]] = []

    for segment in approximated:
        if isinstance(segment, Move):
            if current:
                rings.append(current)
            current = [(segment.end.real, segment.end.imag)]
            continue
        if isinstance(segment, Arc):
            # After conversion arcs should be represented as cubics, but guard anyway.
            approximated.approximate_arcs_with_cubics(error=arc_error_base / detail_multiplier)
            return _path_to_polygons(approximated, tolerance, detail_scale)
        length_error_base = tolerance / 10.0 if tolerance else 0.01
        length = segment.length(error=length_error_base / detail_multiplier)
        if length == 0:
            continue
        step_denominator = max(tolerance, 0.1)
        steps = max(int(length / step_denominator * detail_multiplier), 1)
        # Include start point if this is the first segment in the current ring.
        if not current:
            start_point = segment.start
            current.append((start_point.real, start_point.imag))
        for step in range(1, steps + 1):
            point = segment.point(step / steps)
            current.append((point.real, point.imag))
        # Close rings when encountering Close segments by fall-through.
    if current:
        rings.append(current)

    polygons: List[Polygon] = []
    if not rings:
        return polygons

    loops: List[Polygon] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        polygon = Polygon(ring)
        if polygon.area == 0 or polygon.is_empty:
            continue
        loops.append(polygon)

    if not loops:
        return polygons

    loops.sort(key=lambda poly: poly.area, reverse=True)
    structures: List[dict] = []
    processed: List[dict] = []

    for poly in loops:
        containers = [item for item in processed if item["poly"].contains(poly)]
        depth = len(containers)
        if depth % 2 == 0:
            entry = {"poly": poly, "holes": []}
            structures.append(entry)
            processed.append({"poly": poly, "role": "shell", "ref": entry})
        else:
            shell_containers = [item for item in containers if item.get("role") == "shell"]
            if shell_containers:
                shell_containers.sort(key=lambda item: item["poly"].area)
                shell_containers[-1]["ref"]["holes"].append(poly)
            processed.append({"poly": poly, "role": "hole"})

    for structure in structures:
        shell = structure["poly"]
        shell_holes = structure["holes"]
        hole_coords = [list(hole.exterior.coords) for hole in shell_holes]
        polygons.append(Polygon(shell.exterior.coords, hole_coords))

    return polygons


def _clip_reference(element) -> str | None:
    clip_value = None
    if hasattr(element, "values"):
        clip_value = element.values.get("clip-path")
    if not clip_value:
        return None
    clip_value = clip_value.strip()
    if clip_value.startswith("url(") and clip_value.endswith(")"):
        inner = clip_value[4:-1].strip()
        if inner.startswith("#"):
            return inner[1:]
    return None


def _build_clip_paths(svg: SVG, sampling: SamplingConfig, svg_path: str) -> dict[str, BaseGeometry]:
    clip_geometries: dict[str, BaseGeometry] = {}
    try:
        import xml.etree.ElementTree as ET

        tree = ET.parse(svg_path)
        root = tree.getroot()
        namespace = "{http://www.w3.org/2000/svg}"
        clip_ids = [node.get("id") for node in root.findall(f".//{namespace}clipPath") if node.get("id")]
    except Exception:  # pragma: no cover - fallback for XML issues
        clip_ids = []

    for clip_id in clip_ids:
        element = svg.get_element_by_id(clip_id)
        if not isinstance(element, ClipPath):
            continue
        polygons: List[Polygon] = []
        for child in element:
            try:
                child.reify()
            except Exception:
                pass
            path_data = child.d() if hasattr(child, "d") and callable(child.d) else None
            if not path_data:
                continue
            path = Path(path_data)
            polygons.extend(
                _path_to_polygons(path, sampling.segment_tolerance, sampling.curve_detail_scale)
            )
        if not polygons:
            continue
        geom = unary_union([poly.buffer(0) for poly in polygons if not poly.is_empty])
        if not geom.is_empty:
            clip_geometries[clip_id] = geom
    return clip_geometries


def _color_to_brightness(color: Color | None) -> float:
    if not color or getattr(color, "alpha", 0) == 0:
        return 1.0
    r, g, b = color.red, color.green, color.blue
    brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return max(0.0, min(1.0, brightness))


def _color_to_rgb(color: Color | None) -> tuple[int, int, int] | None:
    if not color or getattr(color, "alpha", 0) == 0:
        return None
    return (int(color.red), int(color.green), int(color.blue))


def _value_to_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (list, tuple)):
        return _value_to_float(value[0] if value else 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        if hasattr(value, "value"):
            try:
                return float(value.value)
            except (TypeError, ValueError):
                try:
                    return float(value.value())  # type: ignore[operator]
                except Exception:
                    return 0.0
    return 0.0


def _matrix_to_affine_params(matrix) -> list[float]:
    if matrix is None:
        return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    a = getattr(matrix, "a", 1.0)
    b = getattr(matrix, "b", 0.0)
    c = getattr(matrix, "c", 0.0)
    d = getattr(matrix, "d", 1.0)
    e = getattr(matrix, "e", 0.0)
    f = getattr(matrix, "f", 0.0)
    return [a, c, b, d, e, f]


def _normalize_font_style(style: str | None) -> str:
    if not style:
        return "normal"
    style_lower = str(style).strip().lower()
    if style_lower in {"normal", "italic", "oblique"}:
        return style_lower
    return "normal"


def _normalize_font_weight(weight) -> str | int:
    if weight is None:
        return "normal"
    if isinstance(weight, (int, float)):
        return int(weight)
    weight_str = str(weight).strip().lower()
    if weight_str.isdigit():
        return int(weight_str)
    valid = {
        "ultralight",
        "light",
        "normal",
        "regular",
        "book",
        "medium",
        "semibold",
        "demibold",
        "bold",
        "heavy",
        "black",
    }
    if weight_str in valid:
        return weight_str
    return "normal"


def _font_properties_for_text(element: Text, font_size: float):
    from matplotlib.font_manager import FontProperties

    family = getattr(element, "font_family", None)
    if isinstance(family, str):
        families = [f.strip().strip("'\"") for f in family.split(",") if f.strip()]
    elif isinstance(family, (list, tuple)):
        families = [str(f).strip().strip("'\"") for f in family if str(f).strip()]
    else:
        families = []
    style = _normalize_font_style(getattr(element, "font_style", None))
    weight = _normalize_font_weight(getattr(element, "font_weight", None))
    return FontProperties(
        family=families or None,
        style=style,
        weight=weight,
        size=font_size,
    )


def _text_font_available(element: Text, font_size: float) -> bool:
    family = getattr(element, "font_family", None)
    if not family:
        return True
    try:
        from matplotlib.font_manager import findfont

        findfont(_font_properties_for_text(element, font_size), fallback_to_default=False)
        return True
    except Exception:
        return False


def _combined_bounds(geometries: Sequence[BaseGeometry]) -> Tuple[float, float, float, float] | None:
    bounds = [
        geometry.bounds
        for geometry in geometries
        if not geometry.is_empty
        and len(geometry.bounds) == 4
        and all(math.isfinite(value) for value in geometry.bounds)
    ]
    if not bounds:
        return None
    return (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        max(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
    )


def _fit_lines_to_bounds(
    lines: List[LineString],
    target_bounds: Tuple[float, float, float, float] | None,
) -> List[LineString]:
    if not lines or target_bounds is None:
        return lines

    line_bounds = _combined_bounds(lines)
    if line_bounds is None:
        return lines

    minx, miny, maxx, maxy = line_bounds
    target_minx, target_miny, target_maxx, target_maxy = target_bounds
    width = maxx - minx
    height = maxy - miny
    target_width = target_maxx - target_minx
    target_height = target_maxy - target_miny
    if width <= 0 or height <= 0 or target_width <= 0 or target_height <= 0:
        return lines

    scale_factor = min(1.0, target_width / width, target_height / height)
    target_center_x = target_minx + target_width / 2.0
    target_center_y = target_miny + target_height / 2.0
    source_center_x = minx + width / 2.0
    source_center_y = miny + height / 2.0

    fitted: List[LineString] = []
    for line in lines:
        geom = line
        if scale_factor < 1.0:
            geom = shapely_scale(
                geom,
                xfact=scale_factor,
                yfact=scale_factor,
                origin=(source_center_x, source_center_y),
            )
        geom = shapely_translate(
            geom,
            xoff=target_center_x - source_center_x,
            yoff=target_center_y - source_center_y,
        )
        if not geom.is_empty and geom.length > 0:
            fitted.append(geom)
    return fitted


def _normalize_hershey_text(text_content: str) -> str:
    return _WHITESPACE_RE.sub(" ", text_content).strip()


def _text_to_polygons(
    element: Text,
    tolerance: float,
    *,
    text_override: str | None = None,
) -> List[Polygon]:
    text_content = text_override if text_override is not None else getattr(element, "text", None)
    if not text_content:
        return []
    font_size_value = getattr(element, "font_size", None)
    font_size = _value_to_float(font_size_value) or 16.0
    if font_size <= 0:
        return []

    try:
        from matplotlib.textpath import TextPath

        font_props = _font_properties_for_text(element, font_size)
        text_path = TextPath(
            (0, 0),
            text_content,
            prop=font_props,
            size=font_size,
            usetex=False,
        )
    except Exception as exc:  # pragma: no cover - backend/font issues
        logger.debug("Unable to convert text '%s' to path: %s", text_content, exc)
        return []

    bbox = text_path.get_extents()
    text_width = bbox.width if bbox.width is not None else 0.0
    anchor = getattr(element, "anchor", None)
    x_base = _value_to_float(getattr(element, "x", 0.0))
    dx = _value_to_float(getattr(element, "dx", 0.0))
    y_base = _value_to_float(getattr(element, "y", 0.0))
    dy = _value_to_float(getattr(element, "dy", 0.0))
    x_reference = bbox.x0
    if anchor in {"middle", "center"}:
        x_reference = bbox.x0 + text_width / 2.0
    elif anchor in {"end", "right"}:
        x_reference = bbox.x1
    x_offset = x_base + dx - x_reference
    y_offset = y_base + dy

    polygons_data = text_path.to_polygons(closed_only=True)
    if not polygons_data:
        return []

    loops: List[Polygon] = []
    for coords in polygons_data:
        if len(coords) < 3:
            continue
        transformed: List[Tuple[float, float]] = []
        for x_val, y_val in coords:
            transformed.append((x_val + x_offset, y_offset - y_val))
        if transformed[0] != transformed[-1]:
            transformed.append(transformed[0])
        polygon = Polygon(transformed)
        if polygon.is_empty or polygon.area == 0:
            continue
        loops.append(polygon)

    if not loops:
        return []

    loops.sort(key=lambda poly: poly.area, reverse=True)
    structures: List[dict] = []
    processed: List[dict] = []
    for poly in loops:
        containers = [item for item in processed if item["poly"].contains(poly)]
        depth = len(containers)
        if depth % 2 == 0:
            entry = {"poly": poly, "holes": []}
            structures.append(entry)
            processed.append({"poly": poly, "role": "shell", "ref": entry})
        else:
            shell_containers = [item for item in containers if item.get("role") == "shell"]
            if shell_containers:
                shell_containers.sort(key=lambda item: item["poly"].area)
                shell_containers[-1]["ref"]["holes"].append(poly)
            processed.append({"poly": poly, "role": "hole"})

    polygons: List[Polygon] = []
    for structure in structures:
        shell = structure["poly"]
        holes = [list(hole.exterior.coords) for hole in structure["holes"]]
        polygon = Polygon(shell.exterior.coords, holes)
        if polygon.is_empty or polygon.area <= 0:
            continue
        polygons.append(polygon)

    matrix = getattr(element, "transform", None)
    if matrix is not None:
        is_identity = getattr(matrix, "is_identity", None)
        identity = False
        if callable(is_identity):
            identity = is_identity()
        if not identity:
            params = _matrix_to_affine_params(matrix)
            transformed_polygons: List[Polygon] = []
            for polygon in polygons:
                transformed = shapely_affine_transform(polygon, params)
                if transformed.is_empty or transformed.area <= 0:
                    continue
                transformed_polygons.append(transformed)
            polygons = transformed_polygons

    return polygons


def _hershey_lines_for_text(
    text_content: str,
    *,
    x_base: float,
    y_base: float,
    font_size: float,
    matrix=None,
) -> List[LineString]:
    text_content = _normalize_hershey_text(text_content)
    if not text_content:
        return []
    try:
        from HersheyFonts import HersheyFonts
    except Exception as exc:  # pragma: no cover - dependency import issues
        logger.debug("Unable to load Hershey font renderer: %s", exc)
        return []

    try:
        font = HersheyFonts()
        font.load_default_font("rowmans")
        font.normalize_rendering(max(font_size, 1e-6))
        raw_lines = list(font.lines_for_text(text_content))
    except Exception as exc:  # pragma: no cover - defensive for bad glyph data
        logger.debug("Unable to render Hershey text '%s': %s", text_content, exc)
        return []

    lines: List[LineString] = []
    for start, end in raw_lines:
        line = LineString(
            [
                (x_base + float(start[0]), y_base - float(start[1])),
                (x_base + float(end[0]), y_base - float(end[1])),
            ]
        )
        if matrix is not None:
            line = shapely_affine_transform(line, _matrix_to_affine_params(matrix))
        if not line.is_empty and line.length > 0:
            lines.append(line)
    return lines


def _text_to_hershey_lines(element: Text) -> List[LineString]:
    text_content = getattr(element, "text", None)
    if not text_content:
        return []
    font_size = _value_to_float(getattr(element, "font_size", None)) or 16.0
    x_base = _value_to_float(getattr(element, "x", 0.0)) + _value_to_float(getattr(element, "dx", 0.0))
    y_base = _value_to_float(getattr(element, "y", 0.0)) + _value_to_float(getattr(element, "dy", 0.0))
    return _hershey_lines_for_text(
        str(text_content),
        x_base=x_base,
        y_base=y_base,
        font_size=font_size,
        matrix=getattr(element, "transform", None),
    )


def _path_to_lines(path: Path, tolerance: float, detail_scale: float) -> List[List[Tuple[float, float]]]:
    approximated = Path(path)
    detail_multiplier = detail_scale if detail_scale and detail_scale > 0 else 1.0
    arc_error_base = tolerance / 10.0 if tolerance else 0.1
    approximated.approximate_arcs_with_cubics(error=arc_error_base / detail_multiplier)

    lines: List[List[Tuple[float, float]]] = []
    current: List[Tuple[float, float]] = []

    for segment in approximated:
        if isinstance(segment, Move):
            if current:
                lines.append(current)
            current = [(segment.end.real, segment.end.imag)]
            continue
        if isinstance(segment, Line):
            if not current:
                start_point = segment.start
                current.append((start_point.real, start_point.imag))
            end_point = (segment.end.real, segment.end.imag)
            if current[-1] != end_point:
                current.append(end_point)
            continue
        length_error_base = tolerance / 10.0 if tolerance else 0.01
        length = segment.length(error=length_error_base / detail_multiplier)
        if length == 0:
            continue
        steps = max(int(length / max(tolerance, 0.1) * detail_multiplier), 1)
        if not current:
            start_point = segment.start
            current.append((start_point.real, start_point.imag))
        for step in range(1, steps + 1):
            point = segment.point(step / steps)
            current.append((point.real, point.imag))
    if current:
        lines.append(current)
    return lines


def _path_to_stroke_polygons(path: Path, stroke_width: float, tolerance: float, detail_scale: float) -> List[Polygon]:
    if stroke_width <= 0:
        return []
    subpaths = _path_to_lines(path, tolerance, detail_scale)
    polygons: List[Polygon] = []
    radius = stroke_width / 2.0
    for points in subpaths:
        if len(points) < 2:
            continue
        line = LineString(points)
        buffered = line.buffer(radius, cap_style=2, join_style=2)
        if buffered.is_empty:
            continue
        for poly in _geometry_to_polygons(buffered):
            if poly.area <= 0:
                continue
            polygons.append(poly)
    return polygons


def _geometry_to_polygons(geometry: BaseGeometry) -> List[Polygon]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]  # type: ignore[return-value]
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)  # type: ignore[return-value]
    if geometry.geom_type == "GeometryCollection":
        polygons: List[Polygon] = []
        for part in geometry.geoms:
            polygons.extend(_geometry_to_polygons(part))
        return polygons
    return []


def _geometry_to_lines(geometry: BaseGeometry) -> List[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry] if geometry.length > 0 else []
    if isinstance(geometry, MultiLineString):
        return [line for line in geometry.geoms if line.length > 0]
    if isinstance(geometry, GeometryCollection):
        lines: List[LineString] = []
        for part in geometry.geoms:
            lines.extend(_geometry_to_lines(part))
        return lines
    return []


def _lines_to_geometry(lines: List[LineString]) -> BaseGeometry | None:
    usable = [line for line in lines if not line.is_empty and line.length > 0]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    return MultiLineString([list(line.coords) for line in usable])


def _resolve_visibility(shapes: List[ShapeGeometry]) -> List[ShapeGeometry]:
    if not shapes:
        return []

    occlusion: BaseGeometry = GeometryCollection()
    visible_reversed: List[ShapeGeometry] = []

    for shape in reversed(shapes):
        geometry = shape.geometry
        if geometry.is_empty:
            continue
        if not occlusion.is_empty:
            geometry = geometry.difference(occlusion)

        line_parts = _geometry_to_lines(geometry)
        polygon_parts = _geometry_to_polygons(geometry)
        if line_parts and not polygon_parts:
            for line in line_parts:
                visible_reversed.append(
                    ShapeGeometry(
                        geometry=line,
                        brightness=shape.brightness,
                        stroke_width=shape.stroke_width,
                        color=shape.color,
                        centerline_geometry=shape.centerline_geometry,
                    )
                )
            continue

        geometry = geometry.buffer(0)
        polys_for_union: List[BaseGeometry] = []
        for polygon in _geometry_to_polygons(geometry):
            if polygon.is_empty or polygon.area <= 0:
                continue
            visible_reversed.append(
                ShapeGeometry(
                    geometry=polygon,
                    brightness=shape.brightness,
                    stroke_width=shape.stroke_width,
                    color=shape.color,
                    centerline_geometry=(
                        _lines_to_geometry(
                            _geometry_to_lines(shape.centerline_geometry.intersection(polygon))
                        )
                        if shape.centerline_geometry is not None
                        else None
                    ),
                )
            )
            polys_for_union.append(polygon)
        if polys_for_union:
            occlusion = occlusion.union(unary_union(polys_for_union))

    return list(reversed(visible_reversed))


def _apply_clip(polygons: List[Polygon], clip: BaseGeometry | None) -> List[Polygon]:
    if clip is None:
        return polygons
    clipped: List[Polygon] = []
    for polygon in polygons:
        inter = polygon.intersection(clip)
        if inter.is_empty:
            continue
        for piece in _geometry_to_polygons(inter):
            if piece.is_empty or piece.area <= 0:
                continue
            clipped.append(piece)
    return clipped


def _apply_clip_to_lines(lines: List[LineString], clip: BaseGeometry | None) -> List[LineString]:
    if clip is None:
        return lines
    clipped: List[LineString] = []
    for line in lines:
        inter = line.intersection(clip)
        clipped.extend(_geometry_to_lines(inter))
    return clipped


def _composite_pixel_rgba(pixel) -> Tuple[tuple[int, int, int], float, float]:
    if len(pixel) == 4:
        r, g, b, a = pixel
    elif len(pixel) == 3:
        r, g, b = pixel
        a = 255
    else:
        r = g = b = pixel[0]
        a = 255
    alpha = max(0.0, min(1.0, a / 255.0))
    inv_alpha = 1.0 - alpha
    comp_r = int(round(r * alpha + 255.0 * inv_alpha))
    comp_g = int(round(g * alpha + 255.0 * inv_alpha))
    comp_b = int(round(b * alpha + 255.0 * inv_alpha))
    brightness = (0.299 * comp_r + 0.587 * comp_g + 0.114 * comp_b) / 255.0
    return (comp_r, comp_g, comp_b), max(0.0, min(1.0, brightness)), alpha


def _raster_pil_image_to_shape_geometries(
    pil_image,
    bbox: Tuple[float, float, float, float],
    sampling: SamplingConfig,
    clip_geom: BaseGeometry | None,
    transform_params: List[float] | None = None,
) -> List[ShapeGeometry]:
    shapes: List[ShapeGeometry] = []

    width_px, height_px = pil_image.size
    if width_px == 0 or height_px == 0:
        return shapes

    minx, miny, maxx, maxy = bbox
    width_world = maxx - minx
    height_world = maxy - miny
    if width_world <= 0 or height_world <= 0:
        return shapes

    spacing = getattr(sampling, "raster_sample_spacing", None)
    if not spacing or spacing <= 0:
        spacing = max(sampling.segment_tolerance, 1.0)
    max_cells = int(getattr(sampling, "raster_max_cells", 4000))
    columns = max(1, min(width_px, int(math.ceil(width_world / spacing))))
    rows = max(1, min(height_px, int(math.ceil(height_world / spacing))))
    total_cells = columns * rows
    if total_cells > max_cells:
        scale = math.sqrt(total_cells / max_cells)
        columns = max(1, min(width_px, int(columns / scale)))
        rows = max(1, min(height_px, int(rows / scale)))

    if columns == 0 or rows == 0:
        return shapes

    try:
        from PIL import Image as PILImage
    except Exception as exc:  # pragma: no cover - optional raster dependency
        logger.debug("Skipping image because Pillow is unavailable: %s", exc)
        return shapes

    resized = pil_image.convert("RGBA").resize((columns, rows), PILImage.BILINEAR)
    pixels = resized.load()

    if transform_params is None:
        transform_params = [width_world, 0.0, 0.0, height_world, minx, miny]

    alpha_threshold = 0.05
    skip_brightness_threshold = 0.995
    brightness_tol = 0.08
    color_tol = 18.0

    def flush_run(run):
        if not run:
            return
        weight = run["weight"]
        if weight <= 0:
            return
        brightness = run["brightness_sum"] / weight
        brightness = max(0.0, min(1.0, brightness))
        color = run["color_sum"]
        if color is not None:
            rgb = tuple(
                max(0, min(255, int(round(channel / weight))))
                for channel in color
            )
        else:
            rgb = None
        x0 = run["start"] / columns
        x1 = run["end"] / columns
        if x1 <= x0:
            return
        row_index = run["row"]
        y0 = row_index / rows
        y1 = (row_index + 1) / rows
        if y1 <= y0:
            return
        base_poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        if base_poly.is_empty or base_poly.area <= 0:
            return
        transformed = shapely_affine_transform(base_poly, transform_params)
        if transformed.is_empty or transformed.area <= 0:
            return
        if clip_geom is not None:
            polygons = _apply_clip([transformed], clip_geom)
        else:
            polygons = [transformed]
        for polygon in polygons:
            polygon = polygon.buffer(0)
            if polygon.is_empty or polygon.area <= 0:
                continue
            shapes.append(
                ShapeGeometry(
                    geometry=polygon,
                    brightness=brightness,
                    stroke_width=None,
                    color=rgb,
                )
            )

    for row in range(rows):
        run = None
        for col in range(columns):
            pixel = pixels[col, row]
            color_rgb, brightness, alpha = _composite_pixel_rgba(pixel)
            if alpha <= alpha_threshold or brightness >= skip_brightness_threshold:
                flush_run(run)
                run = None
                continue

            weight = alpha
            if not run:
                run = {
                    "row": row,
                    "start": col,
                    "end": col + 1,
                    "weight": weight,
                    "brightness_sum": brightness * weight,
                    "avg_brightness": brightness,
                    "color_sum": [channel * weight for channel in color_rgb],
                    "avg_color": color_rgb,
                }
                continue

            avg_brightness = run["brightness_sum"] / run["weight"]
            brightness_diff = abs(brightness - avg_brightness)
            current_color = tuple(channel / run["weight"] for channel in run["color_sum"])
            color_diff = max(abs(color_rgb[i] - current_color[i]) for i in range(3))
            if brightness_diff > brightness_tol or color_diff > color_tol:
                flush_run(run)
                run = {
                    "row": row,
                    "start": col,
                    "end": col + 1,
                    "weight": weight,
                    "brightness_sum": brightness * weight,
                    "avg_brightness": brightness,
                    "color_sum": [channel * weight for channel in color_rgb],
                    "avg_color": color_rgb,
                }
                continue

            run["end"] = col + 1
            run["weight"] += weight
            run["brightness_sum"] += brightness * weight
            for idx in range(3):
                run["color_sum"][idx] += color_rgb[idx] * weight

        flush_run(run)

    return shapes


def _image_to_shape_geometries(
    element: Image, sampling: SamplingConfig, clip_geom: BaseGeometry | None
) -> List[ShapeGeometry]:
    try:
        element.load()
    except Exception as exc:  # pragma: no cover - guard for malformed images
        logger.debug("Skipping image that cannot be loaded: %s", exc)
        return []

    pil_image = getattr(element, "image", None)
    bbox = element.bbox()
    if pil_image is None or bbox is None:
        return []
    return _raster_pil_image_to_shape_geometries(
        pil_image,
        bbox,
        sampling,
        clip_geom,
        transform_params=_matrix_to_affine_params(getattr(element, "transform", None)),
    )


def parse_svg(
    svg_path: str,
    sampling: SamplingConfig,
    *,
    force_hershey_text: bool = False,
) -> List[ShapeGeometry]:
    svg = SVG.parse(svg_path)

    clip_geometries = _build_clip_paths(svg, sampling, svg_path)

    shapes: List[ShapeGeometry] = []

    for element in svg.elements():
        try:
            element.reify()
        except Exception as exc:  # pragma: no cover - guard for malformed elements
            logger.debug("Skipping element that cannot be reified: %s", exc)
            continue

        tolerance = sampling.segment_tolerance
        clip_ref = _clip_reference(element)
        clip_geom = clip_geometries.get(clip_ref) if clip_ref else None

        if isinstance(element, Image):
            image_shapes = _image_to_shape_geometries(element, sampling, clip_geom)
            if image_shapes:
                shapes.extend(image_shapes)
            continue

        is_text = isinstance(element, Text)
        path: Path | None = None
        fill_sources: List[Polygon] = []
        fill_color = getattr(element, "fill", None)
        fill_alpha = getattr(fill_color, "alpha", 0) if fill_color else 0
        has_visible_fill = fill_color is not None and fill_alpha not in (None, 0)

        if is_text:
            text_font_size = _value_to_float(getattr(element, "font_size", None)) or 16.0
            font_available = _text_font_available(element, text_font_size)
            if force_hershey_text or not font_available:
                normalized_text = _normalize_hershey_text(str(getattr(element, "text", "") or ""))
                target_bounds = _combined_bounds(
                    _text_to_polygons(element, tolerance, text_override=normalized_text)
                )
                lines = _fit_lines_to_bounds(_text_to_hershey_lines(element), target_bounds)
                lines = _apply_clip_to_lines(lines, clip_geom)
                if lines:
                    brightness = _color_to_brightness(fill_color)
                    rgb = _color_to_rgb(fill_color)
                    for line in lines:
                        shapes.append(
                            ShapeGeometry(
                                geometry=line,
                                brightness=brightness,
                                stroke_width=0.0,
                                color=rgb,
                            )
                        )
                continue
            fill_sources = _text_to_polygons(element, tolerance)
            if has_visible_fill and not fill_sources:
                lines = _apply_clip_to_lines(_text_to_hershey_lines(element), clip_geom)
                if lines:
                    brightness = _color_to_brightness(fill_color)
                    rgb = _color_to_rgb(fill_color)
                    for line in lines:
                        shapes.append(
                            ShapeGeometry(
                                geometry=line,
                                brightness=brightness,
                                stroke_width=0.0,
                                color=rgb,
                            )
                        )
                continue
        else:
            if not hasattr(element, "d"):
                continue
            try:
                path_data = element.d()
            except Exception as exc:
                logger.debug("Skipping element without path data: %s", exc)
                continue
            if not path_data:
                continue
            path = Path(path_data)
            if has_visible_fill:
                fill_sources = _path_to_polygons(path, tolerance, sampling.curve_detail_scale)

        if has_visible_fill:
            polygons = _apply_clip(fill_sources, clip_geom) if fill_sources else []
            if polygons:
                brightness = _color_to_brightness(fill_color)
                rgb = _color_to_rgb(fill_color)
                for polygon in polygons:
                    polygon = polygon.buffer(0)
                    if polygon.is_empty or polygon.area <= 0:
                        continue
                    shapes.append(
                        ShapeGeometry(
                            geometry=polygon,
                            brightness=brightness,
                            stroke_width=None,
                            color=rgb,
                        )
                    )

        stroke_color = getattr(element, "stroke", None)
        stroke_width = getattr(element, "stroke_width", None)
        stroke_alpha = getattr(stroke_color, "alpha", 0) if stroke_color else 0
        stroke_polygons: List[Polygon] = []
        if stroke_color is not None and stroke_alpha not in (None, 0) and stroke_width is not None:
            try:
                stroke_w = float(stroke_width)
            except (TypeError, ValueError):
                stroke_w = float(getattr(stroke_width, "value", 0))
            if stroke_w > 0:
                stroke_lines: List[LineString] = []
                if is_text:
                    if fill_sources:
                        union_source = unary_union([poly.buffer(0) for poly in fill_sources if not poly.is_empty])
                        if not union_source.is_empty:
                            outer = union_source.buffer(stroke_w / 2.0, cap_style=2, join_style=2)
                            if not outer.is_empty:
                                inner = union_source.buffer(-stroke_w / 2.0, cap_style=2, join_style=2)
                                stroke_geom = outer if inner.is_empty else outer.difference(inner)
                                stroke_polygons = _geometry_to_polygons(stroke_geom)
                else:
                    if path is not None:
                        if sampling.plot_mode in {"centerline", "auto"}:
                            stroke_lines = [
                                LineString(points)
                                for points in _path_to_lines(
                                    path,
                                    tolerance,
                                    sampling.curve_detail_scale,
                                )
                                if len(points) >= 2
                            ]
                            stroke_lines = _apply_clip_to_lines(stroke_lines, clip_geom)
                        if sampling.plot_mode == "centerline":
                            brightness = _color_to_brightness(stroke_color)
                            rgb = _color_to_rgb(stroke_color)
                            for line in stroke_lines:
                                if line.is_empty or line.length <= 0:
                                    continue
                                shapes.append(
                                    ShapeGeometry(
                                        geometry=line,
                                        brightness=brightness,
                                        stroke_width=stroke_w,
                                        color=rgb,
                                    )
                                )
                            continue
                        stroke_polygons = _path_to_stroke_polygons(
                            path, stroke_w, tolerance, sampling.curve_detail_scale
                        )
                if stroke_polygons:
                    stroke_polygons = _apply_clip(stroke_polygons, clip_geom)
                    if stroke_polygons:
                        brightness = _color_to_brightness(stroke_color)
                        rgb = _color_to_rgb(stroke_color)
                        centerline_geometry = (
                            _lines_to_geometry(stroke_lines)
                            if sampling.plot_mode == "auto"
                            else None
                        )
                        for polygon in stroke_polygons:
                            polygon = polygon.buffer(0)
                            if polygon.is_empty or polygon.area <= 0:
                                continue
                            clipped_centerline = (
                                _lines_to_geometry(
                                    _geometry_to_lines(centerline_geometry.intersection(polygon))
                                )
                                if centerline_geometry is not None
                                else None
                            )
                            shapes.append(
                                ShapeGeometry(
                                    geometry=polygon,
                                    brightness=brightness,
                                    stroke_width=stroke_w,
                                    color=rgb,
                                    centerline_geometry=clipped_centerline,
                                )
                            )

    return _resolve_visibility(shapes)


def _combined_shape_bounds(shapes: List[ShapeGeometry]) -> Tuple[float, float, float, float, float, float]:
    bounds = [
        shape.geometry.bounds
        for shape in shapes
        if not shape.geometry.is_empty
    ]
    finite_bounds = [
        bound
        for bound in bounds
        if len(bound) == 4 and all(math.isfinite(value) for value in bound)
    ]
    if not finite_bounds:
        raise ValueError("SVG bounds are invalid; cannot scale.")

    minx = min(bound[0] for bound in finite_bounds)
    miny = min(bound[1] for bound in finite_bounds)
    maxx = max(bound[2] for bound in finite_bounds)
    maxy = max(bound[3] for bound in finite_bounds)
    width = maxx - minx
    height = maxy - miny
    if width == 0 and height == 0:
        raise ValueError("SVG has zero width or height after parsing; cannot scale.")
    return minx, miny, maxx, maxy, width, height


def _place_shapes_with_bounds(
    shapes: List[ShapeGeometry],
    printer: PrinterConfig,
    scale_factor: float,
    minx: float,
    miny: float,
    width: float,
    height: float,
    alignment: str = "center",
) -> Tuple[List[ShapeGeometry], float]:
    alignment = normalize_alignment(alignment)
    x_factor, y_factor = _ALIGNMENT_FACTORS[alignment]
    scaled_width = width * scale_factor
    scaled_height = height * scale_factor
    x_margin = (printer.printable_width - scaled_width) * x_factor
    y_margin = (printer.printable_depth - scaled_height) * y_factor
    x_offset = printer.x_min + x_margin
    y_offset = printer.y_min + y_margin + scaled_height

    translated_shapes: List[ShapeGeometry] = []
    for shape in shapes:
        geom = shapely_translate(shape.geometry, xoff=-minx, yoff=-miny)
        geom = shapely_scale(geom, xfact=scale_factor, yfact=scale_factor, origin=(0, 0))
        geom = shapely_scale(geom, xfact=1.0, yfact=-1.0, origin=(0, 0))
        geom = shapely_translate(geom, xoff=x_offset, yoff=y_offset)
        centerline_geometry = shape.centerline_geometry
        if centerline_geometry is not None:
            centerline_geometry = shapely_translate(centerline_geometry, xoff=-minx, yoff=-miny)
            centerline_geometry = shapely_scale(
                centerline_geometry,
                xfact=scale_factor,
                yfact=scale_factor,
                origin=(0, 0),
            )
            centerline_geometry = shapely_scale(
                centerline_geometry,
                xfact=1.0,
                yfact=-1.0,
                origin=(0, 0),
            )
            centerline_geometry = shapely_translate(
                centerline_geometry,
                xoff=x_offset,
                yoff=y_offset,
            )
        stroke_width = None if shape.stroke_width is None else shape.stroke_width * scale_factor
        translated_shapes.append(
            ShapeGeometry(
                geometry=geom,
                brightness=shape.brightness,
                stroke_width=stroke_width,
                color=shape.color,
                centerline_geometry=centerline_geometry,
            )
        )

    return translated_shapes, scale_factor


def place_shapes_on_bed(
    shapes: List[ShapeGeometry],
    printer: PrinterConfig,
    scale_factor: float,
    alignment: str = "center",
) -> Tuple[List[ShapeGeometry], float]:
    if not shapes:
        return [], scale_factor
    if not math.isfinite(scale_factor) or scale_factor <= 0:
        raise ValueError("Scale factor must be greater than zero.")

    minx, miny, _, _, width, height = _combined_shape_bounds(shapes)
    return _place_shapes_with_bounds(
        shapes,
        printer,
        scale_factor,
        minx,
        miny,
        width,
        height,
        alignment=alignment,
    )


def fit_shapes_to_bed(
    shapes: List[ShapeGeometry],
    printer: PrinterConfig,
    alignment: str = "center",
) -> Tuple[List[ShapeGeometry], float]:
    if not shapes:
        return [], 1.0

    minx, miny, _, _, width, height = _combined_shape_bounds(shapes)

    available_width = printer.printable_width
    available_height = printer.printable_depth
    scale_candidates: List[float] = []
    if width > 0:
        scale_candidates.append(available_width / width)
    if height > 0:
        scale_candidates.append(available_height / height)
    if not scale_candidates:
        raise ValueError("Printer has no printable area; cannot scale SVG.")
    scale_factor = min(scale_candidates)

    return _place_shapes_with_bounds(
        shapes,
        printer,
        scale_factor,
        minx,
        miny,
        width,
        height,
        alignment=alignment,
    )
