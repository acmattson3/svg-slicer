from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List

import yaml


@dataclass
class Feedrates:
    draw_mm_s: float
    travel_mm_s: float
    z_mm_s: float

    @property
    def draw_feedrate(self) -> float:
        return self.draw_mm_s * 60.0

    @property
    def travel_feedrate(self) -> float:
        return self.travel_mm_s * 60.0

    @property
    def z_feedrate(self) -> float:
        return self.z_mm_s * 60.0


@dataclass
class PrinterConfig:
    name: str
    bed_width: float
    bed_depth: float
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_draw: float
    z_travel: float
    z_lift: float
    feedrates: Feedrates
    start_gcode: List[str]
    end_gcode: List[str]

    @property
    def printable_width(self) -> float:
        return self.x_max - self.x_min

    @property
    def printable_depth(self) -> float:
        return self.y_max - self.y_min


@dataclass
class InfillConfig:
    base_spacing: float
    min_density: float
    max_density: float
    angles: List[float]


@dataclass
class SamplingConfig:
    segment_tolerance: float
    outline_simplify_tolerance: float


@dataclass
class RenderingConfig:
    line_width: float


@dataclass
class PerimeterConfig:
    thickness: float
    density: float
    min_fill_width: float


@dataclass
class SlicerConfig:
    printer: PrinterConfig
    infill: InfillConfig
    perimeter: PerimeterConfig
    sampling: SamplingConfig
    rendering: RenderingConfig


class ConfigError(Exception):
    """Raised when configuration values are missing or invalid."""


def _require(mapping: Dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing required configuration key: {key}")
    return mapping[key]


def _parse_printer_config(printer_raw: Dict[str, Any], fallback_name: str | None = None) -> PrinterConfig:
    bed = _require(printer_raw, "bed_size_mm")
    offsets = _require(printer_raw, "origin_offsets_mm")
    z_heights = _require(printer_raw, "z_heights_mm")
    feedrates_raw = _require(printer_raw, "feedrates_mm_s")

    feedrates = Feedrates(
        draw_mm_s=float(_require(feedrates_raw, "draw")),
        travel_mm_s=float(_require(feedrates_raw, "travel")),
        z_mm_s=float(_require(feedrates_raw, "z")),
    )

    printer_name = str(printer_raw.get("name", fallback_name or "PenPlotter"))

    return PrinterConfig(
        name=printer_name,
        bed_width=float(_require(bed, "width")),
        bed_depth=float(_require(bed, "depth")),
        x_min=float(_require(offsets, "x_min")),
        x_max=float(_require(offsets, "x_max")),
        y_min=float(_require(offsets, "y_min")),
        y_max=float(_require(offsets, "y_max")),
        z_draw=float(_require(z_heights, "draw")),
        z_travel=float(_require(z_heights, "travel")),
        z_lift=float(printer_raw.get("z_lift_height_mm", z_heights.get("travel", 5.0))),
        feedrates=feedrates,
        start_gcode=list(printer_raw.get("start_gcode", [])),
        end_gcode=list(printer_raw.get("end_gcode", [])),
    )


def load_config(path: str | pathlib.Path, profile: str | None = None) -> SlicerConfig:
    config_path = pathlib.Path(path)
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    printer_raw: Dict[str, Any]
    printer_profile_name: str | None = None

    if "printers" in raw:
        printers_section = raw.get("printers")
        if not isinstance(printers_section, dict) or not printers_section:
            raise ConfigError("'printers' must be a non-empty mapping of profiles.")

        if profile is None:
            default_profile = raw.get("default_printer")
            if default_profile:
                if default_profile not in printers_section:
                    available = ", ".join(sorted(printers_section))
                    raise ConfigError(
                        f"Default printer profile '{default_profile}' not found. Available profiles: {available}"
                    )
                printer_profile_name = str(default_profile)
            else:
                printer_profile_name = next(iter(printers_section))
        else:
            if profile not in printers_section:
                available = ", ".join(sorted(printers_section))
                raise ConfigError(
                    f"Printer profile '{profile}' not found. Available profiles: {available}"
                )
            printer_profile_name = str(profile)

        printer_raw = printers_section[printer_profile_name]
        if not isinstance(printer_raw, dict):
            raise ConfigError(f"Printer profile '{printer_profile_name}' must be a mapping of settings.")
    else:
        if profile is not None:
            raise ConfigError(
                "Printer profile specified but configuration does not define any profiles."
            )
        printer_raw = _require(raw, "printer")

    printer = _parse_printer_config(printer_raw, fallback_name=printer_profile_name)

    infill_raw = _require(raw, "infill")
    infill = InfillConfig(
        base_spacing=float(_require(infill_raw, "base_line_spacing_mm")),
        min_density=float(_require(infill_raw, "min_density")),
        max_density=float(_require(infill_raw, "max_density")),
        angles=list(_require(infill_raw, "angles_degrees")),
    )

    sampling_raw = _require(raw, "sampling")
    sampling = SamplingConfig(
        segment_tolerance=float(_require(sampling_raw, "segment_length_tolerance_mm")),
        outline_simplify_tolerance=float(
            sampling_raw.get(
                "outline_simplify_tolerance_mm",
                sampling_raw.get("segment_length_tolerance_mm", 0.5),
            )
        ),
    )

    perimeter_raw = raw.get("perimeter", {})
    perimeter = PerimeterConfig(
        thickness=float(perimeter_raw.get("thickness_mm", 0.45)),
        density=float(perimeter_raw.get("density", 1.0)),
        min_fill_width=float(perimeter_raw.get("min_fill_width_mm", 0.8)),
    )

    rendering_raw = raw.get("rendering", {})
    rendering = RenderingConfig(
        line_width=float(rendering_raw.get("preview_line_width_mm", 0.35)),
    )

    return SlicerConfig(
        printer=printer,
        infill=infill,
        perimeter=perimeter,
        sampling=sampling,
        rendering=rendering,
    )
