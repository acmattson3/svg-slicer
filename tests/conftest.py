from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from svg_slicer.config import SlicerConfig, load_config
from svg_slicer.svg_parser import ShapeGeometry


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")


def make_config_dict(*, color_mode: bool = False, profiles: bool = False) -> Dict[str, Any]:
    printer_block: Dict[str, Any] = {
        "name": "Test Printer",
        "bed_size_mm": {"width": 120.0, "depth": 80.0},
        "origin_offsets_mm": {"x_min": 10.0, "x_max": 110.0, "y_min": 5.0, "y_max": 75.0},
        "z_heights_mm": {"draw": 0.2, "travel": 1.2},
        "z_lift_height_mm": 1.5,
        "feedrates_mm_s": {"draw": 20.0, "travel": 50.0, "z": 5.0},
        "start_gcode": ["G21", "G90"],
        "end_gcode": ["M18"],
        "color_mode": color_mode,
        "available_colors": ["#000000", "#FF0000"] if color_mode else [],
        "pause_gcode": ["M600"],
    }

    root: Dict[str, Any] = {
        "infill": {
            "base_line_spacing_mm": 2.0,
            "min_density": 0.1,
            "max_density": 0.9,
            "angles_degrees": [0.0, 90.0],
        },
        "perimeter": {
            "thickness_mm": 0.5,
            "count": 1,
            "min_fill_width_mm": 0.6,
            "min_fill_mode": "min",
        },
        "sampling": {
            "segment_length_tolerance_mm": 0.5,
            "outline_simplify_tolerance_mm": 0.25,
            "curve_detail_scale": 1.0,
            "raster_sample_spacing_mm": 2.0,
            "raster_line_spacing_mm": None,
            "raster_max_cells": 4000,
            "image_mode": "vectorize",
            "image_vector_num_colors": 16,
            "image_vector_epsilon_px": 6.0,
            "image_vector_min_area_px": 64.0,
            "image_vector_blur_kernel_px": 3,
            "image_vector_max_pixels": 250000,
        },
        "rendering": {
            "preview_line_width_mm": 0.35,
        },
    }

    if profiles:
        root["default_printer"] = "fast"
        root["printers"] = {
            "fast": {**printer_block, "name": "Fast"},
            "precise": {
                **printer_block,
                "name": "Precise",
                "feedrates_mm_s": {"draw": 10.0, "travel": 20.0, "z": 4.0},
            },
        }
    else:
        root["printer"] = printer_block

    return root


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(make_config_dict(), sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def color_config_path(tmp_path: Path) -> Path:
    path = tmp_path / "color_config.yaml"
    path.write_text(yaml.safe_dump(make_config_dict(color_mode=True), sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def profile_config_path(tmp_path: Path) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(make_config_dict(profiles=True), sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def slicer_config(config_path: Path) -> SlicerConfig:
    return load_config(config_path)


@pytest.fixture
def square_shape() -> ShapeGeometry:
    return ShapeGeometry(
        geometry=Polygon([(0, 0), (20, 0), (20, 10), (0, 10)]),
        brightness=0.4,
        stroke_width=None,
        color=(20, 30, 40),
    )


@pytest.fixture
def simple_svg_path(tmp_path: Path) -> Path:
    svg = (
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"100\" height=\"100\">\n"
        "<rect x=\"10\" y=\"20\" width=\"40\" height=\"30\" fill=\"#ff0000\" />\n"
        "</svg>\n"
    )
    path = tmp_path / "simple.svg"
    path.write_text(svg, encoding="utf-8")
    return path


@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
