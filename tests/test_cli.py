from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon

import svg_slicer.cli as cli
from svg_slicer.gcode import Toolpath
from svg_slicer.svg_parser import ShapeGeometry


def test_brightness_to_density_mapping(slicer_config) -> None:
    low_brightness = cli._brightness_to_density(0.0, slicer_config)
    high_brightness = cli._brightness_to_density(1.0, slicer_config)
    assert low_brightness == pytest.approx(slicer_config.infill.max_density)
    assert high_brightness == pytest.approx(slicer_config.infill.min_density)


def test_geometry_to_polygons_handles_multiple_types() -> None:
    poly_a = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
    poly_b = Polygon([(3, 3), (5, 3), (5, 5), (3, 5)])

    from_poly = cli._geometry_to_polygons(poly_a)
    from_multi = cli._geometry_to_polygons(MultiPolygon([poly_a, poly_b]))
    from_collection = cli._geometry_to_polygons(GeometryCollection([poly_a, poly_b]))

    assert len(from_poly) == 1
    assert len(from_multi) == 2
    assert len(from_collection) == 2


def test_generate_toolpaths_for_shapes_without_fit_returns_scale_1(slicer_config) -> None:
    shapes = [
        ShapeGeometry(
            geometry=Polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),
            brightness=0.25,
            stroke_width=None,
            color=(10, 10, 10),
        )
    ]

    toolpaths, scale = cli.generate_toolpaths_for_shapes(shapes, slicer_config, fit_to_bed=False)
    assert toolpaths
    assert scale == 1.0


def test_generate_toolpaths_skips_effectively_white_shapes_in_bw_mode(slicer_config) -> None:
    shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),
        brightness=1.0,
        stroke_width=None,
        color=(255, 255, 255),
    )

    with pytest.raises(RuntimeError, match="No toolpaths were generated"):
        cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)


def test_parse_scale_argument_accepts_auto_none_factor_and_percent() -> None:
    assert cli._parse_scale_argument("auto") is None
    assert cli._parse_scale_argument("none") == pytest.approx(1.0)
    assert cli._parse_scale_argument("1.5") == pytest.approx(1.5)
    assert cli._parse_scale_argument("50%") == pytest.approx(0.5)

    with pytest.raises(cli.argparse.ArgumentTypeError):
        cli._parse_scale_argument("0")


def test_parse_alignment_argument_accepts_supported_positions() -> None:
    assert cli._parse_alignment_argument("top-left") == "top-left"
    assert cli._parse_alignment_argument("center") == "center"
    assert cli._parse_alignment_argument("bottom-right") == "bottom-right"

    with pytest.raises(cli.argparse.ArgumentTypeError):
        cli._parse_alignment_argument("left")


def test_argument_parser_supports_manual_scale() -> None:
    parser = cli.build_argument_parser()

    none_args = parser.parse_args(["input.svg", "--scale", "none"])
    factor_args = parser.parse_args(["input.svg", "--scale", "0.75"])
    auto_args = parser.parse_args(["input.svg", "--scale", "auto"])

    assert none_args.scale == pytest.approx(1.0)
    assert factor_args.scale == pytest.approx(0.75)
    assert auto_args.scale is None


def test_argument_parser_supports_alignment() -> None:
    parser = cli.build_argument_parser()

    default_args = parser.parse_args(["input.svg"])
    aligned_args = parser.parse_args(["input.svg", "--alignment", "center-right"])

    assert default_args.alignment == "center"
    assert aligned_args.alignment == "center-right"


def test_argument_parser_supports_rotation() -> None:
    parser = cli.build_argument_parser()

    args = parser.parse_args(["input.svg", "--rotate", "90"])

    assert args.rotate == pytest.approx(90.0)


def test_argument_parser_supports_pdf_page_and_hershey() -> None:
    parser = cli.build_argument_parser()

    args = parser.parse_args(["input.pdf", "--pdf-page", "3", "--hershey"])

    assert args.artwork == Path("input.pdf")
    assert args.pdf_page == 3
    assert args.hershey is True


def test_argument_parser_supports_raster_spacing() -> None:
    parser = cli.build_argument_parser()

    args = parser.parse_args(["input.pdf", "--raster-spacing", "0.75"])

    assert args.raster_spacing == pytest.approx(0.75)


