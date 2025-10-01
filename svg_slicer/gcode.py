from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from .config import Feedrates, PrinterConfig

Point = Tuple[float, float]
Polyline = Sequence[Point]


@dataclass
class Toolpath:
    points: Polyline
    tag: str = "infill"


class GcodeGenerator:
    def __init__(self, printer: PrinterConfig) -> None:
        self.printer = printer
        self._gcode: List[str] = []
        self._position: Point | None = None
        self._z_height: float = printer.z_travel
        self._feedrate: float | None = None

    def _emit(self, line: str) -> None:
        self._gcode.append(line)

    def _format_xy(self, point: Point) -> str:
        x, y = point
        return f"X{x:.3f} Y{y:.3f}"

    def _emit_with_feed(self, command: str, point_fmt: str, feedrate: float) -> None:
        if self._feedrate != feedrate:
            self._emit(f"{command} {point_fmt} F{feedrate:.0f}")
            self._feedrate = feedrate
        else:
            self._emit(f"{command} {point_fmt}")

    def _rapid_move(self, point: Point, feedrate: float) -> None:
        if self._position == point:
            return
        self._emit_with_feed("G0", self._format_xy(point), feedrate)
        self._position = point

    def _linear_move(self, point: Point, feedrate: float) -> None:
        if self._position == point:
            return
        self._emit_with_feed("G1", self._format_xy(point), feedrate)
        self._position = point

    def _set_z(self, z: float, feedrate: float) -> None:
        if abs(self._z_height - z) < 1e-6:
            return
        if self._feedrate != feedrate:
            self._emit(f"G1 Z{z:.3f} F{feedrate:.0f}")
            self._feedrate = feedrate
        else:
            self._emit(f"G1 Z{z:.3f}")
        self._z_height = z

    def emit_header(self) -> None:
        for line in self.printer.start_gcode:
            self._emit(line)

    def emit_footer(self) -> None:
        for line in self.printer.end_gcode:
            self._emit(line)

    def draw_toolpaths(self, toolpaths: Iterable[Toolpath], feedrates: Feedrates) -> None:
        draw_feed = feedrates.draw_feedrate
        travel_feed = feedrates.travel_feedrate
        z_feed = feedrates.z_feedrate
        draw_height = self.printer.z_draw
        travel_height = self.printer.z_travel

        for toolpath in toolpaths:
            points = list(toolpath.points)
            if len(points) < 2:
                continue
            start = points[0]
            self._set_z(travel_height, z_feed)
            self._rapid_move(start, travel_feed)
            self._set_z(draw_height, z_feed)
            for point in points[1:]:
                self._linear_move(point, draw_feed)
            self._set_z(travel_height, z_feed)

    def generate(self) -> List[str]:
        return self._gcode


def toolpaths_from_polylines(polylines: Iterable[Polyline], tag: str = "infill") -> List[Toolpath]:
    return [Toolpath(points=tuple(polyline), tag=tag) for polyline in polylines if len(polyline) >= 2]
