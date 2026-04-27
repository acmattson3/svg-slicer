from __future__ import annotations

import pytest

import svg_slicer.gui as gui


def test_settings_tab_round_trips_raster_spacing(qapp, slicer_config) -> None:
    tab = gui.SettingsTab()
    tab.set_config(slicer_config, profiles=[], current_profile=None)

    tab.raster_spacing_spin.setValue(0.65)
    tab.glide_threshold_spin.setValue(1.1)

    updated = tab._assemble_config()

    assert updated.sampling.raster_sample_spacing == pytest.approx(0.65)
    assert updated.sampling.raster_max_cells == slicer_config.sampling.raster_max_cells
    assert updated.printer.glide_threshold == pytest.approx(1.1)


def test_config_to_yaml_includes_raster_sampling(qapp, slicer_config) -> None:
    slicer_config.sampling.raster_sample_spacing = 0.9
    slicer_config.sampling.raster_max_cells = 1234
    slicer_config.printer.glide_threshold = 1.4

    data = gui.MainWindow._config_to_yaml(slicer_config)

    assert data["sampling"]["raster_sample_spacing_mm"] == pytest.approx(0.9)
    assert data["sampling"]["raster_max_cells"] == 1234
    assert data["printer"]["glide_threshold_mm"] == pytest.approx(1.4)
