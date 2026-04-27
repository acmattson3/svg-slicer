from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from .config import Feedrates, PrinterConfig

Point = Tuple[float, float]
Polyline = Sequence[Point]

logger = logging.getLogger(__name__)


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class Toolpath:
    points: Polyline
    tag: str = "infill"
    source_color: tuple[int, int, int] | None = None
    assigned_color: str | None = None
    brightness: float | None = None


class GcodeGenerator:
    def __init__(self, printer: PrinterConfig) -> None:
        self.printer = printer
        self._gcode: List[str] = []
        self._position: Point | None = None
        self._z_height: float = printer.z_travel
        self._pen_is_down = False
        self._feedrate: float | None = None
        self._elapsed_time: float = 0.0

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
        if self._position is not None:
            distance = _distance(self._position, point)
            self._accumulate_motion_time(distance, feedrate)
        self._emit_with_feed(
            self.printer.travel_move_command or "G0",
            self._format_xy(point),
            feedrate,
        )
        self._position = point

    def _linear_move(self, point: Point, feedrate: float) -> None:
        if self._position == point:
            return
        if self._position is not None:
            distance = _distance(self._position, point)
            self._accumulate_motion_time(distance, feedrate)
        self._emit_with_feed(
            self.printer.draw_move_command or "G1",
            self._format_xy(point),
            feedrate,
        )
        self._position = point

    def _set_z(self, z: float, feedrate: float) -> None:
        if abs(self._z_height - z) < 1e-6:
            return
        distance = abs(self._z_height - z)
        if self._feedrate != feedrate:
            self._emit(f"G1 Z{z:.3f} F{feedrate:.0f}")
            self._feedrate = feedrate
        else:
            self._emit(f"G1 Z{z:.3f}")
        self._z_height = z
        self._accumulate_motion_time(distance, feedrate)

    def _set_pen_state(self, drawing: bool, z: float, feedrate: float) -> None:
        command = self.printer.draw_command if drawing else self.printer.lift_command
        if command:
            if self._pen_is_down != drawing:
                self._emit(command)
            self._pen_is_down = drawing
            self._z_height = z
            return

        self._set_z(z, feedrate)
        self._pen_is_down = drawing

    def _accumulate_motion_time(self, distance: float, feedrate: float) -> None:
        if distance <= 0 or feedrate <= 0:
            return
        speed_mm_s = feedrate / 60.0
        if speed_mm_s <= 0:
            return
        self._elapsed_time += distance / speed_mm_s

    def emit_header(self) -> None:
        for line in self.printer.start_gcode:
            self._emit(line)

    def emit_footer(self) -> None:
        for line in self.printer.end_gcode:
            self._emit(line)

    def emit_comment(self, text: str) -> None:
        self._emit(f"; {text}")

    def emit_command(self, line: str) -> None:
        self._emit(line)

    def draw_single_toolpath(self, toolpath: Toolpath, feedrates: Feedrates) -> None:
        points = list(toolpath.points)
        if len(points) < 2:
            return
        draw_feed = feedrates.draw_feedrate
        travel_feed = feedrates.travel_feedrate
        z_feed = feedrates.z_feedrate
        draw_height = self.printer.z_draw
        travel_height = self.printer.z_raster_travel if toolpath.tag == "raster" else self.printer.z_travel

        start = points[0]
        self._set_pen_state(False, travel_height, z_feed)
        self._rapid_move(start, travel_feed)
        self._set_pen_state(True, draw_height, z_feed)
        for point in points[1:]:
            self._linear_move(point, draw_feed)
        self._set_pen_state(False, travel_height, z_feed)

    def draw_toolpaths(self, toolpaths: Iterable[Toolpath], feedrates: Feedrates) -> None:
        path_list = [toolpath for toolpath in toolpaths if len(toolpath.points) >= 2]
        if not path_list:
            return

        draw_feed = feedrates.draw_feedrate
        travel_feed = feedrates.travel_feedrate
        z_feed = feedrates.z_feedrate
        glide_threshold = max(0.0, float(self.printer.glide_threshold))

        for index, toolpath in enumerate(path_list):
            points = list(toolpath.points)
            draw_height = self.printer.z_draw
            travel_height = self.printer.z_raster_travel if toolpath.tag == "raster" else self.printer.z_travel
            start = points[0]

            if self._pen_is_down and self._position == start and abs(self._z_height - draw_height) < 1e-6:
                pass
            else:
                self._set_pen_state(False, travel_height, z_feed)
                self._rapid_move(start, travel_feed)
                self._set_pen_state(True, draw_height, z_feed)

            for point in points[1:]:
                self._linear_move(point, draw_feed)

            next_toolpath = path_list[index + 1] if index + 1 < len(path_list) else None
            if next_toolpath is None:
                self._set_pen_state(False, travel_height, z_feed)
                continue

            next_points = list(next_toolpath.points)
            next_start = next_points[0]
            gap = _distance(points[-1], next_start)
            if glide_threshold > 0 and gap <= glide_threshold:
                self._linear_move(next_start, draw_feed)
                continue

            self._set_pen_state(False, travel_height, z_feed)

    def generate(self) -> List[str]:
        return self._gcode

    @property
    def elapsed_time_seconds(self) -> float:
        return self._elapsed_time

    def formatted_elapsed_time(self) -> str:
        return _format_duration(self._elapsed_time)


def toolpaths_from_polylines(
    polylines: Iterable[Polyline],
    tag: str = "infill",
    source_color: tuple[int, int, int] | None = None,
    brightness: float | None = None,
) -> List[Toolpath]:
    return [
        Toolpath(points=tuple(polyline), tag=tag, source_color=source_color, brightness=brightness)
        for polyline in polylines
        if len(polyline) >= 2
    ]


def _format_duration(seconds: float) -> str:
    total_seconds = max(0.0, float(seconds))
    if total_seconds < 60.0:
        return f"{total_seconds:.1f}s"
    rounded = int(round(total_seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"
