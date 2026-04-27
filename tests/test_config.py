from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from svg_slicer.config import ConfigError, Feedrates, _normalize_hex_color, load_config


def _make_base_config() -> dict:
    return {
        "printer": {
            "name": "Test Printer",
            "bed_size_mm": {"width": 120.0, "depth": 80.0},
            "origin_offsets_mm": {"x_min": 10.0, "x_max": 110.0, "y_min": 5.0, "y_max": 75.0},
            "z_heights_mm": {"draw": 0.2, "travel": 1.2},
            "z_lift_height_mm": 1.5,
            "glide_threshold_mm": 0.8,
            "feedrates_mm_s": {"draw": 20.0, "travel": 50.0, "z": 5.0},
            "start_gcode": ["G21"],
            "end_gcode": ["M18"],
            "color_mode": False,
            "available_colors": [],
            "pause_gcode": ["M600"],
        },
        "infill": {
            "base_line_spacing_mm": 2.0,
            "min_density": 0.1,
            "max_density": 0.9,
            "angles_degrees": [0.0, 90.0],
        },
        "perimeter": {"thickness_mm": 0.5, "count": 1, "min_fill_width_mm": 0.6, "min_fill_mode": "min"},
        "sampling": {
            "segment_length_tolerance_mm": 0.5,
            "outline_simplify_tolerance_mm": 0.25,
            "curve_detail_scale": 1.0,
        },
        "rendering": {"preview_line_width_mm": 0.35},
    }


def test_feedrates_properties_convert_mm_s_to_mm_min() -> None:
    rates = Feedrates(draw_mm_s=12.5, travel_mm_s=40.0, z_mm_s=5.5)
    assert rates.draw_feedrate == pytest.approx(750.0)
    assert rates.travel_feedrate == pytest.approx(2400.0)
    assert rates.z_feedrate == pytest.approx(330.0)


def test_normalize_hex_color_accepts_hash_and_uppercases() -> None:
    assert _normalize_hex_color("#a1b2c3") == "#A1B2C3"
    assert _normalize_hex_color("a1b2c3") == "#A1B2C3"


def test_normalize_hex_color_rejects_bad_values() -> None:
    with pytest.raises(ConfigError):
        _normalize_hex_color("#12345")
    with pytest.raises(ConfigError):
        _normalize_hex_color("#GGGGGG")


def test_load_config_from_flat_mapping(config_path: Path) -> None:
    cfg = load_config(config_path)
    assert cfg.printer.name == "Test Printer"
    assert cfg.printer.printable_width == pytest.approx(100.0)
    assert cfg.printer.printable_depth == pytest.approx(70.0)
    assert cfg.printer.z_raster_travel == pytest.approx(cfg.printer.z_travel)
    assert cfg.printer.glide_threshold == pytest.approx(0.8)
    assert cfg.sampling.curve_detail_scale == pytest.approx(1.0)
    assert cfg.perimeter.count == 1
    assert cfg.perimeter.min_fill_mode == "min"


def test_load_config_reads_raster_travel_height(tmp_path: Path) -> None:
    data = _make_base_config()
    data["printer"]["z_heights_mm"]["raster_travel"] = 0.8
    path = tmp_path / "raster_travel.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    cfg = load_config(path)

    assert cfg.printer.z_raster_travel == pytest.approx(0.8)


def test_load_config_reads_glide_threshold(tmp_path: Path) -> None:
    data = _make_base_config()
    data["printer"]["glide_threshold_mm"] = 1.25
    path = tmp_path / "glide_threshold.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    cfg = load_config(path)

    assert cfg.printer.glide_threshold == pytest.approx(1.25)


def test_load_config_profiles_use_default(profile_config_path: Path) -> None:
    cfg = load_config(profile_config_path)
    assert cfg.printer.name == "Fast"


def test_load_config_profiles_explicit_choice(profile_config_path: Path) -> None:
    cfg = load_config(profile_config_path, profile="precise")
    assert cfg.printer.name == "Precise"
    assert cfg.printer.feedrates.draw_mm_s == pytest.approx(10.0)


def test_load_config_profile_not_found_raises(profile_config_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(profile_config_path, profile="missing")
    assert "Available profiles" in str(exc.value)


def test_load_config_color_mode_requires_palette(tmp_path: Path) -> None:
    data = _make_base_config()
    data["printer"]["color_mode"] = True
    data["printer"]["available_colors"] = []
    path = tmp_path / "bad_color.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "available_colors" in str(exc.value)


def test_load_config_empty_pause_gcode_defaults_to_m600(tmp_path: Path) -> None:
    data = _make_base_config()
    data["printer"]["pause_gcode"] = []
    path = tmp_path / "pause_default.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.printer.pause_gcode == ["M600"]


def test_load_config_perimeter_density_fallback_sets_count(tmp_path: Path) -> None:
    data = _make_base_config()
    data["perimeter"].pop("count", None)
    data["perimeter"]["density"] = 2.6
    path = tmp_path / "density_fallback.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.perimeter.count == 3


def test_load_config_perimeter_min_fill_mode_default_is_min(tmp_path: Path) -> None:
    data = _make_base_config()
    data["perimeter"].pop("min_fill_mode", None)
    path = tmp_path / "mode_default.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.perimeter.min_fill_mode == "min"


def test_load_config_perimeter_min_fill_mode_invalid_raises(tmp_path: Path) -> None:
    data = _make_base_config()
    data["perimeter"]["min_fill_mode"] = "bad-mode"
    path = tmp_path / "mode_invalid.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
