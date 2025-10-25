from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from shapely.affinity import affine_transform as shapely_affine_transform
from shapely.affinity import scale as shapely_scale
from shapely.affinity import translate as shapely_translate
from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from svgelements import Arc, ClipPath, Color, Move, Path, SVG, Text
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath

from .config import PrinterConfig, SamplingConfig


logger = logging.getLogger(__name__)


@dataclass
class ShapeGeometry:
    geometry: BaseGeometry
    brightness: float
    stroke_width: float | None = None
    color: tuple[int, int, int] | None = None


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


def _font_properties_for_text(element: Text, font_size: float) -> FontProperties:
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


def _text_to_polygons(element: Text, tolerance: float) -> List[Polygon]:
    text_content = getattr(element, "text", None)
    if not text_content:
        return []
    font_size_value = getattr(element, "font_size", None)
    font_size = _value_to_float(font_size_value) or 16.0
    if font_size <= 0:
        return []

    try:
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


def parse_svg(svg_path: str, sampling: SamplingConfig) -> List[ShapeGeometry]:
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

        is_text = isinstance(element, Text)
        path: Path | None = None
        fill_sources: List[Polygon] = []

        if is_text:
            fill_sources = _text_to_polygons(element, tolerance)
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
            fill_sources = _path_to_polygons(path, tolerance, sampling.curve_detail_scale)

        fill_color = getattr(element, "fill", None)
        fill_alpha = getattr(fill_color, "alpha", 0) if fill_color else 0
        if fill_color is not None and fill_alpha not in (None, 0):
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
                        stroke_polygons = _path_to_stroke_polygons(
                            path, stroke_w, tolerance, sampling.curve_detail_scale
                        )
                if stroke_polygons:
                    stroke_polygons = _apply_clip(stroke_polygons, clip_geom)
                    if stroke_polygons:
                        brightness = _color_to_brightness(stroke_color)
                        rgb = _color_to_rgb(stroke_color)
                        for polygon in stroke_polygons:
                            polygon = polygon.buffer(0)
                            if polygon.is_empty or polygon.area <= 0:
                                continue
                            shapes.append(
                                ShapeGeometry(
                                    geometry=polygon,
                                    brightness=brightness,
                                    stroke_width=stroke_w,
                                    color=rgb,
                                )
                            )

    return _resolve_visibility(shapes)


def fit_shapes_to_bed(shapes: List[ShapeGeometry], printer: PrinterConfig) -> Tuple[List[ShapeGeometry], float]:
    if not shapes:
        return [], 1.0

    combined = shapes[0].geometry
    for shape in shapes[1:]:
        combined = combined.union(shape.geometry)

    minx, miny, maxx, maxy = combined.bounds
    width = maxx - minx
    height = maxy - miny
    if width == 0 or height == 0:
        raise ValueError("SVG has zero width or height after parsing; cannot scale.")

    available_width = printer.printable_width
    available_height = printer.printable_depth
    scale_factor = min(available_width / width, available_height / height)

    # Pre-compute translation and scaling parameters.
    translated_shapes: List[ShapeGeometry] = []
    for shape in shapes:
        geom = shapely_translate(shape.geometry, xoff=-minx, yoff=-miny)
        geom = shapely_scale(geom, xfact=scale_factor, yfact=scale_factor, origin=(0, 0))
        geom = shapely_scale(geom, xfact=1.0, yfact=-1.0, origin=(0, 0))
        scaled_height = height * scale_factor
        geom = shapely_translate(geom, xoff=printer.x_min, yoff=printer.y_min + scaled_height)
        stroke_width = None if shape.stroke_width is None else shape.stroke_width * scale_factor
        translated_shapes.append(
            ShapeGeometry(
                geometry=geom,
                brightness=shape.brightness,
                stroke_width=stroke_width,
                color=shape.color,
            )
        )

    return translated_shapes, scale_factor
