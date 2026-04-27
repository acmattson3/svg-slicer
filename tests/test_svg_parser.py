from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import LineString, Polygon

from svg_slicer.config import PrinterConfig
from svg_slicer.svg_parser import ShapeGeometry, fit_shapes_to_bed, parse_svg, place_shapes_on_bed


def test_parse_svg_basic_fill(simple_svg_path: Path, slicer_config) -> None:
    shapes = parse_svg(str(simple_svg_path), slicer_config.sampling)
    assert shapes

    fill_shapes = [shape for shape in shapes if shape.stroke_width is None]
    assert fill_shapes
    color = fill_shapes[0].color
    assert color == (255, 0, 0)
    assert fill_shapes[0].brightness == pytest.approx(0.299, rel=1e-2)


def test_parse_svg_stroke_produces_stroke_geometry(tmp_path: Path, slicer_config) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<path d="M10 10 L90 10" fill="none" stroke="#0000FF" stroke-width="4" />\n'
        '</svg>\n'
    )
    path = tmp_path / "stroke.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling)
    assert any(shape.stroke_width is not None for shape in shapes)


def test_parse_svg_plot_mode_uses_stroke_centerline(tmp_path: Path, slicer_config) -> None:
    slicer_config.sampling.plot_mode = "centerline"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<path d="M10 10 C 30 90, 70 90, 90 10" fill="none" stroke="#0000FF" stroke-width="4" />\n'
        '</svg>\n'
    )
    path = tmp_path / "plot_mode_curve.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling)

    assert shapes
    assert all(isinstance(shape.geometry, LineString) for shape in shapes)
    assert any(len(list(shape.geometry.coords)) > 2 for shape in shapes)


def test_parse_svg_plot_mode_keeps_straight_line_as_single_segment(tmp_path: Path, slicer_config) -> None:
    slicer_config.sampling.plot_mode = "centerline"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<path d="M10 10 L90 10" fill="none" stroke="#0000FF" stroke-width="4" />\n'
        '</svg>\n'
    )
    path = tmp_path / "plot_mode_line.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling)

    assert len(shapes) == 1
    assert list(shapes[0].geometry.coords) == [(10.0, 10.0), (90.0, 10.0)]


def test_parse_svg_auto_plot_mode_keeps_trace_and_centerline(tmp_path: Path, slicer_config) -> None:
    slicer_config.sampling.plot_mode = "auto"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<path d="M10 10 L90 10" fill="none" stroke="#0000FF" stroke-width="4" />\n'
        '</svg>\n'
    )
    path = tmp_path / "auto_plot_mode.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling)

    assert shapes
    assert any(isinstance(shape.geometry, Polygon) for shape in shapes)
    assert any(shape.centerline_geometry is not None for shape in shapes)


def test_parse_svg_respects_clip_path(tmp_path: Path, slicer_config) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<defs><clipPath id="c"><rect x="0" y="0" width="30" height="30" /></clipPath></defs>\n'
        '<rect x="0" y="0" width="80" height="80" fill="#00ff00" clip-path="url(#c)" />\n'
        '</svg>\n'
    )
    path = tmp_path / "clipped.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling)
    assert shapes
    combined = shapes[0].geometry
    for shape in shapes[1:]:
        combined = combined.union(shape.geometry)
    minx, miny, maxx, maxy = combined.bounds
    assert minx >= -1.0
    assert miny >= -1.0
    assert maxx <= 31.0
    assert maxy <= 31.0


def test_parse_svg_hershey_text_produces_centerline_strokes(tmp_path: Path, slicer_config) -> None:
    pytest.importorskip("HersheyFonts")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<text x="10" y="50" font-size="12" fill="#000000">Hi</text>\n'
        "</svg>\n"
    )
    path = tmp_path / "text.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling, force_hershey_text=True)

    assert shapes
    assert all(isinstance(shape.geometry, LineString) for shape in shapes)
    assert all(shape.stroke_width is not None for shape in shapes)


def test_parse_svg_missing_font_falls_back_to_hershey(tmp_path: Path, slicer_config) -> None:
    pytest.importorskip("HersheyFonts")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
        '<text x="10" y="50" font-size="12" font-family="DefinitelyMissingPlotterFont" '
        'fill="#000000">Hi</text>\n'
        "</svg>\n"
    )
    path = tmp_path / "missing_font_text.svg"
    path.write_text(svg, encoding="utf-8")

    shapes = parse_svg(str(path), slicer_config.sampling)

    assert shapes
    assert all(isinstance(shape.geometry, LineString) for shape in shapes)


