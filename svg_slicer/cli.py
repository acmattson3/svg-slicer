from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
try:
    from shapely.validation import make_valid as shapely_make_valid
except ImportError:  # pragma: no cover - older Shapely versions
    shapely_make_valid = None

from .config import SlicerConfig, load_config
from .gcode import GcodeGenerator, Toolpath, toolpaths_from_polylines
from .infill import generate_rectilinear_infill
from .preview import render_toolpaths
from .svg_parser import ShapeGeometry, fit_shapes_to_bed, parse_svg

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


def _notify(progress_update: Optional[ProgressCallback], message: str) -> None:
    if progress_update:
        progress_update(message)


def _clean_geometry(geometry: BaseGeometry) -> BaseGeometry:
    if geometry.is_empty:
        return geometry
    geom = geometry
    if geom.is_valid:
        return geom
    if shapely_make_valid is not None:
        try:
            geom = shapely_make_valid(geom)
        except Exception:  # pragma: no cover - defensive
            geom = geometry
    if not geom.is_valid:
        try:
            geom = geom.buffer(0)
        except Exception:  # pragma: no cover - defensive
            return geometry
    return geom


def _brightness_to_density(brightness: float, config: SlicerConfig) -> float:
    # Map brightness (0=black, 1=white) into density range.
    dynamic_range = config.infill.max_density - config.infill.min_density
    return config.infill.min_density + (1.0 - brightness) * dynamic_range


def _geometry_to_polygons(geometry: BaseGeometry) -> List[Polygon]:
    geometry = _clean_geometry(geometry)
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        polygons: List[Polygon] = []
        for part in geometry.geoms:
            polygons.extend(_geometry_to_polygons(part))
        return polygons
    return []


def _ring_to_polyline(ring: LinearRing, tolerance: float) -> List[tuple[float, float]]:
    coords = list(ring.coords)
    if len(coords) < 2:
        return []
    line = LineString(coords)
    simplified = line.simplify(max(tolerance, 0.0), preserve_topology=True)
    simplified_coords = list(simplified.coords)
    if len(simplified_coords) < 3:
        simplified_coords = coords
    if simplified_coords[0] != simplified_coords[-1]:
        simplified_coords.append(simplified_coords[0])
    return [(float(x), float(y)) for x, y in simplified_coords]


def _max_dimension(polygon: Polygon) -> float:
    if polygon.is_empty:
        return 0.0
    rect = polygon.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    if len(coords) < 2:
        return 0.0
    edges = [
        LineString([coords[i], coords[i + 1]]).length
        for i in range(len(coords) - 1)
    ]
    if not edges:
        return 0.0
    return max(edges)


def _min_dimension(polygon: Polygon) -> float:
    if polygon.is_empty:
        return 0.0
    rect = polygon.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    if len(coords) < 2:
        return 0.0
    edges = [
        LineString([coords[i], coords[i + 1]]).length
        for i in range(len(coords) - 1)
    ]
    if not edges:
        return 0.0
    return min(edges)


def _fill_dimension(polygon: Polygon, mode: str) -> float:
    return _max_dimension(polygon) if mode == "max" else _min_dimension(polygon)


def _select_infill_regions(
    polygon: Polygon,
    min_fill_width: float,
    min_fill_mode: str,
) -> List[Polygon]:
    polygon = _clean_geometry(polygon)
    if polygon.is_empty:
        return []

    if min_fill_width <= 0:
        return _geometry_to_polygons(polygon)

    if min_fill_mode == "max":
        return [polygon] if _fill_dimension(polygon, min_fill_mode) >= min_fill_width else []

    # "min" mode: keep only locally wide-enough regions using an opening operation.
    radius = max(min_fill_width / 2.0, 1e-6)
    eroded = _clean_geometry(polygon.buffer(-radius))
    if eroded.is_empty:
        return []
    reopened = _clean_geometry(eroded.buffer(radius))
    clipped = _clean_geometry(reopened.intersection(polygon))
    return [part for part in _geometry_to_polygons(clipped) if not part.is_empty and part.area > 0]


