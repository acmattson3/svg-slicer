from __future__ import annotations

import argparse
import logging
import math
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

import yaml

try:
    from PySide6.QtCore import QPointF, QRectF, Qt, Signal
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
    from PySide6.QtWidgets import (
        QApplication,
        QFileDialog,
        QFormLayout,
        QGraphicsItem,
        QGraphicsPathItem,
        QGraphicsScene,
        QGraphicsView,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QProgressDialog,
        QSizePolicy,
        QTabWidget,
        QVBoxLayout,
        QWidget,
        QComboBox,
        QDoubleSpinBox,
        QCheckBox,
        QStyle,
        QStyleOptionGraphicsItem,
    )
except ImportError as exc:  # pragma: no cover - GUI dependencies are optional for tests
    raise RuntimeError(
        "PySide6 is required to launch the SVG slicer GUI. Install it with `pip install PySide6`."
    ) from exc

from shapely.affinity import rotate as shapely_rotate, scale as shapely_scale, translate as shapely_translate
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon

from .cli import generate_toolpaths_for_shapes, write_toolpaths_to_gcode
from .config import (
    Feedrates,
    InfillConfig,
    PerimeterConfig,
    PrinterConfig,
    RenderingConfig,
    SamplingConfig,
    SlicerConfig,
    load_config,
)
from .gcode import Toolpath
from .svg_parser import ShapeGeometry, parse_svg


_LOGGER = logging.getLogger(__name__)


def _running_in_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = platform.uname().release.lower()
    except Exception:
        release = ""
    if "microsoft" in release or "wsl" in release:
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8") as version_file:
            return "microsoft" in version_file.read().lower()
    except Exception:
        return False


def _configure_qt_platform() -> None:
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if not os.environ.get("WAYLAND_DISPLAY"):
        return
    if _running_in_wsl():
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        _LOGGER.debug("Detected Wayland on WSL; switching Qt platform to 'xcb' to avoid protocol errors.")


@dataclass
class LoadedModel:
    path: Path
    original_shapes: List[ShapeGeometry]
    normalized_shapes: List[ShapeGeometry] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    scale: float = 1.0
    initial_scale: float = 1.0
    rotation_degrees: float = 0.0
    position: Tuple[float, float] = (0.0, 0.0)
    bounding_width: float = 0.0
    bounding_height: float = 0.0
    _origin_offset: Tuple[float, float] = (0.0, 0.0)
    item: Optional["ModelGraphicsItem"] = None

    def transformed_shapes(self, *, include_position: bool = True) -> List[ShapeGeometry]:
        geometries, _ = self._compute_transformed_shapes(include_position=include_position)
        return geometries

    def display_shapes(self) -> List[ShapeGeometry]:
        geometries, _ = self._compute_transformed_shapes(include_position=False)
        return geometries

    def footprint_dimensions(
        self,
        *,
        scale: Optional[float] = None,
        rotation: Optional[float] = None,
    ) -> Tuple[float, float]:
        _, bounds = self._scaled_rotated_geometries(
            scale=self._sanitize_scale(scale),
            rotation=self._sanitize_rotation(rotation),
            capture_shapes=False,
        )
        min_x, min_y, max_x, max_y = bounds
        return max(0.0, max_x - min_x), max(0.0, max_y - min_y)

    def _compute_transformed_shapes(
        self,
        *,
        include_position: bool,
        scale: Optional[float] = None,
        rotation: Optional[float] = None,
    ) -> Tuple[List[ShapeGeometry], Tuple[float, float, float, float]]:
        actual_scale = self._sanitize_scale(scale)
        actual_rotation = self._sanitize_rotation(rotation)

        staged, bounds = self._scaled_rotated_geometries(
            scale=actual_scale,
            rotation=actual_rotation,
            capture_shapes=True,
        )
        min_x, min_y, max_x, max_y = bounds

        offset_x = -min_x
        offset_y = -min_y
        translate_x = offset_x + (self.position[0] if include_position else 0.0)
        translate_y = offset_y + (self.position[1] if include_position else 0.0)

        transformed: List[ShapeGeometry] = []
        for geom, brightness, stroke_width, color in staged or []:
            if translate_x != 0.0 or translate_y != 0.0:
                geom = shapely_translate(geom, xoff=translate_x, yoff=translate_y)
            transformed.append(
                ShapeGeometry(
                    geometry=geom,
                    brightness=brightness,
                    stroke_width=stroke_width,
                    color=color,
                )
            )

        if scale is None and rotation is None:
            self.bounding_width = max(0.0, max_x - min_x)
            self.bounding_height = max(0.0, max_y - min_y)
            self._origin_offset = (offset_x, offset_y)

        return transformed, bounds

    def _scaled_rotated_geometries(
        self,
        *,
        scale: float,
        rotation: float,
        capture_shapes: bool,
    ) -> Tuple[
        Optional[List[Tuple[Any, float, Optional[float], Optional[tuple[int, int, int]]]]],
        Tuple[float, float, float, float],
    ]:
        staged: Optional[List[Tuple[Any, float, Optional[float], Optional[tuple[int, int, int]]]]] = (
            [] if capture_shapes else None
        )

        min_x = math.inf
        min_y = math.inf
        max_x = -math.inf
        max_y = -math.inf

        # Determine rotation origin in scaled coordinates if possible.
        origin_x = self.width * scale / 2.0 if self.width else 0.0
        origin_y = self.height * scale / 2.0 if self.height else 0.0
        origin = (origin_x, origin_y)
        rotate_needed = not math.isclose(rotation % 360.0, 0.0, abs_tol=1e-7)

        for shape in self.normalized_shapes:
            geom = shapely_scale(shape.geometry, xfact=scale, yfact=scale, origin=(0, 0))
            if rotate_needed:
                geom = shapely_rotate(geom, rotation, origin=origin, use_radians=False)
            if not geom.is_empty:
                bounds = geom.bounds
                min_x = min(min_x, bounds[0])
                min_y = min(min_y, bounds[1])
                max_x = max(max_x, bounds[2])
                max_y = max(max_y, bounds[3])
            if capture_shapes and staged is not None:
                staged.append(
                    (
                        geom,
                        shape.brightness,
                        None if shape.stroke_width is None else shape.stroke_width * scale,
                        shape.color,
                    )
                )

        if min_x is math.inf or min_y is math.inf or max_x is -math.inf or max_y is -math.inf:
            min_x = min_y = max_x = max_y = 0.0

        return staged, (min_x, min_y, max_x, max_y)

    def _sanitize_scale(self, scale: Optional[float]) -> float:
        value = self.scale if scale is None else scale
        return max(value, 1e-6)

    def _sanitize_rotation(self, rotation: Optional[float]) -> float:
        if rotation is None:
            return self.rotation_degrees
        return rotation


