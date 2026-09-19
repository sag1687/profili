# -*- coding: utf-8 -*-
# Copyright (C) 2024 Dott. Sarino Alfonso Grande <info@sinocloud.it>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import time

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.PyQt.QtCore import pyqtSignal, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication

from .qt_compat import QtCompat


class DrawPolylineTool(QgsMapTool):
    """
    Multi-vertex polyline drawing tool.

    - Left single-click: add a vertex (rubber band follows).
    - Double left-click: finish and emit the polyline (min 2 points).
    - Right-click: cancel current drawing.
    """

    lineDrawn = pyqtSignal(list)  # emits list[QgsPointXY]
    #: Live preview tick while drawing, emits the vertices confirmed so far
    #: plus the current cursor position (tentative last point). Throttled
    #: so it can drive a "ProfiloExpress" preview without flooding it on
    #: fast mouse movement.
    previewChanged = pyqtSignal(list)
    #: Emitted when an in-progress drawing is abandoned (right-click cancel,
    #: or the tool being deactivated) rather than finished normally, so
    #: callers can close any live preview window and restore their UI.
    drawingCancelled = pyqtSignal()

    PREVIEW_INTERVAL_MS = 120

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._points = []
        self._rb = QgsRubberBand(
            self.canvas, QgsWkbTypes.GeometryType.LineGeometry
        )
        self._rb.setColor(QColor("#00e5ff"))
        self._rb.setWidth(3)
        self._rb_temp = QgsRubberBand(
            self.canvas, QgsWkbTypes.GeometryType.LineGeometry
        )
        self._rb_temp.setColor(QColor("#0099bb"))
        self._rb_temp.setWidth(2)
        self._rb_temp.setLineStyle(QtCompat.PenStyle.DashLine)
        self._last_press_screen_pos = None
        self._last_press_time = None
        self._pending_preview_pt = None
        self._preview_timer = QTimer()
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(self.PREVIEW_INTERVAL_MS)
        self._preview_timer.timeout.connect(self._emit_preview)

    # ------------------------------------------------------------------
    def canvasPressEvent(self, e):
        if e.button() == QtCompat.MouseButton.RightButton:
            self.reset()
            return
        if e.button() == QtCompat.MouseButton.LeftButton:
            # Qt's real event order for a double-click is
            # Press -> Release -> Press -> DoubleClick -> Release: the
            # second press always arrives BEFORE canvasDoubleClickEvent, so
            # a flag only ever set inside canvasDoubleClickEvent can never
            # be seen here in time. Instead, treat a press that lands close
            # in both time and screen position to the previous one as the
            # second half of a double-click and skip it, rather than adding
            # a near-duplicate vertex at the end of the line.
            now = time.monotonic()
            screen_pos = e.pos()
            if (
                self._points
                and self._last_press_screen_pos is not None
                and self._last_press_time is not None
                and (now - self._last_press_time) * 1000
                <= QApplication.doubleClickInterval()
                and (screen_pos - self._last_press_screen_pos).manhattanLength()
                <= QApplication.startDragDistance()
            ):
                self._last_press_screen_pos = None
                self._last_press_time = None
                return
            self._last_press_screen_pos = screen_pos
            self._last_press_time = now
            pt = self.toMapCoordinates(e.pos())
            self._points.append(pt)
            self._rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
            for p in self._points:
                self._rb.addPoint(p, True)
            if len(self._points) >= 2:
                self.previewChanged.emit(list(self._points))

    def canvasMoveEvent(self, e):
        if self._points:
            pt = self.toMapCoordinates(e.pos())
            self._rb_temp.reset(QgsWkbTypes.GeometryType.LineGeometry)
            self._rb_temp.addPoint(self._points[-1], False)
            self._rb_temp.addPoint(pt, True)
            self._pending_preview_pt = pt
            if not self._preview_timer.isActive():
                self._preview_timer.start()

    def _emit_preview(self):
        if self._pending_preview_pt is not None and self._points:
            self.previewChanged.emit(
                self._points + [self._pending_preview_pt]
            )

    def canvasDoubleClickEvent(self, e):
        if (e.button() == QtCompat.MouseButton.LeftButton
                and len(self._points) >= 2):
            pts = list(self._points)
            self.reset(emit_cancelled=False)
            self.lineDrawn.emit(pts)

    def reset(self, emit_cancelled=True):
        had_points = bool(self._points)
        self._points = []
        self._last_press_screen_pos = None
        self._last_press_time = None
        self._pending_preview_pt = None
        self._preview_timer.stop()
        self._rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self._rb_temp.reset(QgsWkbTypes.GeometryType.LineGeometry)
        if emit_cancelled and had_points:
            self.drawingCancelled.emit()

    def deactivate(self):
        self.reset()
        super().deactivate()


