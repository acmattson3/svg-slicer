from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

from shapely import geometry
from shapely.affinity import rotate
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

from .config import InfillConfig

Point = Tuple[float, float]
Polyline = List[Point]


def _collect_segments(geom: BaseGeometry) -> Iterable[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, geometry.MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, (geometry.Point, geometry.MultiPoint)):
        return []
    if isinstance(geom, geometry.GeometryCollection):
        segments: List[LineString] = []
        for part in geom.geoms:
            segments.extend(_collect_segments(part))
        return segments
    if isinstance(geom, Polygon):
        return _collect_segments(geom.boundary)
    raise TypeError(f"Unsupported geometry type for infill segmentation: {geom.geom_type}")


def _linestring_to_polyline(line: LineString) -> Polyline:
    coords = list(line.coords)
    if len(coords) < 2:
        return []
    return [(float(x), float(y)) for x, y in coords]


def _point_distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _merge_boustrophedon(lines: Sequence[Polyline], max_gap: float) -> List[Polyline]:
    merged: List[Polyline] = []
    current: Polyline = []
    for line in lines:
        if not line:
            continue
        if not current:
            current = list(line)
            continue
        if _point_distance(current[-1], line[0]) <= max_gap:
            if _point_distance(current[-1], line[0]) > 0:
                current.append(line[0])
            current.extend(line[1:])
        else:
            merged.append(current)
            current = list(line)
    if current:
        merged.append(current)
    return merged


def generate_rectilinear_infill(
    polygon: Polygon,
    density: float,
    config: InfillConfig,
) -> List[Polyline]:
    if polygon.is_empty or polygon.area <= 0:
        return []

    density = max(config.min_density, min(config.max_density, density))
    if density <= 0:
        return []

    base_spacing = max(config.base_spacing, 0.01)
    spacing = base_spacing / max(density, 1e-3)

    minx, miny, maxx, maxy = polygon.bounds
    length_margin = math.hypot(maxx - minx, maxy - miny) + spacing

    toolpaths: List[Polyline] = []

    centroid = polygon.centroid
    origin = (float(centroid.x), float(centroid.y))

    for index, angle in enumerate(config.angles):
        rotated = rotate(polygon, -angle, origin=origin, use_radians=False)
        min_rx, min_ry, max_rx, max_ry = rotated.bounds
        # Extend bounds to ensure we cover entire polygon after rotation.
        start = min_ry - spacing
        stop = max_ry + spacing

        current_y = start
        pass_toolpaths: List[Polyline] = []

        while current_y <= stop:
            sweep_line = LineString(
                [
                    (min_rx - length_margin, current_y),
                    (max_rx + length_margin, current_y),
                ]
            )
            clipped = rotated.intersection(sweep_line)
            segments = _collect_segments(clipped)
            for segment in segments:
                polyline = _linestring_to_polyline(segment)
                if not polyline:
                    continue
                rotated_back = rotate(
                    LineString(polyline),
                    angle,
                    origin=origin,
                    use_radians=False,
                )
                pass_toolpaths.append(_linestring_to_polyline(rotated_back))
            current_y += spacing

        alternating: List[Polyline] = []
        for idx, polyline in enumerate(pass_toolpaths):
            if idx % 2 == 1:
                alternating.append(list(reversed(polyline)))
            else:
                alternating.append(polyline)

        if index % 2 == 1:
            alternating.reverse()
        merged = _merge_boustrophedon(alternating, max_gap=spacing * 2.0)
        toolpaths.extend(merged)

    return [polyline for polyline in toolpaths if polyline]