def test_rotate_shapes_rotates_geometry_and_centerline() -> None:
    shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (4, 0), (4, 2), (0, 2)]),
        brightness=0.0,
        stroke_width=1.0,
        color=(0, 0, 0),
        centerline_geometry=LineString([(0, 1), (4, 1)]),
    )

    rotated = cli.rotate_shapes([shape], 90.0)

    minx, miny, maxx, maxy = rotated[0].geometry.bounds
    assert minx == pytest.approx(1.0)
    assert miny == pytest.approx(-1.0)
    assert maxx == pytest.approx(3.0)
    assert maxy == pytest.approx(3.0)
    assert list(rotated[0].centerline_geometry.coords) == pytest.approx([(2.0, -1.0), (2.0, 3.0)])


def test_thin_feature_gets_infill_when_any_dimension_meets_min_fill_width(slicer_config) -> None:
    slicer_config.perimeter.thickness = 0.05
    slicer_config.perimeter.min_fill_width = 0.3
    slicer_config.perimeter.min_fill_mode = "max"
    slicer_config.infill.base_spacing = 0.05
    slicer_config.infill.min_density = 1.0
    slicer_config.infill.max_density = 1.0
    slicer_config.infill.angles = [0.0]

    # Thin in one axis (0.2 mm), long in the other (1.2 mm).
    # With "any dimension" logic this should still be infilled.
    thin_shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (1.2, 0), (1.2, 0.2), (0, 0.2)]),
        brightness=0.0,
        stroke_width=None,
        color=(0, 0, 0),
    )

    toolpaths, _ = cli.generate_toolpaths_for_shapes([thin_shape], slicer_config, fit_to_bed=False)
    assert any(path.tag == "infill" for path in toolpaths)


def test_thin_feature_skips_infill_in_default_min_mode(slicer_config) -> None:
    slicer_config.perimeter.thickness = 0.05
    slicer_config.perimeter.min_fill_width = 0.3
    slicer_config.perimeter.min_fill_mode = "min"
    slicer_config.infill.base_spacing = 0.05
    slicer_config.infill.min_density = 1.0
    slicer_config.infill.max_density = 1.0
    slicer_config.infill.angles = [0.0]

    thin_shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (1.2, 0), (1.2, 0.2), (0, 0.2)]),
        brightness=0.0,
        stroke_width=None,
        color=(0, 0, 0),
    )

    toolpaths, _ = cli.generate_toolpaths_for_shapes([thin_shape], slicer_config, fit_to_bed=False)
    assert not any(path.tag == "infill" for path in toolpaths)


def test_concave_shape_respects_min_fill_mode_locally(slicer_config) -> None:
    slicer_config.perimeter.thickness = 0.0
    slicer_config.perimeter.min_fill_width = 0.3
    slicer_config.infill.base_spacing = 0.08
    slicer_config.infill.min_density = 1.0
    slicer_config.infill.max_density = 1.0
    slicer_config.infill.angles = [0.0]

    # Wide body (3.0 mm tall) with a thin right arm (0.2 mm tall).
    concave = ShapeGeometry(
        geometry=Polygon(
            [
                (0.0, 0.0),
                (3.0, 0.0),
                (3.0, 1.4),
                (5.0, 1.4),
                (5.0, 1.6),
                (3.0, 1.6),
                (3.0, 3.0),
                (0.0, 3.0),
            ]
        ),
        brightness=0.0,
        stroke_width=None,
        color=(0, 0, 0),
    )

    slicer_config.perimeter.min_fill_mode = "min"
    min_mode_paths, _ = cli.generate_toolpaths_for_shapes([concave], slicer_config, fit_to_bed=False)
    min_mode_points = [pt for p in min_mode_paths if p.tag == "infill" for pt in p.points]
    assert min_mode_points
    assert all(x <= 3.2 for x, _ in min_mode_points)

    slicer_config.perimeter.min_fill_mode = "max"
    max_mode_paths, _ = cli.generate_toolpaths_for_shapes([concave], slicer_config, fit_to_bed=False)
    max_mode_points = [pt for p in max_mode_paths if p.tag == "infill" for pt in p.points]
    assert any(x > 3.2 for x, _ in max_mode_points)


def test_perimeter_count_controls_outline_passes(slicer_config) -> None:
    slicer_config.perimeter.thickness = 0.2
    slicer_config.perimeter.min_fill_width = 999.0  # disable infill for this check
    shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (6, 0), (6, 4), (0, 4)]),
        brightness=0.5,
        stroke_width=None,
        color=(0, 0, 0),
    )

    slicer_config.perimeter.count = 1
    one_count_paths, _ = cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)
    one_count_outlines = [path for path in one_count_paths if path.tag == "outline"]

    slicer_config.perimeter.count = 2
    two_count_paths, _ = cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)
    two_count_outlines = [path for path in two_count_paths if path.tag == "outline"]

    assert len(one_count_outlines) == 1
    assert len(two_count_outlines) == 2


