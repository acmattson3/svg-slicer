from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable, List

from shapely.geometry import GeometryCollection, LinearRing, LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from .config import SlicerConfig, load_config
from .gcode import GcodeGenerator, Toolpath, toolpaths_from_polylines
from .infill import generate_rectilinear_infill
from .preview import render_toolpaths
from .svg_parser import ShapeGeometry, fit_shapes_to_bed, parse_svg

logger = logging.getLogger(__name__)


def _brightness_to_density(brightness: float, config: SlicerConfig) -> float:
    # Map brightness (0=black, 1=white) into density range.
    dynamic_range = config.infill.max_density - config.infill.min_density
    return config.infill.min_density + (1.0 - brightness) * dynamic_range


def _geometry_to_polygons(geometry: BaseGeometry) -> List[Polygon]:
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
    simplified = line.simplify(max(tolerance, 0.0), preserve_topology=False)
    simplified_coords = list(simplified.coords)
    if len(simplified_coords) < 3:
        simplified_coords = coords
    if simplified_coords[0] != simplified_coords[-1]:
        simplified_coords.append(simplified_coords[0])
    return [(float(x), float(y)) for x, y in simplified_coords]


def _build_toolpaths(shapes: Iterable[ShapeGeometry], config: SlicerConfig) -> List[Toolpath]:
    toolpaths: List[Toolpath] = []
    perimeter_width = max(config.perimeter.thickness, 0.0)
    perimeter_density = max(config.perimeter.density, 0.0)
    for shape in shapes:
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
            perimeter_geom: BaseGeometry | None = None
            interior_geom: BaseGeometry | None = polygon

            if perimeter_width > 0:
                inner = polygon.buffer(-perimeter_width)
                if inner.is_empty:
                    perimeter_geom = polygon
                    interior_geom = None
                else:
                    perimeter_geom = polygon.difference(inner)
                    interior_geom = inner

            if perimeter_geom and not perimeter_geom.is_empty:
                perimeter_geom = perimeter_geom.buffer(0)
                for per_poly in _geometry_to_polygons(perimeter_geom):
                    outlines: List[List[tuple[float, float]]] = []
                    exterior = _ring_to_polyline(
                        per_poly.exterior,
                        config.sampling.outline_simplify_tolerance,
                    )
                    if exterior:
                        outlines.append(exterior)
                    for interior_ring in per_poly.interiors:
                        interior_polyline = _ring_to_polyline(
                            interior_ring,
                            config.sampling.outline_simplify_tolerance,
                        )
                        if interior_polyline:
                            outlines.append(interior_polyline)
                    toolpaths.extend(toolpaths_from_polylines(outlines, tag="outline"))

                    perimeter_lines = generate_rectilinear_infill(
                        per_poly,
                        perimeter_density if perimeter_density > 0 else config.infill.max_density,
                        config.infill,
                    )
                    toolpaths.extend(toolpaths_from_polylines(perimeter_lines, tag="outline"))
            else:
                outlines: List[List[tuple[float, float]]] = []
                exterior = _ring_to_polyline(
                    polygon.exterior,
                    config.sampling.outline_simplify_tolerance,
                )
                if exterior:
                    outlines.append(exterior)
                for interior_ring in polygon.interiors:
                    interior_polyline = _ring_to_polyline(
                        interior_ring,
                        config.sampling.outline_simplify_tolerance,
                    )
                    if interior_polyline:
                        outlines.append(interior_polyline)
                toolpaths.extend(toolpaths_from_polylines(outlines, tag="outline"))

            if interior_geom and not interior_geom.is_empty:
                interior_geom = interior_geom.buffer(0)
                for infill_poly in _geometry_to_polygons(interior_geom):
                    polylines = generate_rectilinear_infill(infill_poly, density, config.infill)
                    toolpaths.extend(toolpaths_from_polylines(polylines, tag="infill"))
    return toolpaths


def slice_svg_to_gcode(svg_path: Path, output_path: Path, config: SlicerConfig, preview: bool, preview_file: Path | None) -> None:
    shapes = parse_svg(str(svg_path), config.sampling)
    if not shapes:
        raise RuntimeError("No drawable shapes with fills were found in the SVG.")

    fitted_shapes, scale_factor = fit_shapes_to_bed(shapes, config.printer)
    logger.info("Scaled SVG by factor %.3f to fit printable area", scale_factor)

    toolpaths = _build_toolpaths(fitted_shapes, config)
    if not toolpaths:
        raise RuntimeError("No toolpaths were generated from the SVG.")

    generator = GcodeGenerator(config.printer)
    generator.emit_header()
    generator.draw_toolpaths(toolpaths, config.printer.feedrates)
    generator.emit_footer()

    gcode_lines = generator.generate()
    output_path.write_text("\n".join(gcode_lines) + "\n", encoding="utf-8")
    logger.info("Wrote %d G-code lines to %s", len(gcode_lines), output_path)

    if preview or preview_file:
        polylines = [toolpath.points for toolpath in toolpaths]
        render_toolpaths(polylines, config.printer, config.rendering, str(preview_file) if preview_file else None)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Slice an SVG into G-code for pen plotter 3D printers.")
    parser.add_argument("svg", type=Path, help="Path to the source SVG file")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to slicer configuration YAML")
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
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="[%(levelname)s] %(message)s")

    config = load_config(args.config)

    try:
        slice_svg_to_gcode(args.svg, args.output, config, args.preview, args.preview_file)
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Failed to slice SVG: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
