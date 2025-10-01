from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from shapely.affinity import scale as shapely_scale
from shapely.affinity import translate as shapely_translate
from shapely.geometry import GeometryCollection, Polygon
from shapely.geometry.base import BaseGeometry
from svgelements import Arc, Color, Move, Path, SVG

from .config import PrinterConfig, SamplingConfig


logger = logging.getLogger(__name__)


@dataclass
class ShapeGeometry:
    geometry: BaseGeometry
    brightness: float


def _shoelace_area(points: Sequence[Tuple[float, float]]) -> float:
    area = 0.0
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        area += (x1 * y2) - (x2 * y1)
    return area / 2.0


def _path_to_polygons(path: Path, tolerance: float) -> List[Polygon]:
    # Ensure arcs are approximated by cubic curves for sampling stability.
    approximated = Path(path)
    approximated.approximate_arcs_with_cubics(error=tolerance / 10.0 if tolerance else 0.1)

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
            approximated.approximate_arcs_with_cubics(error=tolerance / 10.0 if tolerance else 0.1)
            return _path_to_polygons(approximated, tolerance)
        length = segment.length(error=tolerance / 10.0 if tolerance else 0.01)
        if length == 0:
            continue
        steps = max(int(length / max(tolerance, 0.1)), 1)
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

    shells: List[Tuple[Polygon, List[Polygon]]] = []
    holes: List[Polygon] = []

    for ring in rings:
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        polygon = Polygon(ring)
        if polygon.area == 0:
            continue
        area = _shoelace_area(ring)
        if area < 0:
            holes.append(polygon)
        else:
            shells.append((polygon, []))

    if not shells:
        # Treat all as shells if orientation detection failed.
        for hole in holes:
            shells.append((hole, []))
        holes = []

    for hole in holes:
        for shell, shell_holes in shells:
            if shell.contains(hole):
                shell_holes.append(hole)
                break

    for shell, shell_holes in shells:
        hole_coords = [list(hole.exterior.coords) for hole in shell_holes]
        polygons.append(Polygon(shell.exterior.coords, hole_coords))

    return polygons


def _color_to_brightness(color: Color | None) -> float:
    if not color or getattr(color, "alpha", 0) == 0:
        return 1.0
    r, g, b = color.red, color.green, color.blue
    brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return max(0.0, min(1.0, brightness))


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
        for polygon in _geometry_to_polygons(geometry):
            if polygon.is_empty or polygon.area <= 0:
                continue
            visible_reversed.append(ShapeGeometry(polygon, shape.brightness))
        occlusion = occlusion.union(shape.geometry)

    return list(reversed(visible_reversed))


def parse_svg(svg_path: str, sampling: SamplingConfig) -> List[ShapeGeometry]:
    svg = SVG.parse(svg_path)

    shapes: List[ShapeGeometry] = []

    for element in svg.elements():
        if not hasattr(element, "d"):
            continue
        try:
            element.reify()
        except Exception as exc:  # pragma: no cover - guard for malformed elements
            logger.debug("Skipping element that cannot be reified: %s", exc)
            continue
        try:
            path_data = element.d()
        except Exception as exc:
            logger.debug("Skipping element without path data: %s", exc)
            continue
        if not path_data:
            continue
        path = Path(path_data)
        polygons = _path_to_polygons(path, sampling.segment_tolerance)
        if not polygons:
            continue
        brightness = _color_to_brightness(getattr(element, "fill", None))
        for polygon in polygons:
            polygon = polygon.buffer(0)
            if polygon.is_empty or polygon.area <= 0:
                continue
            shapes.append(ShapeGeometry(geometry=polygon, brightness=brightness))

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
        translated_shapes.append(ShapeGeometry(geometry=geom, brightness=shape.brightness))

    return translated_shapes, scale_factor