def _add_polygon_to_path(path: QPainterPath, polygon: Polygon) -> None:
    exterior = list(polygon.exterior.coords)
    if len(exterior) >= 2:
        path.moveTo(exterior[0][0], exterior[0][1])
        for x, y in exterior[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
    for interior in polygon.interiors:
        coords = list(interior.coords)
        if len(coords) >= 2:
            path.moveTo(coords[0][0], coords[0][1])
            for x, y in coords[1:]:
                path.lineTo(x, y)
            path.closeSubpath()


def _add_line_to_path(path: QPainterPath, line: LineString) -> None:
    coords = list(line.coords)
    if len(coords) < 2:
        return
    path.moveTo(coords[0][0], coords[0][1])
    for x, y in coords[1:]:
        path.lineTo(x, y)


def _geometry_to_path(path: QPainterPath, geometry) -> None:  # type: ignore[no-untyped-def]
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        _add_polygon_to_path(path, geometry)
    elif isinstance(geometry, MultiPolygon):
        for part in geometry.geoms:
            _geometry_to_path(path, part)
    elif isinstance(geometry, LineString):
        _add_line_to_path(path, geometry)
    elif isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            _geometry_to_path(path, part)
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            _geometry_to_path(path, part)


def _clamp(value: float, *, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(max(value, lower), upper)


def _brightness_to_gray(brightness: float | None) -> int:
    if brightness is None:
        return 150
    return int(round(_clamp(brightness) * 255))


def _shape_to_qcolor(shape: ShapeGeometry) -> QColor:
    if shape.color is not None:
        r, g, b = shape.color
        return QColor(int(r), int(g), int(b))
    gray = _brightness_to_gray(shape.brightness)
    return QColor(gray, gray, gray)


def _toolpath_to_qcolor(toolpath: Toolpath) -> QColor:
    if toolpath.assigned_color:
        qc = QColor(toolpath.assigned_color)
        if qc.isValid():
            return qc
    if toolpath.source_color is not None:
        r, g, b = toolpath.source_color
        return QColor(int(r), int(g), int(b))
    gray = _brightness_to_gray(toolpath.brightness)
    return QColor(gray, gray, gray)


def build_model_path(shapes: Sequence[ShapeGeometry]) -> QPainterPath:
    painter_path = QPainterPath()
    for shape in shapes:
        _geometry_to_path(painter_path, shape.geometry)
    return painter_path


class ModelGraphicsItem(QGraphicsPathItem):
    """Graphics item representing an SVG model positioned on the build plate."""

    def __init__(
        self,
        model: LoadedModel,
        bed_rect: QRectF,
        moved_callback: Callable[[LoadedModel], None] | None,
    ) -> None:
        super().__init__()
        self.model = model
        self._bed_rect = bed_rect
        self._moved_callback = moved_callback
        self._color_paths: List[tuple[QColor, QPainterPath]] = []

        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setZValue(1)

        self.setPen(Qt.NoPen)
        self.setBrush(Qt.NoBrush)

        self.refresh_path()
        self.setPos(model.position[0], model.position[1])

    def refresh_path(self) -> None:
        shapes = self.model.display_shapes()
        color_paths: dict[tuple[int, int, int, int], QPainterPath] = {}

        for shape in shapes:
            if shape.geometry.is_empty:
                continue
            shape_path = QPainterPath()
            _geometry_to_path(shape_path, shape.geometry)
            if shape_path.isEmpty():
                continue
            qcolor = _shape_to_qcolor(shape)
            key = (qcolor.red(), qcolor.green(), qcolor.blue(), qcolor.alpha())
            color_paths.setdefault(key, QPainterPath()).addPath(shape_path)

        self._color_paths = [(QColor(r, g, b, a), path) for (r, g, b, a), path in color_paths.items()]

        painter_path = build_model_path(shapes)
        self.setPath(painter_path)
        self.update()

    def set_bed_rect(self, bed_rect: QRectF) -> None:
        self._bed_rect = bed_rect

    def paint(self, painter, option: QStyleOptionGraphicsItem, widget=None) -> None:  # type: ignore[override]
        if not self._color_paths:
            super().paint(painter, option, widget)
            return

        painter.save()
        for color, path in self._color_paths:
            pen = QPen(color)
            pen.setWidthF(0)
            pen.setCosmetic(True)
            painter.setPen(pen)

            brush_color = QColor(color)
            brush_color.setAlpha(60)
            painter.setBrush(brush_color)
            painter.drawPath(path)

        if option.state & QStyle.State_Selected:
            highlight_pen = QPen(QColor("#ff9500"))
            highlight_pen.setWidthF(0)
            highlight_pen.setCosmetic(True)
            highlight_pen.setStyle(Qt.DashLine)
            painter.setPen(highlight_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self.path())

        painter.restore()

    def itemChange(self, change, value):  # type: ignore[override]
        if change == QGraphicsItem.ItemPositionChange and self._bed_rect is not None:
            if isinstance(value, QPointF):
                new_pos = value
            elif hasattr(value, "toPointF"):
                new_pos = value.toPointF()
            else:
                new_pos = QPointF(value)
            rect = self.boundingRect()
            min_x = self._bed_rect.left()
            min_y = self._bed_rect.top()
            max_x = self._bed_rect.right() - rect.width()
            max_y = self._bed_rect.bottom() - rect.height()
            if max_x < min_x:
                x = (self._bed_rect.left() + self._bed_rect.right() - rect.width()) / 2.0
            else:
                x = min(max(new_pos.x(), min_x), max_x)
            if max_y < min_y:
                y = (self._bed_rect.top() + self._bed_rect.bottom() - rect.height()) / 2.0
            else:
                y = min(max(new_pos.y(), min_y), max_y)
            return QPointF(x, y)
        if change == QGraphicsItem.ItemPositionHasChanged:
            pos = self.pos()
            self.model.position = (pos.x(), pos.y())
            if self._moved_callback:
                self._moved_callback(self.model)
        return super().itemChange(change, value)

class GuiLogHandler(logging.Handler):
    """Redirect logging records into the GUI text console."""

    def __init__(self, callback: Callable[[str], None]):
        super().__init__(level=logging.INFO)
        self._callback = callback
        self.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - defensive
            message = record.getMessage()
        self._callback(message)


class BuildPlateView(QGraphicsView):
    """Interactive view representing the printer bed with draggable SVG models."""

    svgDropped = Signal(list)
    arrangementChanged = Signal()
    modelSelected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setBackgroundBrush(QColor("#f0f0f0"))
        self.setDragMode(QGraphicsView.NoDrag)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self._bed_item: QGraphicsPathItem | None = None
        self._info_item = self._scene.addText("Load a configuration to begin")
        self._info_item.setDefaultTextColor(QColor("#777777"))
        self._info_item.setZValue(5)

        self._printer: PrinterConfig | None = None
        self._bed_rect = QRectF()
        self._toolpath_items: List[QGraphicsPathItem] = []
        self._model_items: List[ModelGraphicsItem] = []
        self._suppress_selection_signal = False

        self._scene.selectionChanged.connect(self._handle_selection_changed)

    def set_printer(self, printer: PrinterConfig) -> None:
        self._printer = printer
        self.clear_models()
        self.clear_toolpaths()
        self._scene.clear()

        rect = QRectF(
            printer.x_min,
            printer.y_min,
            printer.printable_width,
            printer.printable_depth,
        )
        self._bed_rect = rect
        self._scene.setSceneRect(rect)

        bed_pen = QPen(QColor("#4a4a4a"))
        bed_pen.setWidthF(0.6)
        self._bed_item = self._scene.addRect(rect, bed_pen, QColor("#ffffff"))
        self._bed_item.setZValue(-2)

        grid_pen = QPen(QColor("#d0d0d0"))
        grid_pen.setWidthF(0)
        grid_pen.setCosmetic(True)
        spacing_candidates = [printer.printable_width, printer.printable_depth]
        grid_spacing = max(value for value in spacing_candidates if value > 0) / 10.0 if any(
            value > 0 for value in spacing_candidates
        ) else 10.0
        x = printer.x_min
        while x <= printer.x_max:
            line = self._scene.addLine(x, printer.y_min, x, printer.y_max, grid_pen)
            line.setZValue(-3)
            x += grid_spacing
        y = printer.y_min
        while y <= printer.y_max:
            line = self._scene.addLine(printer.x_min, y, printer.x_max, y, grid_pen)
            line.setZValue(-3)
            y += grid_spacing

        self._info_item = self._scene.addText("Drop SVG files onto the build plate")
        self._info_item.setDefaultTextColor(QColor("#777777"))
        self._info_item.setZValue(5)
        self._update_info_position()
        self._update_info_visibility()
        self._refit()

    def reset_models(self, models: Sequence[LoadedModel]) -> None:
        self.clear_models()
        for model in models:
            self.add_model(model)
        self._update_info_visibility()

    def add_model(self, model: LoadedModel) -> None:
        if not self._bed_rect or self._bed_rect.isNull():
            return
        item = ModelGraphicsItem(model, self._bed_rect, self._on_model_moved)
        self._scene.addItem(item)
        self._model_items.append(item)
        model.item = item
        self._update_info_visibility()

    def update_model_item(self, model: LoadedModel) -> None:
        if model.item is None:
            return
        model.item.set_bed_rect(self._bed_rect)
        model.item.refresh_path()
        model.item.setPos(model.position[0], model.position[1])

    def remove_model(self, model: LoadedModel) -> None:
        if model.item and model.item in self._model_items:
            self._scene.removeItem(model.item)
            self._model_items.remove(model.item)
        model.item = None
        self._update_info_visibility()

    def clear_models(self) -> None:
        for item in self._model_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
            item.model.item = None
        self._model_items.clear()
        self._update_info_visibility()

    def update_toolpaths(self, toolpaths: Iterable[Toolpath]) -> None:
        self.clear_toolpaths()

        for toolpath in toolpaths:
            points = list(toolpath.points)
            if len(points) < 2:
                continue
            painter_path = QPainterPath()
            painter_path.moveTo(points[0][0], points[0][1])
            for x, y in points[1:]:
                painter_path.lineTo(x, y)
            color = _toolpath_to_qcolor(toolpath)
            pen = QPen(color)
            pen.setWidthF(0)
            pen.setCosmetic(True)
            item = self._scene.addPath(painter_path, pen)
            item.setZValue(4)
            self._toolpath_items.append(item)

        self._update_info_visibility()

    def clear_toolpaths(self) -> None:
        for item in self._toolpath_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self._toolpath_items.clear()
        self._update_info_visibility()

    def _accepts_mime(self, event) -> bool:
        mime = event.mimeData()
        if not mime.hasUrls():
            return False
        return any(url.isLocalFile() and url.toLocalFile().lower().endswith(".svg") for url in mime.urls())

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._accepts_mime(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._accepts_mime(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        if not self._accepts_mime(event):
            event.ignore()
            return
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".svg")
        ]
        if paths:
            self.svgDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refit()

    def _refit(self) -> None:
        if not self._bed_item:
            return
        rect = self._bed_item.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self.resetTransform()
        self.fitInView(rect, Qt.KeepAspectRatio)

    def _on_model_moved(self, model: LoadedModel) -> None:
        for item in self._model_items:
            item.set_bed_rect(self._bed_rect)
        self.clear_toolpaths()
        self.arrangementChanged.emit()
        self._update_info_visibility()

    def _update_info_position(self) -> None:
        if not self._info_item or not self._bed_item:
            return
        bed_rect = self._bed_item.rect()
        text_rect = self._info_item.boundingRect()
        x = bed_rect.x() + (bed_rect.width() - text_rect.width()) / 2.0
        y = bed_rect.y() + (bed_rect.height() - text_rect.height()) / 2.0
        self._info_item.setPos(x, y)

    def _update_info_visibility(self) -> None:
        if not self._info_item:
            return
        should_show = not self._model_items and not self._toolpath_items
        self._info_item.setVisible(should_show)

    def _handle_selection_changed(self) -> None:
        if self._suppress_selection_signal:
            return
        selected_items = [item for item in self._model_items if item.isSelected()]
        model = selected_items[0].model if selected_items else None
        self.modelSelected.emit(model)

    def select_model(self, model: Optional[LoadedModel]) -> None:
        self._suppress_selection_signal = True
        try:
            for item in self._model_items:
                item.setSelected(item.model is model)
            if model and model.item:
                self.centerOn(model.item)
        finally:
            self._suppress_selection_signal = False
        self.modelSelected.emit(model)

    @property
    def bed_rect(self) -> QRectF:
        return QRectF(self._bed_rect)

class PrepareTab(QWidget):
    """Tab that handles model preparation and slicing actions."""

    filesDropped = Signal(list)
    addFilesRequested = Signal()
    removeSelectionRequested = Signal()
    clearRequested = Signal()
    sliceRequested = Signal()
    browseOutputRequested = Signal()
    modelSelectionChanged = Signal(int)
    scaleApplyRequested = Signal(float)
    scaleResetRequested = Signal()
    rotationApplyRequested = Signal(float)
    rotationResetRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.build_plate = BuildPlateView()
        self.build_plate.svgDropped.connect(self.filesDropped)
        self._ignore_list_signal = False
        self._current_model: Optional[LoadedModel] = None

        self.printer_label = QLabel("Printer: —")
        self.scale_label = QLabel("Models: 0")
        self.status_label = QLabel("")

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.file_list.setMinimumWidth(220)

        self.add_button = QPushButton("Add SVGs…")
        self.remove_button = QPushButton("Remove Selected")
        self.clear_button = QPushButton("Clear All")
        self.slice_button = QPushButton("Slice")
        self.slice_button.setEnabled(False)

        self.output_path_edit = QLineEdit(str(Path("output.gcode")))
        self.output_path_edit.setPlaceholderText("output.gcode")
        self.output_browse_button = QPushButton("Browse…")

        controls = QVBoxLayout()
        controls.addWidget(self.printer_label)
        controls.addWidget(self.scale_label)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Queued SVGs:"))
        controls.addWidget(self.file_list)

        self.file_list.itemSelectionChanged.connect(self._on_list_selection_changed)

        controls.addSpacing(8)
        controls.addWidget(QLabel("Model scale:"))

        scale_row = QHBoxLayout()
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(1.0, 1000.0)
        self.scale_spin.setDecimals(1)
        self.scale_spin.setSuffix(" %")
        self.scale_spin.setSingleStep(5.0)
        self.scale_spin.setEnabled(False)
        scale_row.addWidget(self.scale_spin)

        self.scale_apply_button = QPushButton("Apply")
        self.scale_apply_button.setEnabled(False)
        scale_row.addWidget(self.scale_apply_button)

        self.scale_reset_button = QPushButton("Reset")
        self.scale_reset_button.setEnabled(False)
        scale_row.addWidget(self.scale_reset_button)

        controls.addLayout(scale_row)

        controls.addSpacing(8)
        controls.addWidget(QLabel("Model rotation:"))

        rotation_row = QHBoxLayout()
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(-180.0, 180.0)
        self.rotation_spin.setDecimals(1)
        self.rotation_spin.setSuffix(" °")
        self.rotation_spin.setSingleStep(5.0)
        self.rotation_spin.setEnabled(False)
        rotation_row.addWidget(self.rotation_spin)

        self.rotation_apply_button = QPushButton("Apply")
        self.rotation_apply_button.setEnabled(False)
        rotation_row.addWidget(self.rotation_apply_button)

        self.rotation_reset_button = QPushButton("Reset")
        self.rotation_reset_button.setEnabled(False)
        rotation_row.addWidget(self.rotation_reset_button)

        controls.addLayout(rotation_row)

        self.dimensions_label = QLabel("Footprint: —")
        controls.addWidget(self.dimensions_label)

        button_row = QHBoxLayout()
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)
        controls.addLayout(button_row)
        controls.addWidget(self.clear_button)
        controls.addSpacing(12)

        controls.addWidget(QLabel("Output G-code path:"))
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_path_edit)
        output_row.addWidget(self.output_browse_button)
        controls.addLayout(output_row)
        controls.addSpacing(12)

        controls.addWidget(self.slice_button)
        controls.addStretch(1)
        controls.addWidget(self.status_label)

        layout = QHBoxLayout(self)
        layout.addWidget(self.build_plate, stretch=3)
        layout.addLayout(controls, stretch=2)

        self.scale_spin.valueChanged.connect(self._on_scale_value_changed)
        self.rotation_spin.valueChanged.connect(self._on_rotation_value_changed)

        self.add_button.clicked.connect(self.addFilesRequested)
        self.remove_button.clicked.connect(self.removeSelectionRequested)
        self.clear_button.clicked.connect(self.clearRequested)
        self.slice_button.clicked.connect(self.sliceRequested)
        self.output_browse_button.clicked.connect(self.browseOutputRequested)
        self.filesDropped.connect(lambda _: self.status_label.setText(""))
        self.scale_apply_button.clicked.connect(self._emit_scale_apply)
        self.scale_reset_button.clicked.connect(lambda: self.scaleResetRequested.emit())
        self.rotation_apply_button.clicked.connect(self._emit_rotation_apply)
        self.rotation_reset_button.clicked.connect(lambda: self.rotationResetRequested.emit())

    def set_printer(self, printer: PrinterConfig) -> None:
        self.build_plate.set_printer(printer)
        self.printer_label.setText(
            f"Printer: {printer.name} ({printer.printable_width:.1f} × {printer.printable_depth:.1f} mm)"
        )

    def update_toolpaths(self, toolpaths: Iterable[Toolpath]) -> None:
        toolpaths_list = list(toolpaths)
        if toolpaths_list:
            self.build_plate.update_toolpaths(toolpaths_list)
            self.status_label.setText("")
        else:
            self.clear_toolpaths()

    def clear_toolpaths(self) -> None:
        self.build_plate.clear_toolpaths()

    def refresh_models(self, models: Sequence[LoadedModel], selected_index: Optional[int] = None) -> None:
        self.file_list.clear()
        for model in models:
            item = QListWidgetItem(model.path.name)
            item.setData(Qt.UserRole, str(model.path))
            item.setToolTip(str(model.path))
            self.file_list.addItem(item)
        self.scale_label.setText(f"Models: {len(models)}")

        self._ignore_list_signal = True
        try:
            if selected_index is not None and 0 <= selected_index < len(models):
                self.file_list.setCurrentRow(selected_index)
            else:
                self.file_list.clearSelection()
        finally:
            self._ignore_list_signal = False

        self.build_plate.reset_models(models)
        self.slice_button.setEnabled(bool(models))

    def selected_indices(self) -> List[int]:
        return [index.row() for index in self.file_list.selectedIndexes()]

    def set_selected_index(self, index: Optional[int]) -> None:
        self._ignore_list_signal = True
        try:
            if index is None or index < 0 or index >= self.file_list.count():
                self.file_list.clearSelection()
            else:
                self.file_list.setCurrentRow(index)
        finally:
            self._ignore_list_signal = False

    def update_scale_controls(self, model: Optional[LoadedModel], pending_scale: Optional[float] = None) -> None:
        self._current_model = model
        if not model:
            self.scale_spin.setEnabled(False)
            self.scale_apply_button.setEnabled(False)
            self.scale_reset_button.setEnabled(False)
            self.rotation_spin.setEnabled(False)
            self.rotation_apply_button.setEnabled(False)
            self.rotation_reset_button.setEnabled(False)
            self.dimensions_label.setText("Footprint: —")
            self.scale_spin.blockSignals(True)
            self.scale_spin.setValue(100.0)
            self.scale_spin.blockSignals(False)
            self.rotation_spin.blockSignals(True)
            self.rotation_spin.setValue(0.0)
            self.rotation_spin.blockSignals(False)
            return

        scale_value = pending_scale if pending_scale is not None else model.scale
        rotation_value = model.rotation_degrees

        self.scale_spin.blockSignals(True)
        self.scale_spin.setEnabled(True)
        self.scale_spin.setValue(scale_value * 100.0)
        self.scale_spin.blockSignals(False)

        self.scale_apply_button.setEnabled(True)
        self.scale_reset_button.setEnabled(True)

        self.rotation_spin.blockSignals(True)
        self.rotation_spin.setEnabled(True)
        self.rotation_spin.setValue(rotation_value)
        self.rotation_spin.blockSignals(False)

        self.rotation_apply_button.setEnabled(True)
        self.rotation_reset_button.setEnabled(True)

        width, height = model.footprint_dimensions(scale=scale_value, rotation=rotation_value)
        if width > 0 and height > 0:
            self.dimensions_label.setText(f"Footprint: {width:.1f} × {height:.1f} mm")
        else:
            self.dimensions_label.setText("Footprint: —")

    def _on_list_selection_changed(self) -> None:
        if self._ignore_list_signal:
            return
        indices = self.selected_indices()
        index = indices[0] if indices else -1
        self.modelSelectionChanged.emit(index)

    def _emit_scale_apply(self) -> None:
        if not self.scale_spin.isEnabled():
            return
        self.scaleApplyRequested.emit(self.scale_spin.value() / 100.0)

    def _on_scale_value_changed(self, value: float) -> None:
        if not self.scale_spin.isEnabled() or not self._current_model:
            return
        scale_value = value / 100.0
        rotation_value = self.rotation_spin.value()
        width, height = self._current_model.footprint_dimensions(scale=scale_value, rotation=rotation_value)
        if width > 0 and height > 0:
            self.dimensions_label.setText(f"Footprint: {width:.1f} × {height:.1f} mm")
        else:
            self.dimensions_label.setText("Footprint: —")

    def _emit_rotation_apply(self) -> None:
        if not self.rotation_spin.isEnabled():
            return
        self.rotationApplyRequested.emit(self.rotation_spin.value())

    def _on_rotation_value_changed(self, value: float) -> None:
        if not self.rotation_spin.isEnabled() or not self._current_model:
            return
        scale_value = self.scale_spin.value() / 100.0
        width, height = self._current_model.footprint_dimensions(scale=scale_value, rotation=value)
        if width > 0 and height > 0:
            self.dimensions_label.setText(f"Footprint: {width:.1f} × {height:.1f} mm")
        else:
            self.dimensions_label.setText("Footprint: —")

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def set_output_path(self, path: Path) -> None:
        self.output_path_edit.setText(str(path))

    def output_path(self) -> Path:
        text = self.output_path_edit.text().strip()
        if text:
            return Path(text)
        return Path("output.gcode")


