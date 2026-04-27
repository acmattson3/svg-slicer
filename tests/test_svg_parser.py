from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import LineString, Polygon

from svg_slicer.config import PrinterConfig
from svg_slicer.svg_parser import (
    ShapeGeometry,
    _hershey_lines_for_text,
    _raster_pil_image_to_shape_geometries,
    fit_shapes_to_bed,
    parse_svg,
    place_shapes_on_bed,
)


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


def test_hershey_lines_merge_connected_segments() -> None:
    pytest.importorskip("HersheyFonts")

    lines = _hershey_lines_for_text("O", x_base=0.0, y_base=0.0, font_size=12.0)

    assert lines
    assert any(len(list(line.coords)) > 2 for line in lines)


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


def test_parse_svg_hershey_text_fits_original_text_bounds(tmp_path: Path, slicer_config) -> None:
    pytest.importorskip("HersheyFonts")

    def write_text_svg(path: Path, font_family: str = "") -> None:
        font_attr = f' font-family="{font_family}"' if font_family else ""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="80">\n'
            f'<text x="20" y="50" font-size="24"{font_attr} fill="#000000">Wide Text</text>\n'
            "</svg>\n"
        )
        path.write_text(svg, encoding="utf-8")

    path = tmp_path / "fit_text.svg"
    write_text_svg(path)

    outline_shapes = parse_svg(str(path), slicer_config.sampling)
    hershey_shapes = parse_svg(str(path), slicer_config.sampling, force_hershey_text=True)

    missing_font_path = tmp_path / "fit_missing_font_text.svg"
    write_text_svg(missing_font_path, "DefinitelyMissingPlotterFont")
    fallback_outline_shapes = parse_svg(str(path), slicer_config.sampling)
    missing_font_hershey_shapes = parse_svg(str(missing_font_path), slicer_config.sampling)

    def combined_bounds(shapes):
        bounds = [shape.geometry.bounds for shape in shapes]
        return (
            min(bound[0] for bound in bounds),
            min(bound[1] for bound in bounds),
            max(bound[2] for bound in bounds),
            max(bound[3] for bound in bounds),
        )

    outline_bounds = combined_bounds(outline_shapes)
    hershey_bounds = combined_bounds(hershey_shapes)
    fallback_outline_bounds = combined_bounds(fallback_outline_shapes)
    missing_font_hershey_bounds = combined_bounds(missing_font_hershey_shapes)

    assert hershey_bounds[0] >= outline_bounds[0] - 1e-6
    assert hershey_bounds[1] >= outline_bounds[1] - 1e-6
    assert hershey_bounds[2] <= outline_bounds[2] + 1e-6
    assert hershey_bounds[3] <= outline_bounds[3] + 1e-6
    assert missing_font_hershey_bounds[0] >= fallback_outline_bounds[0] - 1e-6
    assert missing_font_hershey_bounds[1] >= fallback_outline_bounds[1] - 1e-6
    assert missing_font_hershey_bounds[2] <= fallback_outline_bounds[2] + 1e-6
    assert missing_font_hershey_bounds[3] <= fallback_outline_bounds[3] + 1e-6


def test_parse_svg_hershey_text_normalizes_outer_and_tab_whitespace(tmp_path: Path, slicer_config) -> None:
    pytest.importorskip("HersheyFonts")
    normal_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">\n'
        '<text x="20" y="50" font-size="18" fill="#000000">The dog ran fast</text>\n'
        "</svg>\n"
    )
    spaced_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">\n'
        '<text x="20" y="50" font-size="18" fill="#000000">  The\tdog ran   fast  </text>\n'
        "</svg>\n"
    )
    normal_path = tmp_path / "normal_text.svg"
    spaced_path = tmp_path / "spaced_text.svg"
    normal_path.write_text(normal_svg, encoding="utf-8")
    spaced_path.write_text(spaced_svg, encoding="utf-8")

    normal_shapes = parse_svg(str(normal_path), slicer_config.sampling, force_hershey_text=True)
    spaced_shapes = parse_svg(str(spaced_path), slicer_config.sampling, force_hershey_text=True)

    normal_width = max(shape.geometry.bounds[2] for shape in normal_shapes) - min(
        shape.geometry.bounds[0] for shape in normal_shapes
    )
    spaced_width = max(shape.geometry.bounds[2] for shape in spaced_shapes) - min(
        shape.geometry.bounds[0] for shape in spaced_shapes
    )

    assert spaced_width == pytest.approx(normal_width, rel=1e-6)


def test_raster_image_sampling_outputs_alternating_scanlines(slicer_config) -> None:
    pil_image_module = pytest.importorskip("PIL.Image")
    image = pil_image_module.new("RGBA", (3, 2), (0, 0, 0, 255))
    slicer_config.sampling.raster_sample_spacing = 1.0
    slicer_config.sampling.raster_max_cells = 100

    shapes = _raster_pil_image_to_shape_geometries(
        image,
        (0.0, 0.0, 3.0, 2.0),
        slicer_config.sampling,
        None,
    )

    assert len(shapes) == 2
    assert all(isinstance(shape.geometry, LineString) for shape in shapes)
    assert all(shape.toolpath_tag == "raster" for shape in shapes)
    assert list(shapes[0].geometry.coords) == pytest.approx([(0.0, 0.5), (3.0, 0.5)])
    assert list(shapes[1].geometry.coords) == pytest.approx([(3.0, 1.5), (0.0, 1.5)])


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