def _generate_perimeter_loops(
    polygon: Polygon,
    step: float,
    target_width: float,
    tolerance: float,
) -> List[List[tuple[float, float]]]:
    polygon = _clean_geometry(polygon)
    step = max(step, 1e-6)
    target_width = max(target_width, step)
    max_passes = max(1, int(math.ceil(target_width / step)))

    loops: List[List[tuple[float, float]]] = []
    current = polygon

    for _ in range(max_passes):
        current = _clean_geometry(current)
        if current.is_empty or current.area <= 0:
            break
        for poly in _geometry_to_polygons(current):
            exterior = _ring_to_polyline(poly.exterior, tolerance)
            if exterior:
                loops.append(exterior)
            for interior_ring in poly.interiors:
                interior_polyline = _ring_to_polyline(interior_ring, tolerance)
                if interior_polyline:
                    loops.append(interior_polyline)
        current = current.buffer(-step)

    return [loop for loop in loops if len(loop) >= 2]


def _build_stroke_toolpaths(shape: ShapeGeometry, config: SlicerConfig) -> List[Toolpath]:
    if shape.stroke_width is None:
        return []
    loops = _generate_perimeter_loops(
        polygon=shape.geometry,
        step=config.perimeter.thickness,
        target_width=shape.stroke_width,
        tolerance=config.sampling.outline_simplify_tolerance,
    )
    return toolpaths_from_polylines(loops, tag="outline", source_color=shape.color, brightness=shape.brightness)


def _build_toolpaths(
    shapes: Iterable[ShapeGeometry],
    config: SlicerConfig,
    *,
    progress_update: Optional[ProgressCallback] = None,
) -> List[Toolpath]:
    toolpaths: List[Toolpath] = []
    shape_list = list(shapes)
    total_shapes = len(shape_list)
    perimeter_width = max(config.perimeter.thickness, 0.0)
    perimeter_count = max(int(config.perimeter.count), 1)
    min_fill_width = max(config.perimeter.min_fill_width, 0.0)
    min_fill_mode = str(config.perimeter.min_fill_mode).strip().lower()
    if min_fill_mode not in {"min", "max"}:
        min_fill_mode = "min"
    for index, shape in enumerate(shape_list, start=1):
        if shape.stroke_width is not None:
            _notify(
                progress_update,
                f"Generating stroke outlines ({index}/{total_shapes})…",
            )
            stroke_paths = _build_stroke_toolpaths(shape, config)
            toolpaths.extend(stroke_paths)
            continue

        _notify(
            progress_update,
            f"Preparing fill geometry ({index}/{total_shapes})…",
        )
        geometry = shape.geometry
        if geometry.is_empty:
            continue
        polygons: List[Polygon]
        polygons = _geometry_to_polygons(geometry)
        if not polygons:
            logger.debug("Skipping unsupported geometry type: %s", geometry.geom_type)
            continue
        density = _brightness_to_density(shape.brightness, config)
        for polygon in polygons:
            polygon = _clean_geometry(polygon)
            interior_geom: BaseGeometry | None = polygon

            if perimeter_width > 0:
                perimeter_target_width = perimeter_width * perimeter_count
                _notify(
                    progress_update,
                    f"Generating perimeters ({index}/{total_shapes})…",
                )
                loops = _generate_perimeter_loops(
                    polygon,
                    perimeter_width,
                    perimeter_target_width,
                    config.sampling.outline_simplify_tolerance,
                )
                toolpaths.extend(
                    toolpaths_from_polylines(
                        loops,
                        tag="outline",
                        source_color=shape.color,
                        brightness=shape.brightness,
                    )
                )

                # Keep infill touching the innermost perimeter by only offsetting
                # inside by (count - 1) perimeter widths.
                interior_offset = perimeter_width * max(perimeter_count - 1, 0)
                interior_candidate = _clean_geometry(polygon.buffer(-interior_offset))
                if interior_candidate.is_empty:
                    interior_geom = None
                else:
                    interior_geom = interior_candidate

            if interior_geom and not interior_geom.is_empty:
                _notify(
                    progress_update,
                    f"Generating infill ({index}/{total_shapes})…",
                )
                interior_geom = _clean_geometry(interior_geom)
                for infill_poly in _geometry_to_polygons(interior_geom):
                    infill_poly = _clean_geometry(infill_poly)
                    for fill_region in _select_infill_regions(
                        infill_poly,
                        min_fill_width=min_fill_width,
                        min_fill_mode=min_fill_mode,
                    ):
                        polylines = generate_rectilinear_infill(fill_region, density, config.infill)
                        toolpaths.extend(
                            toolpaths_from_polylines(
                                polylines,
                                tag="infill",
                                source_color=shape.color,
                                brightness=shape.brightness,
                            )
                        )
    return toolpaths