class SettingsTab(QWidget):
    """Configuration editor inspired by traditional slicer settings panes."""

    configApplied = Signal(SlicerConfig)
    saveRequested = Signal(SlicerConfig)
    profileSelected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: SlicerConfig | None = None
        self._updating_fields = False
        self._updating_profile = False

        layout = QVBoxLayout(self)

        self.profile_combo = QComboBox()
        self.profile_combo.setVisible(False)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        layout.addWidget(self.profile_combo, alignment=Qt.AlignLeft)

        printer_group = QGroupBox("Printer")
        printer_form = QFormLayout()
        printer_group.setLayout(printer_form)

        self.printer_name_edit = QLineEdit()
        self._set_wide(self.printer_name_edit, minimum_width=240)
        printer_form.addRow("Name", self.printer_name_edit)

        self.bed_width_spin = self._make_spin(0.0, 2000.0, 1.0)
        self.bed_depth_spin = self._make_spin(0.0, 2000.0, 1.0)
        self._set_wide(self.bed_width_spin)
        self._set_wide(self.bed_depth_spin)
        printer_form.addRow("Bed width (mm)", self.bed_width_spin)
        printer_form.addRow("Bed depth (mm)", self.bed_depth_spin)

        self.x_min_spin = self._make_spin(-1000.0, 1000.0, 0.1)
        self.x_max_spin = self._make_spin(-1000.0, 2000.0, 0.1)
        self.y_min_spin = self._make_spin(-1000.0, 1000.0, 0.1)
        self.y_max_spin = self._make_spin(-1000.0, 2000.0, 0.1)
        self._set_wide(self.x_min_spin)
        self._set_wide(self.x_max_spin)
        self._set_wide(self.y_min_spin)
        self._set_wide(self.y_max_spin)
        printer_form.addRow("X min (mm)", self.x_min_spin)
        printer_form.addRow("X max (mm)", self.x_max_spin)
        printer_form.addRow("Y min (mm)", self.y_min_spin)
        printer_form.addRow("Y max (mm)", self.y_max_spin)

        self.z_draw_spin = self._make_spin(-50.0, 200.0, 0.1)
        self.z_travel_spin = self._make_spin(-50.0, 200.0, 0.1)
        self.z_lift_spin = self._make_spin(-50.0, 200.0, 0.1)
        self._set_wide(self.z_draw_spin)
        self._set_wide(self.z_travel_spin)
        self._set_wide(self.z_lift_spin)
        printer_form.addRow("Z draw (mm)", self.z_draw_spin)
        printer_form.addRow("Z travel (mm)", self.z_travel_spin)
        printer_form.addRow("Z lift (mm)", self.z_lift_spin)

        self.feedrate_draw_spin = self._make_spin(0.1, 2000.0, 0.1)
        self.feedrate_travel_spin = self._make_spin(0.1, 2000.0, 0.1)
        self.feedrate_z_spin = self._make_spin(0.1, 2000.0, 0.1)
        self._set_wide(self.feedrate_draw_spin)
        self._set_wide(self.feedrate_travel_spin)
        self._set_wide(self.feedrate_z_spin)
        printer_form.addRow("Feedrate draw (mm/s)", self.feedrate_draw_spin)
        printer_form.addRow("Feedrate travel (mm/s)", self.feedrate_travel_spin)
        printer_form.addRow("Feedrate Z (mm/s)", self.feedrate_z_spin)

        self.color_mode_checkbox = QCheckBox("Enable color mode")
        printer_form.addRow("Color mode", self.color_mode_checkbox)

        self.available_colors_edit = QLineEdit()
        self.available_colors_edit.setPlaceholderText("#000000, #FF0000, #00FF00")
        self._set_wide(self.available_colors_edit)
        printer_form.addRow("Available colors", self.available_colors_edit)

        self.start_gcode_edit = QPlainTextEdit()
        self.start_gcode_edit.setPlaceholderText("One G-code command per line")
        self.end_gcode_edit = QPlainTextEdit()
        self.end_gcode_edit.setPlaceholderText("One G-code command per line")
        self.pause_gcode_edit = QPlainTextEdit()
        self.pause_gcode_edit.setPlaceholderText("Commands executed between colour changes (e.g. manual pause script)")
        self._set_wide(self.start_gcode_edit, minimum_width=260, vertical_policy=QSizePolicy.Expanding)
        self._set_wide(self.end_gcode_edit, minimum_width=260, vertical_policy=QSizePolicy.Expanding)
        self._set_wide(self.pause_gcode_edit, minimum_width=260, vertical_policy=QSizePolicy.Expanding)
        self.start_gcode_edit.setMinimumHeight(90)
        self.end_gcode_edit.setMinimumHeight(90)
        self.pause_gcode_edit.setMinimumHeight(90)
        printer_form.addRow("Start G-code", self.start_gcode_edit)
        printer_form.addRow("End G-code", self.end_gcode_edit)
        printer_form.addRow("Pause G-code", self.pause_gcode_edit)

        layout.addWidget(printer_group)

        infill_group = QGroupBox("Infill")
        infill_form = QFormLayout()
        infill_group.setLayout(infill_form)

        self.infill_spacing_spin = self._make_spin(0.01, 50.0, 0.01, decimals=3)
        self.infill_min_density_spin = self._make_spin(0.0, 1.0, 0.01, decimals=3)
        self.infill_max_density_spin = self._make_spin(0.0, 1.0, 0.01, decimals=3)
        self.infill_angles_edit = QLineEdit()
        self.infill_angles_edit.setPlaceholderText("e.g. 0, 90")
        self._set_wide(self.infill_spacing_spin)
        self._set_wide(self.infill_min_density_spin)
        self._set_wide(self.infill_max_density_spin)
        self._set_wide(self.infill_angles_edit)

        infill_form.addRow("Base spacing (mm)", self.infill_spacing_spin)
        infill_form.addRow("Min density", self.infill_min_density_spin)
        infill_form.addRow("Max density", self.infill_max_density_spin)
        infill_form.addRow("Angles (deg)", self.infill_angles_edit)

        layout.addWidget(infill_group)

        perimeter_group = QGroupBox("Perimeter")
        perimeter_form = QFormLayout()
        perimeter_group.setLayout(perimeter_form)

        self.perimeter_thickness_spin = self._make_spin(0.0, 10.0, 0.01, decimals=3)
        self.perimeter_density_spin = self._make_spin(0.0, 5.0, 0.01, decimals=3)
        self.perimeter_min_fill_spin = self._make_spin(0.0, 10.0, 0.01, decimals=3)
        self._set_wide(self.perimeter_thickness_spin)
        self._set_wide(self.perimeter_density_spin)
        self._set_wide(self.perimeter_min_fill_spin)

        perimeter_form.addRow("Thickness (mm)", self.perimeter_thickness_spin)
        perimeter_form.addRow("Density", self.perimeter_density_spin)
        perimeter_form.addRow("Min fill width (mm)", self.perimeter_min_fill_spin)

        layout.addWidget(perimeter_group)

        sampling_group = QGroupBox("Sampling")
        sampling_form = QFormLayout()
        sampling_group.setLayout(sampling_form)

        self.segment_tolerance_spin = self._make_spin(0.001, 10.0, 0.01, decimals=4)
        self.outline_tolerance_spin = self._make_spin(0.001, 10.0, 0.01, decimals=4)
        self._set_wide(self.segment_tolerance_spin)
        self._set_wide(self.outline_tolerance_spin)

        sampling_form.addRow("Segment tolerance (mm)", self.segment_tolerance_spin)
        sampling_form.addRow("Outline simplify (mm)", self.outline_tolerance_spin)

        layout.addWidget(sampling_group)

        rendering_group = QGroupBox("Rendering")
        rendering_form = QFormLayout()
        rendering_group.setLayout(rendering_form)

        self.preview_line_width_spin = self._make_spin(0.01, 5.0, 0.01, decimals=3)
        self._set_wide(self.preview_line_width_spin)
        rendering_form.addRow("Preview line width (mm)", self.preview_line_width_spin)

        layout.addWidget(rendering_group)

        button_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply Settings")
        self.save_button = QPushButton("Save Config As…")
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.save_button)
        layout.addLayout(button_row)
        layout.addStretch(1)

        self.apply_button.clicked.connect(self._on_apply_clicked)
        self.save_button.clicked.connect(self._on_save_clicked)

    @staticmethod
    def _make_spin(minimum: float, maximum: float, step: float, *, decimals: int = 2) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        spin.setAlignment(Qt.AlignRight)
        return spin

    @staticmethod
    def _set_wide(widget, minimum_width: int = 180, vertical_policy: QSizePolicy.Policy = QSizePolicy.Fixed) -> None:
        widget.setMinimumWidth(minimum_width)
        widget.setSizePolicy(QSizePolicy.Expanding, vertical_policy)

    def set_config(self, config: SlicerConfig, profiles: List[str], current_profile: Optional[str]) -> None:
        self._config = config
        self._updating_fields = True

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if profiles:
            self.profile_combo.addItems(profiles)
            if current_profile and current_profile in profiles:
                index = profiles.index(current_profile)
                self.profile_combo.setCurrentIndex(index)
            else:
                self.profile_combo.setCurrentIndex(0)
            self.profile_combo.setVisible(True)
        else:
            self.profile_combo.setVisible(False)
        self.profile_combo.blockSignals(False)

        printer = config.printer
        self.printer_name_edit.setText(printer.name)
        self.bed_width_spin.setValue(printer.bed_width)
        self.bed_depth_spin.setValue(printer.bed_depth)
        self.x_min_spin.setValue(printer.x_min)
        self.x_max_spin.setValue(printer.x_max)
        self.y_min_spin.setValue(printer.y_min)
        self.y_max_spin.setValue(printer.y_max)
        self.z_draw_spin.setValue(printer.z_draw)
        self.z_travel_spin.setValue(printer.z_travel)
        self.z_lift_spin.setValue(printer.z_lift)
        self.feedrate_draw_spin.setValue(printer.feedrates.draw_mm_s)
        self.feedrate_travel_spin.setValue(printer.feedrates.travel_mm_s)
        self.feedrate_z_spin.setValue(printer.feedrates.z_mm_s)
        self.color_mode_checkbox.setChecked(printer.color_mode)
        self.available_colors_edit.setText(", ".join(printer.available_colors))
        self.start_gcode_edit.setPlainText("\n".join(printer.start_gcode))
        self.end_gcode_edit.setPlainText("\n".join(printer.end_gcode))
        self.pause_gcode_edit.setPlainText("\n".join(printer.pause_gcode))

        infill = config.infill
        self.infill_spacing_spin.setValue(infill.base_spacing)
        self.infill_min_density_spin.setValue(infill.min_density)
        self.infill_max_density_spin.setValue(infill.max_density)
        self.infill_angles_edit.setText(", ".join(f"{angle:g}" for angle in infill.angles))

        perimeter = config.perimeter
        self.perimeter_thickness_spin.setValue(perimeter.thickness)
        self.perimeter_density_spin.setValue(perimeter.density)
        self.perimeter_min_fill_spin.setValue(perimeter.min_fill_width)

        sampling = config.sampling
        self.segment_tolerance_spin.setValue(sampling.segment_tolerance)
        self.outline_tolerance_spin.setValue(sampling.outline_simplify_tolerance)

        rendering = config.rendering
        self.preview_line_width_spin.setValue(rendering.line_width)

        self._updating_fields = False

    def _assemble_config(self) -> SlicerConfig:
        if not self._config:
            raise RuntimeError("Configuration has not been initialised.")

        color_mode = self.color_mode_checkbox.isChecked()
        palette = self._parse_palette(self.available_colors_edit.text())
        if color_mode and not palette:
            raise ValueError("Color mode requires at least one available color.")

        pause_lines = [line for line in self.pause_gcode_edit.toPlainText().splitlines() if line.strip()]
        if not pause_lines:
            pause_lines = ["M600"]

        printer = PrinterConfig(
            name=self.printer_name_edit.text().strip() or self._config.printer.name,
            bed_width=self.bed_width_spin.value(),
            bed_depth=self.bed_depth_spin.value(),
            x_min=self.x_min_spin.value(),
            x_max=self.x_max_spin.value(),
            y_min=self.y_min_spin.value(),
            y_max=self.y_max_spin.value(),
            z_draw=self.z_draw_spin.value(),
            z_travel=self.z_travel_spin.value(),
            z_lift=self.z_lift_spin.value(),
            feedrates=Feedrates(
                draw_mm_s=self.feedrate_draw_spin.value(),
                travel_mm_s=self.feedrate_travel_spin.value(),
                z_mm_s=self.feedrate_z_spin.value(),
            ),
            start_gcode=[line for line in self.start_gcode_edit.toPlainText().splitlines() if line.strip()],
            end_gcode=[line for line in self.end_gcode_edit.toPlainText().splitlines() if line.strip()],
            color_mode=color_mode,
            available_colors=palette,
            pause_gcode=pause_lines,
        )

        try:
            angles = self._parse_angles(self.infill_angles_edit.text())
        except ValueError as exc:
            raise ValueError(f"Invalid infill angles: {exc}") from exc

        infill = InfillConfig(
            base_spacing=self.infill_spacing_spin.value(),
            min_density=self.infill_min_density_spin.value(),
            max_density=self.infill_max_density_spin.value(),
            angles=angles,
        )

        perimeter = PerimeterConfig(
            thickness=self.perimeter_thickness_spin.value(),
            density=self.perimeter_density_spin.value(),
            min_fill_width=self.perimeter_min_fill_spin.value(),
        )

        sampling = SamplingConfig(
            segment_tolerance=self.segment_tolerance_spin.value(),
            outline_simplify_tolerance=self.outline_tolerance_spin.value(),
        )

        rendering = RenderingConfig(
            line_width=self.preview_line_width_spin.value(),
        )

        return SlicerConfig(
            printer=printer,
            infill=infill,
            perimeter=perimeter,
            sampling=sampling,
            rendering=rendering,
        )

    @staticmethod
    def _parse_angles(text: str) -> List[float]:
        stripped = text.strip()
        if not stripped:
            return [0.0]
        angles: List[float] = []
        for chunk in stripped.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            angles.append(float(chunk))
        if not angles:
            angles.append(0.0)
        return angles

    @staticmethod
    def _parse_palette(text: str) -> List[str]:
        stripped = text.replace("\n", ",")
        entries: List[str] = []
        for chunk in stripped.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            value = chunk[1:] if chunk.startswith("#") else chunk
            if len(value) != 6 or any(c not in "0123456789abcdefABCDEF" for c in value):
                raise ValueError(f"Invalid hex color '{chunk}'. Expected format like #RRGGBB.")
            entries.append(f"#{value.upper()}")
        return entries

    def _on_apply_clicked(self) -> None:
        try:
            config = self._assemble_config()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid Settings", f"Could not apply settings:\n{exc}")
            return
        self._config = config
        self.configApplied.emit(config)

    def _on_save_clicked(self) -> None:
        try:
            config = self._assemble_config()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid Settings", f"Cannot save configuration:\n{exc}")
            return
        self._config = config
        self.saveRequested.emit(config)

    def _on_profile_changed(self, index: int) -> None:
        if self._updating_fields or not self.profile_combo.isVisible():
            return
        profile = self.profile_combo.currentText()
        if profile:
            self.profileSelected.emit(profile)