def test_fit_shapes_to_bed_scales_into_printable_area(slicer_config) -> None:
    shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (40, 0), (40, 20), (0, 20)]),
        brightness=0.5,
        stroke_width=2.0,
        color=(1, 2, 3),
    )
    fitted, scale = fit_shapes_to_bed([shape], slicer_config.printer)
    assert fitted
    assert scale > 0

    minx, miny, maxx, maxy = fitted[0].geometry.bounds
    printer: PrinterConfig = slicer_config.printer
    assert printer.x_min <= minx <= printer.x_max
    assert printer.y_min <= miny <= printer.y_max
    assert printer.x_min <= maxx <= printer.x_max
    assert printer.y_min <= maxy <= printer.y_max
    assert fitted[0].stroke_width == pytest.approx(shape.stroke_width * scale)


def test_place_shapes_on_bed_uses_manual_scale_factor(slicer_config) -> None:
    shape = ShapeGeometry(
        geometry=Polygon([(10, 20), (50, 20), (50, 50), (10, 50)]),
        brightness=0.5,
        stroke_width=2.0,
        color=(1, 2, 3),
        centerline_geometry=LineString([(10, 20), (50, 50)]),
    )

    placed, scale = place_shapes_on_bed([shape], slicer_config.printer, 1.0)

    assert scale == 1.0
    minx, miny, maxx, maxy = placed[0].geometry.bounds
    printer: PrinterConfig = slicer_config.printer
    assert minx == pytest.approx(printer.x_min + 30.0)
    assert miny == pytest.approx(printer.y_min + 20.0)
    assert maxx == pytest.approx(printer.x_min + 70.0)
    assert maxy == pytest.approx(printer.y_min + 50.0)
    assert placed[0].stroke_width == pytest.approx(shape.stroke_width)
    assert placed[0].centerline_geometry is not None
    centerline_coords = list(placed[0].centerline_geometry.coords)
    assert centerline_coords[0] == pytest.approx((printer.x_min + 30.0, printer.y_min + 50.0))
    assert centerline_coords[1] == pytest.approx((printer.x_min + 70.0, printer.y_min + 20.0))


def test_place_shapes_on_bed_supports_center_alignment(slicer_config) -> None:
    shape = ShapeGeometry(
        geometry=Polygon([(10, 20), (50, 20), (50, 50), (10, 50)]),
        brightness=0.5,
        stroke_width=2.0,
        color=(1, 2, 3),
        centerline_geometry=LineString([(10, 20), (50, 50)]),
    )

    placed, scale = place_shapes_on_bed([shape], slicer_config.printer, 1.0, alignment="center")

    assert scale == 1.0
    minx, miny, maxx, maxy = placed[0].geometry.bounds
    printer: PrinterConfig = slicer_config.printer
    assert minx == pytest.approx(printer.x_min + 30.0)
    assert miny == pytest.approx(printer.y_min + 20.0)
    assert maxx == pytest.approx(printer.x_min + 70.0)
    assert maxy == pytest.approx(printer.y_min + 50.0)
    assert placed[0].centerline_geometry is not None
    centerline_coords = list(placed[0].centerline_geometry.coords)
    assert centerline_coords[0] == pytest.approx((printer.x_min + 30.0, printer.y_min + 50.0))
    assert centerline_coords[1] == pytest.approx((printer.x_min + 70.0, printer.y_min + 20.0))


def test_fit_shapes_to_bed_supports_bottom_alignment(slicer_config) -> None:
    shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (20, 0), (20, 10), (0, 10)]),
        brightness=0.5,
        stroke_width=None,
        color=(1, 2, 3),
    )

    fitted, scale = fit_shapes_to_bed([shape], slicer_config.printer, alignment="bottom-middle")

    assert scale == pytest.approx(5.0)
    minx, miny, maxx, maxy = fitted[0].geometry.bounds
    printer: PrinterConfig = slicer_config.printer
    assert minx == pytest.approx(printer.x_min)
    assert maxx == pytest.approx(printer.x_max)
    assert miny == pytest.approx(printer.y_min + 20.0)
    assert maxy == pytest.approx(printer.y_max)


def test_fit_shapes_to_bed_zero_dimension_raises(slicer_config) -> None:
    degenerate = ShapeGeometry(
        geometry=Polygon([(0, 0), (0, 0), (0, 0)]),
        brightness=0.5,
        stroke_width=None,
        color=None,
    )
    with pytest.raises(ValueError):
        fit_shapes_to_bed([degenerate], slicer_config.printer)
