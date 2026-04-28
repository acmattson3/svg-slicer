from __future__ import annotations

import pytest

from svg_slicer.config import Feedrates, PrinterConfig
from svg_slicer.gcode import (
    GcodeGenerator,
    Toolpath,
    _format_duration,
    _optimize_toolpath_order,
    toolpaths_from_polylines,
)


def _printer() -> PrinterConfig:
    return PrinterConfig(
        name="Plotter",
        bed_width=200.0,
        bed_depth=200.0,
        x_min=0.0,
        x_max=200.0,
        y_min=0.0,
        y_max=200.0,
        z_draw=0.2,
        z_travel=1.2,
        z_raster_travel=1.2,
        z_lift=1.5,
        glide_threshold=0.8,
        feedrates=Feedrates(draw_mm_s=10.0, travel_mm_s=20.0, z_mm_s=5.0),
        start_gcode=["G21"],
        end_gcode=["M18"],
    )


def test_toolpaths_from_polylines_filters_short_lines() -> None:
    paths = toolpaths_from_polylines([
        [(0, 0)],
        [(0, 0), (1, 1)],
        [(2, 2), (3, 3), (4, 4)],
    ])
    assert len(paths) == 2
    assert all(len(path.points) >= 2 for path in paths)


def test_format_duration_variants() -> None:
    assert _format_duration(5.2) == "5.2s"
    assert _format_duration(65) == "1m 05s"
    assert _format_duration(3661) == "1h 01m 01s"


def test_gcode_generator_draw_single_toolpath_emits_motion_and_time() -> None:
    gen = GcodeGenerator(_printer())
    toolpath = Toolpath(points=((0.0, 0.0), (10.0, 0.0), (10.0, 5.0)))

    gen.emit_header()
    gen.draw_single_toolpath(toolpath, _printer().feedrates)
    gen.emit_footer()

    lines = gen.generate()
    assert lines[0] == "G21"
    assert lines[-1] == "M18"
    assert any(line.startswith("G0 X0.000 Y0.000") for line in lines)
    assert any(line.startswith("G1 X10.000 Y0.000") for line in lines)
    assert any(line.startswith("G1 Z0.200") for line in lines)
    assert any(line.startswith("G1 Z1.200") for line in lines)
    assert gen.elapsed_time_seconds > 0
    assert "s" in gen.formatted_elapsed_time()


def test_gcode_generator_uses_raster_travel_height_for_raster_toolpaths() -> None:
    printer = _printer()
    printer.z_travel = 5.0
    printer.z_raster_travel = 0.8
    gen = GcodeGenerator(printer)

    gen.draw_single_toolpath(
        Toolpath(points=((0.0, 0.0), (4.0, 0.0)), tag="raster"),
        printer.feedrates,
    )

    lines = gen.generate()
    assert any(line.startswith("G1 Z0.800") for line in lines)
    assert not any(line.startswith("G1 Z5.000") for line in lines)


def test_gcode_generator_skips_degenerate_toolpath() -> None:
    gen = GcodeGenerator(_printer())
    gen.draw_single_toolpath(Toolpath(points=((1.0, 1.0),)), _printer().feedrates)
    assert gen.generate() == []


def test_gcode_generator_glides_across_short_gap_between_toolpaths() -> None:
    gen = GcodeGenerator(_printer())
    toolpaths = [
        Toolpath(points=((0.0, 0.0), (1.0, 0.0))),
        Toolpath(points=((1.5, 0.0), (2.5, 0.0))),
    ]

    gen.draw_toolpaths(toolpaths, _printer().feedrates)

    lines = gen.generate()
    assert sum(1 for line in lines if line.startswith("G0 ")) == 1
    assert any(line.startswith("G1 X1.500 Y0.000") for line in lines)
    assert sum(1 for line in lines if line.startswith("G1 Z1.200")) == 1


def test_gcode_generator_does_not_glide_across_different_glyph_groups() -> None:
    printer = _printer()
    printer.glide_threshold = 2.5
    gen = GcodeGenerator(printer)
    toolpaths = [
        Toolpath(points=((0.0, 0.0), (1.0, 0.0)), glide_group="glyph:a"),
        Toolpath(points=((1.5, 0.0), (2.5, 0.0)), glide_group="glyph:b"),
    ]

    gen.draw_toolpaths(toolpaths, printer.feedrates)

    lines = gen.generate()
    assert sum(1 for line in lines if line.startswith("G0 ")) == 2
    assert sum(1 for line in lines if line.startswith("G1 Z1.200")) >= 2