class MainWindow(QMainWindow):
    """Main GUI window that orchestrates slicing and configuration."""

    def __init__(self, config_path: Path, profile: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("SVG Slicer")
        self.resize(1200, 720)

        self.config_path = config_path
        self.config_profile = profile
        self.config: SlicerConfig | None = None
        self.available_profiles: List[str] = []
        self.models: List[LoadedModel] = []
        self._current_toolpaths: List[Toolpath] = []
        self._selected_model: Optional[LoadedModel] = None
        self._selection_guard = False

        self._central_widget = QWidget()
        self.setCentralWidget(self._central_widget)
        self._layout = QVBoxLayout(self._central_widget)

        self.tabs = QTabWidget()
        self.prepare_tab = PrepareTab()
        self.settings_tab = SettingsTab()
        self.tabs.addTab(self.prepare_tab, "Prepare")
        self.tabs.addTab(self.settings_tab, "Settings")
        self._layout.addWidget(self.tabs, stretch=1)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Log output will appear here.")
        self.log_output.setMaximumBlockCount(5000)
        self.log_output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._layout.addWidget(self.log_output, stretch=0)

        self.statusBar().showMessage("Ready")

        self._log_handler = GuiLogHandler(self._append_log)
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.INFO)

        self._create_menus()
        self._wire_events()

        self._load_config_from_file(self.config_path, self.config_profile, show_errors=True)

    def _create_menus(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        load_config_action = file_menu.addAction("Load Config…")
        load_config_action.triggered.connect(self._prompt_load_config)

        save_config_action = file_menu.addAction("Save Config As…")
        save_config_action.triggered.connect(lambda: self.settings_tab._on_save_clicked())

        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

    def _wire_events(self) -> None:
        self.prepare_tab.filesDropped.connect(self._handle_files_added)
        self.prepare_tab.addFilesRequested.connect(self._prompt_add_files)
        self.prepare_tab.removeSelectionRequested.connect(self._remove_selected_models)
        self.prepare_tab.clearRequested.connect(self._clear_models)
        self.prepare_tab.sliceRequested.connect(self._perform_slice)
        self.prepare_tab.browseOutputRequested.connect(self._prompt_output_path)
        self.prepare_tab.build_plate.arrangementChanged.connect(self._on_arrangement_changed)
        self.prepare_tab.modelSelectionChanged.connect(self._on_model_selection_from_list)
        self.prepare_tab.scaleApplyRequested.connect(self._apply_scale_from_ui)
        self.prepare_tab.scaleResetRequested.connect(self._reset_selected_model_scale)
        self.prepare_tab.rotationApplyRequested.connect(self._apply_rotation_from_ui)
        self.prepare_tab.rotationResetRequested.connect(self._reset_selected_model_rotation)
        self.prepare_tab.build_plate.modelSelected.connect(lambda model: self._set_selected_model(model, origin="plate"))

        self.settings_tab.configApplied.connect(self._apply_new_config)
        self.settings_tab.saveRequested.connect(self._save_config_as)
        self.settings_tab.profileSelected.connect(self._switch_profile)

    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    @staticmethod
    def _normalize_shapes(shapes: Sequence[ShapeGeometry]) -> Tuple[List[ShapeGeometry], float, float]:
        if not shapes:
            return [], 0.0, 0.0
        bounds = [shape.geometry.bounds for shape in shapes if not shape.geometry.is_empty]
        if not bounds:
            normalized = [
                ShapeGeometry(
                    geometry=shape.geometry,
                    brightness=shape.brightness,
                    stroke_width=shape.stroke_width,
                    color=shape.color,
                )
                for shape in shapes
            ]
            return normalized, 0.0, 0.0

        min_x = min(bound[0] for bound in bounds)
        min_y = min(bound[1] for bound in bounds)
        max_x = max(bound[2] for bound in bounds)
        max_y = max(bound[3] for bound in bounds)
        width = max(0.0, max_x - min_x)
        height = max(0.0, max_y - min_y)

        normalized_shapes: List[ShapeGeometry] = []
        for shape in shapes:
            geom = shape.geometry
            if not geom.is_empty:
                geom = shapely_translate(geom, xoff=-min_x, yoff=-min_y)
            normalized_shapes.append(
                ShapeGeometry(
                    geometry=geom,
                    brightness=shape.brightness,
                    stroke_width=shape.stroke_width,
                    color=shape.color,
                )
            )
        return normalized_shapes, width, height

    def _configure_model_for_printer(
        self,
        model: LoadedModel,
        *,
        preserve_position: bool = False,
        index: int = 0,
    ) -> None:
        if not self.config:
            return

        normalized, width, height = self._normalize_shapes(model.original_shapes)
        model.normalized_shapes = normalized
        model.width = width
        model.height = height

        printer = self.config.printer
        base_width, base_height = model.footprint_dimensions(scale=1.0, rotation=model.rotation_degrees)
        scale_candidates: List[float] = []
        if base_width > 0 and printer.printable_width > 0:
            scale_candidates.append(printer.printable_width / base_width)
        if base_height > 0 and printer.printable_depth > 0:
            scale_candidates.append(printer.printable_depth / base_height)
        max_scale = max(min(scale_candidates) * 0.98, 1e-6) if scale_candidates else 1.0
        max_scale = max(max_scale, 1e-6)

        if preserve_position:
            model.display_shapes()
            model.initial_scale = max_scale
            current_scale = model.scale if model.scale > 0 else max_scale
            new_scale = min(max(current_scale, 1e-6), max_scale if scale_candidates else current_scale)

            previous_width = model.bounding_width if model.bounding_width > 0 else model.width * current_scale
            previous_height = model.bounding_height if model.bounding_height > 0 else model.height * current_scale
            centre_x = model.position[0] + (previous_width / 2.0 if previous_width > 0 else 0.0)
            centre_y = model.position[1] + (previous_height / 2.0 if previous_height > 0 else 0.0)

            model.scale = new_scale
            model.display_shapes()
            scaled_width = model.bounding_width if model.bounding_width > 0 else model.width * model.scale
            scaled_height = model.bounding_height if model.bounding_height > 0 else model.height * model.scale

            if scaled_width > 0 and scaled_height > 0:
                x = centre_x - scaled_width / 2.0
                y = centre_y - scaled_height / 2.0
            else:
                x, y = model.position
        else:
            model.scale = max_scale
            model.initial_scale = max_scale
            model.display_shapes()
            scaled_width = model.bounding_width if model.bounding_width > 0 else model.width * model.scale
            scaled_height = model.bounding_height if model.bounding_height > 0 else model.height * model.scale

            x = printer.x_min + max(0.0, (printer.printable_width - scaled_width) / 2.0)
            y = printer.y_min + max(0.0, (printer.printable_depth - scaled_height) / 2.0)
            if index:
                stagger = max(min(printer.printable_width, printer.printable_depth) * 0.05, 5.0)
                x += index * stagger
                y += index * stagger

        model.position = (x, y)
        self._constrain_model_within_bed(model)

    def _reconfigure_all_models(self, preserve_positions: bool = True) -> None:
        for idx, model in enumerate(self.models):
            self._configure_model_for_printer(model, preserve_position=preserve_positions, index=idx)
            if model.item:
                self.prepare_tab.build_plate.update_model_item(model)

    def _constrain_model_within_bed(self, model: LoadedModel) -> None:
        if not self.config:
            return
        printer = self.config.printer
        # Refresh bounds so we clamp using the current rotated footprint.
        model.display_shapes()
        width = model.bounding_width if model.bounding_width > 0 else model.width * model.scale
        height = model.bounding_height if model.bounding_height > 0 else model.height * model.scale
        x, y = model.position

        limit_x_min = printer.x_min
        limit_x_max = printer.x_max - width if width > 0 else printer.x_max
        if limit_x_max < limit_x_min:
            x = printer.x_min
        else:
            x = min(max(x, limit_x_min), limit_x_max)

        limit_y_min = printer.y_min
        limit_y_max = printer.y_max - height if height > 0 else printer.y_max
        if limit_y_max < limit_y_min:
            y = printer.y_min
        else:
            y = min(max(y, limit_y_min), limit_y_max)

        model.position = (x, y)

    def _rebuild_model_views(self) -> None:
        selected_index = self._selected_model_index()
        self.prepare_tab.refresh_models(self.models, selected_index=selected_index)
        self._update_selection_ui(origin="refresh")

    def _selected_model_index(self) -> Optional[int]:
        if self._selected_model and self._selected_model in self.models:
            return self.models.index(self._selected_model)
        return None

    def _set_selected_model(self, model: Optional[LoadedModel], *, origin: str) -> None:
        if model and model not in self.models:
            model = None
        self._selected_model = model
        self._update_selection_ui(origin=origin)

    def _update_selection_ui(self, origin: str, pending_scale: Optional[float] = None) -> None:
        if self._selection_guard:
            return
        self._selection_guard = True
        try:
            index = self._selected_model_index()
            if origin != "list":
                self.prepare_tab.set_selected_index(index)
            if origin != "plate":
                self.prepare_tab.build_plate.select_model(self._selected_model)
            self.prepare_tab.update_scale_controls(self._selected_model, pending_scale=pending_scale)
        finally:
            self._selection_guard = False

    def _on_model_selection_from_list(self, index: int) -> None:
        if 0 <= index < len(self.models):
            self._set_selected_model(self.models[index], origin="list")
        else:
            self._set_selected_model(None, origin="list")

    def _apply_scale_from_ui(self, scale: float) -> None:
        if not self._selected_model:
            return
        self._apply_scale_to_model(self._selected_model, scale)

    def _reset_selected_model_scale(self) -> None:
        if not self._selected_model:
            return
        self._apply_scale_to_model(self._selected_model, self._selected_model.initial_scale)

    def _apply_rotation_from_ui(self, rotation: float) -> None:
        if not self._selected_model:
            return
        self._apply_rotation_to_model(self._selected_model, rotation)

    def _reset_selected_model_rotation(self) -> None:
        if not self._selected_model:
            return
        self._apply_rotation_to_model(self._selected_model, 0.0)

    def _apply_rotation_to_model(self, model: LoadedModel, rotation: float) -> None:
        if not self.config:
            return

        model.display_shapes()
        previous_width = model.bounding_width if model.bounding_width > 0 else model.width * model.scale
        previous_height = model.bounding_height if model.bounding_height > 0 else model.height * model.scale
        centre_x = model.position[0] + (previous_width / 2.0 if previous_width > 0 else 0.0)
        centre_y = model.position[1] + (previous_height / 2.0 if previous_height > 0 else 0.0)

        model.rotation_degrees = rotation
        model.display_shapes()
        width_after = model.bounding_width if model.bounding_width > 0 else model.width * model.scale
        height_after = model.bounding_height if model.bounding_height > 0 else model.height * model.scale
        if width_after > 0 and height_after > 0:
            new_x = centre_x - width_after / 2.0
            new_y = centre_y - height_after / 2.0
        else:
            new_x, new_y = model.position
        model.position = (new_x, new_y)
        self._constrain_model_within_bed(model)

        if model.item:
            self.prepare_tab.build_plate.update_model_item(model)

        self._invalidate_toolpaths("Rotation updated; slice to regenerate G-code.")
        self._append_log(f"[INFO] Updated rotation for {model.path.name} to {model.rotation_degrees:.1f}°")
        self._update_selection_ui(origin="rotation")

    def _apply_scale_to_model(self, model: LoadedModel, scale: float) -> None:
        if not self.config:
            return
        requested_scale = max(scale, 1e-6)
        printer = self.config.printer
        base_width, base_height = model.footprint_dimensions(scale=1.0, rotation=model.rotation_degrees)
        scale_candidates: List[float] = []
        if base_width > 0 and printer.printable_width > 0:
            scale_candidates.append(printer.printable_width / base_width)
        if base_height > 0 and printer.printable_depth > 0:
            scale_candidates.append(printer.printable_depth / base_height)
        max_scale = max(min(scale_candidates) * 0.98, 1e-6) if scale_candidates else requested_scale
        max_scale = max(max_scale, 1e-6)
        effective_scale = min(requested_scale, max_scale) if scale_candidates else requested_scale

        model.display_shapes()
        current_scale = model.scale if model.scale > 0 else model.initial_scale
        previous_width = model.bounding_width if model.bounding_width > 0 else model.width * current_scale
        previous_height = model.bounding_height if model.bounding_height > 0 else model.height * current_scale
        centre_x = model.position[0] + (previous_width / 2.0 if previous_width > 0 else 0.0)
        centre_y = model.position[1] + (previous_height / 2.0 if previous_height > 0 else 0.0)

        model.scale = effective_scale
        model.display_shapes()
        width_after = model.bounding_width if model.bounding_width > 0 else model.width * model.scale
        height_after = model.bounding_height if model.bounding_height > 0 else model.height * model.scale
        if width_after > 0 and height_after > 0:
            new_x = centre_x - width_after / 2.0
            new_y = centre_y - height_after / 2.0
        else:
            new_x, new_y = model.position
        model.position = (new_x, new_y)
        self._constrain_model_within_bed(model)

        if model.item:
            self.prepare_tab.build_plate.update_model_item(model)

        self._invalidate_toolpaths("Scale updated; slice to regenerate G-code.")
        self._append_log(f"[INFO] Updated scale for {model.path.name} to {model.scale * 100:.1f}%")
        self._update_selection_ui(origin="scale")

    def _invalidate_toolpaths(self, message: Optional[str] = None) -> None:
        self._current_toolpaths = []
        self.prepare_tab.clear_toolpaths()
        if message:
            self.prepare_tab.set_status(message)

    def _on_arrangement_changed(self) -> None:
        if not self.models:
            return
        self._invalidate_toolpaths("Placement updated; slice to generate G-code.")

    def _prompt_load_config(self) -> None:
        start_dir = str(self.config_path.parent if self.config_path else Path.cwd())
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select Configuration File",
            start_dir,
            "YAML Files (*.yaml *.yml);;All Files (*)",
        )
        if not filename:
            return
        self._load_config_from_file(Path(filename), None, show_errors=True)

    def _prompt_add_files(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "Select SVG files",
            str(Path.cwd()),
            "SVG Files (*.svg);;All Files (*)",
        )
        if filenames:
            self._handle_files_added(filenames)

    def _prompt_output_path(self) -> None:
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Select output G-code file",
            str(self.prepare_tab.output_path()),
            "G-code Files (*.gcode);;All Files (*)",
        )
        if output_path:
            self.prepare_tab.set_output_path(Path(output_path))

    def _handle_files_added(self, filenames: Iterable[str]) -> None:
        if not self.config:
            QMessageBox.warning(self, "Configuration Missing", "Load a configuration before adding SVG files.")
            return
        added = 0
        selected_model: Optional[LoadedModel] = None
        for name in filenames:
            path = Path(name)
            if not path.exists():
                self._append_log(f"[WARNING] File not found: {path}")
                continue
            try:
                shapes = parse_svg(str(path), self.config.sampling)
            except Exception as exc:
                self._append_log(f"[ERROR] Failed to parse {path.name}: {exc}")
                continue
            model = LoadedModel(path=path, original_shapes=shapes)
            index = len(self.models)
            self.models.append(model)
            self._configure_model_for_printer(model, preserve_position=False, index=index)
            self._append_log(f"[INFO] Added {path.name}")
            added += 1
            selected_model = model
            if len(self.models) == 1:
                suggested = path.with_suffix(".gcode")
                self.prepare_tab.set_output_path(suggested)
        if added:
            if selected_model:
                self._selected_model = selected_model
            self._rebuild_model_views()
            self._invalidate_toolpaths("Placement updated; slice to generate G-code.")
        else:
            self.prepare_tab.set_status("No new SVGs were added.")

    def _remove_selected_models(self) -> None:
        indices = sorted(self.prepare_tab.selected_indices(), reverse=True)
        if not indices:
            return
        removed_any = False
        for index in indices:
            if 0 <= index < len(self.models):
                removed = self.models.pop(index)
                self._append_log(f"[INFO] Removed {removed.path.name}")
                removed_any = True
        if removed_any:
            if self.models:
                next_index = min(indices[-1], len(self.models) - 1)
                self._selected_model = self.models[next_index]
            else:
                self._selected_model = None
            self._rebuild_model_views()
            if self.models:
                self._invalidate_toolpaths("Placement updated; slice to generate G-code.")
            else:
                self._invalidate_toolpaths("Queue is empty.")
        else:
            self.prepare_tab.set_status("No models were removed.")

    def _clear_models(self) -> None:
        if not self.models:
            return
        self.models.clear()
        self._selected_model = None
        self._rebuild_model_views()
        self._invalidate_toolpaths("Queue cleared.")
        self._append_log("[INFO] Cleared SVG queue.")

    def _perform_slice(self) -> None:
        if not self.config:
            QMessageBox.warning(self, "Missing Configuration", "Load a configuration before slicing.")
            return
        if not self.models:
            QMessageBox.warning(self, "Nothing to Slice", "Add SVG files to the build plate first.")
            return

        shapes: List[ShapeGeometry] = []
        for model in self.models:
            shapes.extend(model.transformed_shapes())
        if not shapes:
            QMessageBox.warning(self, "No Geometry", "The queued SVGs do not contain drawable geometry.")
            return

        progress = QProgressDialog("Generating toolpaths…", None, 0, 0, self)
        progress.setWindowTitle("Slicing")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.show()
        QApplication.processEvents()

        try:
            toolpaths, _ = generate_toolpaths_for_shapes(shapes, self.config, fit_to_bed=False)
            if not toolpaths:
                raise RuntimeError("No toolpaths were generated from the current placement.")

            output_path = self.prepare_tab.output_path()
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            result = write_toolpaths_to_gcode(toolpaths, output_path, self.config)
        except Exception as exc:
            progress.close()
            QMessageBox.critical(self, "Slice Failed", f"Could not generate G-code:\n{exc}")
            self._append_log(f"[ERROR] Slice failed: {exc}")
            self.prepare_tab.set_status(f"Slicing failed: {exc}")
            return
        finally:
            progress.close()

        self._current_toolpaths = toolpaths
        self.prepare_tab.update_toolpaths(toolpaths)

        status_message = f"G-code saved to {output_path}"
        if result.color_order:
            colors_formatted = " -> ".join(result.color_order)
            status_message += f" | Color order: {colors_formatted}"
            self._append_log(f"[INFO] Color order: {colors_formatted}")

        self.prepare_tab.set_status(status_message)
        self.statusBar().showMessage(status_message, 5000)
        self._append_log(f"[INFO] Wrote {result.line_count} G-code lines to {output_path}.")

    def _apply_new_config(self, config: SlicerConfig) -> None:
        self.config = config
        self.prepare_tab.set_printer(config.printer)
        self._reconfigure_all_models(preserve_positions=True)
        self._rebuild_model_views()
        message = f"Updated configuration for printer '{config.printer.name}'."
        self._append_log(f"[INFO] {message}")
        self._invalidate_toolpaths("Configuration updated; slice to regenerate G-code.")

    def _save_config_as(self, config: SlicerConfig) -> None:
        start_dir = str(self.config_path.parent if self.config_path else Path.cwd())
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Configuration File",
            start_dir,
            "YAML Files (*.yaml *.yml);;All Files (*)",
        )
        if not filename:
            return
        path = Path(filename)
        try:
            data = self._config_to_yaml(config)
            with path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, sort_keys=False)
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save configuration:\n{exc}")
            self._append_log(f"[ERROR] Failed to save configuration: {exc}")
            return
        self._append_log(f"[INFO] Saved configuration to {path}.")
        self.statusBar().showMessage(f"Configuration saved to {path}", 5000)

    def _switch_profile(self, profile: str) -> None:
        if not self.config_path:
            return
        self._load_config_from_file(self.config_path, profile, show_errors=True)

    def _load_config_from_file(self, path: Path, profile: Optional[str], *, show_errors: bool) -> None:
        try:
            config = load_config(path, profile=profile)
        except Exception as exc:
            if show_errors:
                QMessageBox.critical(self, "Configuration Error", f"Failed to load configuration:\n{exc}")
            self._append_log(f"[ERROR] Failed to load configuration: {exc}")
            return

        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except Exception:
            raw = {}

        profiles: List[str] = []
        printers_section = raw.get("printers")
        if isinstance(printers_section, dict):
            profiles = list(printers_section.keys())

        self.config_path = path
        self.config_profile = profile
        self.config = config
        self.available_profiles = profiles

        current_profile = profile if profile else (profiles[0] if profiles else None)

        self.settings_tab.set_config(config, profiles, current_profile)
        self.prepare_tab.set_printer(config.printer)
        self._reconfigure_all_models(preserve_positions=True)
        self._rebuild_model_views()
        self._append_log(
            f"[INFO] Loaded configuration from {path}" + (f" (profile '{profile}')" if profile else "")
        )
        self.statusBar().showMessage(f"Using configuration: {path}", 5000)
        if self.models:
            self._invalidate_toolpaths("Configuration loaded; slice to regenerate G-code.")
        else:
            self.prepare_tab.set_status("")

    @staticmethod
    def _config_to_yaml(config: SlicerConfig) -> dict:
        printer = {
            "name": config.printer.name,
            "bed_size_mm": {"width": config.printer.bed_width, "depth": config.printer.bed_depth},
            "origin_offsets_mm": {
                "x_min": config.printer.x_min,
                "x_max": config.printer.x_max,
                "y_min": config.printer.y_min,
                "y_max": config.printer.y_max,
            },
            "z_heights_mm": {
                "draw": config.printer.z_draw,
                "travel": config.printer.z_travel,
            },
            "z_lift_height_mm": config.printer.z_lift,
            "feedrates_mm_s": {
                "draw": config.printer.feedrates.draw_mm_s,
                "travel": config.printer.feedrates.travel_mm_s,
                "z": config.printer.feedrates.z_mm_s,
            },
            "color_mode": config.printer.color_mode,
            "available_colors": list(config.printer.available_colors),
            "pause_gcode": list(config.printer.pause_gcode),
        }
        if config.printer.start_gcode:
            printer["start_gcode"] = list(config.printer.start_gcode)
        if config.printer.end_gcode:
            printer["end_gcode"] = list(config.printer.end_gcode)

        return {
            "printer": printer,
            "infill": {
                "base_line_spacing_mm": config.infill.base_spacing,
                "min_density": config.infill.min_density,
                "max_density": config.infill.max_density,
                "angles_degrees": list(config.infill.angles),
            },
            "perimeter": {
                "thickness_mm": config.perimeter.thickness,
                "density": config.perimeter.density,
                "min_fill_width_mm": config.perimeter.min_fill_width,
            },
            "sampling": {
                "segment_length_tolerance_mm": config.sampling.segment_tolerance,
                "outline_simplify_tolerance_mm": config.sampling.outline_simplify_tolerance,
            },
            "rendering": {
                "preview_line_width_mm": config.rendering.line_width,
            },
        }

    def closeEvent(self, event) -> None:  # type: ignore[override]
        logging.getLogger().removeHandler(self._log_handler)
        super().closeEvent(event)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SVG Slicer GUI")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Configuration YAML file to load.")
    parser.add_argument(
        "--printer-profile",
        type=str,
        default=None,
        help="Optional printer profile defined in the configuration file.",
    )
    args, qt_args = parser.parse_known_args(argv)

    _configure_qt_platform()

    app = QApplication([sys.argv[0], *qt_args])
    window = MainWindow(args.config, profile=args.printer_profile)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
