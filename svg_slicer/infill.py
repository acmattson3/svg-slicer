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


def _merge_boustrophedon(
    lines: Sequence[Polyline],
    max_gap: float,
    region: Polygon | None = None,
) -> List[Polyline]:
    pending: List[Polyline] = [list(line) for line in lines if len(line) >= 2]
    if not pending:
        return []

    merged: List[Polyline] = []
    current: Polyline = []
    last_point: Point | None = None

    while pending:
        if not current:
            line = pending.pop(0)
            current = list(line)
            last_point = current[-1]
            continue

        distances = [
            _point_distance(last_point, line[0]) if last_point is not None else 0.0
            for line in pending
        ]
        next_index = min(range(len(pending)), key=lambda idx: distances[idx])
        line = pending.pop(next_index)
        distance = distances[next_index] if last_point is not None else 0.0

        if distance <= max_gap:
            if distance > 0 and last_point is not None and region is not None:
                connector = LineString([last_point, line[0]])
                if not region.buffer(1e-9).covers(connector):
                    merged.append(current)
                    current = list(line)
                    last_point = current[-1]
                    continue
            if distance > 0 and last_point is not None:
                current.append(line[0])
            current.extend(line[1:])
            last_point = current[-1]
        else:
            merged.append(current)
            current = list(line)
            last_point = current[-1]

    if current:
        merged.append(current)

    return merged


def _glue_polylines(
    polylines: List[Polyline],
    tolerance: float,
    region: Polygon | None = None,
) -> List[Polyline]:
    if not polylines:
        return []

    glued: List[Polyline] = []
    current = list(polylines[0])

    for polyline in polylines[1:]:
        if len(polyline) < 2:
            continue
        start = current[-1]
        start_distance = _point_distance(start, polyline[0])
        end_distance = _point_distance(start, polyline[-1])
        candidate = polyline

        if end_distance < start_distance:
            candidate = list(reversed(polyline))
            start_distance = end_distance
        if start_distance <= tolerance:
            if start_distance > 0 and region is not None:
                connector = LineString([start, candidate[0]])
                if not region.buffer(1e-9).covers(connector):
                    glued.append(current)
                    current = list(candidate)
                    continue
            if start_distance > 0:
                current.append(candidate[0])
            current.extend(candidate[1:])
        else:
            glued.append(current)
            current = list(candidate)

    glued.append(current)
    return glued


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

    centroid = polygon.centroid
    origin = (float(centroid.x), float(centroid.y))

    angle_paths: List[List[Polyline]] = []
    merge_tolerance = min(spacing * 0.25, config.base_spacing * 0.25)
    merge_tolerance = max(merge_tolerance, 1e-4)

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
        merged = _merge_boustrophedon(alternating, max_gap=merge_tolerance, region=polygon)
        angle_paths.append([poly for poly in merged if poly])

    merged_paths = _interleave_orientations(angle_paths)
    return _glue_polylines(merged_paths, merge_tolerance, region=polygon)


def _interleave_orientations(angle_polylines: List[List[Polyline]]) -> List[Polyline]:
    candidates: List[Tuple[int, Polyline]] = []
    for angle_index, polylines in enumerate(angle_polylines):
        for polyline in polylines:
            if len(polyline) >= 2:
                candidates.append((angle_index, list(polyline)))

    if not candidates:
        return []

    result: List[Polyline] = []
    current_point: Point | None = None

    while candidates:
        if current_point is None:
            angle_idx = min((angle for angle, _ in candidates), default=0)
            for idx, (a_idx, poly) in enumerate(candidates):
                if a_idx == angle_idx:
                    chosen_index = idx
                    chosen_poly = poly
                    reverse = False
                    break
            else:
                chosen_index = 0
                chosen_poly = candidates[0][1]
                reverse = False
        else:
            best_dist = float("inf")
            chosen_index = 0
            chosen_poly = candidates[0][1]
            reverse = False
            for idx, (_, poly) in enumerate(candidates):
                start = poly[0]
                end = poly[-1]
                dist_start = _point_distance(current_point, start)
                dist_end = _point_distance(current_point, end)
                if dist_end < dist_start:
                    dist = dist_end
                    rev = True
                else:
                    dist = dist_start
                    rev = False
                if dist < best_dist - 1e-6 or (abs(dist - best_dist) <= 1e-6 and idx < chosen_index):
                    best_dist = dist
                    chosen_index = idx
                    chosen_poly = poly
                    reverse = rev

        _, polyline = candidates.pop(chosen_index)
        if reverse:
            polyline = list(reversed(polyline))

        result.append(polyline)
        current_point = polyline[-1]

    return result