def test_gcode_generator_lifts_across_large_gap_between_toolpaths() -> None:
    gen = GcodeGenerator(_printer())
    toolpaths = [
        Toolpath(points=((0.0, 0.0), (1.0, 0.0))),
        Toolpath(points=((5.0, 0.0), (6.0, 0.0))),
    ]

    gen.draw_toolpaths(toolpaths, _printer().feedrates)

    lines = gen.generate()
    assert any(line.startswith("G1 Z1.200") for line in lines)
    assert any(line.startswith("G0 X5.000 Y0.000") for line in lines)


def test_gcode_generator_verbose_comments_report_glide_and_lift() -> None:
    gen = GcodeGenerator(_printer(), verbose_comments=True)
    toolpaths = [
        Toolpath(points=((0.0, 0.0), (1.0, 0.0)), tag="stroke", source_color=(0, 0, 0)),
        Toolpath(points=((1.5, 0.0), (2.5, 0.0)), tag="stroke", source_color=(0, 0, 0)),
        Toolpath(points=((5.0, 0.0), (6.0, 0.0)), tag="stroke", source_color=(0, 0, 0)),
    ]

    gen.draw_toolpaths(toolpaths, _printer().feedrates)

    lines = gen.generate()
    assert any("TOOLPATH 1/3 tag=stroke" in line for line in lines)
    assert any("GLIDE gap=0.500mm to toolpath 2/3" in line for line in lines)
    assert any("LIFT gap=2.500mm before toolpath 3/3" in line for line in lines)


def test_optimize_toolpath_order_reorders_and_reverses_for_shorter_travel() -> None:
    ordered = _optimize_toolpath_order(
        [
            Toolpath(points=((10.0, 0.0), (11.0, 0.0))),
            Toolpath(points=((2.0, 0.0), (1.2, 0.0))),
            Toolpath(points=((0.0, 0.0), (1.0, 0.0))),
        ],
        start_point=(0.0, 0.0),
    )

    assert ordered[0].points == ((0.0, 0.0), (1.0, 0.0))
    assert ordered[1].points == ((1.2, 0.0), (2.0, 0.0))
    assert ordered[2].points == ((10.0, 0.0), (11.0, 0.0))


def test_gcode_generator_draw_toolpaths_optimizes_order_for_non_raster_paths() -> None:
    gen = GcodeGenerator(_printer())
    toolpaths = [
        Toolpath(points=((10.0, 0.0), (11.0, 0.0))),
        Toolpath(points=((2.0, 0.0), (1.2, 0.0))),
        Toolpath(points=((0.0, 0.0), (1.0, 0.0))),
    ]

    gen.draw_toolpaths(toolpaths, _printer().feedrates)

    lines = gen.generate()
    first_rapid = next(line for line in lines if line.startswith("G0 "))
    assert first_rapid.startswith("G0 X0.000 Y0.000")
    assert any(line.startswith("G1 X1.200 Y0.000") for line in lines)


def test_feedrate_is_not_repeated_when_unchanged() -> None:
    gen = GcodeGenerator(_printer())
    path = Toolpath(points=((0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (6.0, 0.0)))
    gen.draw_single_toolpath(path, _printer().feedrates)
    g1_lines = [line for line in gen.generate() if line.startswith("G1 X")]
    assert g1_lines
    assert " F600" in g1_lines[0]
    assert all(" F600" not in line for line in g1_lines[1:])


def test_elapsed_time_ignores_zero_distance_moves() -> None:
    gen = GcodeGenerator(_printer())
    path = Toolpath(points=((1.0, 1.0), (1.0, 1.0), (2.0, 1.0)))
    gen.draw_single_toolpath(path, _printer().feedrates)
    assert gen.elapsed_time_seconds > 0
    # The exact time is implementation-specific, but should be finite and small.
    assert gen.elapsed_time_seconds < 10