def generate_toolpaths_for_shapes(
    shapes: Iterable[ShapeGeometry],
    config: SlicerConfig,
    *,
    fit_to_bed: bool = True,
    progress_update: Optional[ProgressCallback] = None,
) -> Tuple[List[Toolpath], float]:
    shape_list = list(shapes)
    if not shape_list:
        raise RuntimeError("No drawable shapes with fills were found in the SVG.")

    _notify(progress_update, "Preparing shapes…")
    if fit_to_bed:
        _notify(progress_update, "Fitting shapes to printable area…")
        fitted_shapes, scale_factor = fit_shapes_to_bed(shape_list, config.printer)
    else:
        fitted_shapes = shape_list
        scale_factor = 1.0
    _notify(progress_update, "Generating toolpaths…")
    toolpaths = _build_toolpaths(
        fitted_shapes,
        config,
        progress_update=progress_update,
    )
    if not toolpaths:
        raise RuntimeError("No toolpaths were generated from the SVG.")
    _notify(progress_update, "Toolpaths ready.")
    return toolpaths, scale_factor


@dataclass
class ColorPlan:
    ordered_colors: List[str]
    groups: List[tuple[str, List[Toolpath]]]
    usage_by_color: Dict[str, float]


@dataclass
class GcodeWriteResult:
    line_count: int
    color_order: List[str]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    dr = a[0] - b[0]
    dg = a[1] - b[1]
    db = a[2] - b[2]
    return math.sqrt(dr * dr + dg * dg + db * db)


_GRAY_SATURATION_THRESHOLD = 0.08