def test_auto_plot_mode_uses_centerline_for_pen_width_strokes(slicer_config) -> None:
    slicer_config.sampling.plot_mode = "auto"
    slicer_config.perimeter.thickness = 0.25
    slicer_config.sampling.plot_stroke_width_threshold = slicer_config.perimeter.thickness
    shape = ShapeGeometry(
        geometry=Polygon([(0, -0.05), (5, -0.05), (5, 0.05), (0, 0.05)]),
        brightness=0.0,
        stroke_width=0.2,
        color=(0, 0, 0),
        centerline_geometry=LineString([(0, 0), (5, 0)]),
    )

    toolpaths, _ = cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)

    assert len(toolpaths) == 1
    assert toolpaths[0].tag == "stroke"
    assert list(toolpaths[0].points) == [(0.0, 0.0), (5.0, 0.0)]


def test_auto_plot_mode_traces_wide_strokes(slicer_config) -> None:
    slicer_config.sampling.plot_mode = "auto"
    slicer_config.perimeter.thickness = 0.25
    slicer_config.sampling.plot_stroke_width_threshold = slicer_config.perimeter.thickness
    shape = ShapeGeometry(
        geometry=Polygon([(0, -0.5), (5, -0.5), (5, 0.5), (0, 0.5)]),
        brightness=0.0,
        stroke_width=1.0,
        color=(0, 0, 0),
        centerline_geometry=LineString([(0, 0), (5, 0)]),
    )

    toolpaths, _ = cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)

    assert any(path.tag == "outline" for path in toolpaths)
    assert not any(list(path.points) == [(0.0, 0.0), (5.0, 0.0)] for path in toolpaths)


def test_infill_touches_innermost_perimeter_without_extra_gap(slicer_config) -> None:
    slicer_config.perimeter.thickness = 0.2
    slicer_config.perimeter.min_fill_width = 0.0
    slicer_config.infill.base_spacing = 0.2
    slicer_config.infill.min_density = 1.0
    slicer_config.infill.max_density = 1.0
    slicer_config.infill.angles = [0.0]

    shape = ShapeGeometry(
        geometry=Polygon([(0, 0), (6, 0), (6, 4), (0, 4)]),
        brightness=0.0,
        stroke_width=None,
        color=(0, 0, 0),
    )

    slicer_config.perimeter.count = 1
    one_count_paths, _ = cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)
    one_count_infill = [path for path in one_count_paths if path.tag == "infill"]
    assert one_count_infill
    one_count_min_y = min(point[1] for path in one_count_infill for point in path.points)

    slicer_config.perimeter.count = 2
    two_count_paths, _ = cli.generate_toolpaths_for_shapes([shape], slicer_config, fit_to_bed=False)
    two_count_infill = [path for path in two_count_paths if path.tag == "infill"]
    assert two_count_infill
    two_count_min_y = min(point[1] for path in two_count_infill for point in path.points)

    assert one_count_min_y == pytest.approx(0.0, abs=1e-6)
    assert two_count_min_y >= 0.19


def test_plan_color_sequence_orders_by_least_usage(color_config_path: Path) -> None:
    config = cli.load_config(color_config_path)

    short = Toolpath(points=((0, 0), (1, 0)), source_color=(0, 0, 0))
    long = Toolpath(points=((0, 0), (10, 0)), source_color=(250, 0, 0))
    plan = cli._plan_color_sequence([long, short], config)

    assert plan is not None
    assert plan.ordered_colors[0] == "#000000"
    assert plan.ordered_colors[1] == "#FF0000"


def test_plan_color_sequence_skips_white_assigned_color(color_config_path: Path) -> None:
    config = cli.load_config(color_config_path)
    config.printer.available_colors = ["#FFFFFF", "#000000"]

    white = Toolpath(points=((0, 0), (1, 0)), source_color=(252, 252, 252))
    black = Toolpath(points=((0, 0), (2, 0)), source_color=(0, 0, 0))
    plan = cli._plan_color_sequence([white, black], config)

    assert plan is not None
    assert plan.ordered_colors == ["#000000"]
    assert white.assigned_color is None
    assert black.assigned_color == "#000000"


