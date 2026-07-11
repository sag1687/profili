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

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor

from .qt_compat import ensure_qt_compat, QtCompat
ensure_qt_compat(Qt)


class DrawPolylineTool(QgsMapTool):
    """
    Multi-vertex polyline drawing tool.

    - Left single-click: add a vertex (rubber band follows).
    - Double left-click: finish and emit the polyline (min 2 points).
    - Right-click: cancel current drawing.
    """

    lineDrawn = pyqtSignal(list)   # emits list[QgsPointXY]

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._points = []
        self._rb = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._rb.setColor(QColor("#00e5ff"))
        self._rb.setWidth(3)
        self._rb_temp = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._rb_temp.setColor(QColor("#0099bb"))
        self._rb_temp.setWidth(2)
        self._rb_temp.setLineStyle(QtCompat.DashLine)
        self._double_clicking = False

    # ------------------------------------------------------------------
    def canvasPressEvent(self, e):
        if e.button() == QtCompat.RightButton:
            self.reset()
            return
        if e.button() == QtCompat.LeftButton:
            # double-click fires a press first; ignore the extra press
            if self._double_clicking:
                self._double_clicking = False
                return
            pt = self.toMapCoordinates(e.pos())
            self._points.append(pt)
            self._rb.reset(QgsWkbTypes.LineGeometry)
            for p in self._points:
                self._rb.addPoint(p, True)

    def canvasMoveEvent(self, e):
        if self._points:
            pt = self.toMapCoordinates(e.pos())
            self._rb_temp.reset(QgsWkbTypes.LineGeometry)
            self._rb_temp.addPoint(self._points[-1], False)
            self._rb_temp.addPoint(pt, True)

    def canvasDoubleClickEvent(self, e):
        if e.button() == QtCompat.LeftButton and len(self._points) >= 2:
            self._double_clicking = True
            pts = list(self._points)
            self.reset()
            self.lineDrawn.emit(pts)

    def reset(self):
        self._points = []
        self._double_clicking = False
        self._rb.reset(QgsWkbTypes.LineGeometry)
        self._rb_temp.reset(QgsWkbTypes.LineGeometry)

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

    areaDrawn = pyqtSignal(list)   # emits closed list[QgsPointXY]

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._start = None
        self._rb = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        color = QColor("#f5a623")
        color.setAlpha(70)
        self._rb.setColor(color)
        self._rb.setWidth(2)

    def canvasPressEvent(self, e):
        if e.button() == QtCompat.RightButton:
            self.reset()
            return
        if e.button() != QtCompat.LeftButton:
            return
        if not (e.modifiers() & QtCompat.ShiftModifier):
            return
        self._start = self.toMapCoordinates(e.pos())
        self._update_rect(self._start)

    def canvasMoveEvent(self, e):
        if self._start is None:
            return
        self._update_rect(self.toMapCoordinates(e.pos()))

    def canvasReleaseEvent(self, e):
        if e.button() != QtCompat.LeftButton or self._start is None:
            return
        end = self.toMapCoordinates(e.pos())
        points = self._rect_points(self._start, end)
        self.reset()
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
        self._rb.reset(QgsWkbTypes.PolygonGeometry)
        for p in points:
            self._rb.addPoint(p, True)

    def reset(self):
        self._start = None
        self._rb.reset(QgsWkbTypes.PolygonGeometry)

    def deactivate(self):
        self.reset()
        super().deactivate()
