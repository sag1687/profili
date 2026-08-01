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
import datetime
import math

from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QApplication
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsPointXY

from .dialog import ProfiliSezioniComuniDialog
from .map_tool import (
    DrawPolygonAreaTool,
    DrawPolylineTool,
    DrawRectangleAreaTool,
)
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
    calculate_profile,
    generate_profile_svg,
    generate_profile_svg_pegstyle,
    generate_quick_profile_svg,
    generate_profile_results_html,
    export_profile_csv,
    build_pickets,
)
from .core_sections import (
    calculate_cross_sections,
    generate_cross_sections_svg,
    generate_cross_sections_results_html,
)
from .core_confronto import (
    compare_dtm_rasters,
    compare_gpkg_summaries,
    generate_compare_html,
    generate_delta_svg,
    generate_dtm_compare_html,
    list_output_gpkgs,
    read_gpkg_summary,
)
from .core_pointcloud import write_las_point_cloud


class ProfiliSezioniComuniPlugin:
    # Scale denominator below which dense per-feature labels (pickets,
    # section markers) become visible. Above it (zoomed further out, as
    # over a long alignment) the features still render, just unlabelled,
    # instead of collapsing into unreadable overlapping text.
    DENSE_LABEL_MIN_SCALE = 5000

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None
        self.map_tool = None
        self.area_tool = None
        self.area_tool_polygon = None

        # last results state
        self.last_points_data = None
        self.last_total_dist = None
        self.last_svg_content = None
        self.last_profile_svg = None
        self.last_sections_svg = None
        self.last_results_html = None
        self.last_cross_sections_data = None
        self.last_vector_layers = []
        self.last_vector_gpkg = None
        self.last_chart_png_path = None
        self.current_mode = "profile"  # "profile" | "sections"
        self._last_drawn_points = None
        # Shared guard across raster download / GeoPackage compare / DTM
        # compare: each already disables its own button while running, but
        # nothing stopped the user from switching tabs and starting a
        # *different* long GDAL/network operation on top of it.
        self._busy = False

        # ProfiloExpress: live profile preview while drawing on the canvas
        self._profilo_express_raster = None
        self._profilo_express_preview = None

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
        self.iface.addPluginToMenu("&Profili, Sezioni e Comuni", self.action)

    def unload(self):
        # Unset any map tool of ours still active on the canvas and drop our
        # reference to it: QgsMapCanvas keeps calling into the C++ side of
        # the tool on the next mouse event even after Python has no more
        # references, which crashes if we don't detach it here first.
        canvas = self.iface.mapCanvas()
        for tool, signal, slot in (
            (self.map_tool, "lineDrawn", self._on_line_drawn),
            (self.map_tool, "previewChanged", self._on_profilo_express_preview),
            (self.map_tool, "drawingCancelled", self._on_drawing_cancelled),
            (self.area_tool, "areaDrawn", self._on_area_drawn),
            (self.area_tool, "drawingCancelled", self._on_drawing_cancelled),
            (self.area_tool_polygon, "areaDrawn", self._on_area_drawn),
            (
                self.area_tool_polygon,
                "drawingCancelled",
                self._on_drawing_cancelled,
            ),
        ):
            if tool is None:
                continue
            if canvas.mapTool() is tool:
                canvas.unsetMapTool(tool)
            try:
                getattr(tool, signal).disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.map_tool = None
        self.area_tool = None
        self.area_tool_polygon = None

        # `close()` alone only hides these QDialogs: parented to
        # iface.mainWindow(), the underlying C++ objects (and every signal
        # connection still pointing back into this plugin instance, e.g.
        # button clicks -> self.run()) would otherwise stay alive for the
        # rest of the QGIS session. deleteLater() actually schedules their
        # destruction, which also drops those connections.
        if self._profilo_express_preview is not None:
            self._profilo_express_preview.close()
            self._profilo_express_preview.deleteLater()
            self._profilo_express_preview = None

        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(
                "&Profili, Sezioni e Comuni", self.action
            )
            try:
                self.action.triggered.disconnect(self.run)
            except (TypeError, RuntimeError):
                pass
            self.action.deleteLater()
            self.action = None

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
        return os.path.join(
            self._default_output_dir(), f"{prefix}_{stamp}.gpkg"
        )

    def _output_root_group(self):
        root = QgsProject.instance().layerTreeRoot()
        group_name = "Profili, Sezioni e Comuni"
        group = root.findGroup(group_name)
        return group or root.addGroup(group_name)

    def _new_output_group(self, title, is_raster=False):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        name = f"{title} - {stamp}"
        root = self._output_root_group()
        if is_raster:
            # Raster groups (downloaded DTM, DTM-comparison diff) always
            # get appended at the bottom of our group, so they render
            # underneath any vector output group -- regardless of whether
            # the raster was loaded before or after the vectors.
            return root.addGroup(name)
        # Vector output groups (profile, sections, comparison) always get
        # inserted at the top, so they stay visible on top of any raster
        # already present instead of being hidden underneath it.
        return root.insertGroup(0, name)

    def _ensure_subgroup(self, parent_group, name):
        group = parent_group.findGroup(name) if parent_group else None
        return group or parent_group.addGroup(name)

    def _add_output_layer(self, layer, parent_group, subgroup_name=None):
        if not layer or not layer.isValid():
            return
        project = QgsProject.instance()
        if project.mapLayer(layer.id()) is None:
            project.addMapLayer(layer, False)
        target = (
            self._ensure_subgroup(parent_group, subgroup_name)
            if subgroup_name
            else parent_group
        )
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
            pieces.append(
                f"{self._format_bytes(transferred)} / "
                f"{self._format_bytes(total)}"
            )
        if speed:
            pieces.append(f"{self._format_bytes(speed)}/s")
        if eta is not None:
            pieces.append(
                f"tempo rimanente / ETA {self._format_duration(eta)}"
            )
        if elapsed:
            pieces.append(
                f"trascorso / elapsed {self._format_duration(elapsed)}"
            )
        self.dialog.lbl_download_status.setText(
            " · ".join(p for p in pieces if p)
        )
        QApplication.processEvents()
        return True

    def _chart_image_html(self, image_path, fallback_svg):
        img_style = (
            'display:block;width:100%;max-width:1700px;height:auto;'
            'border:1px solid #2d3757;border-radius:6px;background:#12151e;'
        )
        if image_path:
            url = QUrl.fromLocalFile(image_path).toString()
        elif fallback_svg:
            # PNG conversion failed: fall back to the SVG itself as a data
            # URI image rather than pasting raw <svg> markup into the
            # results HTML — QTextBrowser (used whenever QtWebEngine isn't
            # available) cannot render an inline <svg> tag at all, so the
            # chart would otherwise simply vanish instead of degrading.
            import base64

            encoded = base64.b64encode(
                fallback_svg.encode("utf-8")
            ).decode("ascii")
            url = f"data:image/svg+xml;base64,{encoded}"
        else:
            return ""
        return '<img src="{0}" style="{1}">'.format(url, img_style)

    def _svg_to_png(self, svg_content, name):
        if not svg_content:
            return None
        try:
            from qgis.PyQt.QtCore import QByteArray, QSize as QtSize
            from qgis.PyQt.QtGui import QImage, QPainter, QColor
            from qgis.PyQt.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(QByteArray(svg_content.encode("utf-8")))
            default_size = renderer.defaultSize()
            if default_size.isEmpty():
                default_size = QtSize(1700, 1100)
            width = max(900, int(default_size.width()))
            height = max(520, int(default_size.height()))
            image = QImage(
                width,
                height,
                QImage.Format.Format_ARGB32,
            )
            image.fill(QColor("#12151e"))
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter)
            painter.end()
            chart_dir = os.path.join(self._default_output_dir(), "_charts")
            os.makedirs(chart_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(chart_dir, f"{name}_{stamp}.png")
            image.save(path, "PNG")
            return path
        except Exception as exc:
            self._push(
                "Grafico / Chart", f"PNG grafico non creato: {exc}", "Warning"
            )
            return None

    def _write_vector_package(self, layers, gpkg_path):
        if not layers:
            raise ValueError("No vector layers available.")
        if not gpkg_path.lower().endswith(".gpkg"):
            gpkg_path += ".gpkg"

        from qgis.core import QgsVectorFileWriter

        for idx, layer in enumerate(layers):
            layer_name = (
                layer.name().lower().replace(" ", "_").replace("—", "_")
            )
            # writeAsVectorFormatV3/V2 (modern QGIS) vs. writeAsVectorFormat
            # (older API, removed in recent QGIS): resolved once via getattr
            # so a real error raised *while writing* is never mistaken for
            # "this API doesn't exist on this QGIS version".
            writer = getattr(
                QgsVectorFileWriter, "writeAsVectorFormatV3", None
            ) or getattr(QgsVectorFileWriter, "writeAsVectorFormatV2", None)
            if writer is not None:
                options = QgsVectorFileWriter.SaveVectorOptions()
                options.driverName = "GPKG"
                options.fileEncoding = "UTF-8"
                options.layerName = layer_name
                if idx == 0:
                    options.actionOnExistingFile = (
                        QgsVectorFileWriter
                        .ActionOnExistingFile.CreateOrOverwriteFile
                    )
                else:
                    options.actionOnExistingFile = (
                        QgsVectorFileWriter
                        .ActionOnExistingFile.CreateOrOverwriteLayer
                    )
                result = writer(
                    layer,
                    gpkg_path,
                    QgsProject.instance().transformContext(),
                    options,
                )
                err_code = result[0] if isinstance(result, tuple) else result
                if err_code != QgsVectorFileWriter.WriterError.NoError:
                    msg = (
                        result[1]
                        if isinstance(result, tuple) and len(result) > 1
                        else str(err_code)
                    )
                    raise RuntimeError(msg)
            else:
                err = QgsVectorFileWriter.writeAsVectorFormat(
                    layer,
                    gpkg_path,
                    "UTF-8",
                    layer.crs(),
                    "GPKG",
                    layerOptions=[f"TABLE={layer_name}"],
                )
                if isinstance(err, tuple):
                    err = err[0]
                if err != QgsVectorFileWriter.WriterError.NoError:
                    raise RuntimeError(
                        f"Vector export failed for {layer.name()}: {err}"
                    )
        return gpkg_path

    def _register_vector_outputs(self, layers, prefix):
        self.last_vector_layers = [
            layer for layer in layers if layer and layer.isValid()
        ]
        if not self.last_vector_layers:
            self.last_vector_gpkg = None
            return None
        gpkg_path = self._new_output_gpkg_path(prefix)
        self.last_vector_gpkg = self._write_vector_package(
            self.last_vector_layers, gpkg_path
        )
        return self.last_vector_gpkg

    def _enable_labels(
        self,
        layer,
        field_name="label",
        color="#f1f5f9",
        size=8,
        min_label_scale=None,
    ):
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
            fmt = QgsTextFormat()
            fmt.setSize(size)
            fmt.setColor(QColor(color))
            buffer = QgsTextBufferSettings()
            buffer.setEnabled(True)
            buffer.setSize(1.0)
            buffer.setColor(QColor("#12151e"))
            fmt.setBuffer(buffer)
            settings.setFormat(fmt)
            if min_label_scale:
                # Dense per-feature labels (pickets, section markers) turn
                # into unreadable clutter once zoomed out over a long
                # alignment. Only show them once zoomed in past this scale
                # denominator; the map still shows the features themselves
                # at any zoom, just without labels.
                settings.scaleVisibility = True
                settings.minimumScale = min_label_scale
                settings.maximumScale = 0
            layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            layer.setLabelsEnabled(True)
            layer.triggerRepaint()
        except Exception:  # nosec B110
            pass

    def _style_line_layer(
        self, layer, color, width=0.7, label_field=None, min_label_scale=None
    ):
        try:
            from qgis.core import QgsLineSymbol, QgsSingleSymbolRenderer

            symbol = QgsLineSymbol.createSimple(
                {
                    "color": color,
                    "width": str(width),
                }
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            if label_field:
                self._enable_labels(
                    layer, label_field, min_label_scale=min_label_scale
                )
            layer.triggerRepaint()
        except Exception:  # nosec B110
            pass

    def _style_marker_layer(
        self,
        layer,
        color,
        size=2.2,
        label_field=None,
        min_label_scale=None,
    ):
        try:
            from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer

            symbol = QgsMarkerSymbol.createSimple(
                {
                    "name": "circle",
                    "color": color,
                    "outline_color": "#12151e",
                    "outline_width": "0.2",
                    "size": str(size),
                }
            )
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            if label_field:
                self._enable_labels(
                    layer, label_field, min_label_scale=min_label_scale
                )
            layer.triggerRepaint()
        except Exception:  # nosec B110
            pass

    # ──────────────────────────────────────────────────────────────
    # 3D outputs (Z-enabled layers + LAS point cloud)
    # ──────────────────────────────────────────────────────────────

    def _create_3d_axis_and_points(
        self, xyz_points, crs_auth, group, stamp, name_prefix, label_prefix,
        color,
    ):
        """Build a Z-enabled LineStringZ axis and PointZ samples layer from
        a flat list of (x, y, z) tuples, plus a best-effort LAS point
        cloud. Returns the vector layers created (the point cloud layer,
        when it can be loaded, is added to the map but not returned since
        it cannot be packaged into a GeoPackage)."""
        from qgis.core import QgsFeature, QgsGeometry, QgsPoint, QgsVectorLayer

        valid = [
            (x, y, z)
            for x, y, z in xyz_points
            if x is not None and y is not None and z is not None
        ]
        layers = []

        if len(valid) >= 2:
            line_layer = QgsVectorLayer(
                f"LineStringZ?crs={crs_auth}&field=id:integer",
                f"{name_prefix}_3d_asse_{stamp}",
                "memory",
            )
            feat = QgsFeature()
            feat.setGeometry(
                QgsGeometry.fromPolyline(
                    [QgsPoint(x, y, z) for x, y, z in valid]
                )
            )
            feat.setAttributes([1])
            line_layer.dataProvider().addFeature(feat)
            line_layer.updateExtents()
            self._style_line_layer(line_layer, color, 1.0)
            self._add_output_layer(
                line_layer,
                group,
                "05 " + label_prefix + " 3D — asse / 3D axis",
            )
            layers.append(line_layer)

        if valid:
            pts_layer = QgsVectorLayer(
                f"PointZ?crs={crs_auth}&field=id:integer"
                f"&field=quota_m:double",
                f"{name_prefix}_3d_punti_{stamp}",
                "memory",
            )
            feats = []
            for idx, (x, y, z) in enumerate(valid, 1):
                f = QgsFeature()
                f.setGeometry(QgsGeometry(QgsPoint(x, y, z)))
                f.setAttributes([idx, float(z)])
                feats.append(f)
            pts_layer.dataProvider().addFeatures(feats)
            pts_layer.updateExtents()
            self._style_marker_layer(pts_layer, color, 1.6)
            self._add_output_layer(
                pts_layer,
                group,
                "06 " + label_prefix + " 3D — punti / 3D points",
            )
            layers.append(pts_layer)

            self._export_and_try_load_point_cloud(
                valid, name_prefix, group, label_prefix
            )

        return layers

    def _export_and_try_load_point_cloud(
        self, xyz_points, name_prefix, group, label_prefix
    ):
        """Write xyz_points as a LAS 1.2 file (stdlib-only, no external
        dependency) and try to add it as a QgsPointCloudLayer. Loading a
        raw LAS file requires QGIS to be built with PDAL support: when
        that provider isn't available the file is still saved to disk and
        the user is informed — this never raises/crashes the plugin."""
        try:
            out_dir = os.path.join(self._default_output_dir(), "_pointcloud")
            os.makedirs(out_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_prefix = "".join(
                c for c in name_prefix if c.isalnum() or c in ("_", "-")
            ) or "profilo"
            path = os.path.join(out_dir, f"{safe_prefix}_{stamp}.las")
            n_points = write_las_point_cloud(xyz_points, path)
        except Exception as exc:
            self._push(
                "Nuvola di punti / Point cloud",
                f"Export LAS non riuscito / LAS export failed: {exc}",
                "Warning",
            )
            return

        try:
            from qgis.core import QgsPointCloudLayer

            pc_layer = QgsPointCloudLayer(
                path, f"{name_prefix}_pointcloud_{stamp}", "pdal"
            )
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Point cloud layer non caricato (provider PDAL assente o "
                f"file non valido): {exc}",
                "Profili, Sezioni e Comuni",
                Qgis.MessageLevel.Info,
            )
            pc_layer = None

        if pc_layer is not None and pc_layer.isValid():
            self._add_output_layer(
                pc_layer,
                group,
                label_prefix + " 3D — nuvola di punti / point cloud",
            )
        else:
            self._push(
                "Nuvola di punti / Point cloud",
                (
                    "File LAS salvato ({0} punti) in {1} — apribile con "
                    "CloudCompare o con installazioni QGIS con supporto "
                    "PDAL. / LAS file saved ({0} points) at {1} — open it "
                    "with CloudCompare or a QGIS build with PDAL support."
                ).format(n_points, path),
                "Info",
            )

    def _create_sections_3d_layers(
        self, data, crs_auth, group, stamp, xform_from_wgs
    ):
        """Z-enabled counterpart of the planimetric cross-section layers:
        one LineStringZ per section (real elevation as Z) plus a combined
        PointZ layer, and a best-effort LAS point cloud of all section
        samples together."""
        from qgis.core import (
            QgsFeature,
            QgsGeometry,
            QgsPoint,
            QgsPointXY,
            QgsVectorLayer,
        )

        line_layer = QgsVectorLayer(
            f"LineStringZ?crs={crs_auth}&field=sezione:integer"
            f"&field=label:string(80)",
            f"sezioni_3d_asse_{stamp}",
            "memory",
        )
        pts_layer = QgsVectorLayer(
            f"PointZ?crs={crs_auth}&field=sezione:integer"
            f"&field=offset_m:double&field=quota_m:double",
            f"sezioni_3d_punti_{stamp}",
            "memory",
        )

        line_feats = []
        pt_feats = []
        all_xyz = []
        for sec in data.get("sections", []):
            sec_pts_3d = []
            for p in sec.get("points", []):
                lon, lat, z = p.get("lon"), p.get("lat"), p.get("elevation")
                if lon is None or lat is None or z is None:
                    continue
                pm = xform_from_wgs.transform(QgsPointXY(lon, lat))
                sec_pts_3d.append(
                    (pm.x(), pm.y(), float(z), p.get("offset_m"))
                )

            if len(sec_pts_3d) >= 2:
                f_line = QgsFeature()
                f_line.setGeometry(
                    QgsGeometry.fromPolyline(
                        [QgsPoint(x, y, z) for x, y, z, _off in sec_pts_3d]
                    )
                )
                f_line.setAttributes(
                    [
                        sec.get("index"),
                        "S{0:02d}".format(int(sec.get("index") or 0)),
                    ]
                )
                line_feats.append(f_line)

            for x, y, z, off in sec_pts_3d:
                f_pt = QgsFeature()
                f_pt.setGeometry(QgsGeometry(QgsPoint(x, y, z)))
                f_pt.setAttributes([sec.get("index"), off, z])
                pt_feats.append(f_pt)
                all_xyz.append((x, y, z))

        layers = []
        if line_feats:
            line_layer.dataProvider().addFeatures(line_feats)
            line_layer.updateExtents()
            self._style_line_layer(line_layer, "#34d399", 0.8, "label")
            self._add_output_layer(
                line_layer, group, "09 Sezioni 3D / 3D sections"
            )
            layers.append(line_layer)
        if pt_feats:
            pts_layer.dataProvider().addFeatures(pt_feats)
            pts_layer.updateExtents()
            self._style_marker_layer(pts_layer, "#34d399", 1.4)
            self._add_output_layer(
                pts_layer,
                group,
                "10 Punti sezione 3D / 3D section points",
            )
            layers.append(pts_layer)
        if all_xyz:
            self._export_and_try_load_point_cloud(
                all_xyz, "sezioni", group, "Sezioni / Sections"
            )

        return layers

    def _style_section_polygons(self, layer):
        try:
            from qgis.core import (
                QgsCategorizedSymbolRenderer,
                QgsFillSymbol,
                QgsRendererCategory,
            )

            categories = []
            for value, label, fill, outline in (
                ("sterro", "Sterro / Cut", "239,68,68,105", "#ef4444"),
                ("riporto", "Riporto / Fill", "34,197,94,105", "#22c55e"),
                (
                    "sezione",
                    "Area sezione / Section area",
                    "79,115,196,55",
                    "#4f73c4",
                ),
            ):
                symbol = QgsFillSymbol.createSimple(
                    {
                        "color": fill,
                        "outline_color": outline,
                        "outline_width": "0.25",
                    }
                )
                categories.append(QgsRendererCategory(value, symbol, label))
            layer.setRenderer(QgsCategorizedSymbolRenderer("tipo", categories))
            self._enable_labels(layer, "label", "#f1f5f9", 7)
            layer.triggerRepaint()
        except Exception:  # nosec B110
            pass

    def _style_section_lines(self, layer):
        try:
            from qgis.core import (
                QgsCategorizedSymbolRenderer,
                QgsLineSymbol,
                QgsRendererCategory,
            )

            categories = []
            for value, label, color, width in (
                ("terreno", "Terreno / Ground", "#34d399", "0.65"),
                ("progetto", "Progetto / Design", "#f59e0b", "0.55"),
            ):
                symbol = QgsLineSymbol.createSimple(
                    {
                        "color": color,
                        "width": width,
                    }
                )
                categories.append(QgsRendererCategory(value, symbol, label))
            layer.setRenderer(QgsCategorizedSymbolRenderer("tipo", categories))
            self._enable_labels(layer, "label", "#f1f5f9", 7)
            layer.triggerRepaint()
        except Exception:
            self._style_line_layer(layer, "#34d399", 0.55, "label")

    def _style_grid_layer(self, layer):
        self._style_line_layer(layer, "#5b6b82", 0.2)

    def _nice_step(self, value, target=8):
        value = max(float(value or 0.0), 0.001)
        raw = value / max(target, 1)
        exp = math.floor(math.log10(raw)) if raw > 0 else 0
        base = raw / (10 ** exp)
        if base < 1.5:
            nice = 1
        elif base < 3.5:
            nice = 2
        elif base < 7.5:
            nice = 5
        else:
            nice = 10
        return nice * (10 ** exp)

    def _nice_exaggeration(self, relief, span):
        """Round a raw vertical-exaggeration ratio to an engineering-style
        value (1x, 2x, 5x, 10x, 20x...), so the diagram relief reads
        clearly without an arbitrary decimal multiplier."""
        relief = max(float(relief or 0.0), 0.01)
        span = max(float(span or 0.0), 1.0)
        target = (span * 0.35) / relief
        candidates = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000)
        for value in candidates:
            if value >= target:
                return value
        return candidates[-1]

    def _build_chart_annotations(self, crs_auth, stamp, group, panels):
        """Build the Fattori-style Grid_Line + Labels_point layers (frame,
        elevation/distance ticks, vertical-exaggeration annotation) shared
        by both the elevation profile and the cross-section diagrams."""
        from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry

        grid_layer = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=panel:integer"
            f"&field=tipo:string(16)&field=chart_png:string(254)",
            f"Diagramma Griglia {stamp}",
            "memory",
        )
        labels_layer = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=panel:integer"
            f"&field=cat:string(16)&field=label:string(80)"
            f"&field=chart_png:string(254)",
            f"Diagramma Etichette {stamp}",
            "memory",
        )
        grid_feats = []
        label_feats = []

        for idx, panel in enumerate(panels):
            origin = panel["origin"]
            x_span = max(float(panel["x_span"]), 0.001)
            z_min = float(panel["z_min"])
            z_max = max(float(panel["z_max"]), z_min + 0.001)
            exag = float(panel["exag"])
            title = panel.get("title", "")

            def pt(x_val, z_val, origin=origin, z_min=z_min, exag=exag):
                return QgsPointXY(
                    origin.x() + x_val,
                    origin.y() + (z_val - z_min) * exag,
                )

            frame = QgsFeature()
            frame.setGeometry(
                QgsGeometry.fromPolylineXY(
                    [
                        pt(0, z_min), pt(x_span, z_min),
                        pt(x_span, z_max), pt(0, z_max), pt(0, z_min),
                    ]
                )
            )
            frame.setAttributes([idx, "frame", ""])
            grid_feats.append(frame)

            z_step = self._nice_step(z_max - z_min, 5)
            z = math.ceil(z_min / z_step) * z_step
            while z <= z_max + 1e-6:
                line = QgsFeature()
                line.setGeometry(
                    QgsGeometry.fromPolylineXY([pt(0, z), pt(x_span, z)])
                )
                line.setAttributes([idx, "griglia", ""])
                grid_feats.append(line)
                tick = QgsFeature()
                tick.setGeometry(QgsGeometry.fromPointXY(pt(0, z)))
                tick.setAttributes([idx, "tick_z", f"{z:.1f} m"])
                label_feats.append(tick)
                z += z_step

            x_step = self._nice_step(x_span, 8)
            x = 0.0
            while x <= x_span + 1e-6:
                line = QgsFeature()
                line.setGeometry(
                    QgsGeometry.fromPolylineXY([pt(x, z_min), pt(x, z_max)])
                )
                line.setAttributes([idx, "tick", ""])
                grid_feats.append(line)
                tick = QgsFeature()
                tick.setGeometry(QgsGeometry.fromPointXY(pt(x, z_min)))
                tick.setAttributes([idx, "tick_x", f"{x:.0f} m"])
                label_feats.append(tick)
                x += x_step

            exag_text = (
                f"Esagerazione verticale {exag:.0f}x / "
                f"Vertical exaggeration {exag:.0f}x"
                if exag != 1
                else "Scala reale / True scale (1:1)"
            )
            frame_height = (z_max - z_min) * exag
            scale_pt = QgsFeature()
            scale_pt.setGeometry(
                QgsGeometry.fromPointXY(
                    QgsPointXY(
                        origin.x() + x_span * 0.5,
                        origin.y() - frame_height * 0.15 - 1.0,
                    )
                )
            )
            scale_pt.setAttributes(
                [idx, "scala", f"{title} — {exag_text}".strip(" —")]
            )
            label_feats.append(scale_pt)

        if grid_feats:
            grid_layer.dataProvider().addFeatures(grid_feats)
            grid_layer.updateExtents()
            self._style_grid_layer(grid_layer)
            self._add_output_layer(
                grid_layer, group, "05 Griglia diagramma / Chart grid"
            )
        if label_feats:
            labels_layer.dataProvider().addFeatures(label_feats)
            labels_layer.updateExtents()
            self._style_marker_layer(labels_layer, "#c3ccd6", 0.1, "label")
            self._add_output_layer(
                labels_layer,
                group,
                "05 Griglia diagramma / Chart grid",
            )
        return [
            layer
            for layer in (grid_layer, labels_layer)
            if layer.featureCount() > 0
        ]

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
            self.dialog.btn_layer_sez.clicked.connect(
                self._run_sections_from_layer
            )
            # Tab Comuni
            self.dialog.btn_comuni_download_source.clicked.connect(
                lambda: self._open_url(ISTAT_ADMIN_BOUNDARIES_PAGE)
            )
            # Tab Download Raster
            self.dialog.btn_draw_area.clicked.connect(self._start_area_drawing)
            self.dialog.btn_draw_area_polygon.clicked.connect(
                self._start_area_drawing_polygon
            )
            self.dialog.btn_browse_download_output.clicked.connect(
                self._browse_download_output
            )
            self.dialog.btn_open_raster_source.clicked.connect(
                self._open_raster_source
            )
            self.dialog.btn_add_tinitaly_wcs.clicked.connect(
                self._add_tinitaly_wcs
            )
            self.dialog.btn_download_raster.clicked.connect(
                self._download_raster_area
            )
            self.dialog.btn_pick_insertion.clicked.connect(
                self._set_insertion_from_canvas_center
            )
            # Tab Confronto
            self.dialog.btn_refresh_gpkg.clicked.connect(
                self._refresh_confronto_gpkgs
            )
            self.dialog.btn_compare_gpkg.clicked.connect(
                self._run_gpkg_compare
            )
            self.dialog.btn_compare_dtm.clicked.connect(self._run_dtm_compare)
            # Export
            self.dialog.btn_export_csv.clicked.connect(self._export_csv)
            self.dialog.btn_export_vectors.clicked.connect(
                self._export_vectors
            )
            self.dialog.btn_export_dxf.clicked.connect(self._export_dxf)
            self.dialog.btn_export_png.clicked.connect(self._export_png)
            self.dialog.btn_export_pdf.clicked.connect(self._export_pdf)
            self.dialog.btn_print_layout.clicked.connect(self._print_layout)

        self.dialog.populate_rasters()
        self._refresh_confronto_gpkgs()
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
            self.area_tool.drawingCancelled.connect(
                self._on_drawing_cancelled
            )
        self.iface.mapCanvas().setMapTool(self.area_tool)
        self._push(
            "Download raster",
            "Tieni premuto SHIFT e trascina un rettangolo. / Hold SHIFT and "
            "drag a rectangle.",
        )

    def _start_area_drawing_polygon(self):
        """Activate the free-hand polygon tool for raster downloads: the
        raster is then clipped to the exact drawn shape, not just its
        bounding box."""
        self.dialog.hide()
        if not self.area_tool_polygon:
            self.area_tool_polygon = DrawPolygonAreaTool(
                self.iface.mapCanvas()
            )
            self.area_tool_polygon.areaDrawn.connect(self._on_area_drawn)
            self.area_tool_polygon.drawingCancelled.connect(
                self._on_drawing_cancelled
            )
        self.iface.mapCanvas().setMapTool(self.area_tool_polygon)
        self._push(
            "Download raster",
            "Clicca per aggiungere vertici, doppio click per chiudere il "
            "poligono. / Click to add vertices, double-click to close the "
            "polygon.",
        )

    def _on_area_drawn(self, points):
        canvas = self.iface.mapCanvas()
        # self.sender() is a QObject feature; ProfiliSezioniComuniPlugin is
        # a plain Python object, so it isn't available here. Both the
        # rectangle and polygon area tools connect to this same slot, and
        # whichever one just finished drawing is still the active map tool
        # at this point, so we can identify it that way instead.
        tool = canvas.mapTool()
        if tool in (self.area_tool, self.area_tool_polygon):
            canvas.unsetMapTool(tool)
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
            project = QgsProject.instance()
            # addMapLayer() would insert it at the top of the layer tree
            # (QGIS default), covering any profile/section vector layers
            # already drawn. Add it without a legend entry, then append it
            # at the bottom ourselves so it renders underneath them.
            project.addMapLayer(layer, False)
            project.layerTreeRoot().addLayer(layer)
            self._push(
                "TINITALY",
                "WCS TINITALY caricato / TINITALY WCS loaded.",
                "Success",
            )
            self.dialog.lbl_download_status.setText(
                "WCS TINITALY caricato in QGIS."
            )
        except Exception as e:
            msg = (
                f"{e}\n\n"
                "Il download area del plugin usera' comunque gli ZIP "
                "ufficiali TINITALY "
                "e non dipendera' dal WCS.\n"
                "The area download will use official TINITALY ZIP tiles and "
                "will not depend on WCS."
            )
            QMessageBox.warning(self.dialog, "TINITALY WCS", msg)
            self.dialog.lbl_download_status.setText(
                "WCS non disponibile; fallback ZIP ufficiali TINITALY attivo."
            )

    def _download_raster_area(self):
        if self._busy:
            self._push(
                "Errore / Error",
                "Un'altra operazione e' gia' in corso / Another operation "
                "is already running.",
                "Warning",
            )
            return
        area_points = self.dialog._download_area_points
        if not area_points:
            self._push(
                "Errore / Error",
                "Disegna prima un'area / Draw an area first.",
                "Critical",
            )
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
                "Il DTM Zenodo e' un dataset remoto di circa {size}; il "
                "plugin tentera' "
                "un ritaglio dell'area con GDAL/vsicurl. Continuare?\n\n"
                "Fonte: {url}\nLicenza: {license}"
            ).format(
                size=meta.get("size"),
                url=meta.get("url"),
                license=meta.get("license"),
            )
            yes = getattr(QMessageBox, "Yes", None)
            if yes is None and hasattr(QMessageBox, "StandardButton"):
                yes = QMessageBox.StandardButton.Yes
            if (
                QMessageBox.question(self.dialog, "Download HR-DTM-5m", msg)
                != yes
            ):
                return

        self.dialog.progress.setRange(0, 100)
        self.dialog.progress.setValue(0)
        self.dialog.progress.setVisible(True)
        self.dialog.lbl_download_status.setText(
            "Download/ritaglio in corso..."
        )
        # Disabled for the duration: QApplication.processEvents() below pumps
        # the event loop, so a second click here would re-enter this method
        # while GDAL/urllib handles from the first call are still open.
        self.dialog.btn_download_raster.setEnabled(False)
        self._busy = True
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
                group = self._new_output_group(
                    "Raster download / Downloaded raster", is_raster=True
                )
                self._add_output_layer(layer, group, "GeoTIFF")
            layer_msg = (
                " e caricato in QGIS" if layer and layer.isValid() else ""
            )
            self.dialog.lbl_download_status.setText(
                f"Raster salvato{layer_msg}: {final_path}"
            )
            self._push(
                "Download raster",
                f"Salvato / Saved: {final_path}. Fonte/licenza scritte nella "
                f"ricevuta.",
                "Success",
            )
        except Exception as e:
            QMessageBox.critical(self.dialog, "Download raster", str(e))
            self.dialog.lbl_download_status.setText(f"Errore / Error: {e}")
        finally:
            self._busy = False
            self.dialog.progress.setVisible(False)
            self.dialog.progress.setRange(0, 0)
            self.dialog.btn_download_raster.setEnabled(
                bool(self.dialog._download_area_points)
            )

    # ──────────────────────────────────────────────────────────────
    # Confronto prima/dopo — before/after comparison
    # ──────────────────────────────────────────────────────────────

    def _refresh_confronto_gpkgs(self):
        try:
            gpkg_paths = list_output_gpkgs(self._default_output_dir())
            self.dialog.populate_confronto_gpkgs(gpkg_paths)
            self.dialog.lbl_confronto_status.setText(
                f"{len(gpkg_paths)} GeoPackage trovati in "
                f"{self._default_output_dir()} / GeoPackages found."
            )
        except Exception as e:
            self.dialog.lbl_confronto_status.setText(f"Errore / Error: {e}")

    def _run_gpkg_compare(self):
        if self._busy:
            self._push(
                "Confronto / Comparison",
                "Un'altra operazione e' gia' in corso / Another operation "
                "is already running.",
                "Warning",
            )
            return
        path_before, path_after = self.dialog.get_confronto_gpkg_paths()
        if not path_before or not path_after:
            self._push(
                "Confronto / Comparison",
                "Seleziona i GeoPackage prima e dopo / Select before and "
                "after GeoPackages.",
                "Critical",
            )
            return
        if path_before == path_after:
            self._push(
                "Confronto / Comparison",
                "I due GeoPackage coincidono / The two GeoPackages are the "
                "same file.",
                "Warning",
            )
            return

        self.dialog.lbl_confronto_status.setText(
            "Confronto in corso... / Comparing..."
        )
        self.dialog.progress.setVisible(True)
        self.dialog.btn_compare_gpkg.setEnabled(False)
        self._busy = True
        QApplication.processEvents()
        try:
            before = read_gpkg_summary(path_before)
            after = read_gpkg_summary(path_after)
            tolerance = self.dialog.sb_conf_tolerance.value()
            result = compare_gpkg_summaries(before, after, tolerance)

            chart_html = ""
            if result["profile_deltas"]:
                svg = generate_delta_svg(
                    result["profile_deltas"], self.dialog.lang
                )
                self.last_svg_content = svg
                chart_path = self._svg_to_png(svg, "confronto")
                self.last_chart_png_path = chart_path
                chart_html = self._chart_image_html(chart_path, svg)

            results_html = generate_compare_html(
                result, self.dialog.lang, chart_html
            )
            self.last_results_html = results_html
            self.dialog.show_results(results_html, "confronto")
            self.dialog.lbl_confronto_status.setText(
                "Confronto GeoPackage completato / GeoPackage comparison done."
            )
            self.dialog.lbl_status.setText(
                "Confronto prima/dopo completato / Before-after comparison "
                "done."
            )
        except Exception as e:
            self._push("Confronto / Comparison", str(e), "Critical")
            self.dialog.lbl_confronto_status.setText(f"Errore / Error: {e}")
        finally:
            self._busy = False
            self.dialog.progress.setVisible(False)
            self.dialog.btn_compare_gpkg.setEnabled(True)

    def _run_dtm_compare(self):
        if self._busy:
            self._push(
                "Confronto DTM / DTM comparison",
                "Un'altra operazione e' gia' in corso / Another operation "
                "is already running.",
                "Warning",
            )
            return
        path_before, path_after = self.dialog.get_confronto_dtm_paths()
        if not path_before or not path_after:
            self._push(
                "Confronto DTM / DTM comparison",
                "Seleziona i DTM prima e dopo / Select before and after DTMs.",
                "Critical",
            )
            return
        if path_before == path_after:
            self._push(
                "Confronto DTM / DTM comparison",
                "I due DTM coincidono / The two DTMs are the same file.",
                "Warning",
            )
            return

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(
            self._default_output_dir(), f"dtm_diff_{stamp}.tif"
        )

        self.dialog.progress.setRange(0, 100)
        self.dialog.progress.setValue(0)
        self.dialog.progress.setVisible(True)
        self.dialog.lbl_confronto_status.setText(
            "Calcolo differenza DTM... / Computing DTM difference..."
        )
        self.dialog.btn_compare_dtm.setEnabled(False)
        self._busy = True
        QApplication.processEvents()

        def _progress(percent=0, message="", **kwargs):
            self.dialog.progress.setValue(max(0, min(int(percent or 0), 100)))
            if message:
                self.dialog.lbl_confronto_status.setText(message)
            QApplication.processEvents()

        try:
            threshold = self.dialog.sb_dtm_threshold.value()
            result = compare_dtm_rasters(
                path_before, path_after, output_path, threshold, _progress
            )

            if self.dialog.chk_load_diff_raster.isChecked():
                layer = self._load_diff_raster(result)
                if layer:
                    group = self._new_output_group(
                        "Confronto DTM / DTM comparison", is_raster=True
                    )
                    self._add_output_layer(
                        layer, group, "Differenza / Difference"
                    )

            results_html = generate_dtm_compare_html(
                result, path_before, path_after, self.dialog.lang
            )
            self.last_results_html = results_html
            self.dialog.show_results(results_html, "confronto")
            self.dialog.lbl_confronto_status.setText(
                f"Raster differenza salvato / Difference raster saved: "
                f"{result['diff_path']}"
            )
            self._push(
                "Confronto DTM / DTM comparison",
                "Differenza salvata / Difference saved: "
                f"{result['diff_path']}",
                "Success",
            )
        except Exception as e:
            QMessageBox.critical(
                self.dialog, "Confronto DTM / DTM comparison", str(e)
            )
            self.dialog.lbl_confronto_status.setText(f"Errore / Error: {e}")
        finally:
            self._busy = False
            self.dialog.progress.setVisible(False)
            self.dialog.progress.setRange(0, 0)
            self.dialog.btn_compare_dtm.setEnabled(True)

    def _load_diff_raster(self, result):
        """Load the DoD raster with a diverging ramp: red=cut, blue=fill."""
        from qgis.core import QgsRasterLayer

        layer = QgsRasterLayer(
            result["diff_path"], os.path.basename(result["diff_path"])
        )
        if not layer.isValid():
            return None
        try:
            from qgis.PyQt.QtGui import QColor
            from qgis.core import (
                QgsColorRampShader,
                QgsRasterShader,
                QgsSingleBandPseudoColorRenderer,
            )

            abs_max = max(
                abs(result.get("min_dz") or 0.0),
                abs(result.get("max_dz") or 0.0),
                result.get("threshold_m") or 0.05,
            )
            threshold = result.get("threshold_m") or 0.05
            shader_fn = QgsColorRampShader()
            shader_fn.setColorRampType(QgsColorRampShader.Type.Interpolated)
            items = [
                QgsColorRampShader.ColorRampItem(
                    -abs_max, QColor("#67001f"), f"{-abs_max:.2f} m"
                ),
                QgsColorRampShader.ColorRampItem(
                    -threshold, QColor("#f4a582"), f"{-threshold:.2f} m"
                ),
                QgsColorRampShader.ColorRampItem(
                    0.0, QColor("#f7f7f7"), "0.00 m"
                ),
                QgsColorRampShader.ColorRampItem(
                    threshold, QColor("#92c5de"), f"{threshold:.2f} m"
                ),
                QgsColorRampShader.ColorRampItem(
                    abs_max, QColor("#053061"), f"{abs_max:.2f} m"
                ),
            ]
            shader_fn.setColorRampItemList(items)
            shader = QgsRasterShader()
            shader.setRasterShaderFunction(shader_fn)
            renderer = QgsSingleBandPseudoColorRenderer(
                layer.dataProvider(), 1, shader
            )
            renderer.setClassificationMin(-abs_max)
            renderer.setClassificationMax(abs_max)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
        except Exception:  # nosec B110
            pass
        return layer

    def _set_insertion_from_canvas_center(self):
        center = self.iface.mapCanvas().extent().center()
        self.dialog.sb_insertion_x.setValue(center.x())
        self.dialog.sb_insertion_y.setValue(center.y())
        self.dialog.lbl_status.setText(
            "Punto di inserimento impostato dal centro mappa / Insertion "
            "point set from map center."
        )

    # ──────────────────────────────────────────────────────────────
    # Drawing
    # ──────────────────────────────────────────────────────────────

    def _start_drawing(self, mode):
        """Activate the polyline drawing map tool."""
        self.current_mode = mode
        express_raster = None

        if mode == "sections":
            raster = self.dialog.get_selected_raster(for_sezioni=True)
            if not raster:
                self._push(
                    "Errore / Error",
                    "Seleziona prima un layer Raster (DEM/DTM) / Select a "
                    "Raster Layer first.",
                    "Critical",
                )
                return
            express_raster = raster

        if mode == "profile":
            source = self.dialog.cb_source.currentData()
            if source == "raster":
                raster = self.dialog.get_selected_raster(for_sezioni=False)
                if not raster:
                    self._push(
                        "Errore / Error",
                        "Seleziona prima un layer Raster (DEM/DTM) / Select a "
                        "Raster Layer first.",
                        "Critical",
                    )
                    return
                express_raster = raster

        self._profilo_express_raster = None
        if self.dialog.chk_profilo_express.isChecked():
            if express_raster is not None:
                self._profilo_express_raster = express_raster
            else:
                self._push(
                    "ProfiloExpress",
                    "Richiede un raster locale come sorgente, non "
                    "disponibile per le sorgenti online / Requires a "
                    "local raster source, not available for online "
                    "sources.",
                    "Warning",
                )

        self.dialog.hide()

        if not self.map_tool:
            self.map_tool = DrawPolylineTool(self.iface.mapCanvas())
            self.map_tool.lineDrawn.connect(self._on_line_drawn)
            self.map_tool.previewChanged.connect(
                self._on_profilo_express_preview
            )
            self.map_tool.drawingCancelled.connect(
                self._on_drawing_cancelled
            )

        self.iface.mapCanvas().setMapTool(self.map_tool)
        self._push(
            "Info",
            "Clicca per aggiungere vertici. Doppio click per terminare. "
            "/ Click to add vertices. Double-click to finish.",
        )

    # ──────────────────────────────────────────────────────────────
    # ProfiloExpress — live profile preview while drawing
    # ──────────────────────────────────────────────────────────────

    def _ensure_profilo_express_preview(self):
        if self._profilo_express_preview is None:
            from qgis.PyQt.QtWidgets import QDialog, QLabel, QVBoxLayout

            dlg = QDialog(self.iface.mainWindow())
            dlg.setWindowTitle("ProfiloExpress")
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.Tool)
            layout = QVBoxLayout(dlg)
            label = QLabel()
            layout.addWidget(label)
            dlg._preview_label = label
            self._profilo_express_preview = dlg
        return self._profilo_express_preview

    def _hide_profilo_express_preview(self):
        if self._profilo_express_preview is not None:
            self._profilo_express_preview.hide()

    def _on_drawing_cancelled(self):
        """User right-clicked to cancel, or the tool got deactivated, while
        drawing was in progress: close any live preview and bring our
        dialog back so it doesn't just vanish with no way back short of
        re-clicking the toolbar icon."""
        self._hide_profilo_express_preview()
        if self.dialog is not None:
            self.dialog.show()
            self.dialog.raise_()

    def _svg_to_pixmap(self, svg_content):
        from qgis.PyQt.QtCore import QByteArray
        from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPixmap
        from qgis.PyQt.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(QByteArray(svg_content.encode("utf-8")))
        if not renderer.isValid():
            return None
        size = renderer.defaultSize()
        if size.isEmpty():
            return None
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.fill(QColor("#12151e"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        return QPixmap.fromImage(image)

    def _on_profilo_express_preview(self, points):
        """Fired on every DrawPolylineTool preview tick. Cheap no-op when
        ProfiloExpress isn't active for the current draw session; must
        never raise, since it runs on ordinary mouse movement."""
        raster = self._profilo_express_raster
        if raster is None or len(points) < 2:
            return
        try:
            points_data, total_dist = calculate_profile(
                points, "raster", raster, sample_count=40
            )
            svg = generate_quick_profile_svg(points_data, total_dist)
            pixmap = self._svg_to_pixmap(svg)
            if pixmap is None:
                return
            dlg = self._ensure_profilo_express_preview()
            dlg._preview_label.setPixmap(pixmap)
            dlg.adjustSize()
            if not dlg.isVisible():
                dlg.show()
        except Exception:  # nosec B110 - best-effort live preview
            pass

    # ──────────────────────────────────────────────────────────────
    # Callback from map tool
    # ──────────────────────────────────────────────────────────────

    def _on_line_drawn(self, points):
        self.iface.mapCanvas().unsetMapTool(self.map_tool)
        self._last_drawn_points = points
        self._hide_profilo_express_preview()

        self.dialog.lbl_status.setText("Calcolo in corso... / Computing...")
        self.dialog.progress.setVisible(True)
        self.dialog.show()
        QApplication.processEvents()

        try:
            if self.current_mode == "sections":
                self.last_profile_svg = None
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
        raster = self.dialog.get_selected_raster(
            for_sezioni=True
        ) or self.dialog.get_selected_raster(for_sezioni=False)
        if not raster:
            return False

        question = (
            "Hai tracciato il profilo. Vuoi calcolare subito anche le sezioni "
            "trasversali, "
            "gli sterri/riporti e i volumi usando lo stesso asse?\n\n"
            "You traced a profile. Do you also want to compute cross "
            "sections, cut/fill and volumes "
            "using the same axis?"
        )
        yes = getattr(QMessageBox, "Yes", None)
        no = getattr(QMessageBox, "No", None)
        if yes is None and hasattr(QMessageBox, "StandardButton"):
            yes = QMessageBox.StandardButton.Yes
        if no is None and hasattr(QMessageBox, "StandardButton"):
            no = QMessageBox.StandardButton.No
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
            if source == "raster"
            else None
        )
        samples_count = self.dialog.sb_samples_prof.value()

        points_data, total_dist = calculate_profile(
            points, source, raster_layer, samples_count
        )
        self.last_points_data = points_data
        self.last_total_dist = total_dist
        self.last_cross_sections_data = None

        raster_name = raster_layer.name() if raster_layer else ""
        labels = self.dialog.get_parameter_labels()
        svg = generate_profile_svg(
            points_data, total_dist, source, raster_name, labels
        )
        self.last_svg_content = svg
        self.last_profile_svg = svg
        self.last_sections_svg = None
        chart_path = self._svg_to_png(svg, "profilo")
        self.last_chart_png_path = chart_path
        chart_html = self._chart_image_html(chart_path, svg)

        svg_pegstyle = generate_profile_svg_pegstyle(
            points_data, total_dist, labels
        )
        chart_path_pegstyle = self._svg_to_png(svg_pegstyle, "profilo_pegstyle")
        chart_html_pegstyle = self._chart_image_html(
            chart_path_pegstyle, svg_pegstyle
        )

        results_html = generate_profile_results_html(
            points_data,
            total_dist,
            source,
            raster_name,
            svg,
            labels,
            chart_html,
            svg_pegstyle,
            chart_html_pegstyle,
        )
        self.last_results_html = results_html
        vector_layers = self._create_profile_vector_layers(
            points_data,
            total_dist,
            points,
            chart_path,
            create_3d=self.dialog.chk_3d_prof.isChecked(),
        )
        try:
            gpkg_path = self._register_vector_outputs(vector_layers, "profilo")
            if gpkg_path:
                results_html += (
                    "<div class='summary-card'><strong>GeoPackage vettoriale "
                    "/ Vector GeoPackage:</strong> "
                    f"{gpkg_path}</div>"
                )
                self.last_results_html = results_html
        except Exception as e:
            self._push(
                "Export GPKG",
                f"GeoPackage non creato / not created: {e}",
                "Warning",
            )
        self.dialog.show_results(results_html, "profilo")

        dist_label = (
            f"{total_dist / 1000:.3f} km"
            if total_dist >= 1000
            else f"{total_dist:.1f} m"
        )
        self.dialog.lbl_status.setText(
            f"Profilo calcolato / Profile calculated — {dist_label}, "
            f"{len(points_data)} campioni"
        )

    def _create_profile_vector_layers(
        self, points_data, total_dist, axis_points, chart_path=None,
        create_3d=False,
    ):
        from qgis.core import (
            QgsVectorLayer,
            QgsFeature,
            QgsGeometry,
            QgsCoordinateTransform,
            QgsCoordinateReferenceSystem,
        )

        project = QgsProject.instance()
        crs_auth = project.crs().authid() or "EPSG:4326"
        stamp = datetime.datetime.now().strftime("%H%M%S")
        group = self._new_output_group(
            "Profilo altimetrico / Elevation profile"
        )
        layers = []

        axis_layer = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=id:integer&field=length_m:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"profilo_asse_{stamp}",
            "memory",
        )
        feat_axis = QgsFeature()
        feat_axis.setGeometry(QgsGeometry.fromPolylineXY(axis_points))
        feat_axis.setAttributes(
            [
                1,
                float(total_dist or 0),
                "Asse profilo / Profile axis",
                chart_path or "",
            ]
        )
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
        feats = []
        for idx, p in enumerate(points_data, 1):
            f = QgsFeature()
            f.setGeometry(
                QgsGeometry.fromPointXY(
                    QgsPointXY(p.get("x", 0), p.get("y", 0))
                )
            )
            f.setAttributes(
                [
                    idx,
                    float(p.get("distance_m") or 0),
                    (
                        float(p.get("elevation"))
                        if p.get("elevation") is not None
                        else None
                    ),
                    float(p.get("lon") or 0),
                    float(p.get("lat") or 0),
                    chart_path or "",
                ]
            )
            feats.append(f)
        if feats:
            samples_layer.dataProvider().addFeatures(feats)
            samples_layer.updateExtents()
            self._style_marker_layer(samples_layer, "#4f73c4", 1.7)
            self._add_output_layer(
                samples_layer, group, "02 Campioni / Samples"
            )
            layers.append(samples_layer)

        pickets = build_pickets(points_data, total_dist)
        pickets_layer = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=id:string(16)&field=prog_m:double"
            f"&field=parziale_m:double&field=quota_m:double&field=lon:double"
            f"&field=lat:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"profilo_picchetti_{stamp}",
            "memory",
        )
        crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = QgsCoordinateTransform(
            crs_wgs, project.crs(), project.transformContext()
        )
        feats = []
        for p in pickets:
            pt = xform.transform(QgsPointXY(p.get("lon", 0), p.get("lat", 0)))
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(pt))
            f.setAttributes(
                [
                    p.get("id"),
                    float(p.get("progressive_m") or 0),
                    float(p.get("partial_m") or 0),
                    (
                        float(p.get("elevation"))
                        if p.get("elevation") is not None
                        else None
                    ),
                    float(p.get("lon") or 0),
                    float(p.get("lat") or 0),
                    "{0} {1:.1f} m".format(
                        p.get("id"), float(p.get("progressive_m") or 0)
                    ),
                    chart_path or "",
                ]
            )
            feats.append(f)
        if feats:
            pickets_layer.dataProvider().addFeatures(feats)
            pickets_layer.updateExtents()
            self._style_marker_layer(
                pickets_layer,
                "#ffffff",
                2.5,
                "label",
                min_label_scale=self.DENSE_LABEL_MIN_SCALE,
            )
            self._add_output_layer(
                pickets_layer, group, "03 Picchetti / Pickets"
            )
            layers.append(pickets_layer)

        valid = [
            p for p in points_data if p.get("elevation") is not None
        ]
        if valid:
            elevs = [float(p["elevation"]) for p in valid]
            min_z, max_z = min(elevs), max(elevs)
            vertical_exag = self._nice_exaggeration(
                max_z - min_z, float(total_dist or 1.0)
            )
            labels = self.dialog.get_parameter_labels()
            diag_x = float(labels.get("insertion_x") or 0.0)
            diag_y = float(labels.get("insertion_y") or 0.0)
            if diag_x == 0.0 and diag_y == 0.0:
                try:
                    center = self.iface.mapCanvas().extent().center()
                    diag_x, diag_y = center.x(), center.y()
                except Exception:  # nosec B110
                    pass
            diag_origin = QgsPointXY(diag_x, diag_y)

            diagram_layer = QgsVectorLayer(
                f"LineString?crs={crs_auth}&field=id:integer"
                f"&field=chart_png:string(254)",
                f"profilo_diagramma_{stamp}",
                "memory",
            )
            diag_pts = [
                QgsPointXY(
                    diag_origin.x() + float(p["distance_m"] or 0.0),
                    diag_origin.y()
                    + (float(p["elevation"]) - min_z) * vertical_exag,
                )
                for p in valid
            ]
            f_diag = QgsFeature()
            f_diag.setGeometry(QgsGeometry.fromPolylineXY(diag_pts))
            f_diag.setAttributes([1, chart_path or ""])
            diagram_layer.dataProvider().addFeature(f_diag)
            diagram_layer.updateExtents()
            self._style_line_layer(diagram_layer, "#34d399", 0.7)
            self._add_output_layer(
                diagram_layer, group, "04 Diagramma quotato / Elevation chart"
            )
            layers.append(diagram_layer)

            layers.extend(
                self._build_chart_annotations(
                    crs_auth,
                    stamp,
                    group,
                    [
                        {
                            "origin": diag_origin,
                            "x_span": float(total_dist or 1.0),
                            "z_min": min_z,
                            "z_max": max_z,
                            "exag": vertical_exag,
                            "title": "Profilo / Profile",
                        }
                    ],
                )
            )

        if create_3d:
            xyz_points = [
                (p.get("x"), p.get("y"), p.get("elevation"))
                for p in points_data
            ]
            layers.extend(
                self._create_3d_axis_and_points(
                    xyz_points,
                    crs_auth,
                    group,
                    stamp,
                    "profilo",
                    "Profilo / Profile",
                    "#34d399",
                )
            )

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
            points,
            raster_layer,
            interval_m,
            half_width_m,
            samples,
            design_start,
            design_end,
            design_grade,
            smooth,
        )
        self.last_cross_sections_data = data
        self.last_points_data = None

        project_title = (
            QgsProject.instance().title()
            or QgsProject.instance().baseName()
            or "Project"
        )
        created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.last_svg_content = generate_cross_sections_svg(
            data, project_title, created_at
        )
        self.last_sections_svg = self.last_svg_content
        chart_path = self._svg_to_png(self.last_svg_content, "sezioni")
        self.last_chart_png_path = chart_path
        chart_html = self._chart_image_html(chart_path, self.last_svg_content)

        labels = self.dialog.get_parameter_labels()
        results_html = (
            "<div class='summary-card'>"
            "<strong>Parametri movimento terra / Earthwork "
            "parameters</strong><br>"
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
            balance=(data.get("total_cut_m3") or 0)
            - (data.get("total_fill_m3") or 0),
        )
        results_html += (
            "<div class='section-title'>Grafici sezioni / Section Charts</div>"
            f"<div class='chart-container'>{chart_html}</div>"
        )
        results_html += generate_cross_sections_results_html(data)
        self.last_results_html = results_html

        vector_layers = self._create_vector_layers(
            data,
            points,
            chart_path,
            create_3d=self.dialog.chk_3d_sez.isChecked(),
        )
        try:
            gpkg_path = self._register_vector_outputs(vector_layers, "sezioni")
            if gpkg_path:
                results_html += (
                    "<div class='summary-card'><strong>GeoPackage vettoriale "
                    "/ Vector GeoPackage:</strong> "
                    f"{gpkg_path}</div>"
                )
                self.last_results_html = results_html
        except Exception as e:
            self._push(
                "Export GPKG",
                f"GeoPackage non creato / not created: {e}",
                "Warning",
            )
        self.dialog.show_results(results_html, "sezioni")

        self.dialog.lbl_status.setText(
            f"Sezioni calcolate / Sections calculated — "
            f"{data['n_sections']} sezioni su "
            f"{data['alignment_length_m']:.1f} m"
        )

    # ──────────────────────────────────────────────────────────────
    # Sections from existing line layer
    # ──────────────────────────────────────────────────────────────

    def _run_sections_from_layer(self):
        raster_layer = self.dialog.get_selected_raster(for_sezioni=True)
        line_layer = self.dialog.get_selected_line_layer()
        if not raster_layer:
            self._push(
                "Errore / Error",
                "Seleziona un layer Raster / Select a Raster layer.",
                "Critical",
            )
            return
        if not line_layer:
            self._push(
                "Errore / Error",
                "Seleziona un layer linea / Select a line layer.",
                "Critical",
            )
            return

        self.current_mode = "sections"
        self.last_profile_svg = None
        self.dialog.lbl_status.setText(
            "Calcolo sezioni da layer... / Computing sections from layer..."
        )
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
            line = max(
                candidates,
                key=lambda pts: sum(
                    (
                        (pts[i - 1].x() - pts[i].x()) ** 2
                        + (pts[i - 1].y() - pts[i].y()) ** 2
                    )
                    ** 0.5
                    for i in range(1, len(pts))
                ),
            )
        else:
            line = candidates[0]

        project_crs = QgsProject.instance().crs()
        if layer.crs() != project_crs:
            xform = QgsCoordinateTransform(
                layer.crs(),
                project_crs,
                QgsProject.instance().transformContext(),
            )
            return [QgsPointXY(xform.transform(QgsPointXY(p))) for p in line]
        return [QgsPointXY(p) for p in line]

    # ──────────────────────────────────────────────────────────────
    # Vector layer creation (for cross sections)
    # ──────────────────────────────────────────────────────────────

    def _create_vector_layers(
        self, data, points, chart_path=None, create_3d=False
    ):
        import time
        from qgis.core import (
            QgsVectorLayer,
            QgsFeature,
            QgsGeometry,
            QgsCoordinateTransform,
            QgsCoordinateReferenceSystem,
        )

        project = QgsProject.instance()
        crs_auth = project.crs().authid() or "EPSG:4326"
        stamp = int(time.time()) % 10000
        group = self._new_output_group(
            "Sezioni e volumi / Sections and volumes"
        )
        layers = []

        # Axis layer
        vl_asse = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=id:integer&field=length_m:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Asse {stamp}",
            "memory",
        )
        pr_asse = vl_asse.dataProvider()
        feat_asse = QgsFeature()
        feat_asse.setGeometry(QgsGeometry.fromPolylineXY(points))
        feat_asse.setAttributes(
            [
                1,
                data.get("alignment_length_m", 0),
                "Asse / Alignment",
                chart_path or "",
            ]
        )
        pr_asse.addFeature(feat_asse)
        vl_asse.updateExtents()
        self._style_line_layer(vl_asse, "#f5a623", 0.9, "label")
        self._add_output_layer(vl_asse, group, "01 Asse / Axis")
        layers.append(vl_asse)

        crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        xform_from_wgs = QgsCoordinateTransform(
            crs_wgs, project.crs(), project.transformContext()
        )

        # Curve points
        curves = data.get("curve_points", [])
        if curves:
            vl_curve = QgsVectorLayer(
                f"Point?crs={crs_auth}&field=id_curva:integer"
                f"&field=prog_m:double"
                f"&field=label:string(80)&field=chart_png:string(254)",
                f"Curve {stamp}",
                "memory",
            )
            pr_c = vl_curve.dataProvider()
            feats_c = []
            for c in curves:
                lon, lat = c.get("lon"), c.get("lat")
                if lon is not None and lat is not None:
                    pt_map = xform_from_wgs.transform(QgsPointXY(lon, lat))
                    f_c = QgsFeature()
                    f_c.setGeometry(QgsGeometry.fromPointXY(pt_map))
                    f_c.setAttributes(
                        [
                            c.get("index"),
                            c.get("progressive_m"),
                            "C{0} R={1}".format(
                                c.get("index"),
                                c.get("estimated_radius_m") or "n/d",
                            ),
                            chart_path or "",
                        ]
                    )
                    feats_c.append(f_c)
            if feats_c:
                pr_c.addFeatures(feats_c)
                vl_curve.updateExtents()
                self._style_marker_layer(vl_curve, "#f97316", 3.0, "label")
                self._add_output_layer(vl_curve, group, "05 Curve / Curves")
                layers.append(vl_curve)

        # Cross section lines
        vl_sez = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=sezione:integer"
            f"&field=prog_m:double&field=area:double"
            f"&field=cut_m2:double&field=fill_m2:double&field=quota_prj:double"
            f"&field=label:string(80)&field=colore:string(16)"
            f"&field=chart_png:string(254)",
            f"Sezioni {stamp}",
            "memory",
        )
        pr_sez = vl_sez.dataProvider()

        vl_pts = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=sezione:integer"
            f"&field=offset_m:double&field=quota:double&field=lon:double"
            f"&field=lat:double"
            f"&field=chart_png:string(254)",
            f"Punti Sezione {stamp}",
            "memory",
        )
        pr_pts = vl_pts.dataProvider()

        vl_centers = QgsVectorLayer(
            f"Point?crs={crs_auth}&field=sezione:integer&field=prog_m:double"
            f"&field=quota_min:double&field=quota_max:double"
            f"&field=quota_prj:double"
            f"&field=area_m2:double&field=cut_m2:double&field=fill_m2:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Centri Sezione {stamp}",
            "memory",
        )
        pr_centers = vl_centers.dataProvider()

        feats_sez = []
        feats_pts = []
        feats_centers = []
        for sec in data.get("sections", []):
            pts_sec = sec.get("points", [])
            if not pts_sec:
                continue
            center_lon = sec.get("center_lon")
            center_lat = sec.get("center_lat")
            if center_lon is not None and center_lat is not None:
                center_pt = xform_from_wgs.transform(
                    QgsPointXY(center_lon, center_lat)
                )
                f_center = QgsFeature()
                f_center.setGeometry(QgsGeometry.fromPointXY(center_pt))
                f_center.setAttributes(
                    [
                        sec.get("index"),
                        sec.get("progressive_m"),
                        sec.get("min_elevation"),
                        sec.get("max_elevation"),
                        sec.get("design_elevation"),
                        sec.get("area_m2") or 0.0,
                        sec.get("cut_area_m2") or 0.0,
                        sec.get("fill_area_m2") or 0.0,
                        "S{0:02d} {1:.1f} m".format(
                            int(sec.get("index") or 0),
                            float(sec.get("progressive_m") or 0),
                        ),
                        chart_path or "",
                    ]
                )
                feats_centers.append(f_center)
            line_pts = []
            for p in pts_sec:
                lon, lat = p.get("lon"), p.get("lat")
                if lon is not None and lat is not None:
                    pt_map = xform_from_wgs.transform(QgsPointXY(lon, lat))
                    line_pts.append(pt_map)
                    f_pt = QgsFeature()
                    f_pt.setGeometry(QgsGeometry.fromPointXY(pt_map))
                    f_pt.setAttributes(
                        [
                            sec.get("index"),
                            p.get("offset_m"),
                            p.get("elevation") or 0.0,
                            lon,
                            lat,
                            chart_path or "",
                        ]
                    )
                    feats_pts.append(f_pt)
            if len(line_pts) >= 2:
                f_sez = QgsFeature()
                f_sez.setGeometry(QgsGeometry.fromPolylineXY(line_pts))
                f_sez.setAttributes(
                    [
                        sec.get("index"),
                        sec.get("progressive_m"),
                        sec.get("area_m2") or 0.0,
                        sec.get("cut_area_m2") or 0.0,
                        sec.get("fill_area_m2") or 0.0,
                        sec.get("design_elevation"),
                        "S{0:02d} {1:.1f} m".format(
                            int(sec.get("index") or 0),
                            float(sec.get("progressive_m") or 0),
                        ),
                        "#4f73c4",
                        chart_path or "",
                    ]
                )
                feats_sez.append(f_sez)

        if feats_sez:
            pr_sez.addFeatures(feats_sez)
            vl_sez.updateExtents()
            self._style_line_layer(
                vl_sez,
                "#4f73c4",
                0.65,
                "label",
                min_label_scale=self.DENSE_LABEL_MIN_SCALE,
            )
            self._add_output_layer(
                vl_sez,
                group,
                "02 Sezioni planimetriche / Planimetric sections",
            )
            layers.append(vl_sez)
        if feats_pts:
            pr_pts.addFeatures(feats_pts)
            vl_pts.updateExtents()
            self._style_marker_layer(vl_pts, "#8ba3c7", 1.4)
            self._add_output_layer(
                vl_pts, group, "03 Punti sezione / Section points"
            )
            layers.append(vl_pts)
        if feats_centers:
            pr_centers.addFeatures(feats_centers)
            vl_centers.updateExtents()
            self._style_marker_layer(
                vl_centers,
                "#ffffff",
                2.6,
                "label",
                min_label_scale=self.DENSE_LABEL_MIN_SCALE,
            )
            self._add_output_layer(
                vl_centers, group, "04 Centri sezione / Section centers"
            )
            layers.append(vl_centers)

        vl_vol = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=da_sez:integer"
            f"&field=a_sez:integer"
            f"&field=dist_m:double&field=volume_m3:double"
            f"&field=sterro_m3:double"
            f"&field=riporto_m3:double&field=cum_sterro:double"
            f"&field=cum_riporto:double"
            f"&field=label:string(80)&field=chart_png:string(254)",
            f"Tratte Volumi {stamp}",
            "memory",
        )
        pr_vol = vl_vol.dataProvider()
        sections_by_index = {
            s.get("index"): s for s in data.get("sections", [])
        }
        feats_vol = []
        for v in data.get("volumes", []):
            s1 = sections_by_index.get(v.get("from_section"))
            s2 = sections_by_index.get(v.get("to_section"))
            if not s1 or not s2:
                continue
            if None in (
                s1.get("center_lon"),
                s1.get("center_lat"),
                s2.get("center_lon"),
                s2.get("center_lat"),
            ):
                continue
            p1 = xform_from_wgs.transform(
                QgsPointXY(s1.get("center_lon"), s1.get("center_lat"))
            )
            p2 = xform_from_wgs.transform(
                QgsPointXY(s2.get("center_lon"), s2.get("center_lat"))
            )
            f_vol = QgsFeature()
            f_vol.setGeometry(QgsGeometry.fromPolylineXY([p1, p2]))
            f_vol.setAttributes(
                [
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
                ]
            )
            feats_vol.append(f_vol)
        if feats_vol:
            pr_vol.addFeatures(feats_vol)
            vl_vol.updateExtents()
            self._style_line_layer(vl_vol, "#a78bfa", 0.75, "label")
            self._add_output_layer(vl_vol, group, "06 Volumi / Volumes")
            layers.append(vl_vol)

        layers.extend(
            self._create_section_drawing_layers(data, stamp, chart_path, group)
        )

        if create_3d:
            layers.extend(
                self._create_sections_3d_layers(
                    data, crs_auth, group, stamp, xform_from_wgs
                )
            )

        return layers

    def _create_section_drawing_layers(
        self, data, stamp, chart_path=None, group=None
    ):
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
            except Exception:  # nosec B110
                pass

        sections = data.get("sections") or []
        if not sections:
            return []

        half_width = float(data.get("half_width_m") or 20.0)
        section_width = max(half_width * 2.0, 1.0)
        max_relief = 1.0
        for sec in sections:
            elevs = [
                p.get("elevation")
                for p in sec.get("points", [])
                if p.get("elevation") is not None
            ]
            if elevs:
                max_relief = max(max_relief, max(elevs) - min(elevs))
        cols = 4
        gap_x = max(section_width * 0.45, 15.0)
        vertical_exag = self._nice_exaggeration(max_relief, section_width)
        gap_y = max(max_relief * vertical_exag * 0.5, 18.0)

        vl_lines = QgsVectorLayer(
            f"LineString?crs={crs_auth}&field=sezione:integer"
            f"&field=prog_m:double"
            f"&field=tipo:string(16)&field=label:string(80)"
            f"&field=colore:string(16)"
            f"&field=chart_png:string(254)",
            f"Disegni Sezioni Linee {stamp}",
            "memory",
        )
        vl_poly = QgsVectorLayer(
            f"Polygon?crs={crs_auth}&field=sezione:integer"
            f"&field=prog_m:double"
            f"&field=tipo:string(16)&field=area_m2:double"
            f"&field=label:string(80)"
            f"&field=colore:string(16)&field=chart_png:string(254)",
            f"Disegni Sezioni Sterro Riporto {stamp}",
            "memory",
        )
        pr_lines = vl_lines.dataProvider()
        pr_poly = vl_poly.dataProvider()
        line_features = []
        polygon_features = []

        def _panel_origin(sec_index):
            col = sec_index % cols
            row = sec_index // cols
            base_x = origin_x + col * (section_width + gap_x)
            base_y = origin_y - row * (max_relief * vertical_exag + gap_y)
            return base_x, base_y

        def _local_xy(sec_index, offset, elevation, min_z):
            base_x, base_y = _panel_origin(sec_index)
            return QgsPointXY(
                base_x + half_width + float(offset or 0.0),
                base_y + (float(elevation) - float(min_z)) * vertical_exag,
            )

        panels = []
        for idx, sec in enumerate(sections):
            pts = [
                p
                for p in sec.get("points", [])
                if p.get("elevation") is not None
            ]
            if len(pts) < 2:
                continue
            elevs = [float(p.get("elevation")) for p in pts]
            min_z = min(elevs)
            design_elev = sec.get("design_elevation")
            frame_values = elevs + (
                [float(design_elev)] if design_elev is not None else []
            )
            base_x, base_y = _panel_origin(idx)
            panel_z_min = min(frame_values)
            panels.append(
                {
                    "origin": QgsPointXY(
                        base_x,
                        base_y + (panel_z_min - min_z) * vertical_exag,
                    ),
                    "x_span": section_width,
                    "z_min": panel_z_min,
                    "z_max": max(frame_values),
                    "exag": vertical_exag,
                    "title": "S{0:02d}".format(int(sec.get("index") or 0)),
                }
            )
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
            f_ground.setAttributes(
                [
                    sec.get("index"),
                    sec.get("progressive_m"),
                    "terreno",
                    label + " terreno",
                    "#4f73c4",
                    chart_path or "",
                ]
            )
            line_features.append(f_ground)

            design = sec.get("design_elevation")
            if design is not None:
                left_pt = _local_xy(idx, -half_width, design, min_z)
                right_pt = _local_xy(idx, half_width, design, min_z)
                f_design = QgsFeature()
                f_design.setGeometry(
                    QgsGeometry.fromPolylineXY([left_pt, right_pt])
                )
                f_design.setAttributes(
                    [
                        sec.get("index"),
                        sec.get("progressive_m"),
                        "progetto",
                        label + " progetto",
                        "#f59e0b",
                        chart_path or "",
                    ]
                )
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
                    f_poly.setGeometry(
                        QgsGeometry.fromPolygonXY([[g1, g2, d2, d1, g1]])
                    )
                    dx = abs(
                        float(p2.get("offset_m") or 0.0)
                        - float(p1.get("offset_m") or 0.0)
                    )
                    area = dx * abs(avg_z - float(design))
                    f_poly.setAttributes(
                        [
                            sec.get("index"),
                            sec.get("progressive_m"),
                            tipo,
                            area,
                            label + " " + tipo,
                            color,
                            chart_path or "",
                        ]
                    )
                    polygon_features.append(f_poly)
            else:
                baseline = min_z
                ring = list(ground_pts)
                ring.append(
                    _local_xy(idx, pts[-1].get("offset_m"), baseline, min_z)
                )
                ring.append(
                    _local_xy(idx, pts[0].get("offset_m"), baseline, min_z)
                )
                ring.append(ground_pts[0])
                f_poly = QgsFeature()
                f_poly.setGeometry(QgsGeometry.fromPolygonXY([ring]))
                f_poly.setAttributes(
                    [
                        sec.get("index"),
                        sec.get("progressive_m"),
                        "sezione",
                        sec.get("area_m2") or 0.0,
                        label + " area",
                        "#4f73c4",
                        chart_path or "",
                    ]
                )
                polygon_features.append(f_poly)

        layers = []
        if line_features:
            pr_lines.addFeatures(line_features)
            vl_lines.updateExtents()
            self._style_section_lines(vl_lines)
            if group:
                self._add_output_layer(
                    vl_lines, group, "07 Disegni tecnici / Technical drawings"
                )
            else:
                project.addMapLayer(vl_lines)
            layers.append(vl_lines)
        if polygon_features:
            pr_poly.addFeatures(polygon_features)
            vl_poly.updateExtents()
            self._style_section_polygons(vl_poly)
            if group:
                self._add_output_layer(
                    vl_poly, group, "08 Sterro riporto / Cut fill"
                )
            else:
                project.addMapLayer(vl_poly)
            layers.append(vl_poly)
        if group and panels:
            layers.extend(
                self._build_chart_annotations(
                    crs_auth, stamp, group, panels
                )
            )
        return layers

    # ──────────────────────────────────────────────────────────────
    # Export vectors
    # ──────────────────────────────────────────────────────────────

    def _export_vectors(self):
        layers = [
            layer
            for layer in self.last_vector_layers
            if layer and layer.isValid()
        ]
        if not layers:
            QMessageBox.warning(
                self.dialog,
                "Export GPKG",
                "Nessun layer vettoriale disponibile / No vector layer "
                "available.",
            )
            return

        default_path = self.last_vector_gpkg or self._new_output_gpkg_path(
            "profili_sezioni"
        )
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

    def _write_dxf_package(self, layers, dxf_path):
        from qgis.core import QgsDxfExport
        from qgis.PyQt.QtCore import QFile, QIODevice

        if not dxf_path.lower().endswith(".dxf"):
            dxf_path += ".dxf"

        dxf_layers = [QgsDxfExport.DxfLayer(layer) for layer in layers]
        extent = layers[0].extent()
        for layer in layers[1:]:
            extent.combineExtentWith(layer.extent())
        extent.grow(max(extent.width(), extent.height(), 1.0) * 0.05 + 1.0)

        exporter = QgsDxfExport()
        exporter.setSymbologyExport(
            QgsDxfExport.SymbologyExport.SymbolLayerSymbology
        )
        exporter.setDestinationCrs(layers[0].crs())
        exporter.setExtent(extent)
        exporter.addLayers(dxf_layers)

        qfile = QFile(dxf_path)
        if not qfile.open(QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError(
                f"Impossibile scrivere / cannot write {dxf_path}"
            )
        try:
            result = exporter.writeToFile(qfile, "UTF-8")
        finally:
            qfile.close()
        if result != QgsDxfExport.ExportResult.Success:
            raise RuntimeError(f"DXF export failed ({result.name}).")
        return dxf_path

    def _export_dxf(self):
        layers = [
            layer
            for layer in self.last_vector_layers
            if layer and layer.isValid()
        ]
        if not layers:
            QMessageBox.warning(
                self.dialog,
                "Export DXF",
                "Nessun layer vettoriale disponibile / No vector layer "
                "available.",
            )
            return

        default_path = self._new_output_gpkg_path(
            "profili_sezioni"
        ).replace(".gpkg", ".dxf")
        file_path, _ = QFileDialog.getSaveFileName(
            self.dialog, "Salva DXF / Save DXF", default_path, "DXF (*.dxf)"
        )
        if not file_path:
            return

        try:
            dxf_path = self._write_dxf_package(layers, file_path)
            self._push(
                "Export DXF",
                f"DXF salvato / DXF saved: {dxf_path}",
                "Success",
            )
        except Exception as e:
            QMessageBox.critical(self.dialog, "Export DXF", str(e))

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
            from qgis.PyQt.QtCore import QByteArray, QSize as QtSize
            from qgis.PyQt.QtGui import QImage, QPainter, QColor
            from qgis.PyQt.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(
                QByteArray(self.last_svg_content.encode("utf-8"))
            )
            default_size = renderer.defaultSize()
            if default_size.isEmpty():
                default_size = QtSize(1600, 1040)
            scale = 300 / 96.0
            w = int(default_size.width() * scale)
            h = int(default_size.height() * scale)
            image = QImage(
                w,
                h,
                QImage.Format.Format_ARGB32,
            )
            image.fill(QColor("#0a0c10"))
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter)
            painter.end()
            image.save(file_path, "PNG")
            self._push(
                "Successo / Success",
                f"PNG salvato / PNG saved: {file_path}",
                "Success",
            )
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
        default_path = os.path.join(
            self._default_output_dir(), f"{prefix}_{stamp}.pdf"
        )
        file_path, _ = QFileDialog.getSaveFileName(
            self.dialog, "Salva PDF / Save PDF", default_path, "PDF (*.pdf)"
        )
        if not file_path:
            return
        try:
            saved_path = self._render_layout_to_pdf(
                self._last_drawn_points, file_path
            )
            self._push(
                "Successo / Success",
                f"PDF salvato / PDF saved: {saved_path}",
                "Success",
            )
        except Exception as e:
            QMessageBox.critical(self.dialog, "Errore / Error", str(e))

    def _render_layout_to_pdf(self, points, output_path):
        from qgis.core import QgsLayoutExporter

        if not output_path.lower().endswith(".pdf"):
            output_path += ".pdf"
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        stamp_now = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        layout = self._build_cartographic_layout(
            f"ProfiliSezioni_{stamp_now}",
            points,
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
        if result != QgsLayoutExporter.ExportResult.Success:
            raise Exception(f"Errore durante l'esportazione PDF: {result}")
        if (
            not os.path.exists(output_path)
            or os.path.getsize(output_path) <= 0
        ):
            raise Exception(
                "Errore durante l'esportazione PDF: file non creato o vuoto."
            )
        return output_path

    # ──────────────────────────────────────────────────────────────
    # Cartographic layout building blocks
    # ──────────────────────────────────────────────────────────────

    LAYER_TYPE_LABELS = (
        ("profilo_asse", "Asse profilo / Profile axis"),
        ("profilo_campioni", "Campioni profilo / Profile samples"),
        ("profilo_picchetti", "Picchetti / Pickets"),
        ("disegni sezioni sterro riporto", "Sterro e riporto / Cut and fill"),
        (
            "disegni sezioni linee",
            "Disegni tecnici sezioni / Section drawings",
        ),
        ("punti sezione", "Punti sezione / Section points"),
        ("centri sezione", "Centri sezione / Section centers"),
        ("tratte volumi", "Tratte volumi / Volume segments"),
        ("sezioni", "Sezioni planimetriche / Planimetric sections"),
        ("curve", "Curve / Curves"),
        ("asse", "Asse / Alignment"),
    )

    def _layer_type_label(self, layer_name):
        low = layer_name.lower()
        for prefix, label in self.LAYER_TYPE_LABELS:
            if low.startswith(prefix):
                return label
        return layer_name

    def _unique_layers_by_type(self):
        """Return last output layers deduplicated by typology: [(layer,
        label)]."""
        seen = set()
        unique = []
        for layer in self.last_vector_layers:
            if not layer or not layer.isValid():
                continue
            label = self._layer_type_label(layer.name())
            if label in seen:
                continue
            seen.add(label)
            unique.append((layer, label))
        return unique

    def _layers_by_prefix(self, prefixes):
        found = []
        for layer in self.last_vector_layers:
            if not layer or not layer.isValid():
                continue
            low = layer.name().lower()
            if any(low.startswith(p) for p in prefixes):
                found.append(layer)
        return found

    def _collect_print_charts(self):
        """One entry per available chart, each printed on its own sheet."""
        charts = []
        if self.last_profile_svg:
            charts.append(
                {
                    "svg": self.last_profile_svg,
                    "title": "Profilo altimetrico / Elevation profile",
                    "layers": self._layers_by_prefix(("profilo_picchetti",)),
                }
            )
        if self.last_sections_svg:
            charts.append(
                {
                    "svg": self.last_sections_svg,
                    "title": (
                        "Sezioni trasversali e volumi / Cross sections and "
                        "volumes"),
                    "layers": self._layers_by_prefix(
                        ("centri sezione", "tratte volumi")
                    ),
                }
            )
        return charts

    @staticmethod
    def _nice_grid_interval(span):
        raw = max(span / 5.0, 1e-9)
        magnitude = 10 ** math.floor(math.log10(raw))
        for mult in (1, 2, 5, 10):
            if raw <= mult * magnitude:
                return mult * magnitude
        return 10 * magnitude

    def _write_chart_svg(self, svg_content, name):
        chart_dir = os.path.join(self._default_output_dir(), "_charts")
        os.makedirs(chart_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(chart_dir, f"{name}_{stamp}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg_content)
        return path

    def _build_cartographic_layout(self, layout_name, points):
        """A3 landscape layout: map with grid, scale bar, per-typology legend,
        title block with chart thumbnails, plus one sheet per chart with its
        attribute table."""
        from qgis.PyQt.QtGui import QColor, QFont
        from qgis.core import (
            QgsGeometry,
            QgsLayoutItemLabel,
            QgsLayoutItemLegend,
            QgsLayoutItemMap,
            QgsLayoutItemPage,
            QgsLayoutItemPicture,
            QgsLayoutItemScaleBar,
            QgsLayoutItemShape,
            QgsLayoutMeasurement,
            QgsLayoutPoint,
            QgsLayoutSize,
            QgsPrintLayout,
            QgsUnitTypes,
        )

        # Shared palette matching the plugin's own "SPATIQON" dark chart
        # style (core_elevation.py / core_sections.py SVG generators), so
        # the printed layout's chrome (title block, frames, accents) reads
        # as one coherent product with the on-screen charts.
        PANEL_BORDER = QColor("#2d3757")
        TEXT_LIGHT = QColor("#f1f5f9")
        TEXT_MUTED = QColor("#8ba3c7")
        TEXT_DIM = QColor("#566584")

        project = QgsProject.instance()
        mm = QgsUnitTypes.LayoutUnit.LayoutMillimeters
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(layout_name)

        page = layout.pageCollection().pages()[0]
        page.setPageSize(QgsLayoutSize(420, 297, mm))

        # ── Map with buffered extent ──
        geom = QgsGeometry.fromPolylineXY(points)
        extent = geom.boundingBox()
        buf = 500 / 111320.0 if project.crs().isGeographic() else 500
        extent.setXMinimum(extent.xMinimum() - buf)
        extent.setXMaximum(extent.xMaximum() + buf)
        extent.setYMinimum(extent.yMinimum() - buf)
        extent.setYMaximum(extent.yMaximum() + buf)

        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(10, 10, mm))
        map_item.attemptResize(QgsLayoutSize(280, 195, mm))
        map_item.setExtent(extent)
        layout.addLayoutItem(map_item)
        try:
            map_item.setFrameEnabled(True)
            map_item.setFrameStrokeColor(QColor("#2d3757"))
            map_item.setFrameStrokeWidth(
                QgsLayoutMeasurement(0.5, QgsUnitTypes.LayoutUnit.LayoutMillimeters)
            )
        except Exception:  # nosec B110
            pass
        try:
            layout.setReferenceMap(map_item)
        except Exception:  # nosec B110
            pass

        # ── Grid (reticolo) with coordinate annotations ──
        try:
            from qgis.core import QgsLayoutItemMapGrid

            grid = map_item.grid()
            grid.setEnabled(True)
            interval = self._nice_grid_interval(
                max(extent.width(), extent.height())
            )
            grid.setIntervalX(interval)
            grid.setIntervalY(interval)
            grid.setStyle(QgsLayoutItemMapGrid.GridStyle.Solid)
            grid.setFrameStyle(QgsLayoutItemMapGrid.FrameStyle.Zebra)
            grid.setFrameWidth(2.0)
            grid.setAnnotationEnabled(True)
            grid.setAnnotationPrecision(
                3 if project.crs().isGeographic() else 0
            )
            try:
                pen = grid.pen()
                pen.setColor(QColor(120, 130, 150, 110))
                pen.setWidthF(0.15)
                grid.setPen(pen)
            except Exception:  # nosec B110
                pass
            map_item.updateBoundingRect()
        except Exception:  # nosec B110
            pass

        # ── Scale bar ──
        try:
            scalebar = QgsLayoutItemScaleBar(layout)
            scalebar.setStyle("Single Box")
            scalebar.setLinkedMap(map_item)
            try:
                scalebar.applyDefaultSettings()
            except Exception:  # nosec B110
                pass
            try:
                scalebar.applyDefaultSize()
            except Exception:  # nosec B110
                pass
            layout.addLayoutItem(scalebar)
            scalebar.attemptMove(QgsLayoutPoint(12, 212, mm))
        except Exception:  # nosec B110
            pass

        # ── North arrow (QGIS's own built-in SVG, same one the Layout
        # Designer's "Add North Arrow" toolbar button inserts) ──
        try:
            from qgis.PyQt.QtCore import QFile

            arrow_svg = ":/images/north_arrows/layout_default_north_arrow.svg"
            if QFile(arrow_svg).exists():
                arrow = QgsLayoutItemPicture(layout)
                arrow.setPicturePath(arrow_svg)
                layout.addLayoutItem(arrow)
                arrow.attemptResize(QgsLayoutSize(12, 16, mm))
                arrow.attemptMove(QgsLayoutPoint(278, 14, mm))
        except Exception:  # nosec B110
            pass

        # ── Legend: each typology once ──
        try:
            legend = QgsLayoutItemLegend(layout)
            legend.setTitle("Legenda / Legend")
            legend.setLinkedMap(map_item)
            legend.setAutoUpdateModel(False)
            try:
                legend.setBackgroundEnabled(True)
                legend.setBackgroundColor(QColor(255, 255, 255, 235))
                legend.setFrameEnabled(True)
                legend.setFrameStrokeColor(PANEL_BORDER)
            except Exception:  # nosec B110
                pass
            root = legend.model().rootGroup()
            try:
                root.removeAllChildren()
            except AttributeError:
                for child in list(root.children()):
                    root.removeChildNode(child)
            for layer, label in self._unique_layers_by_type():
                node = root.addLayer(layer)
                if node:
                    node.setName(label)
            layout.addLayoutItem(legend)
            legend.attemptMove(QgsLayoutPoint(295, 10, mm))
            try:
                legend.adjustBoxSize()
            except Exception:  # nosec B110
                pass
        except Exception:  # nosec B110
            pass

        # ── Title block (cartiglio) with small charts ──
        charts = self._collect_print_charts()
        try:
            shape_type = QgsLayoutItemShape.Shape.Rectangle
            cartiglio = QgsLayoutItemShape(layout)
            cartiglio.setShapeType(shape_type)
            layout.addLayoutItem(cartiglio)
            cartiglio.attemptMove(QgsLayoutPoint(10, 240, mm))
            cartiglio.attemptResize(QgsLayoutSize(400, 50, mm))
            from qgis.core import QgsFillSymbol

            cartiglio.setSymbol(
                QgsFillSymbol.createSimple(
                    {
                        "color": "18,21,30,255",
                        "outline_color": "45,55,87,255",
                        "outline_width": "0.3",
                    }
                )
            )
        except Exception:  # nosec B110
            pass

        # Thin accent rule separating the title from the metadata rows,
        # same accent blue used throughout the plugin's own chart style.
        try:
            rule = QgsLayoutItemShape(layout)
            rule.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
            layout.addLayoutItem(rule)
            rule.attemptMove(QgsLayoutPoint(14, 255.5, mm))
            rule.attemptResize(QgsLayoutSize(186, 0.6, mm))
            rule.setSymbol(
                QgsFillSymbol.createSimple(
                    {"color": "79,115,196,255", "outline_style": "no"}
                )
            )
        except Exception:  # nosec B110
            pass

        # Plugin icon in the top-left corner of the title block.
        try:
            logo = QgsLayoutItemPicture(layout)
            logo.setPicturePath(os.path.join(self.plugin_dir, "icon.svg"))
            layout.addLayoutItem(logo)
            logo.attemptResize(QgsLayoutSize(12, 12, mm))
            logo.attemptMove(QgsLayoutPoint(14, 243, mm))
        except Exception:  # nosec B110
            pass

        def _label(
            text,
            x,
            y,
            w,
            h,
            size=10,
            bold=False,
            color=None,
            italic=False,
            page=None,
        ):
            item = QgsLayoutItemLabel(layout)
            item.setText(text)
            font = QFont("Arial", size)
            font.setBold(bold)
            font.setItalic(italic)
            try:
                fmt = item.textFormat()
                fmt.setFont(font)
                fmt.setSize(size)
                if color is not None:
                    fmt.setColor(color)
                item.setTextFormat(fmt)
            except Exception:
                try:
                    item.setFont(font)
                except Exception:  # nosec B110
                    pass
            layout.addLayoutItem(item)
            # Items destined for a page other than the first must get their
            # target page in this SAME attemptMove call: moving them once
            # without a page (implicitly landing on page 0) and only then
            # re-moving them onto the right page is fragile — some QGIS
            # versions leave the item attached to whichever page it first
            # landed on for layout/bounding-rect purposes.
            if page is not None:
                item.attemptMove(QgsLayoutPoint(x, y, mm), True, False, page)
            else:
                item.attemptMove(QgsLayoutPoint(x, y, mm))
            item.attemptResize(QgsLayoutSize(w, h, mm))
            return item

        title = (
            project.title()
            or project.baseName()
            or "Profili, Sezioni e Comuni"
        )
        created = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            scale_txt = f"1:{int(round(map_item.scale())):,}".replace(",", ".")
        except Exception:
            scale_txt = "n/d"
        _label(
            title, 30, 244, 170, 9, size=15, bold=True, color=TEXT_LIGHT
        )
        _label(
            "Profili, Sezioni e Comuni — Dott. Sarino Alfonso Grande",
            14,
            257,
            186,
            6,
            size=9,
            color=TEXT_MUTED,
        )
        _label(
            f"Data / Date: {created}",
            14,
            263,
            186,
            6,
            size=9,
            color=TEXT_MUTED,
        )
        _label(
            f"CRS: {project.crs().authid()}   ·   Scala / Scale: {scale_txt}",
            14,
            269,
            186,
            6,
            size=9,
            color=TEXT_MUTED,
        )
        _label(
            "Quote indicative — non idonee a progettazione strutturale / "
            "Indicative elevations — not for structural design",
            14,
            276,
            190,
            10,
            size=7.5,
            italic=True,
            color=TEXT_DIM,
        )

        # small chart thumbnails inside the title block
        thumb_x = 210
        for chart in charts[:2]:
            try:
                svg_path = self._write_chart_svg(chart["svg"], "cartiglio")
                thumb = QgsLayoutItemPicture(layout)
                thumb.setPicturePath(svg_path)
                thumb.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
                layout.addLayoutItem(thumb)
                thumb.attemptMove(QgsLayoutPoint(thumb_x, 242, mm))
                thumb.attemptResize(QgsLayoutSize(96, 46, mm))
                thumb_x += 100
            except Exception:  # nosec B110
                pass

        # ── One sheet per chart, with its attribute tables ──
        for page_idx, chart in enumerate(charts, start=1):
            try:
                extra_page = QgsLayoutItemPage(layout)
                extra_page.setPageSize(QgsLayoutSize(420, 297, mm))
                layout.pageCollection().addPage(extra_page)
            except Exception as exc:
                QgsMessageLog.logMessage(
                    f"Pagina layout saltata per '{chart.get('title')}': "
                    f"{exc}",
                    "Profili, Sezioni e Comuni",
                    Qgis.MessageLevel.Warning,
                )
                continue

            _label(
                chart["title"],
                10,
                10,
                400,
                10,
                size=14,
                bold=True,
                page=page_idx,
            )

            try:
                from qgis.core import QgsFillSymbol

                header_rule = QgsLayoutItemShape(layout)
                header_rule.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
                layout.addLayoutItem(header_rule)
                header_rule.attemptResize(QgsLayoutSize(400, 0.6, mm))
                header_rule.attemptMove(
                    QgsLayoutPoint(10, 20.5, mm), True, False, page_idx
                )
                header_rule.setSymbol(
                    QgsFillSymbol.createSimple(
                        {"color": "79,115,196,255", "outline_style": "no"}
                    )
                )
            except Exception:  # nosec B110
                pass

            try:
                svg_path = self._write_chart_svg(chart["svg"], "foglio")
                pic = QgsLayoutItemPicture(layout)
                pic.setPicturePath(svg_path)
                pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
                layout.addLayoutItem(pic)
                pic.attemptMove(
                    QgsLayoutPoint(10, 22, mm), True, False, page_idx
                )
                pic.attemptResize(QgsLayoutSize(400, 150, mm))
            except Exception:  # nosec B110
                pass

            table_layers = chart.get("layers") or []
            table_x = 10
            table_w = 400 if len(table_layers) < 2 else 197
            for table_layer in table_layers[:2]:
                try:
                    from qgis.core import (
                        QgsLayoutFrame,
                        QgsLayoutItemAttributeTable,
                    )

                    table = QgsLayoutItemAttributeTable(layout)
                    layout.addMultiFrame(table)
                    table.setVectorLayer(table_layer)
                    table.setMaximumNumberOfFeatures(60)
                    try:
                        fields = [
                            f.name()
                            for f in table_layer.fields()
                            if f.name() not in ("chart_png", "colore")
                        ]
                        table.setDisplayedFields(fields)
                    except Exception:  # nosec B110
                        pass
                    frame = QgsLayoutFrame(layout, table)
                    frame.attemptResize(QgsLayoutSize(table_w, 108, mm))
                    table.addFrame(frame)
                    frame.attemptMove(
                        QgsLayoutPoint(table_x, 178, mm), True, False, page_idx
                    )
                    table_x += table_w + 6
                except Exception:  # nosec B110
                    pass

            _label(
                f"Foglio {page_idx + 1}/{len(charts) + 1} — {title} — "
                f"{created}",
                10,
                289,
                400,
                6,
                size=8,
                color=TEXT_DIM,
                page=page_idx,
            )

        return layout

    # ──────────────────────────────────────────────────────────────
    # Export CSV
    # ──────────────────────────────────────────────────────────────

    def _export_csv(self):
        if self.current_mode == "sections" and self.last_cross_sections_data:
            file_path, _ = QFileDialog.getSaveFileName(
                self.dialog,
                "Salva CSV Sezioni / Save Sections CSV",
                "",
                "CSV (*.csv)",
            )
            if not file_path:
                return
            try:
                data = self.last_cross_sections_data
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=";")
                    writer.writerow(
                        [
                            "Section",
                            "Progressive_m",
                            "Min_m",
                            "Max_m",
                            "Area_m2",
                            "CutArea_m2",
                            "FillArea_m2",
                        ]
                    )
                    for s in data["sections"]:
                        writer.writerow(
                            [
                                s["index"],
                                f"{s['progressive_m']:.2f}",
                                (
                                    f"{s['min_elevation']:.2f}"
                                    if s["min_elevation"] is not None
                                    else ""
                                ),
                                (
                                    f"{s['max_elevation']:.2f}"
                                    if s["max_elevation"] is not None
                                    else ""
                                ),
                                f"{s.get('area_m2', 0):.2f}",
                                f"{s.get('cut_area_m2', 0):.2f}",
                                f"{s.get('fill_area_m2', 0):.2f}",
                            ]
                        )
                    writer.writerow([])
                    writer.writerow(
                        [
                            "From",
                            "To",
                            "Distance_m",
                            "Vol_m3",
                            "Cut_m3",
                            "Fill_m3",
                            "Cum_Cut",
                            "Cum_Fill",
                        ]
                    )
                    for v in data["volumes"]:
                        writer.writerow(
                            [
                                v["from_section"],
                                v["to_section"],
                                f"{v['distance_m']:.2f}",
                                f"{v['volume_m3']:.2f}",
                                f"{v['cut_m3']:.2f}",
                                f"{v['fill_m3']:.2f}",
                                f"{v['cumulative_cut_m3']:.2f}",
                                f"{v['cumulative_fill_m3']:.2f}",
                            ]
                        )
                self._push(
                    "Successo / Success",
                    f"CSV salvato / CSV saved: {file_path}",
                    "Success",
                )
            except Exception as e:
                QMessageBox.critical(self.dialog, "Errore / Error", str(e))

        elif self.last_points_data:
            file_path, _ = QFileDialog.getSaveFileName(
                self.dialog,
                "Salva CSV Profilo / Save Profile CSV",
                "",
                "CSV (*.csv)",
            )
            if not file_path:
                return
            try:
                export_profile_csv(self.last_points_data, file_path)
                self._push(
                    "Successo / Success",
                    f"CSV salvato / CSV saved: {file_path}",
                    "Success",
                )
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
        if not self.last_svg_content:
            self._push(
                "Errore / Error",
                "Nessun risultato da stampare / Nothing to print.",
                "Critical",
            )
            return

        layout_name = (
            "Cross Sections / Sezioni Trasversali"
            if self.current_mode == "sections"
            else "Elevation Profile / Profilo Altimetrico"
        )
        project = QgsProject.instance()
        for old_layout in project.layoutManager().printLayouts():
            if old_layout.name() != layout_name:
                continue
            # Close any open designer window for it first: removing the
            # QgsPrintLayout while its QgsLayoutDesignerDialog is still
            # open leaves that window holding a pointer to a deleted C++
            # object, which crashes QGIS the next time it's touched (or
            # on QGIS shutdown). This is hit on ordinary repeated use of
            # "Stampa" (print), not just a race condition.
            for designer in self.iface.layoutDesigners():
                if designer.layout() is old_layout:
                    designer.close()
            project.layoutManager().removeLayout(old_layout)

        layout = self._build_cartographic_layout(layout_name, points)
        project.layoutManager().addLayout(layout)
        self.iface.openLayoutDesigner(layout)
