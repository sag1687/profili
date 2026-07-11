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

"""
plugin.py — Entry point for the Profili, Sezioni e Comuni QGIS plugin.
"""

import os
import csv
import html
import math
import re
import textwrap
import datetime
import shutil
import tempfile

from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QTextBrowser,
    QVBoxLayout,
)
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtCore import QUrl
from qgis.core import Qgis, QgsProject, QgsPointXY

from .dialog import ProfiliSezioniComuniDialog
from .map_tool import DrawPolylineTool, DrawRectangleAreaTool
from .core_raster_download import (
    ISTAT_ADMIN_BOUNDARIES_PAGE,
    RASTER_SOURCES,
    bbox_text,
    bbox_wgs84_from_points,
    create_download_area_layer,
    download_raster_area,
    tinitaly_wcs_layer,
)
from .core_elevation import (
    calculate_profile, generate_profile_svg,
    generate_profile_results_html, export_profile_csv, build_pickets,
)
from .core_sections import (
    calculate_cross_sections, generate_cross_sections_svg,
    generate_cross_sections_results_html,
)
from .qt_compat import compat_enum


class ProfiliSezioniComuniPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None
        self.map_tool = None
        self.area_tool = None

        # last results state
        self.last_points_data = None
        self.last_total_dist = None
        self.last_svg_content = None
        self.last_results_html = None
        self.last_cross_sections_data = None
        self.last_vector_layers = []
        self.last_vector_gpkg = None
        self.last_chart_png_path = None
        self.last_interpolated_raster_path = None
        self.last_report_context = {}
        self.current_mode = "profile"   # "profile" | "sections"
        self._last_drawn_points = None
        self._popup_connected_layer_ids = set()
        self._active_section_popup = None

    # ──────────────────────────────────────────────────────────────
    # initGui / unload
    # ──────────────────────────────────────────────────────────────

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.svg")
        self.action = QAction(
            QIcon(icon_path),
            "Profili, Sezioni e Comuni",
            self.iface.mainWindow(),
        )
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&GeoFusion Tools", self.action)

    def unload(self):
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&GeoFusion Tools", self.action)

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    def _push(self, title, message, level_name="Info"):
        level = getattr(Qgis, level_name, None)
        if level is None:
            ml = getattr(Qgis, "MessageLevel", None)
            level = getattr(ml, level_name, 0) if ml else 0
        try:
            self.iface.messageBar().pushMessage(title, message, level)
        except TypeError:
            self.iface.messageBar().pushMessage(title, message, level=level)

    def _open_url(self, url):
        QDesktopServices.openUrl(QUrl(url))

    def _default_output_dir(self):
        project = QgsProject.instance()
        try:
            base = project.homePath()
        except Exception:
            base = ""
        if not base:
            base = os.path.expanduser("~/ProfiliSezioniComuni_Output")
        out_dir = os.path.join(base, "profili_sezioni_output")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _new_output_gpkg_path(self, prefix):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self._default_output_dir(), f"{prefix}_{stamp}.gpkg")

    def _new_output_raster_path(self, prefix):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self._default_output_dir(), f"{prefix}_{stamp}.tif")

    def _group_sort_key(self, name):
        text = str(name or "")
        match = re.match(r"^\s*(\d+)\s+", text)
        if match:
            return 0, int(match.group(1)), text.lower()
        return 1, text.lower()

    def _move_group_to_index(self, parent_group, group, index):
        if not parent_group or not group:
            return group
        try:
            children = list(parent_group.children())
            current_index = children.index(group)
        except Exception:
            return group
        if current_index == index:
            return group
        try:
            clone = group.clone()
            expanded = group.isExpanded() if hasattr(group, "isExpanded") else True
            parent_group.insertChildNode(index, clone)
            if hasattr(clone, "setExpanded"):
                clone.setExpanded(expanded)
            parent_group.removeChildNode(group)
            return clone
        except Exception:
            return group

    def _output_root_group(self):
        root = QgsProject.instance().layerTreeRoot()
        group_name = "Profili, Sezioni e Comuni"
        group = root.findGroup(group_name)
        if group is None:
            insert_group = getattr(root, "insertGroup", None)
            if insert_group:
                group = insert_group(0, group_name)
            else:
                group = root.addGroup(group_name)
        return self._move_group_to_index(root, group, 0)

    def _new_output_group(self, title, priority="vector"):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parent = self._output_root_group()
        name = f"{title} - {stamp}"
        insert_group = getattr(parent, "insertGroup", None)
        if priority == "raster":
            if insert_group:
                return insert_group(len(parent.children()), name)
            return parent.addGroup(name)
        if insert_group:
            return insert_group(0, name)
        group = parent.addGroup(name)
        return self._move_group_to_index(parent, group, 0)

    def _ensure_subgroup(self, parent_group, name):
        group = parent_group.findGroup(name) if parent_group else None
        if not parent_group:
            return None
        desired_key = self._group_sort_key(name)
        desired_index = len(parent_group.children())
        for idx, child in enumerate(parent_group.children()):
            child_name = child.name() if hasattr(child, "name") else ""
            if self._group_sort_key(child_name) > desired_key:
                desired_index = idx
                break
        if group is not None:
            return self._move_group_to_index(parent_group, group, desired_index)
        insert_group = getattr(parent_group, "insertGroup", None)
        if insert_group:
            return insert_group(desired_index, name)
        group = parent_group.addGroup(name)
        return self._move_group_to_index(parent_group, group, desired_index)

    def _add_output_layer(self, layer, parent_group, subgroup_name=None):
        if not layer or not layer.isValid():
            return
        project = QgsProject.instance()
        if project.mapLayer(layer.id()) is None:
            project.addMapLayer(layer, False)
        target = self._ensure_subgroup(parent_group, subgroup_name) if subgroup_name else parent_group
        target.addLayer(layer)

    def _format_bytes(self, value):
        if not value:
            return ""
        size = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _format_duration(self, seconds):
        if seconds is None:
            return "n/d"
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:d}h {m:02d}m"
        if m:
            return f"{m:d}m {s:02d}s"
        return f"{s:d}s"

    def _download_progress_callback(self, percent=0, message="", **kwargs):
        percent = max(0, min(int(percent or 0), 100))
        self.dialog.progress.setRange(0, 100)
        self.dialog.progress.setValue(percent)

        pieces = [f"{percent}%", message]
        transferred = kwargs.get("transferred")
        total = kwargs.get("total")
        speed = kwargs.get("speed")
        eta = kwargs.get("eta")
        elapsed = kwargs.get("elapsed")
        if transferred and total:
            pieces.append(f"{self._format_bytes(transferred)} / {self._format_bytes(total)}")
        if speed:
            pieces.append(f"{self._format_bytes(speed)}/s")
        if eta is not None:
            pieces.append(f"tempo rimanente / ETA {self._format_duration(eta)}")
        if elapsed:
            pieces.append(f"trascorso / elapsed {self._format_duration(elapsed)}")
        self.dialog.lbl_download_status.setText(" · ".join(p for p in pieces if p))
        QApplication.processEvents()
        return True

    def _svg_chart_html(self, svg_content, name):
        image_path = self._svg_to_png(svg_content, name)
        self.last_chart_png_path = image_path
        return self._chart_image_html(image_path, svg_content)

    def _chart_image_html(self, image_path, fallback_svg):
        if not image_path:
            return fallback_svg
        url = QUrl.fromLocalFile(image_path).toString()
        return (
            '<img src="{0}" style="display:block;width:100%;max-width:1700px;'
            'height:auto;border:1px solid #2d3757;border-radius:6px;background:#12151e;">'
        ).format(url)

    def _persist_svg_chart(self, svg_content, name):
        if not svg_content:
            return None
        chart_dir = os.path.join(self._default_output_dir(), "_charts")
        os.makedirs(chart_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(chart_dir, f"{name}_{stamp}.svg")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svg_content)
            return path
        except Exception:
            return None

    def _format_crs_name(self, crs):
        if not crs:
            return "n/d"
        try:
            auth = crs.authid() or ""
            desc = crs.description() or ""
            if auth and desc and auth != desc:
                return f"{auth} ({desc})"
            return auth or desc or "n/d"
        except Exception:
            return "n/d"

    def _build_measurement_context(self, source_label="", raster_layer=None, vector_layers=None):
        project = QgsProject.instance()
        project_crs = project.crs()
        raster_crs = raster_layer.crs() if raster_layer else None

        notes = []
        severity = "ok"
        if not project_crs or not project_crs.isValid():
            notes.append(
                "CRS progetto non valido: verificare il sistema di riferimento prima di usare distanze, aree o volumi."
            )
            severity = "warn"
        elif project_crs.isGeographic():
            notes.append(
                "CRS progetto geografico: per misure affidabili da file usare un CRS proiettato metrico."
            )
            severity = "warn"
        else:
            notes.append(
                "CRS progetto proiettato: condizione consigliata per misure tecniche e disegni derivati dal file."
            )

        if raster_layer and raster_crs and raster_crs.isValid():
            if raster_crs.isGeographic():
                notes.append(
                    "Il DEM usa un CRS geografico: il campionamento quota e il raster interpolato del corridoio sono meno robusti."
                )
                severity = "warn"
            elif project_crs.isValid() and project_crs != raster_crs:
                notes.append(
                    "Il tracciato viene riproiettato nel CRS del DEM per leggere le quote."
                )
        elif source_label:
            notes.append(
                f"Sorgente quote attiva: {source_label}."
            )

        layer_lines = []
        context_layers = vector_layers if vector_layers is not None else self.last_vector_layers
        for idx, layer in enumerate(context_layers, 1):
            if not layer or not layer.isValid():
                continue
            geom_label = ""
            try:
                geom_type = layer.geometryType()
                geom_label = {
                    0: "point",
                    1: "line",
                    2: "polygon",
                }.get(geom_type, "vector")
            except Exception:
                geom_label = "vector"
            layer_lines.append(f"{idx:02d}. {layer.name()} [{geom_label}]")
        if self.last_interpolated_raster_path:
            layer_lines.append("R. Raster corridoio interpolato [raster]")
        if raster_layer and raster_layer.isValid():
            layer_lines.append(f"D. DEM sorgente: {raster_layer.name()} [raster]")

        tone_class = "crs-ok" if severity == "ok" else "crs-warn"
        html_lines = "".join(f"<li>{html.escape(line)}</li>" for line in notes)
        layer_html = "".join(f"<li>{html.escape(line)}</li>" for line in layer_lines) or "<li>Nessun layer registrato.</li>"
        html_block = (
            f"<div class='summary-card {tone_class}'>"
            f"<strong>CRS e affidabilità misure / CRS and measurement reliability</strong><br>"
            f"CRS progetto: <strong>{html.escape(self._format_crs_name(project_crs))}</strong><br>"
            f"CRS dato quote: <strong>{html.escape(self._format_crs_name(raster_crs) if raster_crs else source_label or 'n/d')}</strong>"
            f"<ul class='report-list'>{html_lines}</ul>"
            f"</div>"
            f"<div class='summary-card'>"
            f"<strong>Layer generati / Generated layers</strong>"
            f"<ul class='report-list'>{layer_html}</ul>"
            f"</div>"
        )
        return {
            "html": html_block,
            "severity": severity,
            "project_crs": self._format_crs_name(project_crs),
            "data_crs": self._format_crs_name(raster_crs) if raster_crs else (source_label or "n/d"),
            "notes": notes,
            "layers": layer_lines,
            "source_label": source_label or "n/d",
        }

    def _section_detail_svg(self, sec, width=640, height=230):
        pts = [p for p in (sec.get("points") or []) if p.get("elevation") is not None]
        if len(pts) < 2:
            return "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='230'><text x='16' y='30' fill='#8ba3c7'>No data</text></svg>"

        pad_x, pad_y = 24, 20
        plot_w = width - pad_x * 2
        plot_h = height - 58
        offsets = [float(p.get("offset_m") or 0.0) for p in pts]
        elevations = [float(p.get("elevation") or 0.0) for p in pts]
        design = sec.get("design_elevation")
        if design is not None:
            elevations.append(float(design))
        min_x, max_x = min(offsets), max(offsets)
        min_y, max_y = min(elevations), max(elevations)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)

        def sx(value):
            return pad_x + ((float(value) - min_x) / span_x) * plot_w

        def sy(value):
            return pad_y + plot_h - ((float(value) - min_y) / span_y) * plot_h

        d_path = " ".join(
            f"{'M' if i == 0 else 'L'}{sx(p.get('offset_m')):.1f},{sy(p.get('elevation')):.1f}"
            for i, p in enumerate(pts)
        )
        fill_d = f"{d_path} L{sx(offsets[-1]):.1f},{pad_y + plot_h:.1f} L{sx(offsets[0]):.1f},{pad_y + plot_h:.1f} Z"
        design_line = ""
        if design is not None:
            y = sy(design)
            design_line = (
                f"<line x1='{pad_x:.1f}' y1='{y:.1f}' x2='{pad_x + plot_w:.1f}' y2='{y:.1f}' "
                f"stroke='#f59e0b' stroke-width='2' stroke-dasharray='6 4'/>"
            )
        title = "Sezione {0:02d} · Prog. {1:.1f} m".format(
            int(sec.get("index") or 0),
            float(sec.get("progressive_m") or 0.0),
        )
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" rx="10" ry="10" fill="#0e1118" stroke="#2d3757"/>
<text x="20" y="24" fill="#f8fafc" font-size="15" font-family="Arial" font-weight="700">{html.escape(title)}</text>
<path d="{fill_d}" fill="rgba(52,211,153,0.14)" stroke="none"/>
<path d="{d_path}" fill="none" stroke="#34d399" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
{design_line}
<text x="20" y="{height - 16}" fill="#8ba3c7" font-size="12" font-family="Arial">Offset {min_x:.1f} m / {max_x:.1f} m</text>
<text x="{width - 20}" y="{height - 16}" fill="#8ba3c7" font-size="12" font-family="Arial" text-anchor="end">Quote {min_y:.2f} - {max_y:.2f} m</text>
</svg>"""

    def _nice_layout_interval(self, span, geographic=False):
        if geographic:
            candidates = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0]
            target = max(float(span or 0.0) / 4.5, candidates[0])
            for value in candidates:
                if value >= target:
                    return value
            return candidates[-1]
        span = max(float(span or 0.0), 1.0)
        raw = span / 4.5
        mag = 10 ** math.floor(math.log10(raw))
        for factor in (1, 2, 5, 10):
            value = factor * mag
            if value >= raw:
                return value
        return 10 * mag

    def _format_layout_coord(self, value, geographic=False):
        decimals = 6 if geographic else 2
        return f"{float(value):.{decimals}f}"

    def _persist_north_arrow_svg(self):
        path = os.path.join(self._default_output_dir(), "_charts", "north_arrow.svg")
        if os.path.exists(path):
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        svg = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="180" viewBox="0 0 120 180">
  <defs>
    <linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="#f8fafc"/>
      <stop offset="100%" stop-color="#94a3b8"/>
    </linearGradient>
  </defs>
  <rect x="18" y="18" width="84" height="144" rx="18" ry="18" fill="#0f172a" stroke="#334155" stroke-width="4"/>
  <path d="M60 26 L88 108 L60 94 L32 108 Z" fill="url(#g)" stroke="#e2e8f0" stroke-width="3"/>
  <path d="M60 150 L32 108 L60 120 L88 108 Z" fill="#1e293b" stroke="#64748b" stroke-width="3"/>
  <text x="60" y="170" text-anchor="middle" font-family="Arial" font-size="22" font-weight="700" fill="#f8fafc">N</text>
</svg>"""
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        return path

    def _write_svg_asset(self, directory, name, svg_content):
        if not directory or not svg_content:
            return None
        os.makedirs(directory, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "asset")).strip("._") or "asset"
        path = os.path.join(directory, f"{safe_name}.svg")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svg_content)
            return path
        except Exception:
            return None

    def _layout_page_size(self):
        from qgis.core import QgsLayoutSize, QgsUnitTypes

        return QgsLayoutSize(420, 297, QgsUnitTypes.LayoutMillimeters)

    def _layout_ensure_page(self, layout, page_number):
        from qgis.core import QgsLayoutItemPage

        page_number = max(int(page_number or 0), 0)
        pages = layout.pageCollection()
        while pages.pageCount() <= page_number:
            page = QgsLayoutItemPage(layout)
            page.setPageSize(self._layout_page_size())
            pages.addPage(page)
        return pages.page(page_number)

    def _layout_place_item(self, layout, item, x, y, w=None, h=None, page=0):
        from qgis.core import QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes

        self._layout_ensure_page(layout, page)
        if w is not None and h is not None:
            item.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
        point = QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters)
        try:
            item.attemptMove(point, page=page)
            return item
        except TypeError:
            pass
        except Exception:
            pass
        try:
            page_item = layout.pageCollection().page(page)
            origin = page_item.positionWithUnits()
            point = QgsLayoutPoint(
                origin.x() + x,
                origin.y() + y,
                QgsUnitTypes.LayoutMillimeters,
            )
        except Exception:
            try:
                spacing = float(layout.pageCollection().spaceBetweenPages() or 0.0)
            except Exception:
                spacing = 0.0
            point = QgsLayoutPoint(
                x,
                y + page * (297.0 + spacing),
                QgsUnitTypes.LayoutMillimeters,
            )
        item.attemptMove(point)
        return item

    def _layout_add_picture_box(self, layout, image_path, x, y, w, h, *,
                                page=0, frame=True, background="#0f172a",
                                stroke="#0f172a"):
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsLayoutItemPicture

        if not image_path:
            return None
        try:
            item = QgsLayoutItemPicture(layout)
            item.setPicturePath(image_path)
            item.setResizeMode(compat_enum(QgsLayoutItemPicture, "Zoom", "ResizeMode"))
            item.setFrameEnabled(bool(frame))
            try:
                if background is not None:
                    item.setBackgroundColor(QColor(background))
                if stroke is not None:
                    item.setFrameStrokeColor(QColor(stroke))
            except Exception:
                pass
            layout.addLayoutItem(item)
            self._layout_place_item(layout, item, x, y, w, h, page=page)
            return item
        except Exception:
            return None

    def _layout_add_north_arrow(self, layout, x, y, w=18, h=32, *, page=0):
        from qgis.core import QgsLayoutItemPicture

        try:
            arrow_path = self._persist_north_arrow_svg()
            item = QgsLayoutItemPicture(layout)
            item.setPicturePath(arrow_path)
            item.setResizeMode(compat_enum(QgsLayoutItemPicture, "Zoom", "ResizeMode"))
            item.setFrameEnabled(False)
            layout.addLayoutItem(item)
            self._layout_place_item(layout, item, x, y, w, h, page=page)
            return item
        except Exception:
            return None

    def _layout_add_scale_bar(self, layout, map_item, x, y, w=70, h=12, *, page=0):
        from qgis.PyQt.QtGui import QColor, QFont
        from qgis.core import QgsLayoutItemScaleBar, QgsUnitTypes

        try:
            scalebar = QgsLayoutItemScaleBar(layout)
            scalebar.setLinkedMap(map_item)
            try:
                scalebar.setStyle("Single Box")
            except Exception:
                pass
            project_crs = QgsProject.instance().crs()
            unit = (
                getattr(QgsUnitTypes, "DistanceMeters", None)
                if not project_crs.isGeographic() else
                getattr(QgsUnitTypes, "DistanceKilometers", getattr(QgsUnitTypes, "DistanceMeters", None))
            )
            if unit is not None:
                scalebar.setUnits(unit)
            scalebar.setNumberOfSegments(4)
            scalebar.setNumberOfSegmentsLeft(0)
            try:
                units_per_segment = max(map_item.extent().width() / 8.0, 1.0)
                if project_crs.isGeographic():
                    units_per_segment = max(units_per_segment * 111.32, 0.1)
                scalebar.setUnitsPerSegment(units_per_segment)
            except Exception:
                pass
            try:
                scalebar.setHeight(3.6)
            except Exception:
                pass
            try:
                scalebar.setFont(QFont("Arial", 7))
                scalebar.setFontColor(QColor("#e2e8f0"))
                scalebar.setFillColor(QColor("#f8fafc"))
                scalebar.setFillColor2(QColor("#475569"))
                scalebar.setLineColor(QColor("#e2e8f0"))
            except Exception:
                pass
            layout.addLayoutItem(scalebar)
            self._layout_place_item(layout, scalebar, x, y, w, h, page=page)
            try:
                scalebar.update()
            except Exception:
                pass
            return scalebar
        except Exception:
            return None

    def _layout_enable_map_grid(self, map_item):
        try:
            from qgis.core import QgsLayoutItemMapGrid, QgsProject

            grids = map_item.grids()
            add_grid = getattr(grids, "addGrid", None)
            if add_grid is None:
                return None
            grid = add_grid("GeoFusionGrid")
            grid.setEnabled(True)
            extent = map_item.extent()
            geographic = QgsProject.instance().crs().isGeographic()
            grid.setIntervalX(self._nice_layout_interval(extent.width(), geographic))
            grid.setIntervalY(self._nice_layout_interval(extent.height(), geographic))
            try:
                grid.setAnnotationEnabled(True)
                grid.setAnnotationPrecision(5 if geographic else 1)
            except Exception:
                pass
            try:
                frame_style = compat_enum(QgsLayoutItemMapGrid, "InteriorTicks", "FrameStyle", default=None)
                if frame_style is not None:
                    grid.setFrameStyle(frame_style)
            except Exception:
                pass
            try:
                grid.setStyle(compat_enum(QgsLayoutItemMapGrid, "Solid", "GridStyle", default=None))
            except Exception:
                pass
            return grid
        except Exception:
            return None

    def _layout_wrapped_text(self, lines, width=34):
        wrapped = []
        for line in lines or []:
            parts = textwrap.wrap(str(line), width=width) or [str(line)]
            wrapped.extend(parts)
        return "\n".join(wrapped)

    def _layout_add_text_box(self, layout, text, x, y, w, h, *,
                             title=False, font_size=8.5, page=0,
                             background="#1e2437", foreground="#e2e8f0"):
        from qgis.PyQt.QtGui import QColor, QFont
        from qgis.core import QgsLayoutItemLabel

        item = QgsLayoutItemLabel(layout)
        item.setText(text)
        font = QFont("Arial", max(1, int(round(font_size))))
        font.setBold(bool(title))
        item.setFont(font)
        item.setFontColor(QColor(foreground))
        try:
            item.setMarginX(2.5)
            item.setMarginY(2.0)
        except Exception:
            pass
        item.setBackgroundEnabled(True)
        item.setBackgroundColor(QColor(background))
        item.setFrameEnabled(True)
        item.setFrameStrokeColor(QColor("#2d3757"))
        layout.addLayoutItem(item)
        self._layout_place_item(layout, item, x, y, w, h, page=page)
        return item

    def _layout_add_legend(self, layout, map_item, x, y, w, h, *, page=0,
                           title="Legenda / Layers"):
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsLayoutItemLegend

        try:
            legend = QgsLayoutItemLegend(layout)
            legend.setTitle(title)
            try:
                legend.setLinkedMap(map_item)
                legend.setFilterByMapItems([map_item])
                legend.updateFilterByMap(False)
            except Exception:
                pass
            try:
                legend.setResizeToContents(True)
            except Exception:
                pass
            legend.setBackgroundEnabled(True)
            legend.setBackgroundColor(QColor("#0f172a"))
            legend.setFrameEnabled(True)
            legend.setFrameStrokeColor(QColor("#334155"))
            layout.addLayoutItem(legend)
            self._layout_place_item(layout, legend, x, y, w, h, page=page)
            try:
                legend.adjustBoxSize()
            except Exception:
                pass
            return legend
        except Exception:
            return None

    def _layout_table_svg(self, title, rows, width=760):
        rows = [(str(label), str(value)) for label, value in (rows or [])]
        if not rows:
            rows = [("Info", "Nessun dato disponibile")]
        pad = 18
        title_h = 30
        header_h = 24
        row_h = 26
        table_y = pad + title_h
        total_h = table_y + header_h + len(rows) * row_h + pad
        col1_w = int((width - pad * 2) * 0.46)
        col2_x = pad + col1_w
        col2_w = width - pad * 2 - col1_w
        line_y = table_y + header_h
        row_svg = []
        for idx, (label, value) in enumerate(rows):
            y = line_y + idx * row_h
            fill = "#1a2235" if idx % 2 == 0 else "#111827"
            row_svg.append(
                "<rect x='{x}' y='{y}' width='{w1}' height='{h}' fill='{fill}'/>"
                "<rect x='{x2}' y='{y}' width='{w2}' height='{h}' fill='#0f172a'/>"
                "<line x1='{x}' y1='{y2}' x2='{x3}' y2='{y2}' stroke='#334155' stroke-width='1'/>"
                "<text x='{tx1}' y='{ty}' fill='#9ec5ff' font-size='12' font-family='Arial' font-weight='700'>{label}</text>"
                "<text x='{tx2}' y='{ty}' fill='#f8fafc' font-size='12' font-family='Arial'>{value}</text>".format(
                    x=pad,
                    y=y,
                    w1=col1_w,
                    h=row_h,
                    fill=fill,
                    x2=col2_x,
                    w2=col2_w,
                    y2=y + row_h,
                    x3=width - pad,
                    tx1=pad + 8,
                    tx2=col2_x + 8,
                    ty=y + 17,
                    label=html.escape(label.upper()),
                    value=html.escape(value),
                )
            )
        return """<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" rx="12" ry="12" fill="#0e1118" stroke="#334155"/>
<text x="{pad}" y="{title_y}" fill="#f8fafc" font-size="16" font-family="Arial" font-weight="700">{title}</text>
<rect x="{pad}" y="{head_y}" width="{w1}" height="{head_h}" fill="#24314a"/>
<rect x="{x2}" y="{head_y}" width="{w2}" height="{head_h}" fill="#1a2235"/>
<text x="{tx1}" y="{head_text_y}" fill="#9ec5ff" font-size="12" font-family="Arial" font-weight="700">ATTRIBUTO</text>
<text x="{tx2}" y="{head_text_y}" fill="#9ec5ff" font-size="12" font-family="Arial" font-weight="700">VALORE</text>
{rows_svg}
</svg>""".format(
            width=width,
            height=total_h,
            pad=pad,
            title_y=pad + 16,
            title=html.escape(str(title or "Tabella attributi")),
            head_y=table_y,
            w1=col1_w,
            x2=col2_x,
            w2=col2_w,
            head_h=header_h,
            tx1=pad + 8,
            tx2=col2_x + 8,
            head_text_y=table_y + 16,
            rows_svg="".join(row_svg),
        )

    def _profile_report_rows(self):
        points = self.last_points_data or []
        valid = [p for p in points if p.get("elevation") is not None]
        total_m = float(self.last_total_dist or 0.0)
        pickets = build_pickets(points, total_m) if points else []
        context = self.last_report_context or {}
        rows = [
            ("Distanza", f"{total_m:.2f} m"),
            ("Campioni", len(points)),
            ("Picchetti", len(pickets)),
            ("Sorgente", context.get("source_label", "n/d")),
        ]
        if valid:
            rows.extend([
                ("Quota minima", f"{min(float(p['elevation']) for p in valid):.2f} m"),
                ("Quota massima", f"{max(float(p['elevation']) for p in valid):.2f} m"),
            ])
        return rows

    def _section_overview_rows(self, data):
        dp = data.get("design_profile") or {}
        metrics = data.get("road_metrics") or {}
        rows = [
            ("Sviluppo asse", f"{float(data.get('alignment_length_m') or 0.0):.2f} m"),
            ("Numero sezioni", int(data.get("n_sections") or 0)),
            ("Intervallo", f"{float(data.get('interval_m') or 0.0):.2f} m"),
            ("Larghezza totale", f"{float(data.get('half_width_m') or 0.0) * 2.0:.2f} m"),
            ("Sterro totale", f"{float(data.get('total_cut_m3') or 0.0):.2f} m3"),
            ("Riporto totale", f"{float(data.get('total_fill_m3') or 0.0):.2f} m3"),
            ("Bilancio", f"{float((data.get('total_fill_m3') or 0.0) - (data.get('total_cut_m3') or 0.0)):.2f} m3"),
        ]
        if dp.get("start_elevation") is not None:
            rows.append(("Quota progetto iniziale", f"{float(dp.get('start_elevation') or 0.0):.2f} m"))
        if dp.get("end_elevation") is not None:
            rows.append(("Quota progetto finale", f"{float(dp.get('end_elevation') or 0.0):.2f} m"))
        if dp.get("grade_pct") is not None:
            rows.append(("Pendenza progetto", f"{float(dp.get('grade_pct') or 0.0):.2f} %"))
        if metrics:
            rows.append(("Pendenza media", f"{float(metrics.get('avg_grade_pct') or 0.0):.2f} %"))
            rows.append(("Curve rilevate", int(metrics.get("curve_count") or 0)))
        return rows

    def _section_report_rows(self, sec):
        pts = [p for p in (sec.get("points") or []) if p.get("elevation") is not None]
        rows = [
            ("Sezione", int(sec.get("index") or 0)),
            ("Progressiva", f"{float(sec.get('progressive_m') or 0.0):.2f} m"),
            ("Quota minima", "n/d" if sec.get("min_elevation") is None else f"{float(sec.get('min_elevation') or 0.0):.2f} m"),
            ("Quota massima", "n/d" if sec.get("max_elevation") is None else f"{float(sec.get('max_elevation') or 0.0):.2f} m"),
            ("Quota progetto", "n/d" if sec.get("design_elevation") is None else f"{float(sec.get('design_elevation') or 0.0):.2f} m"),
            ("Area sezione", f"{float(sec.get('area_m2') or 0.0):.2f} m2"),
            ("Sterro", f"{float(sec.get('cut_area_m2') or 0.0):.2f} m2"),
            ("Riporto", f"{float(sec.get('fill_area_m2') or 0.0):.2f} m2"),
            ("Campioni validi", len(pts)),
        ]
        return rows

    def _layout_add_page_footer(self, layout, page, text):
        self._layout_add_text_box(
            layout,
            text,
            12, 286, 396, 8,
            page=page,
            font_size=7,
            background="#0f172a",
            foreground="#8ba3c7",
        )

    def _report_layout_extent(self, points):
        from qgis.core import QgsGeometry

        project = QgsProject.instance()
        geom = QgsGeometry.fromPolylineXY(points)
        extent = geom.boundingBox()
        buf = 500 / 111320.0 if project.crs().isGeographic() else 500
        extent.setXMinimum(extent.xMinimum() - buf)
        extent.setXMaximum(extent.xMaximum() + buf)
        extent.setYMinimum(extent.yMinimum() - buf)
        extent.setYMaximum(extent.yMaximum() + buf)
        return extent

    def _current_report_title(self):
        if self.current_mode == "sections":
            return "Cross Sections / Sezioni Trasversali"
        return "Elevation Profile / Profilo Altimetrico"

    def _create_report_layout(self, points, svg_content, layout_name, svg_path, *, add_to_project):
        from qgis.PyQt.QtGui import QColor
        from qgis.core import (
            QgsPrintLayout,
            QgsLayoutItemMap,
        )

        project = QgsProject.instance()
        if add_to_project:
            for layout in project.layoutManager().printLayouts():
                if layout.name() == layout_name:
                    project.layoutManager().removeLayout(layout)

        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(layout_name)
        if add_to_project:
            project.layoutManager().addLayout(layout)

        extent = self._report_layout_extent(points)
        asset_dir = os.path.dirname(svg_path) if svg_path else None
        context = self.last_report_context or {}
        source_label = context.get("source_label", "n/d")
        project_crs = context.get("project_crs", "n/d")
        data_crs = context.get("data_crs", "n/d")
        notes = context.get("notes", [])[:5]
        layers = context.get("layers", []) or ["Nessun layer registrato."]
        project_name = project.title() or project.baseName() or "Project"
        now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        geographic = project.crs().isGeographic()
        total_pages = 2
        if self.current_mode == "sections" and self.last_cross_sections_data:
            total_pages += len(self.last_cross_sections_data.get("sections") or [])

        self._layout_ensure_page(layout, 0).setPageSize(self._layout_page_size())

        map_item = QgsLayoutItemMap(layout)
        map_item.setExtent(extent)
        map_item.setFrameEnabled(True)
        try:
            map_item.setBackgroundColor(QColor("#f8fafc"))
            map_item.setFrameStrokeColor(QColor("#0f172a"))
        except Exception:
            pass
        layout.addLayoutItem(map_item)
        self._layout_place_item(layout, map_item, 12, 24, 278, 176, page=0)
        try:
            layout.setReferenceMap(map_item)
        except Exception:
            pass
        self._layout_enable_map_grid(map_item)
        self._layout_add_north_arrow(layout, 270, 30, 14, 24, page=0)
        self._layout_add_scale_bar(layout, map_item, 18, 192, 80, 10, page=0)

        center = extent.center()
        coord_lines = [
            f"Scale 1:{int(round(map_item.scale())):,}".replace(",", "."),
            f"Center X: {self._format_layout_coord(center.x(), geographic)}",
            f"Center Y: {self._format_layout_coord(center.y(), geographic)}",
            f"X min/max: {self._format_layout_coord(extent.xMinimum(), geographic)} / {self._format_layout_coord(extent.xMaximum(), geographic)}",
            f"Y min/max: {self._format_layout_coord(extent.yMinimum(), geographic)} / {self._format_layout_coord(extent.yMaximum(), geographic)}",
        ]
        sampling_note = "Campionamento: quote lette dal dato sorgente selezionato."
        if self.current_mode == "sections":
            sampling_note = "Campionamento: sezioni e volumi letti dal DEM/DTM selezionato."

        self._layout_add_text_box(
            layout,
            f"{self._current_report_title()}\n{project_name}",
            12, 8, 278, 16,
            title=True,
            font_size=11,
            background="#111827",
            foreground="#f8fafc",
            page=0,
        )
        self._layout_add_text_box(
            layout,
            self._layout_wrapped_text([
                f"Data / Date: {now_str}",
                f"Sorgente / Source: {source_label}",
                sampling_note,
            ], width=34),
            296, 24, 112, 38,
            font_size=8,
            background="#172033",
            page=0,
        )
        self._layout_add_text_box(
            layout,
            self._layout_wrapped_text([
                "CRS e misure",
                f"Project CRS: {project_crs}",
                f"Data CRS: {data_crs}",
                *notes,
            ], width=34),
            296, 66, 112, 64,
            font_size=7.5,
            background="#1a2235",
            page=0,
        )
        self._layout_add_text_box(
            layout,
            self._layout_wrapped_text([
                "Coordinate e inquadramento",
                *coord_lines,
            ], width=34),
            296, 134, 112, 48,
            font_size=7.5,
            background="#132235",
            page=0,
        )
        legend = self._layout_add_legend(
            layout,
            map_item,
            296, 186, 112, 40,
            page=0,
            title="Legenda layer",
        )
        layers_y = 230 if legend else 186
        layers_h = 48 if legend else 92
        self._layout_add_text_box(
            layout,
            self._layout_wrapped_text([
                "Layer in mappa",
                *layers,
            ], width=34),
            296, layers_y, 112, layers_h,
            font_size=7.2,
            background="#0f172a",
            page=0,
        )
        self._layout_add_text_box(
            layout,
            self._layout_wrapped_text([
                "Mappa tecnica",
                "Reticolo, freccia nord, scala grafica e coordinate del riquadro sono allineati al CRS di progetto.",
                "I layer vettoriali generati devono restare sopra il raster nella composizione finale.",
            ], width=76),
            12, 206, 278, 72,
            font_size=7.8,
            background="#101824",
            page=0,
        )
        self._layout_add_page_footer(
            layout,
            0,
            f"Pagina 1/{total_pages} · Inquadramento cartografico e cartiglio tecnico",
        )

        self._layout_ensure_page(layout, 1).setPageSize(self._layout_page_size())
        if not svg_path and svg_content:
            svg_path = self._write_svg_asset(asset_dir or self._default_output_dir(), "chart_overview", svg_content)
        chart_rows = self._profile_report_rows()
        chart_title = "Tabella attributi profilo"
        if self.current_mode == "sections" and self.last_cross_sections_data:
            chart_rows = self._section_overview_rows(self.last_cross_sections_data)
            chart_title = "Tabella attributi generale sezioni"
        summary_table_path = self._write_svg_asset(
            asset_dir or self._default_output_dir(),
            "summary_attributes",
            self._layout_table_svg(chart_title, chart_rows, width=1120),
        )
        self._layout_add_text_box(
            layout,
            f"{self._current_report_title()}\nGrafico generale e tabella attributi",
            12, 10, 396, 20,
            title=True,
            font_size=11,
            background="#111827",
            foreground="#f8fafc",
            page=1,
        )
        self._layout_add_picture_box(
            layout,
            svg_path,
            12, 32, 396, 114,
            page=1,
            frame=True,
            background="#0f172a",
            stroke="#0f172a",
        )
        self._layout_add_picture_box(
            layout,
            summary_table_path,
            12, 152, 396, 126,
            page=1,
            frame=False,
            background=None,
            stroke=None,
        )
        self._layout_add_page_footer(
            layout,
            1,
            f"Pagina 2/{total_pages} · Grafico generale e attributi",
        )

        if self.current_mode == "sections" and self.last_cross_sections_data:
            data = self.last_cross_sections_data
            for offset, sec in enumerate(data.get("sections") or [], start=2):
                self._layout_ensure_page(layout, offset).setPageSize(self._layout_page_size())
                sec_num = int(sec.get("index") or 0)
                section_svg_path = self._write_svg_asset(
                    asset_dir or self._default_output_dir(),
                    f"section_{sec_num:03d}_graph",
                    self._section_detail_svg(sec, 1280, 460),
                )
                section_table_path = self._write_svg_asset(
                    asset_dir or self._default_output_dir(),
                    f"section_{sec_num:03d}_attributes",
                    self._layout_table_svg(
                        f"Sezione {sec_num:02d} · Tabella attributi",
                        self._section_report_rows(sec),
                        width=1120,
                    ),
                )
                related_volumes = []
                for volume in data.get("volumes") or []:
                    if volume.get("from_section") == sec_num or volume.get("to_section") == sec_num:
                        related_volumes.append(
                            "Tratta {0}->{1}: V={2:.2f} m3 | Sterro={3:.2f} m3 | Riporto={4:.2f} m3".format(
                                int(volume.get("from_section") or 0),
                                int(volume.get("to_section") or 0),
                                float(volume.get("volume_m3") or 0.0),
                                float(volume.get("cut_m3") or 0.0),
                                float(volume.get("fill_m3") or 0.0),
                            )
                        )
                related_text = "; ".join(related_volumes[:2]) if related_volumes else "n/d"
                footer_note = self._layout_wrapped_text([
                    "Sorgente quote: {0} | CRS progetto: {1} | CRS dato: {2}".format(
                        source_label,
                        project_crs,
                        data_crs,
                    ),
                    "Relazioni volumetriche: {0}".format(related_text),
                ], width=112)
                self._layout_add_text_box(
                    layout,
                    "Sezione {0:02d} · Progressiva {1:.2f} m".format(
                        sec_num,
                        float(sec.get("progressive_m") or 0.0),
                    ),
                    12, 12, 396, 14,
                    title=True,
                    font_size=11,
                    background="#111827",
                    foreground="#f8fafc",
                    page=offset,
                )
                self._layout_add_picture_box(
                    layout,
                    section_svg_path,
                    12, 32, 396, 104,
                    page=offset,
                    frame=True,
                    background="#0f172a",
                    stroke="#0f172a",
                )
                self._layout_add_picture_box(
                    layout,
                    section_table_path,
                    12, 142, 396, 108,
                    page=offset,
                    frame=False,
                    background=None,
                    stroke=None,
                )
                self._layout_add_text_box(
                    layout,
                    footer_note,
                    12, 254, 396, 24,
                    font_size=7.2,
                    background="#101824",
                    page=offset,
                )
                self._layout_add_page_footer(
                    layout,
                    offset,
                    f"Pagina {offset + 1}/{total_pages} · Sezione {sec_num:02d} con grafico e tabella attributi",
                )
        return layout

    def _render_svg_to_image(self, svg_content, *, fallback_size, min_size=None, scale=1.0,
                             background="#12151e"):
        if not svg_content:
            raise ValueError("Contenuto SVG mancante / Missing SVG content.")

        from qgis.PyQt.QtCore import QByteArray, QSize as QtSize
        from qgis.PyQt.QtGui import QImage, QPainter, QColor
        from qgis.PyQt.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QByteArray(svg_content.encode("utf-8")))
        if not renderer.isValid():
            raise ValueError("SVG non valido / Invalid SVG content.")

        default_size = renderer.defaultSize()
        if default_size.isEmpty():
            default_size = QtSize(*fallback_size)

        width = int(default_size.width() * scale)
        height = int(default_size.height() * scale)
        if min_size:
            width = max(min_size[0], width)
            height = max(min_size[1], height)

        image = QImage(
            width,
            height,
            compat_enum(QImage, "Format_ARGB32", "Format"),
        )
        image.fill(QColor(background))

        painter = QPainter(image)
        try:
            antialiasing = compat_enum(QPainter, "Antialiasing", "RenderHint", default=None)
            if antialiasing is not None:
                painter.setRenderHint(antialiasing)
            renderer.render(painter)
        finally:
            painter.end()
        return image

    def _svg_to_png(self, svg_content, name):
        if not svg_content:
            return None
        try:
            image = self._render_svg_to_image(
                svg_content,
                fallback_size=(1700, 1100),
                min_size=(900, 520),
                background="#12151e",
            )
            chart_dir = os.path.join(self._default_output_dir(), "_charts")
            os.makedirs(chart_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(chart_dir, f"{name}_{stamp}.png")
            if not image.save(path, "PNG"):
                raise IOError(f"Impossibile salvare il PNG: {path}")
            return path
        except Exception as exc:
            self._push("Grafico / Chart", f"PNG grafico non creato: {exc}", "Warning")
            return None

    def _write_vector_package(self, layers, gpkg_path):
        if not layers:
            raise ValueError("No vector layers available.")
        if not gpkg_path.lower().endswith(".gpkg"):
            gpkg_path += ".gpkg"

        from qgis.core import QgsVectorFileWriter

        for idx, layer in enumerate(layers):
            layer_name = layer.name().lower().replace(" ", "_").replace("—", "_")
            try:
                options = QgsVectorFileWriter.SaveVectorOptions()
                options.driverName = "GPKG"
                options.fileEncoding = "UTF-8"
                options.layerName = layer_name
                if idx == 0:
                    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
                else:
                    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
                writer = getattr(QgsVectorFileWriter, "writeAsVectorFormatV3", None)
                if writer is None:
                    writer = getattr(QgsVectorFileWriter, "writeAsVectorFormatV2")
                result = writer(layer, gpkg_path, QgsProject.instance().transformContext(), options)
                err_code = result[0] if isinstance(result, tuple) else result
                if err_code != QgsVectorFileWriter.NoError:
                    msg = result[1] if isinstance(result, tuple) and len(result) > 1 else str(err_code)
                    raise RuntimeError(msg)
            except AttributeError:
                err = QgsVectorFileWriter.writeAsVectorFormat(
                    layer, gpkg_path, "UTF-8", layer.crs(), "GPKG",
                    layerOptions=[f"TABLE={layer_name}"]
                )
                if isinstance(err, tuple):
                    err = err[0]
                if err != QgsVectorFileWriter.NoError:
                    raise RuntimeError(f"Vector export failed for {layer.name()}: {err}")
        return gpkg_path

    def _register_vector_outputs(self, layers, prefix):
        self.last_vector_layers = [layer for layer in layers if layer and layer.isValid()]
        if not self.last_vector_layers:
            self.last_vector_gpkg = None
            return None
        gpkg_path = self._new_output_gpkg_path(prefix)
        self.last_vector_gpkg = self._write_vector_package(self.last_vector_layers, gpkg_path)
        return self.last_vector_gpkg

    def _enable_labels(self, layer, field_name="label", color="#f1f5f9", size=8,
                       placement=None, allow_overlaps=False):
        try:
            from qgis.PyQt.QtGui import QColor
            from qgis.core import (
                QgsPalLayerSettings,
                QgsTextBufferSettings,
                QgsTextFormat,
                QgsVectorLayerSimpleLabeling,
            )

            settings = QgsPalLayerSettings()
            settings.fieldName = field_name
            if placement is not None:
                try:
                    settings.placement = placement
                except Exception:
                    pass
            if allow_overlaps:
                for attr_name, attr_value in (
                        ("displayAll", True),
                        ("obstacle", False),
                ):
                    try:
                        setattr(settings, attr_name, attr_value)
                    except Exception:
                        pass
            fmt = QgsTextFormat()
            fmt.setSize(size)
            fmt.setColor(QColor(color))
            buffer = QgsTextBufferSettings()
            buffer.setEnabled(True)
            buffer.setSize(1.0)
            buffer.setColor(QColor("#12151e"))
            fmt.setBuffer(buffer)
            settings.setFormat(fmt)
            layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            layer.setLabelsEnabled(True)
            layer.triggerRepaint()
        except Exception:
            pass

    FIELD_ALIASES = {
        "id": ("ID", "ID"),
        "id_curva": ("ID curva", "Curve ID"),
        "sezione": ("Sezione", "Section"),
        "da_sez": ("Da sezione", "From section"),
        "a_sez": ("A sezione", "To section"),
        "length_m": ("Lunghezza (m)", "Length (m)"),
        "prog_m": ("Progressiva (m)", "Progressive (m)"),
        "parziale_m": ("Parziale (m)", "Partial (m)"),
        "offset_m": ("Offset (m)", "Offset (m)"),
        "dist_m": ("Distanza (m)", "Distance (m)"),
        "quota_m": ("Quota (m)", "Elevation (m)"),
        "quota": ("Quota (m)", "Elevation (m)"),
        "quota_min": ("Quota min (m)", "Min elevation (m)"),
        "quota_max": ("Quota max (m)", "Max elevation (m)"),
        "quota_prj": ("Quota progetto (m)", "Design elevation (m)"),
        "lon": ("Longitudine", "Longitude"),
        "lat": ("Latitudine", "Latitude"),
        "area": ("Area", "Area"),
        "area_m2": ("Area (m²)", "Area (m²)"),
        "cut_m2": ("Sterro (m²)", "Cut (m²)"),
        "fill_m2": ("Riporto (m²)", "Fill (m²)"),
        "volume_m3": ("Volume (m³)", "Volume (m³)"),
        "sterro_m3": ("Sterro (m³)", "Cut (m³)"),
        "riporto_m3": ("Riporto (m³)", "Fill (m³)"),
        "cum_sterro": ("Sterro cumulato (m³)", "Cumulative cut (m³)"),
        "cum_riporto": ("Riporto cumulato (m³)", "Cumulative fill (m³)"),
        "tipo": ("Tipo", "Type"),
        "colore": ("Colore", "Color"),
        "mode": ("Modalità", "Mode"),
        "value": ("Valore", "Value"),
        "label": ("Etichetta", "Label"),
        "chart_png": ("Grafico PNG", "Chart PNG"),
        "name": ("Nome", "Name"),
        "display_name": ("Nome esteso", "Full name"),
    }

    def _apply_field_aliases(self, layer):
        """Set bilingual (IT/EN) display aliases on a generated layer's fields,
        following the language currently selected in the dialog."""
        lang = getattr(self.dialog, "lang", "it") if getattr(self, "dialog", None) else "it"
        fields = layer.fields()
        for idx, field in enumerate(fields):
            alias_it, alias_en = self.FIELD_ALIASES.get(field.name(), (None, None))
            if alias_it is None:
                continue
            layer.setFieldAlias(idx, alias_en if lang == "en" else alias_it)

    def _style_line_layer(self, layer, color, width=0.7, label_field=None):
        try:
            from qgis.core import QgsLineSymbol, QgsSingleSymbolRenderer
            symbol = QgsLineSymbol.createSimple({
                "color": color,
                "width": str(width),
            })
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            if label_field:
                self._enable_labels(layer, label_field)
            layer.triggerRepaint()
        except Exception:
            pass

    def _style_marker_layer(self, layer, color, size=2.2, label_field=None):
        try:
            from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer
            symbol = QgsMarkerSymbol.createSimple({
                "name": "circle",
                "color": color,
                "outline_color": "#12151e",
                "outline_width": "0.2",
                "size": str(size),
            })
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            if label_field:
                self._enable_labels(layer, label_field)
            layer.triggerRepaint()
        except Exception:
            pass

    def _style_section_label_layer(self, layer):
        try:
            from qgis.core import QgsMarkerSymbol, QgsPalLayerSettings, QgsSingleSymbolRenderer

            symbol = QgsMarkerSymbol.createSimple({
                "name": "circle",
                "color": "0,0,0,0",
                "outline_color": "0,0,0,0",
                "size": "0.4",
            })
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            placement = compat_enum(
                QgsPalLayerSettings,
                "OverPoint",
                "Placement",
                default=None,
            )
            self._enable_labels(
                layer,
                "label",
                "#f8fafc",
                8.5,
                placement=placement,
                allow_overlaps=True,
            )
            layer.triggerRepaint()
        except Exception:
            self._enable_labels(layer, "label", "#f8fafc", 8.5)

    def _style_interpolated_raster_layer(self, layer, min_value, max_value, mode):
        try:
            from qgis.PyQt.QtGui import QColor
            from qgis.core import (
                QgsColorRampShader,
                QgsRasterShader,
                QgsSingleBandPseudoColorRenderer,
            )

            if not layer or not layer.isValid():
                return

            mn = float(min_value)
            mx = float(max_value)
            if not math.isfinite(mn) or not math.isfinite(mx):
                return
            if math.isclose(mn, mx, rel_tol=0.0, abs_tol=1e-9):
                mx = mn + 1.0

            shader = QgsRasterShader()
            color_shader = QgsColorRampShader()
            color_shader.setColorRampType(
                compat_enum(QgsColorRampShader, "Interpolated", "ColorRampType")
            )

            if mode == "delta":
                span = max(abs(mn), abs(mx), 0.01)
                items = [
                    QgsColorRampShader.ColorRampItem(-span, QColor("#15803d"), "Riporto / Fill"),
                    QgsColorRampShader.ColorRampItem(0.0, QColor("#f8fafc"), "Zero"),
                    QgsColorRampShader.ColorRampItem(span, QColor("#b91c1c"), "Sterro / Cut"),
                ]
                layer.setOpacity(0.76)
            else:
                mid = mn + (mx - mn) * 0.5
                items = [
                    QgsColorRampShader.ColorRampItem(mn, QColor("#1d4ed8"), "Min"),
                    QgsColorRampShader.ColorRampItem(mid, QColor("#f59e0b"), "Med"),
                    QgsColorRampShader.ColorRampItem(mx, QColor("#7c2d12"), "Max"),
                ]
                layer.setOpacity(0.72)

            color_shader.setColorRampItemList(items)
            shader.setRasterShaderFunction(color_shader)
            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:
            pass

    def _field_value(self, feature, field_name, default=None):
        try:
            if feature is None or feature.fields().indexOf(field_name) < 0:
                return default
            value = feature[field_name]
            return default if value in (None, "") else value
        except Exception:
            return default

    def _format_feature_value(self, value):
        if value in (None, ""):
            return "—"
        if isinstance(value, float):
            return f"{value:.3f}".rstrip("0").rstrip(".")
        return str(value)

    def _build_section_popup_html(self, layer, feature):
        chart_html = ""

        section_idx = self._field_value(feature, "sezione")
        progressive = self._field_value(feature, "prog_m")
        label = self._field_value(feature, "label")
        title = label or (
            f"Sezione {int(section_idx):02d}" if section_idx not in (None, "") else layer.name()
        )

        section_rows = ""
        if section_idx is not None and self.last_cross_sections_data:
            try:
                section_idx_int = int(section_idx)
                sec = next(
                    (s for s in self.last_cross_sections_data.get("sections", []) if int(s.get("index") or 0) == section_idx_int),
                    None,
                )
                if sec:
                    for row_label, row_value in (
                            ("Sezione", sec.get("index")),
                            ("Progressiva (m)", sec.get("progressive_m")),
                            ("Quota min (m)", sec.get("min_elevation")),
                            ("Quota max (m)", sec.get("max_elevation")),
                            ("Quota progetto (m)", sec.get("design_elevation")),
                            ("Area (m²)", sec.get("area_m2")),
                            ("Sterro (m²)", sec.get("cut_area_m2")),
                            ("Riporto (m²)", sec.get("fill_area_m2")),
                    ):
                        section_rows += (
                            f"<tr><th>{html.escape(str(row_label))}</th>"
                            f"<td>{html.escape(self._format_feature_value(row_value))}</td></tr>"
                        )
                    chart_html = (
                        "<div style='margin:0 0 12px;'>"
                        f"{self._section_detail_svg(sec, 680, 240)}"
                        "</div>"
                    )
            except Exception:
                section_rows = ""

        attr_rows = ""
        for field in feature.fields():
            name = field.name()
            if name == "chart_png":
                continue
            value = self._field_value(feature, name)
            if value in (None, ""):
                continue
            attr_rows += (
                f"<tr><th>{html.escape(name)}</th>"
                f"<td>{html.escape(self._format_feature_value(value))}</td></tr>"
            )

        subtitle = []
        if section_idx not in (None, ""):
            subtitle.append(f"Sezione {int(section_idx):02d}")
        if progressive not in (None, ""):
            subtitle.append(f"Prog. {self._format_feature_value(progressive)} m")
        subtitle_html = " · ".join(html.escape(s) for s in subtitle)

        section_table = (
            "<div style='margin:0 0 12px;'><strong style='color:#f1f5f9;'>Riepilogo sezione</strong>"
            "<table style='width:100%;border-collapse:collapse;margin-top:8px;'>"
            f"{section_rows}</table></div>"
        ) if section_rows else ""
        attr_table = (
            "<div><strong style='color:#f1f5f9;'>Attributi feature</strong>"
            "<table style='width:100%;border-collapse:collapse;margin-top:8px;'>"
            f"{attr_rows}</table></div>"
        ) if attr_rows else ""

        return f"""
        <html>
        <head>
        <style>
          body {{ background:#12151e; color:#e2e8f0; font-family:'Segoe UI',Arial,sans-serif;
                 font-size:12px; margin:14px; line-height:1.55; }}
          h2 {{ margin:0 0 4px; color:#f8fafc; font-size:18px; }}
          .sub {{ color:#8ba3c7; margin:0 0 12px; }}
          table {{ width:100%; border-collapse:collapse; }}
          th {{ width:42%; text-align:left; padding:6px 8px; color:#93c5fd;
               border-bottom:1px solid #2d3757; vertical-align:top; }}
          td {{ padding:6px 8px; border-bottom:1px solid #2d3757; color:#e2e8f0; }}
        </style>
        </head>
        <body>
          <h2>{html.escape(title)}</h2>
          <div class="sub">{subtitle_html}</div>
          {chart_html}
          {section_table}
          {attr_table}
        </body>
        </html>
        """

    def _show_section_feature_popup(self, layer, feature):
        if feature is None:
            return
        if self._active_section_popup is not None:
            try:
                self._active_section_popup.close()
            except Exception:
                pass
            self._active_section_popup = None

        dialog = QDialog(self.dialog or self.iface.mainWindow())
        dialog.setWindowTitle(layer.name())
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        browser.setHtml(self._build_section_popup_html(layer, feature))
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.finished.connect(lambda _res: setattr(self, "_active_section_popup", None))
        self._active_section_popup = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_section_layer_selection_changed(self, layer, selected, _deselected, _clear_and_select):
        if not selected:
            return
        try:
            from qgis.core import QgsFeatureRequest
            request = QgsFeatureRequest()
            request.setFilterFids(list(selected))
            feature = next(layer.getFeatures(request), None)
        except Exception:
            feature = None
        if feature is not None:
            self._show_section_feature_popup(layer, feature)

    def _connect_section_popup(self, layer):
        if not layer or not layer.isValid() or layer.id() in self._popup_connected_layer_ids:
            return
        try:
            if layer.fields().indexOf("sezione") < 0:
                return
        except Exception:
            return
        layer.selectionChanged.connect(
            lambda selected, deselected, clear_and_select, layer=layer:
            self._on_section_layer_selection_changed(layer, selected, deselected, clear_and_select)
        )
        self._popup_connected_layer_ids.add(layer.id())

    def _create_section_interpolated_raster(self, data, raster_layer, group=None):
        sections = data.get("sections") or []
        if not raster_layer or not sections:
            return None, None

        try:
            from osgeo import gdal
            from qgis.core import (
                QgsCoordinateReferenceSystem,
                QgsCoordinateTransform,
                QgsFeature,
                QgsGeometry,
                QgsRasterLayer,
                QgsRectangle,
                QgsVectorLayer,
            )
        except Exception as exc:
            self._push(
                "Raster interpolato / Interpolated raster",
                f"Interpolazione non disponibile: {exc}",
                "Warning",
            )
            return None, None
        try:
            crs_target = raster_layer.crs()
            if crs_target and crs_target.isValid() and crs_target.isGeographic():
                self._push(
                    "Raster interpolato / Interpolated raster",
                    "Raster corridoio non creato: usare un DEM in CRS proiettato metrico.",
                    "Warning",
                )
                return None, None
            crs_auth = crs_target.authid() or "EPSG:4326"
            crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
            project = QgsProject.instance()
            try:
                xform_from_wgs = QgsCoordinateTransform(
                    crs_wgs, crs_target, project.transformContext()
                )
            except Exception:
                xform_from_wgs = QgsCoordinateTransform(crs_wgs, crs_target, project)

            has_design = all(
                sec.get("design_elevation") is not None
                for sec in sections
                if len([p for p in sec.get("points", []) if p.get("elevation") is not None]) >= 2
            )
            raster_mode = "delta" if has_design else "ground"
            raster_title = (
                "Sterro/Riporto interpolato / Interpolated cut-fill"
                if has_design else
                "Quote interpolate / Interpolated elevations"
            )

            pts_layer = QgsVectorLayer(
                f"Point?crs={crs_auth}&field=sezione:integer&field=offset_m:double&field=value:double",
                "corridor_samples",
                "memory",
            )
            corridor_layer = QgsVectorLayer(
                f"Polygon?crs={crs_auth}&field=mode:string(16)",
                "corridor_mask",
                "memory",
            )
            pr_pts = pts_layer.dataProvider()
            pr_mask = corridor_layer.dataProvider()

            feats_pts = []
            values = []
            left_edge = []
            right_edge = []
            sample_spacing = None

            for sec in sections:
                sec_pts = [
                    p for p in sec.get("points", [])
                    if p.get("elevation") is not None and p.get("lon") is not None and p.get("lat") is not None
                ]
                if len(sec_pts) < 2:
                    continue
                sec_pts = sorted(sec_pts, key=lambda p: float(p.get("offset_m") or 0.0))
                if sample_spacing is None and len(sec_pts) >= 2:
                    try:
                        sample_spacing = abs(
                            float(sec_pts[1].get("offset_m") or 0.0) -
                            float(sec_pts[0].get("offset_m") or 0.0)
                        )
                    except Exception:
                        sample_spacing = None

                left_pt = xform_from_wgs.transform(QgsPointXY(sec_pts[0]["lon"], sec_pts[0]["lat"]))
                right_pt = xform_from_wgs.transform(QgsPointXY(sec_pts[-1]["lon"], sec_pts[-1]["lat"]))
                left_edge.append(QgsPointXY(left_pt))
                right_edge.append(QgsPointXY(right_pt))

                design_elev = sec.get("design_elevation")
                for p in sec_pts:
                    pt_map = xform_from_wgs.transform(QgsPointXY(p["lon"], p["lat"]))
                    value = float(p["elevation"])
                    if has_design and design_elev is not None:
                        value -= float(design_elev)
                    f_pt = QgsFeature()
                    f_pt.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pt_map)))
                    f_pt.setAttributes([
                        int(sec.get("index") or 0),
                        float(p.get("offset_m") or 0.0),
                        value,
                    ])
                    feats_pts.append(f_pt)
                    values.append(value)

            if len(feats_pts) < 6 or len(left_edge) < 2 or len(right_edge) < 2:
                return None, None

            pr_pts.addFeatures(feats_pts)
            pts_layer.updateExtents()

            ring = left_edge + list(reversed(right_edge))
            if len(ring) < 4:
                return None, None
            ring.append(left_edge[0])
            mask_geom = QgsGeometry.fromPolygonXY([ring])
            if mask_geom is None or mask_geom.isEmpty():
                return None, None
            try:
                if not mask_geom.isGeosValid():
                    mask_geom = mask_geom.makeValid()
            except Exception:
                pass
            if mask_geom is None or mask_geom.isEmpty():
                return None, None

            f_mask = QgsFeature()
            f_mask.setGeometry(mask_geom)
            f_mask.setAttributes([raster_mode])
            pr_mask.addFeature(f_mask)
            corridor_layer.updateExtents()

            extent = QgsRectangle(mask_geom.boundingBox())
            pixel_size = min(
                abs(float(getattr(raster_layer, "rasterUnitsPerPixelX", lambda: 0.0)() or 0.0)),
                abs(float(getattr(raster_layer, "rasterUnitsPerPixelY", lambda: 0.0)() or 0.0)),
            )
            if pixel_size <= 0 or not math.isfinite(pixel_size):
                interval = float(data.get("interval_m") or 25.0)
                width_step = float(sample_spacing or 5.0)
                pixel_size = max(min(interval, width_step) / 2.0, 0.5)
            pad = pixel_size * 2.0
            extent.setXMinimum(extent.xMinimum() - pad)
            extent.setXMaximum(extent.xMaximum() + pad)
            extent.setYMinimum(extent.yMinimum() - pad)
            extent.setYMaximum(extent.yMaximum() + pad)

            cols = max(16, int(math.ceil(extent.width() / pixel_size)))
            rows = max(16, int(math.ceil(extent.height() / pixel_size)))

            output_path = self._new_output_raster_path("sezioni_raster_interpolato")
            tmp_dir = tempfile.mkdtemp(prefix="profili_sezioni_interp_")
            tmp_gpkg = os.path.join(tmp_dir, "corridor_inputs.gpkg")
            tmp_grid = os.path.join(tmp_dir, "corridor_grid.tif")

            try:
                gdal.UseExceptions()
                self._write_vector_package([pts_layer, corridor_layer], tmp_gpkg)
                gdal.Grid(
                    tmp_grid,
                    tmp_gpkg,
                    options=gdal.GridOptions(
                        format="GTiff",
                        outputType=gdal.GDT_Float32,
                        noData=-9999.0,
                        width=cols,
                        height=rows,
                        outputBounds=[
                            extent.xMinimum(),
                            extent.yMinimum(),
                            extent.xMaximum(),
                            extent.yMaximum(),
                        ],
                        layers=["corridor_samples"],
                        zfield="value",
                        algorithm="invdist:power=2.0:smoothing=0.0:nodata=-9999.0",
                        creationOptions=["COMPRESS=LZW"],
                    ),
                )
                gdal.Warp(
                    output_path,
                    tmp_grid,
                    options=gdal.WarpOptions(
                        format="GTiff",
                        cutlineDSName=tmp_gpkg,
                        cutlineLayer="corridor_mask",
                        cropToCutline=True,
                        dstNodata=-9999.0,
                        multithread=True,
                        creationOptions=["COMPRESS=LZW"],
                    ),
                )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            raster_out = QgsRasterLayer(output_path, raster_title)
            if not raster_out.isValid():
                return None, None

            self._style_interpolated_raster_layer(raster_out, min(values), max(values), raster_mode)
            if group is None:
                group = self._new_output_group("Sezioni e volumi / Sections and volumes")
            self._add_output_layer(
                raster_out,
                group,
                "10 Raster interpolato / Interpolated raster",
            )
            self.last_interpolated_raster_path = output_path
            return raster_out, raster_mode
        except Exception as exc:
            self._push(
                "Raster interpolato / Interpolated raster",
                f"Output opzionale non creato: {exc}",
                "Warning",
            )
            self.last_interpolated_raster_path = None
            return None, None

    def _style_section_polygons(self, layer):
        try:
            from qgis.core import QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsRendererCategory
            categories = []
            for value, label, fill, outline in (
                    ("sterro", "Sterro / Cut", "239,68,68,105", "#ef4444"),
                    ("riporto", "Riporto / Fill", "34,197,94,105", "#22c55e"),
                    ("sezione", "Area sezione / Section area", "79,115,196,55", "#4f73c4")):
                symbol = QgsFillSymbol.createSimple({
                    "color": fill,
                    "outline_color": outline,
                    "outline_width": "0.25",
                })
                categories.append(QgsRendererCategory(value, symbol, label))
            layer.setRenderer(QgsCategorizedSymbolRenderer("tipo", categories))
            self._enable_labels(layer, "label", "#f1f5f9", 7)
            layer.triggerRepaint()
        except Exception:
            pass

    def _style_section_lines(self, layer):
        try:
            from qgis.core import QgsCategorizedSymbolRenderer, QgsLineSymbol, QgsRendererCategory
            categories = []
            for value, label, color, width in (
                    ("terreno", "Terreno / Ground", "#34d399", "0.65"),
                    ("progetto", "Progetto / Design", "#f59e0b", "0.55")):
                symbol = QgsLineSymbol.createSimple({
                    "color": color,
                    "width": width,
                })
                categories.append(QgsRendererCategory(value, symbol, label))
            layer.setRenderer(QgsCategorizedSymbolRenderer("tipo", categories))
            self._enable_labels(layer, "label", "#f1f5f9", 7)
            layer.triggerRepaint()
        except Exception:
            self._style_line_layer(layer, "#34d399", 0.55, "label")

    # ──────────────────────────────────────────────────────────────
    # run
    # ──────────────────────────────────────────────────────────────

    def run(self):
        if not self.dialog:
            self.dialog = ProfiliSezioniComuniDialog(self.iface.mainWindow())
            # Tab Profilo
            self.dialog.btn_draw_prof.clicked.connect(
                lambda: self._start_drawing("profile")
            )
            # Tab Sezioni
            self.dialog.btn_draw_sez.clicked.connect(
                lambda: self._start_drawing("sections")
            )
            self.dialog.btn_layer_sez.clicked.connect(self._run_sections_from_layer)
            # Tab Comuni
            self.dialog.btn_comuni_download_source.clicked.connect(
                lambda: self._open_url(ISTAT_ADMIN_BOUNDARIES_PAGE)
            )
            # Tab Download Raster
            self.dialog.btn_draw_area.clicked.connect(self._start_area_drawing)
            self.dialog.btn_browse_download_output.clicked.connect(self._browse_download_output)
            self.dialog.btn_open_raster_source.clicked.connect(self._open_raster_source)
            self.dialog.btn_add_tinitaly_wcs.clicked.connect(self._add_tinitaly_wcs)
            self.dialog.btn_download_raster.clicked.connect(self._download_raster_area)
            self.dialog.btn_pick_insertion.clicked.connect(self._set_insertion_from_canvas_center)
            # Export
            self.dialog.btn_export_csv.clicked.connect(self._export_csv)
            self.dialog.btn_export_vectors.clicked.connect(self._export_vectors)
            self.dialog.btn_export_png.clicked.connect(self._export_png)
            self.dialog.btn_export_pdf.clicked.connect(self._export_pdf)
            self.dialog.btn_print_layout.clicked.connect(self._print_layout)

        self.dialog.populate_rasters()
        self.dialog.show()
        self.dialog.raise_()

    # ──────────────────────────────────────────────────────────────
    # Raster download area
    # ──────────────────────────────────────────────────────────────

    def _start_area_drawing(self):
        """Activate the Shift-drag rectangle tool for raster downloads."""
        self.dialog.hide()
        if not self.area_tool:
            self.area_tool = DrawRectangleAreaTool(self.iface.mapCanvas())
            self.area_tool.areaDrawn.connect(self._on_area_drawn)
        self.iface.mapCanvas().setMapTool(self.area_tool)
        self._push(
            "Download raster",
            "Tieni premuto SHIFT e trascina un rettangolo. / Hold SHIFT and drag a rectangle.",
        )

    def _on_area_drawn(self, points):
        self.iface.mapCanvas().unsetMapTool(self.area_tool)
        self.dialog.show()
        try:
            rect = bbox_wgs84_from_points(points)
            create_download_area_layer(points)
            self.dialog.set_download_area(points, bbox_text(rect))
            self.dialog.lbl_download_status.setText(
                "Area pronta per il download / Area ready for download."
            )
            self.dialog.tabs.setCurrentIndex(self.dialog.TAB_DOWNLOAD)
        except Exception as e:
            self._push("Errore / Error", str(e), "Critical")
            self.dialog.lbl_download_status.setText(f"Errore / Error: {e}")

    def _browse_download_output(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self.dialog,
            "Salva raster area / Save raster area",
            "",
            "GeoTIFF (*.tif *.tiff)",
        )
        if file_path:
            self.dialog.set_download_output_path(file_path)

    def _open_raster_source(self):
        source_key = self.dialog.get_download_source()
        meta = RASTER_SOURCES.get(source_key) or RASTER_SOURCES["tinitaly"]
        self._open_url(meta.get("url"))

    def _add_tinitaly_wcs(self):
        try:
            layer = tinitaly_wcs_layer()
            QgsProject.instance().addMapLayer(layer)
            self._push("TINITALY", "WCS TINITALY caricato / TINITALY WCS loaded.", "Success")
            self.dialog.lbl_download_status.setText("WCS TINITALY caricato in QGIS.")
        except Exception as e:
            msg = (
                f"{e}\n\n"
                "Il download area del plugin usera' comunque gli ZIP ufficiali TINITALY "
                "e non dipendera' dal WCS.\n"
                "The area download will use official TINITALY ZIP tiles and will not depend on WCS."
            )
            QMessageBox.warning(self.dialog, "TINITALY WCS", msg)
            self.dialog.lbl_download_status.setText(
                "WCS non disponibile; fallback ZIP ufficiali TINITALY attivo."
            )

    def _download_raster_area(self):
        if getattr(self.dialog, "lang", "it") == "en":
            self._push(
                "Errore / Error",
                "TINITALY and HR-DTM-5m only cover Italian territory; "
                "this download is disabled in English.",
                "Critical",
            )
            return
        area_points = self.dialog._download_area_points
        if not area_points:
            self._push("Errore / Error", "Disegna prima un'area / Draw an area first.", "Critical")
            return
        output_path = self.dialog.get_download_output_path()
        if not output_path:
            self._browse_download_output()
            output_path = self.dialog.get_download_output_path()
            if not output_path:
                return

        source_key = self.dialog.get_download_source()
        meta = RASTER_SOURCES.get(source_key) or RASTER_SOURCES["tinitaly"]
        if source_key == "hrdtm5m":
            msg = (
                "Il DTM Zenodo e' un dataset remoto di circa {size}; il plugin tentera' "
                "un ritaglio dell'area con GDAL/vsicurl. Continuare?\n\n"
                "Fonte: {url}\nLicenza: {license}"
            ).format(size=meta.get("size"), url=meta.get("url"), license=meta.get("license"))
            yes = compat_enum(QMessageBox, "Yes", "StandardButton")
            if QMessageBox.question(self.dialog, "Download HR-DTM-5m", msg) != yes:
                return

        self.dialog.progress.setRange(0, 100)
        self.dialog.progress.setValue(0)
        self.dialog.progress.setVisible(True)
        self.dialog.lbl_download_status.setText("Download/ritaglio in corso...")
        QApplication.processEvents()
        try:
            final_path, rect, layer = download_raster_area(
                source_key,
                area_points,
                output_path,
                self.dialog.chk_load_downloaded_raster.isChecked(),
                self._download_progress_callback,
            )
            if layer and layer.isValid():
                group = self._new_output_group("Raster download / Downloaded raster", priority="raster")
                self._add_output_layer(layer, group, "GeoTIFF")
            layer_msg = " e caricato in QGIS" if layer and layer.isValid() else ""
            self.dialog.lbl_download_status.setText(
                f"Raster salvato{layer_msg}: {final_path}"
            )
            self._push(
                "Download raster",
                f"Salvato / Saved: {final_path}. Fonte/licenza scritte nella ricevuta.",
                "Success",
            )
        except Exception as e:
            QMessageBox.critical(self.dialog, "Download raster", str(e))
            self.dialog.lbl_download_status.setText(f"Errore / Error: {e}")
        finally:
            self.dialog.progress.setVisible(False)
            self.dialog.progress.setRange(0, 0)

    def _set_insertion_from_canvas_center(self):
        center = self.iface.mapCanvas().extent().center()
        self.dialog.sb_insertion_x.setValue(center.x())
        self.dialog.sb_insertion_y.setValue(center.y())
        self.dialog.lbl_status.setText(
            "Punto di inserimento impostato dal centro mappa / Insertion point set from map center."
        )

    # ──────────────────────────────────────────────────────────────
    # Drawing
    # ──────────────────────────────────────────────────────────────

    def _start_drawing(self, mode):
        """Activate the polyline drawing map tool."""
        self.current_mode = mode

        if mode == "sections":
            raster = self.dialog.get_selected_raster(for_sezioni=True)
            if not raster:
                self._push(
                    "Errore / Error",
                    "Seleziona prima un layer Raster (DEM/DTM) / Select a Raster Layer first.",
                    "Critical",
                )
                return

        if mode == "profile":
            source = self.dialog.cb_source.currentData()
            if source == "raster":
                raster = self.dialog.get_selected_raster(for_sezioni=False)
                if not raster:
                    self._push(
                        "Errore / Error",
                        "Seleziona prima un layer Raster (DEM/DTM) / Select a Raster Layer first.",
                        "Critical",
                    )
                    return

        self.dialog.hide()

        if not self.map_tool:
            self.map_tool = DrawPolylineTool(self.iface.mapCanvas())
            self.map_tool.lineDrawn.connect(self._on_line_drawn)

        self.iface.mapCanvas().setMapTool(self.map_tool)
        self._push(
            "Info",
            "Clicca per aggiungere vertici. Doppio click per terminare. "
            "/ Click to add vertices. Double-click to finish.",
        )

    # ──────────────────────────────────────────────────────────────
    # Callback from map tool
    # ──────────────────────────────────────────────────────────────

    def _on_line_drawn(self, points):
        self.iface.mapCanvas().unsetMapTool(self.map_tool)
        self._last_drawn_points = points

        self.dialog.lbl_status.setText("Calcolo in corso... / Computing...")
        self.dialog.progress.setVisible(True)
        self.dialog.show()
        QApplication.processEvents()

        try:
            if self.current_mode == "sections":
                self._run_sections(points)
            else:
                self._run_profile(points)
                if self._ask_auto_sections_after_profile():
                    self.current_mode = "sections"
                    self._run_sections(points)
            self.dialog.enable_exports(True)
        except Exception as e:
            self._push("Errore / Error", str(e), "Critical")
            self.dialog.lbl_status.setText(f"Errore / Error: {e}")
        finally:
            self.dialog.progress.setVisible(False)

    def _ask_auto_sections_after_profile(self):
        source = self.dialog.cb_source.currentData()
        if source != "raster":
            return False
        raster = self.dialog.get_selected_raster(for_sezioni=True) or self.dialog.get_selected_raster(for_sezioni=False)
        if not raster:
            return False

        question = (
            "Hai tracciato il profilo. Vuoi calcolare subito anche le sezioni trasversali, "
            "gli sterri/riporti e i volumi usando lo stesso asse?\n\n"
            "You traced a profile. Do you also want to compute cross sections, cut/fill and volumes "
            "using the same axis?"
        )
        yes = compat_enum(QMessageBox, "Yes", "StandardButton")
        no = compat_enum(QMessageBox, "No", "StandardButton")
        answer = QMessageBox.question(
            self.dialog,
            "Profilo / Profile",
            question,
            yes | no,
            yes,
        )
        return answer == yes

    # ──────────────────────────────────────────────────────────────
    # Profile
    # ──────────────────────────────────────────────────────────────

    def _run_profile(self, points):
        source = self.dialog.cb_source.currentData() or "provider_openelev"
        raster_layer = (
            self.dialog.get_selected_raster(for_sezioni=False)
            if source == "raster" else None
        )
        samples_count = self.dialog.sb_samples_prof.value()

        points_data, total_dist = calculate_profile(
            points, source, raster_layer, samples_count
        )
        self.last_points_data = points_data
        self.last_total_dist = total_dist
        self.last_cross_sections_data = None

        raster_name = raster_layer.name() if raster_layer else ""
        source_labels = {
            "provider_openelev": "Open-Elevation API (SRTM/NASA)",
            "provider_opentopo": "OpenTopoData API (SRTM 90m)",
            "raster": raster_name or "DEM/DTM",
        }
        self.last_interpolated_raster_path = None
        labels = self.dialog.get_parameter_labels()
        svg = generate_profile_svg(points_data, total_dist, source, raster_name, labels)
        self.last_svg_content = svg
        chart_path = self._svg_to_png(svg, "profilo")
        self.last_chart_png_path = chart_path
        chart_html = self._chart_image_html(chart_path, svg)

        results_html = generate_profile_results_html(
            points_data,
            total_dist,
            source,
            raster_name,
            svg,
            labels,
            chart_html,
        )
        vector_layers = self._create_profile_vector_layers(points_data, total_dist, points, chart_path)
        self.last_report_context = self._build_measurement_context(
            source_labels.get(source, raster_name or "DEM/DTM"),
            raster_layer,
            vector_layers=vector_layers,
        )
        results_html = self.last_report_context["html"] + results_html
        self.last_results_html = results_html
        try:
            gpkg_path = self._register_vector_outputs(vector_layers, "profilo")
            if gpkg_path:
                results_html += (
                    "<div class='summary-card'><strong>GeoPackage vettoriale / Vector GeoPackage:</strong> "
                    f"{gpkg_path}</div>"
                )
                self.last_results_html = results_html
        except Exception as e:
            self._push("Export GPKG", f"GeoPackage non creato / not created: {e}", "Warning")
        self.dialog.show_results(results_html)

        dist_label = (
            f"{total_dist / 1000:.3f} km" if total_dist >= 1000 else f"{total_dist:.1f} m"
        )
        self.dialog.lbl_status.setText(
            f"Profilo calcolato / Profile calculated — {dist_label}, {len(points_data)} campioni"
        )

    def _create_profile_vector_layers(self, points_data, total_dist, axis_points, chart_path=None):
        from qgis.core import (
            QgsVectorLayer, QgsFeature, QgsGeometry,
            QgsCoordinateTransform, QgsCoordinateReferenceSystem,
        )

        project = QgsProject.instance()
        crs_auth = project.crs().authid() or "EPSG:4326"
        stamp = datetime.datetime.now().strftime("%H%M%S")
        group = self._new_output_group("Profilo altimetrico / Elevation profile")
        layers = []

        axis_layer = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=id:integer&field=length_m:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"profilo_asse_{stamp}",
            "memory",
        )
        self._apply_field_aliases(axis_layer)
        feat_axis = QgsFeature()
        feat_axis.setGeometry(QgsGeometry.fromPolylineXY(axis_points))
        feat_axis.setAttributes([1, float(total_dist or 0), "Asse profilo / Profile axis", chart_path or ""])
        axis_layer.dataProvider().addFeature(feat_axis)
        axis_layer.updateExtents()
        self._style_line_layer(axis_layer, "#f5a623", 0.9, "label")
        self._add_output_layer(axis_layer, group, "01 Asse / Axis")
        layers.append(axis_layer)

        samples_layer = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=id:integer&field=prog_m:double"
            f"&field=quota_m:double&field=lon:double&field=lat:double"
            f"&field=chart_png:string(254)",
            f"profilo_campioni_{stamp}",
            "memory",
        )
        self._apply_field_aliases(samples_layer)
        feats = []
        for idx, p in enumerate(points_data, 1):
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p.get("x", 0), p.get("y", 0))))
            f.setAttributes([
                idx,
                float(p.get("distance_m") or 0),
                float(p.get("elevation")) if p.get("elevation") is not None else None,
                float(p.get("lon") or 0),
                float(p.get("lat") or 0),
                chart_path or "",
            ])
            feats.append(f)
        if feats:
            samples_layer.dataProvider().addFeatures(feats)
            samples_layer.updateExtents()
            self._style_marker_layer(samples_layer, "#4f73c4", 1.7)
            self._add_output_layer(samples_layer, group, "02 Campioni / Samples")
            layers.append(samples_layer)

        pickets = build_pickets(points_data, total_dist)
        pickets_layer = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=id:string(16)&field=prog_m:double"
            f"&field=parziale_m:double&field=quota_m:double&field=lon:double&field=lat:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"profilo_picchetti_{stamp}",
            "memory",
        )
        self._apply_field_aliases(pickets_layer)
        crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        try:
            xform = QgsCoordinateTransform(crs_wgs, project.crs(), project.transformContext())
        except Exception:
            xform = QgsCoordinateTransform(crs_wgs, project.crs(), project)
        feats = []
        for p in pickets:
            pt = xform.transform(QgsPointXY(p.get("lon", 0), p.get("lat", 0)))
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(pt))
            f.setAttributes([
                p.get("id"),
                float(p.get("progressive_m") or 0),
                float(p.get("partial_m") or 0),
                float(p.get("elevation")) if p.get("elevation") is not None else None,
                float(p.get("lon") or 0),
                float(p.get("lat") or 0),
                "{0} {1:.1f} m".format(p.get("id"), float(p.get("progressive_m") or 0)),
                chart_path or "",
            ])
            feats.append(f)
        if feats:
            pickets_layer.dataProvider().addFeatures(feats)
            pickets_layer.updateExtents()
            self._style_marker_layer(pickets_layer, "#ffffff", 2.5, "label")
            self._add_output_layer(pickets_layer, group, "03 Picchetti / Pickets")
            layers.append(pickets_layer)

        return layers

    # ──────────────────────────────────────────────────────────────
    # Cross sections
    # ──────────────────────────────────────────────────────────────

    def _run_sections(self, points):
        raster_layer = self.dialog.get_selected_raster(for_sezioni=True)
        interval_m = self.dialog.sb_interval.value()
        half_width_m = self.dialog.sb_halfwidth.value()
        samples = self.dialog.sb_samples_sez.value()
        smooth = self.dialog.chk_smooth.isChecked()

        def _float_or_none(text):
            t = text.strip() if text else ""
            try:
                return float(t) if t else None
            except ValueError:
                return None

        design_start = _float_or_none(self.dialog.le_start_elev.text())
        design_end = _float_or_none(self.dialog.le_end_elev.text())
        design_grade = _float_or_none(self.dialog.le_grade_pct.text())

        data = calculate_cross_sections(
            points, raster_layer,
            interval_m, half_width_m, samples,
            design_start, design_end, design_grade, smooth,
        )
        self.last_cross_sections_data = data
        self.last_points_data = None

        project_title = QgsProject.instance().title() or QgsProject.instance().baseName() or "Project"
        created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.last_svg_content = generate_cross_sections_svg(data, project_title, created_at)
        chart_path = self._svg_to_png(self.last_svg_content, "sezioni")
        self.last_chart_png_path = chart_path
        chart_html = self._chart_image_html(chart_path, self.last_svg_content)

        labels = self.dialog.get_parameter_labels()
        results_html = (
            "<div class='summary-card'>"
            "<strong>Parametri movimento terra / Earthwork parameters</strong><br>"
            "{cut}: <strong>{cut_v:,.2f} m³</strong> &nbsp;·&nbsp; "
            "{fill}: <strong>{fill_v:,.2f} m³</strong> &nbsp;·&nbsp; "
            "{stockpile}: <strong>{balance:,.2f} m³</strong>"
            "</div>"
        ).format(
            cut=labels.get("cut", "Cut / Sterro"),
            fill=labels.get("fill", "Fill / Riporto"),
            stockpile=labels.get("stockpile", "Stockpiles / Accumuli"),
            cut_v=data.get("total_cut_m3") or 0,
            fill_v=data.get("total_fill_m3") or 0,
            balance=(data.get("total_cut_m3") or 0) - (data.get("total_fill_m3") or 0),
        )
        results_html += (
            "<div class='section-title'>Grafici sezioni / Section Charts</div>"
            f"<div class='chart-container'>{chart_html}</div>"
        )
        results_html += generate_cross_sections_results_html(data)
        self.last_results_html = results_html

        group = self._new_output_group("Sezioni e volumi / Sections and volumes")
        self.last_interpolated_raster_path = None
        vector_layers = self._create_vector_layers(data, points, chart_path, group=group)
        interpolated_raster, raster_mode = self._create_section_interpolated_raster(
            data,
            raster_layer,
            group=group,
        )
        if interpolated_raster and self.last_interpolated_raster_path:
            raster_label = (
                "Sterro/Riporto interpolato / Interpolated cut-fill"
                if raster_mode == "delta" else
                "Quote interpolate / Interpolated elevations"
            )
            results_html += (
                "<div class='summary-card'><strong>Raster corridoio / Corridor raster:</strong> "
                f"{raster_label}<br>{self.last_interpolated_raster_path}</div>"
            )
            self.last_results_html = results_html
        self.last_report_context = self._build_measurement_context(
            raster_layer.name() if raster_layer else "DEM/DTM",
            raster_layer,
            vector_layers=vector_layers,
        )
        results_html = self.last_report_context["html"] + results_html
        self.last_results_html = results_html
        try:
            gpkg_path = self._register_vector_outputs(vector_layers, "sezioni")
            if gpkg_path:
                results_html += (
                    "<div class='summary-card'><strong>GeoPackage vettoriale / Vector GeoPackage:</strong> "
                    f"{gpkg_path}</div>"
                )
                self.last_results_html = results_html
        except Exception as e:
            self._push("Export GPKG", f"GeoPackage non creato / not created: {e}", "Warning")
        self.dialog.show_results(results_html)

        self.dialog.lbl_status.setText(
            f"Sezioni calcolate / Sections calculated — "
            f"{data['n_sections']} sezioni su {data['alignment_length_m']:.1f} m"
        )

    # ──────────────────────────────────────────────────────────────
    # Sections from existing line layer
    # ──────────────────────────────────────────────────────────────

    def _run_sections_from_layer(self):
        raster_layer = self.dialog.get_selected_raster(for_sezioni=True)
        line_layer = self.dialog.get_selected_line_layer()
        if not raster_layer:
            self._push("Errore / Error", "Seleziona un layer Raster / Select a Raster layer.", "Critical")
            return
        if not line_layer:
            self._push("Errore / Error", "Seleziona un layer linea / Select a line layer.", "Critical")
            return

        self.current_mode = "sections"
        self.dialog.lbl_status.setText("Calcolo sezioni da layer... / Computing sections from layer...")
        self.dialog.progress.setVisible(True)
        QApplication.processEvents()
        try:
            feature_mode = self.dialog.cb_line_feature.currentData() or "first"
            points = self._line_points_from_layer(line_layer, feature_mode)
            self._last_drawn_points = points
            self._run_sections(points)
            self.dialog.enable_exports(True)
        except Exception as e:
            self._push("Errore / Error", str(e), "Critical")
            self.dialog.lbl_status.setText(f"Errore / Error: {e}")
        finally:
            self.dialog.progress.setVisible(False)

    def _line_points_from_layer(self, layer, feature_mode):
        from qgis.core import QgsCoordinateTransform
        features = list(layer.getFeatures())
        if not features:
            raise ValueError("The line layer contains no features.")

        def _geom_lines(feat):
            geom = feat.geometry()
            if not geom or geom.isEmpty():
                return []
            if geom.isMultipart():
                return geom.asMultiPolyline()
            line = geom.asPolyline()
            return [line] if line else []

        candidates = []
        for feat in features:
            for line in _geom_lines(feat):
                if len(line) >= 2:
                    candidates.append(line)
        if not candidates:
            raise ValueError("No valid line geometry found in the layer.")

        if feature_mode == "longest":
            line = max(candidates, key=lambda pts: sum(
                ((pts[i - 1].x() - pts[i].x()) ** 2 + (pts[i - 1].y() - pts[i].y()) ** 2) ** 0.5
                for i in range(1, len(pts))
            ))
        else:
            line = candidates[0]

        project_crs = QgsProject.instance().crs()
        if layer.crs() != project_crs:
            xform = QgsCoordinateTransform(
                layer.crs(), project_crs,
                QgsProject.instance().transformContext()
            )
            return [QgsPointXY(xform.transform(QgsPointXY(p))) for p in line]
        return [QgsPointXY(p) for p in line]

    # ──────────────────────────────────────────────────────────────
    # Vector layer creation (for cross sections)
    # ──────────────────────────────────────────────────────────────

    def _create_vector_layers(self, data, points, chart_path=None, group=None):
        import time
        from qgis.core import (
            QgsVectorLayer, QgsFeature, QgsGeometry,
            QgsCoordinateTransform, QgsCoordinateReferenceSystem,
        )

        project = QgsProject.instance()
        crs_auth = project.crs().authid() or "EPSG:4326"
        stamp = int(time.time()) % 10000
        group = group or self._new_output_group("Sezioni e volumi / Sections and volumes")
        layers = []
        popup_layers = []

        # Axis layer
        vl_asse = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=id:integer&field=length_m:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Asse {stamp}", "memory"
        )
        self._apply_field_aliases(vl_asse)
        pr_asse = vl_asse.dataProvider()
        feat_asse = QgsFeature()
        feat_asse.setGeometry(QgsGeometry.fromPolylineXY(points))
        feat_asse.setAttributes([
            1,
            data.get("alignment_length_m", 0),
            "Asse / Alignment",
            chart_path or "",
        ])
        pr_asse.addFeature(feat_asse)
        vl_asse.updateExtents()
        self._style_line_layer(vl_asse, "#f5a623", 0.9, "label")
        self._add_output_layer(vl_asse, group, "01 Asse / Axis")
        layers.append(vl_asse)

        crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        try:
            xform_from_wgs = QgsCoordinateTransform(
                crs_wgs, project.crs(), project.transformContext()
            )
        except Exception:
            xform_from_wgs = QgsCoordinateTransform(crs_wgs, project.crs(), project)

        # Curve points
        curves = data.get("curve_points", [])
        if curves:
            vl_curve = QgsVectorLayer(
                f"Point?crs={crs_auth}&field=id_curva:integer&field=prog_m:double"
                f"&field=label:string(80)&field=chart_png:string(254)",
                f"Curve {stamp}", "memory"
            )
            self._apply_field_aliases(vl_curve)
            pr_c = vl_curve.dataProvider()
            feats_c = []
            for c in curves:
                lon, lat = c.get("lon"), c.get("lat")
                if lon is not None and lat is not None:
                    pt_map = xform_from_wgs.transform(QgsPointXY(lon, lat))
                    f_c = QgsFeature()
                    f_c.setGeometry(QgsGeometry.fromPointXY(pt_map))
                    f_c.setAttributes([
                        c.get("index"),
                        c.get("progressive_m"),
                        "C{0} R={1}".format(c.get("index"), c.get("estimated_radius_m") or "n/d"),
                        chart_path or "",
                    ])
                    feats_c.append(f_c)
            if feats_c:
                pr_c.addFeatures(feats_c)
                vl_curve.updateExtents()
                self._style_marker_layer(vl_curve, "#f97316", 3.0, "label")
                self._add_output_layer(vl_curve, group, "06 Curve / Curves")
                layers.append(vl_curve)

        # Cross section lines
        vl_sez = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=sezione:integer"
            f"&field=prog_m:double&field=area:double"
            f"&field=cut_m2:double&field=fill_m2:double&field=quota_prj:double"
            f"&field=label:string(80)&field=colore:string(16)&field=chart_png:string(254)",
            f"Sezioni {stamp}", "memory"
        )
        self._apply_field_aliases(vl_sez)
        pr_sez = vl_sez.dataProvider()

        vl_pts = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=sezione:integer"
            f"&field=offset_m:double&field=quota:double&field=lon:double&field=lat:double"
            f"&field=chart_png:string(254)",
            f"Punti Sezione {stamp}", "memory"
        )
        self._apply_field_aliases(vl_pts)
        pr_pts = vl_pts.dataProvider()

        vl_centers = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=sezione:integer&field=prog_m:double"
            f"&field=quota_min:double&field=quota_max:double&field=quota_prj:double"
            f"&field=area_m2:double&field=cut_m2:double&field=fill_m2:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Centri Sezione {stamp}", "memory"
        )
        self._apply_field_aliases(vl_centers)
        pr_centers = vl_centers.dataProvider()

        vl_labels = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=sezione:integer&field=prog_m:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Etichette Sezione {stamp}", "memory"
        )
        self._apply_field_aliases(vl_labels)
        pr_labels = vl_labels.dataProvider()

        feats_sez = []
        feats_pts = []
        feats_centers = []
        feats_labels = []
        for sec in data.get("sections", []):
            pts_sec = sec.get("points", [])
            if not pts_sec:
                continue
            label_text = "S{0:02d} {1:.1f} m".format(
                int(sec.get("index") or 0),
                float(sec.get("progressive_m") or 0),
            )
            center_lon = sec.get("center_lon")
            center_lat = sec.get("center_lat")
            if center_lon is not None and center_lat is not None:
                center_pt = xform_from_wgs.transform(QgsPointXY(center_lon, center_lat))
                f_center = QgsFeature()
                f_center.setGeometry(QgsGeometry.fromPointXY(center_pt))
                f_center.setAttributes([
                    sec.get("index"),
                    sec.get("progressive_m"),
                    sec.get("min_elevation"),
                    sec.get("max_elevation"),
                    sec.get("design_elevation"),
                    sec.get("area_m2") or 0.0,
                    sec.get("cut_area_m2") or 0.0,
                    sec.get("fill_area_m2") or 0.0,
                    label_text,
                    chart_path or "",
                ])
                feats_centers.append(f_center)
                f_label = QgsFeature()
                f_label.setGeometry(QgsGeometry.fromPointXY(center_pt))
                f_label.setAttributes([
                    sec.get("index"),
                    sec.get("progressive_m"),
                    label_text,
                    chart_path or "",
                ])
                feats_labels.append(f_label)
            line_pts = []
            for p in pts_sec:
                lon, lat = p.get("lon"), p.get("lat")
                if lon is not None and lat is not None:
                    pt_map = xform_from_wgs.transform(QgsPointXY(lon, lat))
                    line_pts.append(pt_map)
                    f_pt = QgsFeature()
                    f_pt.setGeometry(QgsGeometry.fromPointXY(pt_map))
                    f_pt.setAttributes([
                        sec.get("index"),
                        p.get("offset_m"),
                        p.get("elevation") or 0.0,
                        lon,
                        lat,
                        chart_path or "",
                    ])
                    feats_pts.append(f_pt)
            if len(line_pts) >= 2:
                f_sez = QgsFeature()
                f_sez.setGeometry(QgsGeometry.fromPolylineXY(line_pts))
                f_sez.setAttributes([
                    sec.get("index"),
                    sec.get("progressive_m"),
                    sec.get("area_m2") or 0.0,
                    sec.get("cut_area_m2") or 0.0,
                    sec.get("fill_area_m2") or 0.0,
                    sec.get("design_elevation"),
                    label_text,
                    "#4f73c4",
                    chart_path or "",
                ])
                feats_sez.append(f_sez)

        if feats_sez:
            pr_sez.addFeatures(feats_sez)
            vl_sez.updateExtents()
            self._style_line_layer(vl_sez, "#4f73c4", 0.65)
            self._add_output_layer(vl_sez, group, "02 Sezioni planimetriche / Planimetric sections")
            layers.append(vl_sez)
            popup_layers.append(vl_sez)
        if feats_pts:
            pr_pts.addFeatures(feats_pts)
            vl_pts.updateExtents()
            self._style_marker_layer(vl_pts, "#8ba3c7", 1.4)
            self._add_output_layer(vl_pts, group, "03 Punti sezione / Section points")
            layers.append(vl_pts)
        if feats_labels:
            pr_labels.addFeatures(feats_labels)
            vl_labels.updateExtents()
            self._style_section_label_layer(vl_labels)
            self._add_output_layer(vl_labels, group, "04 Etichette sezione / Section labels")
            layers.append(vl_labels)
            popup_layers.append(vl_labels)
        if feats_centers:
            pr_centers.addFeatures(feats_centers)
            vl_centers.updateExtents()
            self._style_marker_layer(vl_centers, "#ffffff", 2.6)
            self._add_output_layer(vl_centers, group, "05 Centri sezione / Section centers")
            layers.append(vl_centers)
            popup_layers.append(vl_centers)

        vl_vol = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=da_sez:integer&field=a_sez:integer"
            f"&field=dist_m:double&field=volume_m3:double&field=sterro_m3:double"
            f"&field=riporto_m3:double&field=cum_sterro:double&field=cum_riporto:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Tratte Volumi {stamp}",
            "memory",
        )
        self._apply_field_aliases(vl_vol)
        pr_vol = vl_vol.dataProvider()
        sections_by_index = {s.get("index"): s for s in data.get("sections", [])}
        feats_vol = []
        for v in data.get("volumes", []):
            s1 = sections_by_index.get(v.get("from_section"))
            s2 = sections_by_index.get(v.get("to_section"))
            if not s1 or not s2:
                continue
            if None in (s1.get("center_lon"), s1.get("center_lat"), s2.get("center_lon"), s2.get("center_lat")):
                continue
            p1 = xform_from_wgs.transform(QgsPointXY(s1.get("center_lon"), s1.get("center_lat")))
            p2 = xform_from_wgs.transform(QgsPointXY(s2.get("center_lon"), s2.get("center_lat")))
            f_vol = QgsFeature()
            f_vol.setGeometry(QgsGeometry.fromPolylineXY([p1, p2]))
            f_vol.setAttributes([
                v.get("from_section"),
                v.get("to_section"),
                v.get("distance_m"),
                v.get("volume_m3"),
                v.get("cut_m3"),
                v.get("fill_m3"),
                v.get("cumulative_cut_m3"),
                v.get("cumulative_fill_m3"),
                "S{0}-S{1} V={2:.1f} m3".format(
                    v.get("from_section"),
                    v.get("to_section"),
                    float(v.get("volume_m3") or 0),
                ),
                chart_path or "",
            ])
            feats_vol.append(f_vol)
        if feats_vol:
            pr_vol.addFeatures(feats_vol)
            vl_vol.updateExtents()
            self._style_line_layer(vl_vol, "#a78bfa", 0.75, "label")
            self._add_output_layer(vl_vol, group, "07 Volumi / Volumes")
            layers.append(vl_vol)

        drawing_layers = self._create_section_drawing_layers(data, stamp, chart_path, group)
        layers.extend(drawing_layers)
        popup_layers.extend(drawing_layers)
        for section_layer in popup_layers:
            self._connect_section_popup(section_layer)
        return layers

    def _create_section_drawing_layers(self, data, stamp, chart_path=None, group=None):
        from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry

        project = QgsProject.instance()
        crs_auth = project.crs().authid() or "EPSG:4326"
        labels = self.dialog.get_parameter_labels()
        origin_x = float(labels.get("insertion_x") or 0.0)
        origin_y = float(labels.get("insertion_y") or 0.0)
        if origin_x == 0.0 and origin_y == 0.0:
            try:
                center = self.iface.mapCanvas().extent().center()
                origin_x, origin_y = center.x(), center.y()
            except Exception:
                pass

        sections = data.get("sections") or []
        if not sections:
            return []

        half_width = float(data.get("half_width_m") or 20.0)
        section_width = max(half_width * 2.0, 1.0)
        max_relief = 1.0
        for sec in sections:
            elevs = [p.get("elevation") for p in sec.get("points", []) if p.get("elevation") is not None]
            if elevs:
                max_relief = max(max_relief, max(elevs) - min(elevs))
        cols = 4
        gap_x = max(section_width * 0.45, 15.0)
        gap_y = max(max_relief * 1.8, 18.0)
        vertical_exag = 1.0

        vl_lines = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=sezione:integer&field=prog_m:double"
            f"&field=tipo:string(16)&field=label:string(80)&field=colore:string(16)"
            f"&field=chart_png:string(254)",
            f"Disegni Sezioni Linee {stamp}",
            "memory",
        )
        vl_poly = QgsVectorLayer(
            f"Polygon?crs={crs_auth}&field=sezione:integer&field=prog_m:double"
            f"&field=tipo:string(16)&field=area_m2:double&field=label:string(80)"
            f"&field=colore:string(16)&field=chart_png:string(254)",
            f"Disegni Sezioni Sterro Riporto {stamp}",
            "memory",
        )
        self._apply_field_aliases(vl_lines)
        self._apply_field_aliases(vl_poly)
        pr_lines = vl_lines.dataProvider()
        pr_poly = vl_poly.dataProvider()
        line_features = []
        polygon_features = []

        def _local_xy(sec_index, offset, elevation, min_z):
            col = sec_index % cols
            row = sec_index // cols
            base_x = origin_x + col * (section_width + gap_x)
            base_y = origin_y - row * (max_relief + gap_y)
            return QgsPointXY(
                base_x + half_width + float(offset or 0.0),
                base_y + (float(elevation) - float(min_z)) * vertical_exag,
            )

        for idx, sec in enumerate(sections):
            pts = [p for p in sec.get("points", []) if p.get("elevation") is not None]
            if len(pts) < 2:
                continue
            elevs = [float(p.get("elevation")) for p in pts]
            min_z = min(elevs)
            ground_pts = [
                _local_xy(idx, p.get("offset_m"), p.get("elevation"), min_z)
                for p in pts
            ]
            label = "S{0:02d} {1:.1f} m".format(
                int(sec.get("index") or 0),
                float(sec.get("progressive_m") or 0.0),
            )
            f_ground = QgsFeature()
            f_ground.setGeometry(QgsGeometry.fromPolylineXY(ground_pts))
            f_ground.setAttributes([
                sec.get("index"),
                sec.get("progressive_m"),
                "terreno",
                label + " terreno",
                "#4f73c4",
                chart_path or "",
            ])
            line_features.append(f_ground)

            design = sec.get("design_elevation")
            if design is not None:
                left_pt = _local_xy(idx, -half_width, design, min_z)
                right_pt = _local_xy(idx, half_width, design, min_z)
                f_design = QgsFeature()
                f_design.setGeometry(QgsGeometry.fromPolylineXY([left_pt, right_pt]))
                f_design.setAttributes([
                    sec.get("index"),
                    sec.get("progressive_m"),
                    "progetto",
                    label + " progetto",
                    "#f59e0b",
                    chart_path or "",
                ])
                line_features.append(f_design)

                for p1, p2 in zip(pts[:-1], pts[1:]):
                    z1 = p1.get("elevation")
                    z2 = p2.get("elevation")
                    if z1 is None or z2 is None:
                        continue
                    avg_z = (float(z1) + float(z2)) / 2.0
                    tipo = "sterro" if avg_z > float(design) else "riporto"
                    color = "#ef4444" if tipo == "sterro" else "#22c55e"
                    g1 = _local_xy(idx, p1.get("offset_m"), z1, min_z)
                    g2 = _local_xy(idx, p2.get("offset_m"), z2, min_z)
                    d2 = _local_xy(idx, p2.get("offset_m"), design, min_z)
                    d1 = _local_xy(idx, p1.get("offset_m"), design, min_z)
                    f_poly = QgsFeature()
                    f_poly.setGeometry(QgsGeometry.fromPolygonXY([[g1, g2, d2, d1, g1]]))
                    dx = abs(float(p2.get("offset_m") or 0.0) - float(p1.get("offset_m") or 0.0))
                    area = dx * abs(avg_z - float(design))
                    f_poly.setAttributes([
                        sec.get("index"),
                        sec.get("progressive_m"),
                        tipo,
                        area,
                        label + " " + tipo,
                        color,
                        chart_path or "",
                    ])
                    polygon_features.append(f_poly)
            else:
                baseline = min_z
                ring = list(ground_pts)
                ring.append(_local_xy(idx, pts[-1].get("offset_m"), baseline, min_z))
                ring.append(_local_xy(idx, pts[0].get("offset_m"), baseline, min_z))
                ring.append(ground_pts[0])
                f_poly = QgsFeature()
                f_poly.setGeometry(QgsGeometry.fromPolygonXY([ring]))
                f_poly.setAttributes([
                    sec.get("index"),
                    sec.get("progressive_m"),
                    "sezione",
                    sec.get("area_m2") or 0.0,
                    label + " area",
                    "#4f73c4",
                    chart_path or "",
                ])
                polygon_features.append(f_poly)

        layers = []
        if line_features:
            pr_lines.addFeatures(line_features)
            vl_lines.updateExtents()
            self._style_section_lines(vl_lines)
            if group:
                self._add_output_layer(vl_lines, group, "08 Disegni tecnici / Technical drawings")
            else:
                project.addMapLayer(vl_lines)
            layers.append(vl_lines)
        if polygon_features:
            pr_poly.addFeatures(polygon_features)
            vl_poly.updateExtents()
            self._style_section_polygons(vl_poly)
            if group:
                self._add_output_layer(vl_poly, group, "09 Sterro riporto / Cut fill")
            else:
                project.addMapLayer(vl_poly)
            layers.append(vl_poly)
        return layers

    # ──────────────────────────────────────────────────────────────
    # Export vectors
    # ──────────────────────────────────────────────────────────────

    def _export_vectors(self):
        layers = [layer for layer in self.last_vector_layers if layer and layer.isValid()]
        if not layers:
            QMessageBox.warning(
                self.dialog,
                "Export GPKG",
                "Nessun layer vettoriale disponibile / No vector layer available.",
            )
            return

        default_path = self.last_vector_gpkg or self._new_output_gpkg_path("profili_sezioni")
        file_path, _ = QFileDialog.getSaveFileName(
            self.dialog,
            "Salva GeoPackage / Save GeoPackage",
            default_path,
            "GeoPackage (*.gpkg)",
        )
        if not file_path:
            return

        try:
            gpkg_path = self._write_vector_package(layers, file_path)
            self.last_vector_gpkg = gpkg_path
            self._push(
                "Export GPKG",
                f"GeoPackage salvato / GeoPackage saved: {gpkg_path}",
                "Success",
            )
        except Exception as e:
            QMessageBox.critical(self.dialog, "Export GPKG", str(e))

    # ──────────────────────────────────────────────────────────────
    # Export PNG
    # ──────────────────────────────────────────────────────────────

    def _export_png(self):
        if not self.last_svg_content:
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self.dialog, "Salva PNG / Save PNG", "", "PNG (*.png)"
        )
        if not file_path:
            return
        try:
            image = self._render_svg_to_image(
                self.last_svg_content,
                fallback_size=(1600, 1040),
                scale=(300 / 96.0),
                background="#0a0c10",
            )
            if not image.save(file_path, "PNG"):
                raise IOError(f"Impossibile salvare il PNG: {file_path}")
            self._push("Successo / Success", f"PNG salvato / PNG saved: {file_path}", "Success")
        except Exception as e:
            QMessageBox.critical(self.dialog, "Errore / Error", str(e))

    # ──────────────────────────────────────────────────────────────
    # Export PDF
    # ──────────────────────────────────────────────────────────────

    def _export_pdf(self):
        if not self.last_svg_content or not self._last_drawn_points:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "sezioni" if self.current_mode == "sections" else "profilo"
        default_path = os.path.join(self._default_output_dir(), f"{prefix}_{stamp}.pdf")
        file_path, _ = QFileDialog.getSaveFileName(
            self.dialog, "Salva PDF / Save PDF", default_path, "PDF (*.pdf)"
        )
        if not file_path:
            return
        try:
            saved_path = self._render_layout_to_pdf(self._last_drawn_points, self.last_svg_content, file_path)
            self._push("Successo / Success", f"PDF salvato / PDF saved: {saved_path}", "Success")
        except Exception as e:
            QMessageBox.critical(self.dialog, "Errore / Error", str(e))

    def _render_layout_to_pdf(self, points, svg_content, output_path):
        from qgis.core import (
            QgsLayoutExporter,
        )

        if not output_path.lower().endswith(".pdf"):
            output_path += ".pdf"
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        tmp_dir = tempfile.mkdtemp(prefix="profili_sezioni_pdf_")
        try:
            svg_path = os.path.join(tmp_dir, "chart.svg")
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg_content)
            layout = self._create_report_layout(
                points,
                svg_content,
                f"ProfiliSezioni_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}",
                svg_path,
                add_to_project=False,
            )

            exporter = QgsLayoutExporter(layout)
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.appendGeoreference = True
            if hasattr(settings, "dpi"):
                settings.dpi = 300
            if hasattr(settings, "forceVectorOutput"):
                settings.forceVectorOutput = True
            if hasattr(settings, "simplifyGeometries"):
                settings.simplifyGeometries = True
            if hasattr(settings, "exportMetadata"):
                settings.exportMetadata = True

            result = exporter.exportToPdf(output_path, settings)
            if result != compat_enum(QgsLayoutExporter, "Success", "ExportResult"):
                raise Exception(f"Errore durante l'esportazione PDF: {result}")
            if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
                raise Exception("Errore durante l'esportazione PDF: file non creato o vuoto.")
            return output_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────
    # Export CSV
    # ──────────────────────────────────────────────────────────────

    def _export_csv(self):
        if self.current_mode == "sections" and self.last_cross_sections_data:
            file_path, _ = QFileDialog.getSaveFileName(
                self.dialog, "Salva CSV Sezioni / Save Sections CSV", "", "CSV (*.csv)"
            )
            if not file_path:
                return
            try:
                data = self.last_cross_sections_data
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=";")
                    writer.writerow([
                        "Section", "Progressive_m", "Min_m", "Max_m",
                        "Area_m2", "CutArea_m2", "FillArea_m2",
                    ])
                    for s in data["sections"]:
                        writer.writerow([
                            s["index"],
                            f"{s['progressive_m']:.2f}",
                            f"{s['min_elevation']:.2f}" if s["min_elevation"] is not None else "",
                            f"{s['max_elevation']:.2f}" if s["max_elevation"] is not None else "",
                            f"{s.get('area_m2', 0):.2f}",
                            f"{s.get('cut_area_m2', 0):.2f}",
                            f"{s.get('fill_area_m2', 0):.2f}",
                        ])
                    writer.writerow([])
                    writer.writerow([
                        "From", "To", "Distance_m", "Vol_m3",
                        "Cut_m3", "Fill_m3", "Cum_Cut", "Cum_Fill",
                    ])
                    for v in data["volumes"]:
                        writer.writerow([
                            v["from_section"], v["to_section"],
                            f"{v['distance_m']:.2f}",
                            f"{v['volume_m3']:.2f}",
                            f"{v['cut_m3']:.2f}",
                            f"{v['fill_m3']:.2f}",
                            f"{v['cumulative_cut_m3']:.2f}",
                            f"{v['cumulative_fill_m3']:.2f}",
                        ])
                self._push("Successo / Success", f"CSV salvato / CSV saved: {file_path}", "Success")
            except Exception as e:
                QMessageBox.critical(self.dialog, "Errore / Error", str(e))

        elif self.last_points_data:
            file_path, _ = QFileDialog.getSaveFileName(
                self.dialog, "Salva CSV Profilo / Save Profile CSV", "", "CSV (*.csv)"
            )
            if not file_path:
                return
            try:
                export_profile_csv(self.last_points_data, file_path)
                self._push("Successo / Success", f"CSV salvato / CSV saved: {file_path}", "Success")
            except Exception as e:
                QMessageBox.critical(self.dialog, "Errore / Error", str(e))

    # ──────────────────────────────────────────────────────────────
    # Print Layout
    # ──────────────────────────────────────────────────────────────

    def _print_layout(self):
        if not self._last_drawn_points:
            self._push(
                "Errore / Error",
                "Nessun profilo calcolato / No profile computed.",
                "Critical",
            )
            return

        points = self._last_drawn_points

        if self.current_mode == "sections" and self.last_svg_content:
            self._layout_svg(points, self.last_svg_content, "Cross Sections / Sezioni Trasversali")
        elif self.last_svg_content:
            self._layout_svg(points, self.last_svg_content, "Elevation Profile / Profilo Altimetrico")
        else:
            self._push("Errore / Error", "Nessun risultato da stampare / Nothing to print.", "Critical")

    def _layout_svg(self, points, svg_content, layout_name):
        svg_path = self._persist_svg_chart(svg_content, "layout_chart")
        if not svg_path:
            raise RuntimeError("Impossibile preparare il grafico per il layout.")
        layout = self._create_report_layout(
            points,
            svg_content,
            layout_name,
            svg_path,
            add_to_project=True,
        )

        self.iface.openLayoutDesigner(layout)