def test_write_toolpaths_to_gcode_color_mode_inserts_pause(tmp_path: Path, color_config_path: Path) -> None:
    config = cli.load_config(color_config_path)
    toolpaths = [
        Toolpath(points=((0, 0), (5, 0)), source_color=(0, 0, 0)),
        Toolpath(points=((0, 0), (6, 0)), source_color=(255, 0, 0)),
    ]
    output = tmp_path / "out.gcode"

    result = cli.write_toolpaths_to_gcode(toolpaths, output, config)

    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "COLOR ORDER" in text
    assert "M600" in text
    assert result.line_count > 0
    assert len(result.color_order) == 2


def test_write_toolpaths_to_gcode_color_mode_glides_within_color_group(tmp_path: Path, color_config_path: Path) -> None:
    config = cli.load_config(color_config_path)
    toolpaths = [
        Toolpath(points=((0, 0), (5, 0)), source_color=(0, 0, 0)),
        Toolpath(points=((5, 0), (10, 0)), source_color=(0, 0, 0)),
    ]
    output = tmp_path / "glide_color_mode.gcode"

    cli.write_toolpaths_to_gcode(toolpaths, output, config)

    text = output.read_text(encoding="utf-8")
    assert text.count("G0 ") == 1
    assert text.count("G1 Z1.200") == 1


def test_write_toolpaths_to_gcode_verbose_flag_emits_debug_comments(tmp_path: Path, config_path: Path) -> None:
    config = cli.load_config(config_path)
    toolpaths = [
        Toolpath(points=((0, 0), (1, 0))),
        Toolpath(points=((1.5, 0), (2.5, 0))),
    ]
    output = tmp_path / "verbose.gcode"

    cli.write_toolpaths_to_gcode(toolpaths, output, config, verbose_gcode=True)

    text = output.read_text(encoding="utf-8")
    assert "Verbose G-code comments enabled." in text
    assert "TOOLPATH 1/2" in text
    assert "GLIDE gap=0.500mm to toolpath 2/2" in text


def test_write_toolpaths_to_gcode_color_mode_omits_white_paths(tmp_path: Path, color_config_path: Path) -> None:
    config = cli.load_config(color_config_path)
    config.printer.available_colors = ["#FFFFFF", "#000000"]
    toolpaths = [
        Toolpath(points=((0, 0), (5, 0)), source_color=(255, 255, 255)),
        Toolpath(points=((0, 0), (6, 0)), source_color=(0, 0, 0)),
    ]
    output = tmp_path / "white_omitted.gcode"

    result = cli.write_toolpaths_to_gcode(toolpaths, output, config)

    text = output.read_text(encoding="utf-8")
    assert "#FFFFFF" not in result.color_order
    assert result.color_order == ["#000000"]
    assert "COLOR 1/1: #000000" in text


def test_write_toolpaths_to_gcode_color_mode_skips_all_white_output(tmp_path: Path, color_config_path: Path) -> None:
    config = cli.load_config(color_config_path)
    config.printer.available_colors = ["#FFFFFF", "#000000"]
    toolpaths = [Toolpath(points=((0, 0), (5, 0)), source_color=(255, 255, 255))]
    output = tmp_path / "all_white.gcode"

    result = cli.write_toolpaths_to_gcode(toolpaths, output, config)

    text = output.read_text(encoding="utf-8")
    assert result.color_order == []
    assert "No non-white toolpaths after palette assignment." in text
    assert "X5.000" not in text


def test_write_toolpaths_to_gcode_bw_mode_no_color_comments(tmp_path: Path, config_path: Path) -> None:
    config = cli.load_config(config_path)
    toolpaths = [Toolpath(points=((0, 0), (3, 0)), source_color=(0, 0, 0))]
    output = tmp_path / "bw.gcode"

    result = cli.write_toolpaths_to_gcode(toolpaths, output, config)

    text = output.read_text(encoding="utf-8")
    assert "COLOR ORDER" not in text
    assert result.color_order == []


