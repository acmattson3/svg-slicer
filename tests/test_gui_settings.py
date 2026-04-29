from __future__ import annotations

import pytest

import svg_slicer.gui as gui


def test_settings_tab_round_trips_raster_spacing(qapp, slicer_config) -> None:
    tab = gui.SettingsTab()
    tab.set_config(slicer_config, profiles=[], current_profile=None)

    tab.raster_spacing_spin.setValue(0.65)
    tab.raster_line_spacing_spin.setValue(0.8)
    tab.image_mode_combo.setCurrentIndex(tab.image_mode_combo.findData("vectorize"))
    tab.image_vector_num_colors_spin.setValue(24)
    tab.image_vector_epsilon_spin.setValue(2.5)
    tab.image_vector_min_area_spin.setValue(18.0)
    tab.image_vector_blur_kernel_spin.setValue(5)
    tab.image_vector_max_pixels_spin.setValue(123456)
    tab.glide_threshold_spin.setValue(1.1)

    updated = tab._assemble_config()

    assert updated.sampling.raster_sample_spacing == pytest.approx(0.65)
    assert updated.sampling.raster_line_spacing == pytest.approx(0.8)
    assert updated.sampling.raster_max_cells == slicer_config.sampling.raster_max_cells
    assert updated.sampling.image_mode == "vectorize"
    assert updated.sampling.image_vector_num_colors == 24
    assert updated.sampling.image_vector_epsilon == pytest.approx(2.5)
    assert updated.sampling.image_vector_min_area == pytest.approx(18.0)
    assert updated.sampling.image_vector_blur_kernel == 5
    assert updated.sampling.image_vector_max_pixels == 123456
    assert updated.printer.glide_threshold == pytest.approx(1.1)


def test_config_to_yaml_includes_raster_sampling(qapp, slicer_config) -> None:
    slicer_config.sampling.raster_sample_spacing = 0.9
    slicer_config.sampling.raster_line_spacing = 0.8
    slicer_config.sampling.raster_max_cells = 1234
    slicer_config.sampling.image_mode = "vectorize"
    slicer_config.sampling.image_vector_num_colors = 24
    slicer_config.sampling.image_vector_epsilon = 2.5
    slicer_config.sampling.image_vector_min_area = 18.0
    slicer_config.sampling.image_vector_blur_kernel = 5
    slicer_config.sampling.image_vector_max_pixels = 123456
    slicer_config.printer.glide_threshold = 1.4
    slicer_config.printer.available_color_names = ["Black", "Red"]

    data = gui.MainWindow._config_to_yaml(slicer_config)

    assert data["sampling"]["raster_sample_spacing_mm"] == pytest.approx(0.9)
    assert data["sampling"]["raster_line_spacing_mm"] == pytest.approx(0.8)
    assert data["sampling"]["raster_max_cells"] == 1234
    assert data["sampling"]["image_mode"] == "vectorize"
    assert data["sampling"]["image_vector_num_colors"] == 24
    assert data["sampling"]["image_vector_epsilon_px"] == pytest.approx(2.5)
    assert data["sampling"]["image_vector_min_area_px"] == pytest.approx(18.0)
    assert data["sampling"]["image_vector_blur_kernel_px"] == 5
    assert data["sampling"]["image_vector_max_pixels"] == 123456
    assert data["printer"]["glide_threshold_mm"] == pytest.approx(1.4)
    assert data["printer"]["available_color_names"] == ["Black", "Red"]


def test_prepare_tab_exposes_verbose_gcode_checkbox(qapp) -> None:
    tab = gui.PrepareTab()

    assert tab.verbose_gcode_enabled() is False

    tab.verbose_gcode_checkbox.setChecked(True)

    assert tab.verbose_gcode_enabled() is True


def test_prepare_tab_exposes_write_in_order_checkbox(qapp) -> None:
    tab = gui.PrepareTab()

    assert tab.write_in_order_enabled() is False

    tab.write_in_order_checkbox.setChecked(True)

    assert tab.write_in_order_enabled() is True