class DrawRectangleAreaTool(QgsMapTool):
    """
    Shift + drag rectangle tool for raster download areas.

    - Hold Shift and drag with left mouse button: draw an area.
    - Release left mouse button: emit a closed rectangle polygon.
    - Right-click: cancel current drawing.
    """

    areaDrawn = pyqtSignal(list)  # emits closed list[QgsPointXY]
    #: Emitted when an in-progress area drag is abandoned (right-click
    #: cancel, or the tool being deactivated) instead of finished normally.
    drawingCancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._start = None
        self._rb = QgsRubberBand(
            self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry
        )
        color = QColor("#f5a623")
        color.setAlpha(70)
        self._rb.setColor(color)
        self._rb.setWidth(2)

    def canvasPressEvent(self, e):
        if e.button() == QtCompat.MouseButton.RightButton:
            self.reset()
            return
        if e.button() != QtCompat.MouseButton.LeftButton:
            return
        if not (e.modifiers() & QtCompat.KeyboardModifier.ShiftModifier):
            return
        self._start = self.toMapCoordinates(e.pos())
        self._update_rect(self._start)

    def canvasMoveEvent(self, e):
        if self._start is None:
            return
        self._update_rect(self.toMapCoordinates(e.pos()))

    def canvasReleaseEvent(self, e):
        if (e.button() != QtCompat.MouseButton.LeftButton
                or self._start is None):
            return
        end = self.toMapCoordinates(e.pos())
        points = self._rect_points(self._start, end)
        self.reset(emit_cancelled=False)
        if points:
            self.areaDrawn.emit(points)

    def _rect_points(self, p1, p2):
        xmin, xmax = sorted((p1.x(), p2.x()))
        ymin, ymax = sorted((p1.y(), p2.y()))
        if abs(xmax - xmin) <= 0 or abs(ymax - ymin) <= 0:
            return []
        return [
            QgsPointXY(xmin, ymin),
            QgsPointXY(xmax, ymin),
            QgsPointXY(xmax, ymax),
            QgsPointXY(xmin, ymax),
            QgsPointXY(xmin, ymin),
        ]

    def _update_rect(self, end):
        points = self._rect_points(self._start, end)
        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for p in points:
            self._rb.addPoint(p, True)

    def reset(self, emit_cancelled=True):
        had_start = self._start is not None
        self._start = None
        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if emit_cancelled and had_start:
            self.drawingCancelled.emit()

    def deactivate(self):
        self.reset()
        super().deactivate()


class DrawPolygonAreaTool(QgsMapTool):
    """
    Multi-vertex free-hand polygon tool for raster download areas.

    - Left single-click: add a vertex (rubber band follows).
    - Double left-click: close the ring and emit it (min 3 points).
    - Right-click: cancel current drawing.
    """

    areaDrawn = pyqtSignal(list)  # emits closed list[QgsPointXY]
    drawingCancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._points = []
        self._rb = QgsRubberBand(
            self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry
        )
        color = QColor("#f5a623")
        color.setAlpha(70)
        self._rb.setColor(color)
        self._rb.setWidth(2)
        self._last_press_screen_pos = None
        self._last_press_time = None

    def canvasPressEvent(self, e):
        if e.button() == QtCompat.MouseButton.RightButton:
            self.reset()
            return
        if e.button() == QtCompat.MouseButton.LeftButton:
            # Same double-click-vs-second-vertex disambiguation as
            # DrawPolylineTool: the second press of a double-click always
            # arrives before canvasDoubleClickEvent, so it must be filtered
            # here by time/position rather than by a flag set there.
            now = time.monotonic()
            screen_pos = e.pos()
            if (
                self._points
                and self._last_press_screen_pos is not None
                and self._last_press_time is not None
                and (now - self._last_press_time) * 1000
                <= QApplication.doubleClickInterval()
                and (screen_pos - self._last_press_screen_pos).manhattanLength()
                <= QApplication.startDragDistance()
            ):
                self._last_press_screen_pos = None
                self._last_press_time = None
                return
            self._last_press_screen_pos = screen_pos
            self._last_press_time = now
            pt = self.toMapCoordinates(e.pos())
            self._points.append(pt)
            self._update_rubber_band()

    def canvasMoveEvent(self, e):
        if self._points:
            self._update_rubber_band(self.toMapCoordinates(e.pos()))

    def _update_rubber_band(self, tentative_pt=None):
        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for p in self._points:
            self._rb.addPoint(p, True)
        if tentative_pt is not None:
            self._rb.addPoint(tentative_pt, True)

    def canvasDoubleClickEvent(self, e):
        if (e.button() == QtCompat.MouseButton.LeftButton
                and len(self._points) >= 3):
            pts = list(self._points)
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            self.reset(emit_cancelled=False)
            self.areaDrawn.emit(pts)

    def reset(self, emit_cancelled=True):
        had_points = bool(self._points)
        self._points = []
        self._last_press_screen_pos = None
        self._last_press_time = None
        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        if emit_cancelled and had_points:
            self.drawingCancelled.emit()

    def deactivate(self):
        self.reset()
        super().deactivate()