def _rgb_brightness(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _rgb_saturation(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    maxc = max(r, g, b)
    minc = min(r, g, b)
    if maxc == 0:
        return 0.0
    return (maxc - minc) / maxc


def _find_closest_palette_color(
    source: tuple[int, int, int],
    palette: List[str],
    palette_rgb: Dict[str, tuple[int, int, int]],
    palette_order: Dict[str, int],
    grayscale_palette: List[str],
    palette_brightness: Dict[str, float],
) -> str:
    source_sat = _rgb_saturation(source)
    source_brightness = _rgb_brightness(source)

    if grayscale_palette and source_sat <= _GRAY_SATURATION_THRESHOLD:
        return min(
            grayscale_palette,
            key=lambda color: (
                abs(source_brightness - palette_brightness[color]),
                palette_order.get(color, math.inf),
            ),
        )

    return min(
        palette,
        key=lambda color: (
            _color_distance(source, palette_rgb[color]),
            palette_order.get(color, math.inf),
        ),
    )


def _toolpath_length(toolpath: Toolpath) -> float:
    length = 0.0
    points = list(toolpath.points)
    for i in range(1, len(points)):
        x1, y1 = points[i - 1]
        x2, y2 = points[i]
        length += math.hypot(x2 - x1, y2 - y1)
    return length


def _plan_color_sequence(toolpaths: List[Toolpath], config: SlicerConfig) -> ColorPlan | None:
    printer = config.printer
    if not printer.color_mode or not printer.available_colors:
        return None

    palette = printer.available_colors
    palette_rgb = {color: _hex_to_rgb(color) for color in palette}
    palette_order = {color: index for index, color in enumerate(palette)}
    palette_brightness = {color: _rgb_brightness(rgb) for color, rgb in palette_rgb.items()}
    palette_saturation = {color: _rgb_saturation(rgb) for color, rgb in palette_rgb.items()}
    grayscale_palette = [color for color, sat in palette_saturation.items() if sat <= _GRAY_SATURATION_THRESHOLD]

    color_groups: Dict[str, List[Toolpath]] = {}
    usage: Dict[str, float] = {}
    fallback_rgb = (0, 0, 0)

    for toolpath in toolpaths:
        source = toolpath.source_color or fallback_rgb
        best_color = _find_closest_palette_color(
            source,
            palette,
            palette_rgb,
            palette_order,
            grayscale_palette,
            palette_brightness,
        )
        toolpath.assigned_color = best_color
        color_groups.setdefault(best_color, []).append(toolpath)
        usage[best_color] = usage.get(best_color, 0.0) + _toolpath_length(toolpath)

    if not color_groups:
        return None

    ordered_colors = sorted(
        color_groups.keys(),
        key=lambda color: (usage.get(color, 0.0), palette_order.get(color, math.inf)),
    )
    groups = [(color, color_groups[color]) for color in ordered_colors]
    return ColorPlan(ordered_colors=ordered_colors, groups=groups, usage_by_color=usage)


def write_toolpaths_to_gcode(
    toolpaths: Iterable[Toolpath],
    output_path: Path,
    config: SlicerConfig,
    *,
    progress_update: Optional[ProgressCallback] = None,
) -> GcodeWriteResult:
    _notify(progress_update, "Preparing G-code generator…")
    toolpath_list = list(toolpaths)
    generator = GcodeGenerator(config.printer)
    generator.emit_header()

    _notify(progress_update, "Planning color sequence…")
    color_plan = _plan_color_sequence(toolpath_list, config)
    color_order: List[str] = []

    if color_plan and color_plan.ordered_colors:
        color_order = color_plan.ordered_colors
        summary = " -> ".join(color_order)
        generator.emit_comment(f"COLOR ORDER (least usage first): {summary}")
        total_groups = len(color_plan.groups)
        for index, (color, group_paths) in enumerate(color_plan.groups, start=1):
            total_length = color_plan.usage_by_color.get(color, 0.0)
            generator.emit_comment(
                f"COLOR {index}/{total_groups}: {color} ({total_length:.1f} mm of drawing)"
            )
            for path in group_paths:
                generator.draw_single_toolpath(path, config.printer.feedrates)
            if index < total_groups:
                generator.emit_comment("Filament change before next color")
                pause_commands = config.printer.pause_gcode or ["M600"]
                for command in pause_commands:
                    generator.emit_command(command)
    else:
        generator.draw_toolpaths(toolpath_list, config.printer.feedrates)

    estimated_text = generator.formatted_elapsed_time()
    generator.emit_comment(f"Estimated plot time: {estimated_text}")
    generator.emit_footer()

    _notify(progress_update, "Writing G-code to disk…")
    gcode_lines = generator.generate()
    output_path.write_text("\n".join(gcode_lines) + "\n", encoding="utf-8")
    logger.info("Wrote %d G-code lines to %s", len(gcode_lines), output_path)
    if color_order:
        logger.info("Color order: %s", " -> ".join(color_order))
    logger.info("Estimated plot time: %s (motion only)", estimated_text)
    _notify(progress_update, "G-code saved.")
    return GcodeWriteResult(line_count=len(gcode_lines), color_order=color_order)


def slice_svg_to_gcode(svg_path: Path, output_path: Path, config: SlicerConfig, preview: bool, preview_file: Path | None) -> None:
    shapes = parse_svg(str(svg_path), config.sampling)
    toolpaths, scale_factor = generate_toolpaths_for_shapes(shapes, config)
    logger.info("Scaled SVG by factor %.3f to fit printable area", scale_factor)

    write_toolpaths_to_gcode(toolpaths, output_path, config)

    if preview or preview_file:
        polylines = [toolpath.points for toolpath in toolpaths]
        render_toolpaths(polylines, config.printer, config.rendering, str(preview_file) if preview_file else None)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Slice an SVG into G-code for pen plotter 3D printers.")
    parser.add_argument("svg", type=Path, help="Path to the source SVG file")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to slicer configuration YAML")
    parser.add_argument(
        "--printer-profile",
        type=str,
        default=None,
        help="Name of the printer profile to use from the configuration file",
    )
    parser.add_argument("--output", type=Path, default=Path("output.gcode"), help="Destination G-code file")
    parser.add_argument("--preview", action="store_true", help="Display a matplotlib preview of the toolpaths")
    parser.add_argument(
        "--preview-file",
        type=Path,
        default=None,
        help="Optional path to save the preview instead of displaying it",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--color-mode",
        dest="color_mode",
        action="store_true",
        help="Override the configuration to enable color palette mode.",
    )
    parser.add_argument(
        "--bw-mode",
        "--black-white",
        dest="color_mode",
        action="store_false",
        help="Override the configuration to force black and white mode.",
    )
    parser.set_defaults(color_mode=None)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="[%(levelname)s] %(message)s")

    config = load_config(args.config, profile=args.printer_profile)

    if args.color_mode is not None:
        config.printer.color_mode = bool(args.color_mode)
        logger.info(
            "Color mode override: %s",
            "enabled" if config.printer.color_mode else "disabled",
        )

    if config.printer.color_mode and not config.printer.available_colors:
        logger.error(
            "Color mode is enabled but printer profile '%s' does not define any available colors. "
            "Add 'available_colors' to the profile or disable color mode.",
            config.printer.name,
        )
        return 1

    try:
        slice_svg_to_gcode(args.svg, args.output, config, args.preview, args.preview_file)
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Failed to slice SVG: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