def test_main_respects_color_mode_override(monkeypatch, slicer_config, tmp_path: Path) -> None:
    called = {}
    slicer_config.printer.available_colors = ["#000000", "#FF0000"]

    def fake_load_config(path, profile=None):
        return slicer_config

    def fake_slice(
        svg,
        output,
        config,
        preview,
        preview_file,
        *,
        scale_factor=None,
        alignment="center",
        pdf_page=1,
        force_hershey_text=False,
        rotation_degrees=0.0,
        verbose_gcode=False,
    ):
        called["svg"] = svg
        called["output"] = output
        called["color_mode"] = config.printer.color_mode
        called["scale_factor"] = scale_factor
        called["alignment"] = alignment
        called["pdf_page"] = pdf_page
        called["force_hershey_text"] = force_hershey_text
        called["rotation_degrees"] = rotation_degrees
        called["verbose_gcode"] = verbose_gcode

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "slice_svg_to_gcode", fake_slice)

    svg_path = tmp_path / "input.svg"
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    code = cli.main([str(svg_path), "--color-mode"])
    assert code == 0
    assert called["color_mode"] is True
    assert called["scale_factor"] is None
    assert called["alignment"] == "center"
    assert called["pdf_page"] == 1
    assert called["force_hershey_text"] is False
    assert called["rotation_degrees"] == pytest.approx(0.0)


def test_main_passes_manual_scale_to_slice(monkeypatch, slicer_config, tmp_path: Path) -> None:
    called = {}

    def fake_load_config(path, profile=None):
        return slicer_config

    def fake_slice(
        svg,
        output,
        config,
        preview,
        preview_file,
        *,
        scale_factor=None,
        alignment="center",
        pdf_page=1,
        force_hershey_text=False,
        rotation_degrees=0.0,
        verbose_gcode=False,
    ):
        called["scale_factor"] = scale_factor
        called["alignment"] = alignment
        called["pdf_page"] = pdf_page
        called["force_hershey_text"] = force_hershey_text
        called["rotation_degrees"] = rotation_degrees
        called["verbose_gcode"] = verbose_gcode

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "slice_svg_to_gcode", fake_slice)

    svg_path = tmp_path / "input.svg"
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    code = cli.main([str(svg_path), "--scale", "none", "--alignment", "center"])
    assert code == 0
    assert called["scale_factor"] == pytest.approx(1.0)
    assert called["alignment"] == "center"
    assert called["rotation_degrees"] == pytest.approx(0.0)


def test_main_passes_pdf_page_and_hershey_to_slice(monkeypatch, slicer_config, tmp_path: Path) -> None:
    called = {}

    def fake_load_config(path, profile=None):
        return slicer_config

    def fake_slice(
        svg,
        output,
        config,
        preview,
        preview_file,
        *,
        scale_factor=None,
        alignment="center",
        pdf_page=1,
        force_hershey_text=False,
        rotation_degrees=0.0,
        verbose_gcode=False,
    ):
        called["pdf_page"] = pdf_page
        called["force_hershey_text"] = force_hershey_text
        called["rotation_degrees"] = rotation_degrees
        called["verbose_gcode"] = verbose_gcode

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "slice_svg_to_gcode", fake_slice)

    pdf_path = tmp_path / "input.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")

    code = cli.main([str(pdf_path), "--pdf-page", "4", "--hershey", "--rotate", "45"])
    assert code == 0
    assert called["pdf_page"] == 4
    assert called["force_hershey_text"] is True
    assert called["rotation_degrees"] == pytest.approx(45.0)


def test_main_passes_raster_spacing_override(monkeypatch, slicer_config, tmp_path: Path) -> None:
    called = {}

    def fake_load_config(path, profile=None):
        slicer_config.sampling.raster_sample_spacing = 2.0
        return slicer_config

    def fake_slice(
        svg,
        output,
        config,
        preview,
        preview_file,
        *,
        scale_factor=None,
        alignment="center",
        pdf_page=1,
        force_hershey_text=False,
        rotation_degrees=0.0,
        verbose_gcode=False,
    ):
        called["raster_spacing"] = config.sampling.raster_sample_spacing
        called["verbose_gcode"] = verbose_gcode

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "slice_svg_to_gcode", fake_slice)

    pdf_path = tmp_path / "input.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")

    code = cli.main([str(pdf_path), "--raster-spacing", "0.6"])
    assert code == 0
    assert called["raster_spacing"] == pytest.approx(0.6)


def test_main_fails_when_color_mode_enabled_without_palette(monkeypatch, slicer_config, tmp_path: Path) -> None:
    slicer_config.printer.color_mode = True
    slicer_config.printer.available_colors = []

    monkeypatch.setattr(cli, "load_config", lambda path, profile=None: slicer_config)

    svg_path = tmp_path / "input.svg"
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    code = cli.main([str(svg_path)])
    assert code == 1
