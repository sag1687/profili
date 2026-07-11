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
dialog.py — Main dialog for the Profili, Sezioni e Comuni plugin.

Five tabs:
  0. Profilo / Profile
  1. Sezioni / Sections
  2. Comuni / Municipalities
  3. Download Raster / Raster Download
  4. Parametri / Parameters
  5. Risultati / Results
  6. Info
"""

import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QSpinBox, QDoubleSpinBox, QWidget, QTabWidget,
    QCheckBox, QLineEdit, QProgressBar, QGroupBox, QSizePolicy,
    QListWidget, QListWidgetItem, QTextBrowser, QApplication,
    QFormLayout, QFrame,
)
from qgis.PyQt.QtCore import Qt, QSize, QSettings
from qgis.PyQt.QtGui import QPixmap
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes

from .core_raster_download import source_info_text, source_label
from .qt_compat import ensure_qt_compat, QtCompat, compat_enum
ensure_qt_compat(Qt)


# ──────────────────────────────────────────────────────────────────────
# Translation helper — same pattern as q_press
# ──────────────────────────────────────────────────────────────────────

def _t(lang, it, en):
    """Return Italian or English string based on lang ("it" or "en")."""
    return en if lang == "en" else it


# ──────────────────────────────────────────────────────────────────────
# Dark theme stylesheet
# ──────────────────────────────────────────────────────────────────────

OCEAN_STYLE = """
QDialog {
    background-color: #020e1a;
    color: #d0f0ff;
    font-family: 'Segoe UI', 'Inter', 'Roboto', Tahoma, Geneva, Verdana, sans-serif;
    font-size: 13px;
}
QWidget { background-color: #020e1a; color: #d0f0ff; }
QLabel { font-size: 13px; color: #7ac8d8; }
QGroupBox {
    border: 1px solid #0a4a6e;
    border-radius: 10px;
    margin-top: 12px;
    padding: 14px 12px 12px 12px;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #071c2e,stop:1 #020e1a);
    color: #d0f0ff;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    color: #00e5ff;
    font-size: 13px;
}
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    padding: 7px 10px;
    border: 1px solid #0a4a6e;
    border-radius: 6px;
    background: #071c2e;
    color: #d0f0ff;
    selection-background-color: #0a4a6e;
}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {
    border-color: #00e5ff;
}
QComboBox::drop-down { border: none; padding-right: 6px; }
QPushButton {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #006688,stop:1 #004455);
    color: #00e5ff;
    border: 1px solid #00e5ff;
    border-radius: 7px;
    padding: 9px 18px;
    font-weight: 700;
    font-size: 13px;
}
QPushButton:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #0099bb,stop:1 #006688);
    color: #d0f0ff;
}
QPushButton:pressed { background: #004455; }
QPushButton:disabled { background: #071c2e; color: #4a8090; border-color: #0a3a58; }
QPushButton#btnCancel {
    background: #071c2e;
    color: #7ac8d8;
    border: 1px solid #0a4a6e;
}
QPushButton#btnCancel:hover { background: #0d2d42; border-color: #0099bb; }
QPushButton#btnLang {
    background: #071c2e;
    color: #7ac8d8;
    border: 1px solid #0a4a6e;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 700;
}
QPushButton#btnLang:hover { background: #0d2d42; color: #d0f0ff; }
QPushButton#btnExportPng, QPushButton#btnExportPdf {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #005577,stop:1 #003355);
    color: #00e5ff;
    border: 1px solid #0099bb;
}
QPushButton#btnExportPng:hover, QPushButton#btnExportPdf:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #0099bb,stop:1 #005577);
}
QPushButton#btnExportCsv {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #996600,stop:1 #664400);
    color: #ffb700;
    border: 1px solid #ffb700;
}
QPushButton#btnExportCsv:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #cc8800,stop:1 #996600);
}
QPushButton#btnPrintLayout {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #220055,stop:1 #110033);
    color: #cc99ff;
    border: 1px solid #9933ff;
}
QPushButton#btnPrintLayout:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #440088,stop:1 #220055);
}
QPushButton#btnComuniLoad {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #005544,stop:1 #003322);
    color: #00ffb3;
    border: 1px solid #00ffb3;
}
QPushButton#btnComuniLoad:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #008866,stop:1 #005544);
}
QTabWidget::pane {
    border: 1px solid #0a4a6e;
    border-radius: 8px;
    top: -1px;
    background: #020e1a;
}
QTabBar::tab {
    background: #071c2e;
    border: 1px solid #0a4a6e;
    padding: 9px 16px;
    margin-right: 2px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    color: #4a8090;
    font-size: 12px;
}
QTabBar::tab:selected {
    background: #020e1a;
    border-bottom-color: #020e1a;
    font-weight: bold;
    color: #00e5ff;
}
QTabBar::tab:hover:!selected { background: #0d2d42; color: #7ac8d8; }
QProgressBar {
    border: 1px solid #0a4a6e;
    border-radius: 5px;
    height: 6px;
    background: #071c2e;
    color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #0099bb,stop:1 #00e5ff);
    border-radius: 4px;
}
QCheckBox { color: #7ac8d8; spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 4px;
    border: 1px solid #0a4a6e;
    background: #071c2e;
}
QCheckBox::indicator:checked { background: #00e5ff; border-color: #00e5ff; }
QListWidget {
    background: #071c2e;
    border: 1px solid #0a4a6e;
    border-radius: 6px;
    color: #d0f0ff;
    font-size: 12px;
}
QListWidget::item { padding: 6px 10px; }
QListWidget::item:selected { background: #0a4a6e; color: #00e5ff; }
QListWidget::item:hover { background: #0d2d42; }
QTextBrowser {
    background: #071c2e;
    border: 1px solid #0a4a6e;
    border-radius: 6px;
    color: #7ac8d8;
    font-size: 12px;
    font-family: 'Segoe UI', Arial, sans-serif;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background: #071c2e;
    border: none;
    width: 8px; height: 8px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #0a4a6e;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background: #00e5ff;
}
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
"""

SPATIQON_STYLE = """
QDialog {
    background: #12151e;
    color: #e2e8f0;
    font-family: 'Segoe UI', 'Inter', 'Roboto', Tahoma, Geneva, Verdana, sans-serif;
    font-size: 13px;
}
QWidget {
    background: #12151e;
    color: #e2e8f0;
    selection-background-color: #3b5bdb;
    selection-color: #ffffff;
}
QLabel { color: #e2e8f0; }
QLabel#Title {
    font-size: 22px;
    font-weight: 700;
    color: #f1f5f9;
    letter-spacing: 1px;
}
QLabel#Subtitle {
    color: #8ba3c7;
    font-size: 12px;
}
QLabel#Muted {
    color: #566584;
    font-size: 11px;
}
QFrame#Separator {
    background: #2d3757;
    max-height: 1px;
    border: 0;
}
QTabWidget::pane {
    border: 1px solid #2d3757;
    border-radius: 7px;
    background: #1a1e28;
    top: -1px;
}
QTabBar::tab {
    background: #1e2437;
    color: #8ba3c7;
    border: 1px solid #2d3757;
    border-bottom: 0;
    padding: 9px 18px;
    margin-right: 3px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-size: 12px;
}
QTabBar::tab:selected {
    background: #1a1e28;
    color: #f1f5f9;
    border-color: #3b5bdb;
    font-weight: 700;
}
QTabBar::tab:hover { background: #252d42; color: #c7d8ef; }
QGroupBox {
    border: 1px solid #2d3757;
    border-radius: 8px;
    margin-top: 16px;
    padding: 14px;
    background: #1e2437;
    color: #c7d8ef;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #8ba3c7;
    background: #12151e;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    padding: 7px 10px;
    border: 1px solid #2d3757;
    border-radius: 6px;
    background: #0e1118;
    color: #e2e8f0;
    selection-background-color: #3b5bdb;
}
QLineEdit:focus, QComboBox:hover, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #4f73c4;
    background: #0b0e16;
}
QLineEdit::placeholder { color: #566584; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox QAbstractItemView {
    background: #1a1e28;
    color: #c7d8ef;
    border: 1px solid #2d3757;
    selection-background-color: #3b5bdb;
    selection-color: #ffffff;
    outline: 0;
}
QPushButton {
    padding: 8px 14px;
    border: 1px solid #2d3757;
    border-radius: 6px;
    background: #1e2437;
    color: #c7d8ef;
    font-weight: 600;
}
QPushButton:hover { background: #252d42; border-color: #4f73c4; }
QPushButton:pressed { background: #0e1118; }
QPushButton:disabled { background: #161b26; color: #3d4b6a; border-color: #1e2437; }
QPushButton#Primary, QPushButton#btnComuniLoad, QPushButton#btnDownloadRaster {
    background: #3b5bdb;
    color: #ffffff;
    border-color: #4f73c4;
    font-weight: 700;
}
QPushButton#Primary:hover, QPushButton#btnComuniLoad:hover,
QPushButton#btnDownloadRaster:hover {
    background: #4a6beb;
    border-color: #6b8be4;
}
QPushButton#Danger, QPushButton#btnCancel {
    background: #5c1a1a;
    color: #fca5a5;
    border-color: #7f2b2b;
}
QPushButton#Danger:hover, QPushButton#btnCancel:hover { background: #7a2020; }
QPushButton#btnLang {
    padding: 6px 12px;
    font-size: 12px;
}
QPushButton#btnExportPng, QPushButton#btnExportPdf,
QPushButton#btnExportCsv, QPushButton#btnPrintLayout {
    background: #1e2437;
    color: #c7d8ef;
    border-color: #2d3757;
}
QPushButton#btnExportPng:hover, QPushButton#btnExportPdf:hover,
QPushButton#btnExportCsv:hover, QPushButton#btnPrintLayout:hover {
    background: #252d42;
    border-color: #4f73c4;
}
QCheckBox { color: #8ba3c7; spacing: 8px; }
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 3px;
    border: 1px solid #3d4b6a;
    background: #0e1118;
}
QCheckBox::indicator:checked {
    background: #3b5bdb;
    border-color: #4f73c4;
}
QListWidget, QTextBrowser {
    background: #0e1118;
    color: #c7d8ef;
    border: 1px solid #2d3757;
    border-radius: 6px;
    selection-background-color: #3b5bdb;
    selection-color: #ffffff;
}
QListWidget::item { padding: 6px 10px; }
QListWidget::item:selected { background: #3b5bdb; color: #ffffff; }
QListWidget::item:hover { background: #252d42; }
QProgressBar {
    background: #1a1e28;
    border: 1px solid #2d3757;
    border-radius: 4px;
    color: #e2e8f0;
    text-align: center;
    font-size: 11px;
    height: 14px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #3b5bdb, stop:1 #4f73c4);
    border-radius: 3px;
}
QScrollBar:vertical {
    background: #131722;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #2d3757;
    min-height: 24px;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover { background: #3b5bdb; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

# ──────────────────────────────────────────────────────────────────────
# Info tab HTML content
# ──────────────────────────────────────────────────────────────────────

INFO_HTML = """
<html>
<head>
<style>
  body { background:#12151e; color:#e2e8f0; font-family:'Segoe UI',Arial,sans-serif;
         font-size:12px; margin:16px; line-height:1.6; }
  h2   { color:#f1f5f9; font-size:17px; margin:0 0 12px; border-bottom:1px solid #2d3757;
         padding-bottom:6px; }
  h3   { color:#c7d8ef; font-size:13px; margin:16px 0 6px; }
  h4   { color:#f5a623; font-size:12px; margin:12px 0 4px; }
  p, li { margin:3px 0; color:#8ba3c7; }
  a    { color:#8fb3ff; }
  code { background:#0e1118; padding:1px 4px; border-radius:3px; font-family:monospace; }
  table { border-collapse:collapse; width:100%; margin:8px 0; }
  th   { background:#1e2437; color:#8ba3c7; padding:7px 10px; text-align:left;
         border-bottom:2px solid #2d3757; font-size:11px; text-transform:uppercase; }
  td   { padding:6px 10px; border-bottom:1px solid #2d3757; color:#e2e8f0; }
  tr:nth-child(even) td { background:rgba(30,36,55,0.5); }
  .badge-warn { background:rgba(245,166,35,0.16); color:#f5a623; padding:1px 6px;
                border-radius:8px; font-size:11px; font-weight:600; }
  .badge-ok   { background:rgba(52,201,138,0.15); color:#34c98a; padding:1px 6px;
                border-radius:8px; font-size:11px; font-weight:600; }
  .section-sep { border:none; border-top:1px solid #2d3757; margin:16px 0; }
  .callout { background:#1e2437; border:1px solid #2d3757; border-radius:6px;
             padding:10px 12px; margin:8px 0 14px; color:#c7d8ef; }
</style>
</head>
<body>

<h2>PROGETTO / PROJECT</h2>
<div class="callout">
  <p><b>Profili, Sezioni e Comuni</b> usa una grafica coerente con SPATIQON e integra profili,
  sezioni trasversali, sterri, riporti, accumuli e download raster da area disegnata.</p>
  <p>Il plugin &egrave; ispirato al lavoro di <b>Giulio Fattori</b>, in particolare al plugin
  <a href="https://plugins.qgis.org/plugins/Topographical_profiles/">Topographic Profile</a>
  pubblicato nel repository plugin QGIS e indicato nella pagina autore:
  <a href="https://plugins.qgis.org/plugins/author/Giulio%20Fattori/">Giulio Fattori</a>.</p>
</div>

<h2>DOWNLOAD RASTER / RASTER DOWNLOAD</h2>
<table>
  <tr><th>Fonte / Source</th><th>Licenza / License</th><th>Citazione / Citation</th></tr>
  <tr>
    <td>TINITALY 1.1 DEM 10 m — INGV<br>
      ZIP ufficiali per download area · WCS opzionale per visualizzazione<br>
      <a href="http://tinitaly.pi.ingv.it/Download_Area1_1.html">Download area</a> ·
      <a href="http://tinitaly.pi.ingv.it/TINItaly_1_1/wcs?service=WCS&request=getCapabilities">WCS</a>
    </td>
    <td><span class="badge-ok">CC BY 4.0</span></td>
    <td>Tarquini S., Isola I., Favalli M., Battistini A., Dotta G. (2023).
      <i>TINITALY, a digital elevation model of Italy with a 10 meters cell size (Version 1.1)</i>.
      INGV. DOI: <a href="https://doi.org/10.13127/tinitaly/1.1">10.13127/tinitaly/1.1</a></td>
  </tr>
  <tr>
    <td>HR-DTM-5m Italia — Zenodo/CNR<br>
      Record indicato: <a href="https://zenodo.org/records/18872933">18872933</a><br>
      Versione latest verificata: <a href="https://zenodo.org/records/18921767">18921767</a>
      (v1.2, 09/03/2026, file ~22.09 GB)
    </td>
    <td><span class="badge-ok">CC BY 4.0</span></td>
    <td>Panza M., Muto M., Rossi M., Alvioli M., Iovine G., Marchesini I. (2026).
      <i>5m Resolution Digital Terrain Model for Italy</i>, version 1.2.
      Zenodo. DOI: <a href="https://doi.org/10.5281/zenodo.18921767">10.5281/zenodo.18921767</a></td>
  </tr>
</table>

<h2>PROVIDER DI QUOTE ALTIMETRICHE / ELEVATION DATA PROVIDERS</h2>

<h3>1. Open-Elevation API</h3>
<table>
  <tr><th>Attributo</th><th>Valore / Value</th></tr>
  <tr><td>Sito / Site</td><td><a href="https://www.open-elevation.com">open-elevation.com</a></td></tr>
  <tr><td>Dati / Data</td><td>SRTM (Shuttle Radar Topography Mission, NASA, 2000)</td></tr>
  <tr><td>Licenza API</td><td>GPLv2 (open source, uso libero / free use)</td></tr>
  <tr><td>Licenza dati / Data license</td><td>Public Domain (NASA)</td></tr>
  <tr><td>Risoluzione / Resolution</td>
      <td>30 m (SRTM 1-arc-second, US zones) / 90 m (SRTM 3-arc-second, global)</td></tr>
  <tr><td>Accuratezza verticale / Vertical accuracy</td>
      <td>&plusmn;16 m (90% confidence, flat areas) &nbsp; &plusmn;30 m (mountainous)</td></tr>
  <tr><td>Limite API pubblica / Public API limit</td>
      <td><span class="badge-warn">~1.000 req/mese</span> — istanza demo ufficiale / official demo instance</td></tr>
  <tr><td>Nota / Note</td>
      <td>Per uso intensivo ospita la tua istanza (Docker disponibile)
      / For heavy use, self-host (Docker available)</td></tr>
</table>

<h3>2. OpenTopoData API</h3>
<table>
  <tr><th>Attributo</th><th>Valore / Value</th></tr>
  <tr><td>Sito / Site</td><td><a href="https://www.opentopodata.org">opentopodata.org</a></td></tr>
  <tr><td>Dataset disponibili / Available datasets</td>
      <td>SRTM, ASTER GDEM v3, EU-DEM v1.1, GEBCO, NED (USA), mapzen</td></tr>
  <tr><td>Dataset usato da questo plugin</td><td><code>srtm90m</code></td></tr>
  <tr><td>Licenza API</td><td>MIT (uso libero / free use)</td></tr>
  <tr><td>Licenza EU-DEM</td><td>CC BY 4.0 (Copernicus / ESA)</td></tr>
  <tr><td>Risoluzione EU-DEM</td><td>25 m</td></tr>
  <tr><td>Accuratezza EU-DEM / EU-DEM accuracy</td><td>&plusmn;7 m (Europe)</td></tr>
  <tr><td>Accuratezza SRTM / SRTM accuracy</td><td>&plusmn;16 m</td></tr>
  <tr><td>Limite API pubblica / Public API limit</td>
      <td><span class="badge-warn">100 req/giorno, 1 req/secondo</span></td></tr>
</table>

<h3>3. Raster Locale (DEM/DTM locale)</h3>
<table>
  <tr><th>Fonte / Source</th><th>Accuratezza tipica / Typical accuracy</th></tr>
  <tr><td>LiDAR aereo / Airborne LiDAR</td><td><span class="badge-ok">&plusmn;0.1 m</span></td></tr>
  <tr><td>Fotogrammetria UAV / UAV photogrammetry</td><td><span class="badge-ok">&plusmn;0.5 m</span></td></tr>
  <tr><td>Fotogrammetria aerea / Aerial photogrammetry</td><td>&plusmn;1–2 m</td></tr>
  <tr><td>SRTM 30 m (regioni montuose)</td><td><span class="badge-warn">&plusmn;30 m</span></td></tr>
</table>

<hr class="section-sep"/>
<h2>MARGINI DI ERRORE / ERROR MARGINS</h2>
<ul>
  <li><b>SRTM:</b> errore sistematico (bias) fino a &plusmn;6 m; errore casuale &plusmn;16 m (1&sigma;)</li>
  <li><b>EU-DEM:</b> errore medio &plusmn;2.9 m su aree pianeggianti, &plusmn;7 m su zone alpine</li>
  <li><b>Raster locale DEM/DTM:</b> dipende dal dato sorgente (LiDAR: &plusmn;0.1 m; fotogramm.: &plusmn;0.5 m)</li>
  <li style="color:#ef4444;"><b>Avviso / Warning:</b>
      Le quote non sono adatte per progettazione strutturale, solo per analisi indicative.
      Elevation data is not suitable for structural design — for indicative analysis only.</li>
</ul>

<hr class="section-sep"/>
<h2>CONFINI COMUNALI / MUNICIPAL BOUNDARIES</h2>
<table>
  <tr><th>Sorgente / Source</th><th>Dettagli</th></tr>
  <tr><td>Dati</td><td>&copy; OpenStreetMap contributors (ODbL 1.0)</td></tr>
  <tr><td>API</td><td>Nominatim —
      <a href="https://nominatim.openstreetmap.org">nominatim.openstreetmap.org</a></td></tr>
  <tr><td>Termini d'uso / Terms</td>
      <td><span class="badge-warn">max 1 req/secondo, User-Agent obbligatorio</span></td></tr>
  <tr><td>Confini ufficiali / Official boundaries</td>
      <td>ISTAT (<a href="https://www.istat.it">istat.it</a>) —
      pagina download <a href="https://www.istat.it/statistiche-per-temi/focus/informazioni-territoriali-e-cartografiche/unita-amministrative/">Unit&agrave; amministrative</a>.
      Usare ISTAT per confini amministrativi ufficiali; Nominatim resta una ricerca rapida in mappa.
      / Use ISTAT for official administrative boundaries; Nominatim is a quick map lookup.</td></tr>
</table>

<hr class="section-sep"/>
<h2>ALTRI PLUGIN DELLO STESSO AUTORE / OTHER PLUGINS BY THE SAME AUTHOR</h2>
<ul>
  <li><b>Profilo GitHub verificato / Verified GitHub profile:</b>
      <a href="https://github.com/sag1687">github.com/sag1687</a></li>
  <li><b>meteo:</b> <a href="https://github.com/sag1687/meteo">github.com/sag1687/meteo</a></li>
  <li><b>CorsoQFIELD:</b> <a href="https://github.com/sag1687/CorsoQFIELD">github.com/sag1687/CorsoQFIELD</a></li>
  <li><b>Codice-Libero:</b> <a href="https://github.com/sag1687/Codice-Libero">github.com/sag1687/Codice-Libero</a></li>
  <li><b>qgis-docker:</b> <a href="https://github.com/sag1687/qgis-docker">github.com/sag1687/qgis-docker</a></li>
</ul>

<hr class="section-sep"/>
<p style="color:#3a6070;font-size:11px;">
  Plugin: Profili, Sezioni e Comuni v1.2.0 &nbsp;·&nbsp;
  Autore: Dott. Sarino Alfonso Grande &nbsp;·&nbsp;
  <a href="mailto:info@sinocloud.it">info@sinocloud.it</a>
</p>
</body>
</html>
"""

HELP_HTML = """
<html>
<head>
<style>
  body { background:#12151e; color:#e2e8f0; font-family:'Segoe UI',Arial,sans-serif;
         font-size:12px; margin:16px; line-height:1.62; }
  h2 { color:#f1f5f9; font-size:17px; margin:0 0 10px; border-bottom:1px solid #2d3757; padding-bottom:6px; }
  h3 { color:#c7d8ef; font-size:13px; margin:16px 0 6px; }
  p, li { color:#8ba3c7; margin:4px 0; }
  code { background:#0e1118; color:#e2e8f0; padding:1px 4px; border-radius:3px; }
  table { border-collapse:collapse; width:100%; margin:8px 0; }
  th { background:#1e2437; color:#8ba3c7; padding:7px 10px; text-align:left; border-bottom:2px solid #2d3757; }
  td { padding:6px 10px; border-bottom:1px solid #2d3757; color:#e2e8f0; vertical-align:top; }
  .callout { background:#1e2437; border:1px solid #2d3757; border-radius:6px; padding:10px 12px; margin:8px 0 14px; }
</style>
</head>
<body>
<h2>HELP COMPLETO / COMPLETE HELP</h2>
<div class="callout">
  <p><b>IT:</b> Questo plugin &egrave; pensato come strumento operativo per topografi: profilo longitudinale,
  sezioni trasversali, sterri, riporti, accumuli, curve, punti, picchetti, tabelle, grafici, raster di supporto
  e file vettoriali GeoPackage prodotti automaticamente.</p>
  <p><b>EN:</b> This plugin is intended as an operational surveying tool: longitudinal profile,
  cross sections, cut/fill/stockpile balance, curves, points, pickets, tables, charts, supporting rasters
  and automatic GeoPackage vector outputs.</p>
</div>

<h3>1. Preparazione progetto / Project preparation</h3>
<ul>
  <li><b>IT:</b> Imposta un CRS di progetto corretto, preferibilmente metrico. Carica un DEM/DTM locale se serve precisione topografica.</li>
  <li><b>EN:</b> Set a correct project CRS, preferably metric. Load a local DEM/DTM when topographic precision is required.</li>
  <li><b>IT:</b> Le misure derivate direttamente dal file raster richiedono un CRS proiettato metrico; un CRS geografico non &egrave; adatto per elaborati tecnici, sezioni, volumi e raster interpolati del corridoio.</li>
  <li><b>EN:</b> Measurements read directly from the raster file should use a projected metric CRS; a geographic CRS is not suitable for technical outputs, sections, volumes, or corridor interpolation rasters.</li>
  <li><b>IT:</b> I dati API globali sono comodi per analisi preliminari; per lavoro professionale usare raster locali o fonti nazionali citate.</li>
  <li><b>EN:</b> Global API data are useful for preliminary checks; professional work should use local rasters or the cited national sources.</li>
</ul>

<h3>2. Profilo longitudinale / Longitudinal profile</h3>
<ol>
  <li><b>IT:</b> Apri la scheda <code>Profilo</code>, scegli fonte quote e numero campioni. Il default automatico &egrave; adatto a un primo rilievo.</li>
  <li><b>EN:</b> Open <code>Profile</code>, select elevation source and sample count. Automatic defaults fit a first survey pass.</li>
  <li><b>IT:</b> Premi <code>Disegna asse</code>, clicca i vertici in mappa e termina con doppio click.</li>
  <li><b>EN:</b> Click <code>Draw axis</code>, place map vertices and finish with double click.</li>
  <li><b>IT:</b> Se il DEM ha CRS diverso dal progetto, il plugin trasforma automaticamente i punti nel CRS del raster prima di leggere le quote.</li>
  <li><b>EN:</b> If the DEM CRS differs from the project CRS, the plugin automatically transforms points into the raster CRS before reading elevations.</li>
  <li><b>IT:</b> Il plugin genera grafico, tabella campioni, picchetti e GeoPackage con asse, campioni e picchetti.</li>
  <li><b>EN:</b> The plugin generates chart, sample table, pickets and a GeoPackage with axis, samples and pickets.</li>
  <li><b>IT:</b> Dopo il tracciamento il plugin chiede se calcolare subito anche sezioni, sterri, riporti e volumi. Se rispondi no, resta solo il profilo.</li>
  <li><b>EN:</b> After drawing, the plugin asks whether to compute sections, cut/fill and volumes immediately. If you answer no, it keeps only the profile.</li>
</ol>

<h3>3. Sezioni trasversali / Cross sections</h3>
<ol>
  <li><b>IT:</b> Apri <code>Sezioni</code>, seleziona DEM/DTM e scegli se disegnare l'asse o usare un layer linea.</li>
  <li><b>EN:</b> Open <code>Sections</code>, select DEM/DTM and choose whether to draw the axis or use a line layer.</li>
  <li><b>IT:</b> Imposta interasse, semi-ampiezza, campioni per sezione, quota iniziale/finale o pendenza di progetto.</li>
  <li><b>EN:</b> Set interval, half-width, samples per section, design start/end elevation or design grade.</li>
  <li><b>IT:</b> Il plugin calcola sezioni, aree, sterri, riporti, accumuli/bilancio, metriche pendenza, curve e raggi stimati.</li>
  <li><b>EN:</b> The plugin computes sections, areas, cut, fill, balance/stockpile, grade metrics, curves and estimated radii.</li>
  <li><b>IT:</b> Sono creati layer QGIS e GeoPackage: asse, sezioni planimetriche, punti sezione, centri sezione, curve, tratte volumi, disegni tecnici di sezione, poligoni sterro e poligoni riporto.</li>
  <li><b>EN:</b> QGIS layers and GeoPackage are created: axis, planimetric section lines, section points, section centers, curves, volume segments, technical section drawings, cut polygons and fill polygons.</li>
</ol>

<h3>4. Download raster / Raster download</h3>
<ol>
  <li><b>IT:</b> Apri <code>Download Raster</code>, premi <code>SHIFT + disegna area</code>, tieni premuto Shift e trascina un rettangolo.</li>
  <li><b>EN:</b> Open <code>Raster Download</code>, click <code>SHIFT + draw area</code>, hold Shift and drag a rectangle.</li>
  <li><b>IT:</b> Scegli TINITALY 1.1 ZIP ufficiali o HR-DTM-5m Zenodo, indica output GeoTIFF e avvia il ritaglio.</li>
  <li><b>EN:</b> Choose official TINITALY 1.1 ZIP tiles or Zenodo HR-DTM-5m, set GeoTIFF output and start clipping.</li>
  <li><b>IT:</b> TINITALY seleziona automaticamente gli ZIP che intersecano l'area, costruisce un VRT temporaneo e ritaglia il GeoTIFF. Il WCS rimane opzionale e non blocca il download.</li>
  <li><b>EN:</b> TINITALY automatically selects intersecting ZIP tiles, builds a temporary VRT and clips the GeoTIFF. WCS remains optional and does not block download.</li>
  <li><b>IT:</b> Per Zenodo il plugin usa GDAL /vsicurl con cache/range HTTP quando disponibile e mostra percentuale, velocit&agrave;, tempo trascorso e tempo rimanente.</li>
  <li><b>EN:</b> For Zenodo the plugin uses GDAL /vsicurl with HTTP range/cache when available and shows percentage, speed, elapsed time and ETA.</li>
  <li><b>IT:</b> Accanto al raster viene scritta una ricevuta fonte/licenza/citazione.</li>
  <li><b>EN:</b> A source/license/citation receipt is written next to the raster.</li>
</ol>

<h3>5. Comuni / Municipalities</h3>
<ul>
  <li><b>IT:</b> Cerca un comune, seleziona il risultato e carica il confine rapido OSM/Nominatim.</li>
  <li><b>EN:</b> Search a municipality, select a result and load the quick OSM/Nominatim boundary.</li>
  <li><b>IT:</b> Per confini ufficiali usa il pulsante fonte ISTAT e scarica i limiti amministrativi dalla pagina ufficiale.</li>
  <li><b>EN:</b> For official boundaries use the ISTAT source button and download administrative limits from the official page.</li>
</ul>

<h3>6. Output prodotti / Produced outputs</h3>
<table>
  <tr><th>Output</th><th>Contenuto / Content</th></tr>
  <tr><td>GeoPackage automatico</td><td>Asse, campioni profilo, picchetti, sezioni planimetriche, punti sezione, centri, curve, volumi per tratta, disegni tecnici di sezione, poligoni sterro/riporto con colore ed etichetta.</td></tr>
  <tr><td>CSV</td><td>Campioni profilo o tabelle sezioni/volumi.</td></tr>
  <tr><td>PNG/PDF</td><td>Grafici e report stampabili.</td></tr>
  <tr><td>Layout QGIS</td><td>Mappa + grafico per stampa cartografica.</td></tr>
  <tr><td>GeoTIFF + ricevuta</td><td>Raster ritagliato e file testo con fonte/licenza/citazione.</td></tr>
</table>

<h3>7. Controlli qualit&agrave; / Quality checks</h3>
<ul>
  <li><b>IT:</b> Controlla CRS, unit&agrave; metriche, risoluzione raster, valori NoData e coerenza altimetrica prima di usare i volumi.</li>
  <li><b>EN:</b> Check CRS, metric units, raster resolution, NoData values and elevation consistency before using volumes.</li>
  <li><b>IT:</b> Le API globali non sostituiscono rilievo, livellazione, GNSS, LiDAR o DTM ufficiale di progetto.</li>
  <li><b>EN:</b> Global APIs do not replace survey, leveling, GNSS, LiDAR or official project DTM.</li>
</ul>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────
# Main Dialog
# ──────────────────────────────────────────────────────────────────────

class ProfiliSezioniComuniDialog(QDialog):

    TAB_PROFILO = 0
    TAB_SEZIONI = 1
    TAB_COMUNI = 2
    TAB_DOWNLOAD = 3
    TAB_PARAMETRI = 4
    TAB_RISULTATI = 5
    TAB_HELP = 6
    TAB_INFO = 7
    SETTINGS_GROUP = "ProfiliSezioniComuni"

    def __init__(self, parent=None, lang="it"):
        super().__init__(parent)
        self.lang = lang
        self.setWindowTitle("GeoFusion — Profili, Sezioni e Comuni")
        self.resize(1120, 820)
        self.setMinimumSize(QSize(900, 620))
        self.setStyleSheet(SPATIQON_STYLE)

        self._comuni_results = []  # cached search results
        self._selected_comune = None
        self._download_area_points = None

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(14, 14, 14, 14)

        # ── Header ────────────────────────────────────────────────
        header_row = QHBoxLayout()
        logo_lbl = QLabel()
        pix = QPixmap(os.path.join(os.path.dirname(__file__), "icon.svg"))
        if not pix.isNull():
            logo_lbl.setPixmap(
                pix.scaled(52, 52, QtCompat.KeepAspectRatio, QtCompat.SmoothTransformation)
            )
        header_row.addWidget(logo_lbl)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.lbl_header = QLabel()
        self.lbl_header.setObjectName("Title")
        self.lbl_subtitle = QLabel()
        self.lbl_subtitle.setObjectName("Subtitle")
        title_box.addWidget(self.lbl_header)
        title_box.addWidget(self.lbl_subtitle)
        header_row.addLayout(title_box, 1)
        header_row.addStretch()
        self.btn_lang = QPushButton("EN")
        self.btn_lang.setObjectName("btnLang")
        self.btn_lang.setFixedWidth(48)
        self.btn_lang.clicked.connect(self._toggle_lang)
        header_row.addWidget(self.btn_lang)
        main_layout.addLayout(header_row)

        sep = QFrame()
        try:
            sep.setFrameShape(QFrame.Shape.HLine)
        except AttributeError:
            sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("Separator")
        main_layout.addWidget(sep)

        # ── Tabs ──────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tab_prof = QWidget()
        self.tab_sez = QWidget()
        self.tab_comuni = QWidget()
        self.tab_download = QWidget()
        self.tab_parametri = QWidget()
        self.tab_results = QWidget()
        self.tab_help = QWidget()
        self.tab_info = QWidget()

        self.tabs.addTab(self.tab_prof, "")
        self.tabs.addTab(self.tab_sez, "")
        self.tabs.addTab(self.tab_comuni, "")
        self.tabs.addTab(self.tab_download, "")
        self.tabs.addTab(self.tab_parametri, "")
        self.tabs.addTab(self.tab_results, "")
        self.tabs.addTab(self.tab_help, "")
        self.tabs.addTab(self.tab_info, "")

        self._build_tab_profilo()
        self._build_tab_sezioni()
        self._build_tab_comuni()
        self._build_tab_download()
        self._build_tab_parametri()
        self._build_tab_results()
        self._build_tab_help()
        self._build_tab_info()

        main_layout.addWidget(self.tabs)

        # ── Export bar ────────────────────────────────────────────
        export_bar = QHBoxLayout()
        export_bar.setSpacing(8)

        self.btn_export_png = QPushButton()
        self.btn_export_png.setObjectName("btnExportPng")
        self.btn_export_png.setEnabled(False)

        self.btn_export_pdf = QPushButton()
        self.btn_export_pdf.setObjectName("btnExportPdf")
        self.btn_export_pdf.setEnabled(False)

        self.btn_export_csv = QPushButton()
        self.btn_export_csv.setObjectName("btnExportCsv")
        self.btn_export_csv.setEnabled(False)

        self.btn_export_vectors = QPushButton()
        self.btn_export_vectors.setObjectName("btnExportVectors")
        self.btn_export_vectors.setEnabled(False)

        self.btn_print_layout = QPushButton()
        self.btn_print_layout.setObjectName("btnPrintLayout")
        self.btn_print_layout.setEnabled(False)

        export_bar.addWidget(self.btn_export_png)
        export_bar.addWidget(self.btn_export_pdf)
        export_bar.addWidget(self.btn_export_csv)
        export_bar.addWidget(self.btn_export_vectors)
        export_bar.addStretch()
        export_bar.addWidget(self.btn_print_layout)
        main_layout.addLayout(export_bar)

        # ── Status bar ────────────────────────────────────────────
        status_layout = QHBoxLayout()
        self.lbl_status = QLabel("Pronto.")
        self.lbl_status.setStyleSheet("color: #4a8090; font-size: 12px;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumHeight(14)
        self.btn_close = QPushButton()
        self.btn_close.setObjectName("btnCancel")
        status_layout.addWidget(self.lbl_status, 1)
        status_layout.addWidget(self.progress)
        status_layout.addWidget(self.btn_close)
        main_layout.addLayout(status_layout)

        self.btn_close.clicked.connect(self.reject)
        self._load_parameter_settings()
        self._update_ui_lang()
        self._toggle_prof_visibility()
        self._toggle_sez_visibility()

    # ──────────────────────────────────────────────────────────────
    # Tab builders
    # ──────────────────────────────────────────────────────────────

    def _build_tab_profilo(self):
        layout = QVBoxLayout(self.tab_prof)
        layout.setSpacing(8)

        src_group = QGroupBox()
        src_group.setObjectName("grpProfSrc")
        src_layout = QVBoxLayout(src_group)

        row1 = QHBoxLayout()
        self.lbl_source = QLabel()
        row1.addWidget(self.lbl_source)
        self.cb_source = QComboBox()
        # items added/translated in _update_ui_lang
        row1.addWidget(self.cb_source, 1)
        self.lbl_samples_prof = QLabel()
        row1.addWidget(self.lbl_samples_prof)
        self.sb_samples_prof = QSpinBox()
        self.sb_samples_prof.setRange(10, 5000)
        self.sb_samples_prof.setValue(250)
        row1.addWidget(self.sb_samples_prof)
        src_layout.addLayout(row1)

        # DEM row (visible only when raster selected)
        self.row_raster_p = QWidget()
        row2 = QHBoxLayout(self.row_raster_p)
        row2.setContentsMargins(0, 0, 0, 0)
        self.lbl_dem_prof = QLabel("DEM/DTM:")
        row2.addWidget(self.lbl_dem_prof)
        self.cb_raster_prof = QComboBox()
        row2.addWidget(self.cb_raster_prof, 1)
        src_layout.addWidget(self.row_raster_p)

        # Info link
        self.lbl_provider_info = QLabel()
        self.lbl_provider_info.setOpenExternalLinks(False)
        self.lbl_provider_info.setStyleSheet("color: #00e5ff; font-size: 11px;")
        self.lbl_provider_info.setCursor(QtCompat.PointingHandCursor)
        self.lbl_provider_info.mousePressEvent = lambda _e: self.tabs.setCurrentIndex(self.TAB_INFO)
        src_layout.addWidget(self.lbl_provider_info)

        layout.addWidget(src_group)

        row_btn = QHBoxLayout()
        row_btn.addStretch()
        self.btn_draw_prof = QPushButton()
        row_btn.addWidget(self.btn_draw_prof)
        layout.addLayout(row_btn)
        layout.addStretch()

        self.cb_source.currentIndexChanged.connect(self._toggle_prof_visibility)

    def _build_tab_sezioni(self):
        layout = QVBoxLayout(self.tab_sez)
        layout.setSpacing(8)

        input_group = QGroupBox()
        input_group.setObjectName("grpSezInput")
        input_layout = QVBoxLayout(input_group)

        # Axis source
        row_axis = QHBoxLayout()
        self.lbl_axis = QLabel()
        row_axis.addWidget(self.lbl_axis)
        self.cb_axis_source = QComboBox()
        row_axis.addWidget(self.cb_axis_source, 1)
        input_layout.addLayout(row_axis)

        # Line layer row
        self.row_line_layer = QWidget()
        row_line = QHBoxLayout(self.row_line_layer)
        row_line.setContentsMargins(0, 0, 0, 0)
        self.lbl_line_layer = QLabel()
        row_line.addWidget(self.lbl_line_layer)
        self.cb_line_layer = QComboBox()
        row_line.addWidget(self.cb_line_layer, 1)
        self.lbl_feature = QLabel("Feature:")
        row_line.addWidget(self.lbl_feature)
        self.cb_line_feature = QComboBox()
        self.cb_line_feature.addItem("Prima feature / First feature", "first")
        self.cb_line_feature.addItem("Linea più lunga / Longest line", "longest")
        row_line.addWidget(self.cb_line_feature)
        self.btn_layer_sez = QPushButton()
        row_line.addWidget(self.btn_layer_sez)
        input_layout.addWidget(self.row_line_layer)

        # DEM + elevations
        row1 = QHBoxLayout()
        self.lbl_dem_sez = QLabel("DEM/DTM:")
        row1.addWidget(self.lbl_dem_sez)
        self.cb_raster_sez = QComboBox()
        row1.addWidget(self.cb_raster_sez, 1)
        self.lbl_start_elev = QLabel()
        row1.addWidget(self.lbl_start_elev)
        self.le_start_elev = QLineEdit()
        self.le_start_elev.setPlaceholderText("Es: 100.0")
        self.le_start_elev.setFixedWidth(80)
        row1.addWidget(self.le_start_elev)
        self.lbl_end_elev = QLabel()
        row1.addWidget(self.lbl_end_elev)
        self.le_end_elev = QLineEdit()
        self.le_end_elev.setPlaceholderText("Es: 105.0")
        self.le_end_elev.setFixedWidth(80)
        row1.addWidget(self.le_end_elev)
        self.lbl_grade = QLabel()
        row1.addWidget(self.lbl_grade)
        self.le_grade_pct = QLineEdit()
        self.le_grade_pct.setPlaceholderText("Es: 2.5")
        self.le_grade_pct.setFixedWidth(80)
        row1.addWidget(self.le_grade_pct)
        input_layout.addLayout(row1)

        # Interval + halfwidth + samples
        row2 = QHBoxLayout()
        self.lbl_interval = QLabel()
        row2.addWidget(self.lbl_interval)
        self.sb_interval = QDoubleSpinBox()
        self.sb_interval.setRange(1, 1000)
        self.sb_interval.setValue(25)
        row2.addWidget(self.sb_interval)
        self.lbl_halfwidth = QLabel()
        row2.addWidget(self.lbl_halfwidth)
        self.sb_halfwidth = QDoubleSpinBox()
        self.sb_halfwidth.setRange(1, 1000)
        self.sb_halfwidth.setValue(20)
        row2.addWidget(self.sb_halfwidth)
        self.lbl_samples_sez = QLabel()
        row2.addWidget(self.lbl_samples_sez)
        self.sb_samples_sez = QSpinBox()
        self.sb_samples_sez.setRange(5, 500)
        self.sb_samples_sez.setValue(41)
        row2.addWidget(self.sb_samples_sez)
        input_layout.addLayout(row2)

        # Smooth + draw button
        row3 = QHBoxLayout()
        self.chk_smooth = QCheckBox()
        self.chk_smooth.setChecked(True)
        row3.addWidget(self.chk_smooth)
        row3.addStretch()
        self.btn_draw_sez = QPushButton()
        row3.addWidget(self.btn_draw_sez)
        input_layout.addLayout(row3)

        layout.addWidget(input_group)
        layout.addStretch()

        self.cb_axis_source.currentIndexChanged.connect(self._toggle_sez_visibility)

    def _build_tab_comuni(self):
        layout = QVBoxLayout(self.tab_comuni)
        layout.setSpacing(8)

        search_group = QGroupBox()
        search_group.setObjectName("grpComuniSearch")
        sg_layout = QVBoxLayout(search_group)

        row_search = QHBoxLayout()
        self.le_comuni_search = QLineEdit()
        row_search.addWidget(self.le_comuni_search, 1)
        self.btn_comuni_search = QPushButton()
        row_search.addWidget(self.btn_comuni_search)
        sg_layout.addLayout(row_search)

        self.list_comuni = QListWidget()
        self.list_comuni.setMaximumHeight(180)
        sg_layout.addWidget(self.list_comuni)

        # Info card
        self.lbl_comune_info = QLabel()
        self.lbl_comune_info.setWordWrap(True)
        self.lbl_comune_info.setStyleSheet(
            "background:#071c2e; border:1px solid #0a4a6e; border-radius:6px;"
            "padding:8px; color:#7ac8d8; font-size:12px; min-height:60px;"
        )
        sg_layout.addWidget(self.lbl_comune_info)

        row_btns = QHBoxLayout()
        self.btn_comuni_load = QPushButton()
        self.btn_comuni_load.setObjectName("btnComuniLoad")
        self.btn_comuni_load.setEnabled(False)
        row_btns.addWidget(self.btn_comuni_load)

        self.chk_clip_area = QCheckBox()
        row_btns.addWidget(self.chk_clip_area)
        self.btn_comuni_download_source = QPushButton()
        row_btns.addWidget(self.btn_comuni_download_source)
        row_btns.addStretch()
        sg_layout.addLayout(row_btns)

        self.lbl_comuni_status = QLabel("")
        self.lbl_comuni_status.setStyleSheet("color:#4a8090; font-size:11px;")
        sg_layout.addWidget(self.lbl_comuni_status)

        layout.addWidget(search_group)
        layout.addStretch()

        self.list_comuni.currentRowChanged.connect(self._on_comune_selected)
        self.btn_comuni_search.clicked.connect(self.do_comuni_search)
        self.btn_comuni_load.clicked.connect(self.do_load_boundary)
        self.le_comuni_search.returnPressed.connect(self.do_comuni_search)

    def _build_tab_download(self):
        layout = QVBoxLayout(self.tab_download)
        layout.setSpacing(8)

        self.lbl_download_lang_notice = QLabel()
        self.lbl_download_lang_notice.setWordWrap(True)
        self.lbl_download_lang_notice.setStyleSheet(
            "background:#3a2a12; border:1px solid #7a5218; border-radius:6px;"
            "padding:8px; color:#e8b86d; font-size:11px;"
        )
        self.lbl_download_lang_notice.setVisible(False)
        layout.addWidget(self.lbl_download_lang_notice)

        area_group = QGroupBox()
        area_group.setObjectName("grpDownloadArea")
        self.grp_download_area = area_group
        area_layout = QVBoxLayout(area_group)

        row_area = QHBoxLayout()
        self.btn_draw_area = QPushButton()
        self.btn_draw_area.setObjectName("Primary")
        row_area.addWidget(self.btn_draw_area)
        self.lbl_download_area = QLabel()
        self.lbl_download_area.setObjectName("Muted")
        self.lbl_download_area.setWordWrap(True)
        row_area.addWidget(self.lbl_download_area, 1)
        area_layout.addLayout(row_area)

        source_row = QHBoxLayout()
        self.lbl_raster_source = QLabel()
        source_row.addWidget(self.lbl_raster_source)
        self.cb_raster_source = QComboBox()
        source_row.addWidget(self.cb_raster_source, 1)
        self.btn_open_raster_source = QPushButton()
        source_row.addWidget(self.btn_open_raster_source)
        self.btn_add_tinitaly_wcs = QPushButton()
        source_row.addWidget(self.btn_add_tinitaly_wcs)
        area_layout.addLayout(source_row)

        self.lbl_raster_source_info = QLabel()
        self.lbl_raster_source_info.setWordWrap(True)
        self.lbl_raster_source_info.setStyleSheet(
            "background:#0e1118; border:1px solid #2d3757; border-radius:6px;"
            "padding:8px; color:#8ba3c7; font-size:11px;"
        )
        area_layout.addWidget(self.lbl_raster_source_info)

        output_row = QHBoxLayout()
        self.lbl_download_output = QLabel()
        output_row.addWidget(self.lbl_download_output)
        self.le_download_output = QLineEdit()
        output_row.addWidget(self.le_download_output, 1)
        self.btn_browse_download_output = QPushButton()
        output_row.addWidget(self.btn_browse_download_output)
        area_layout.addLayout(output_row)

        action_row = QHBoxLayout()
        self.chk_load_downloaded_raster = QCheckBox()
        self.chk_load_downloaded_raster.setChecked(True)
        action_row.addWidget(self.chk_load_downloaded_raster)
        action_row.addStretch()
        self.btn_download_raster = QPushButton()
        self.btn_download_raster.setObjectName("btnDownloadRaster")
        self.btn_download_raster.setEnabled(False)
        action_row.addWidget(self.btn_download_raster)
        area_layout.addLayout(action_row)

        self.lbl_download_status = QLabel("")
        self.lbl_download_status.setStyleSheet("color:#566584; font-size:11px;")
        area_layout.addWidget(self.lbl_download_status)

        layout.addWidget(area_group)
        layout.addStretch()

        self.cb_raster_source.currentIndexChanged.connect(self._update_raster_source_info)

    def _build_tab_parametri(self):
        layout = QVBoxLayout(self.tab_parametri)
        layout.setSpacing(8)

        labels_group = QGroupBox()
        labels_group.setObjectName("grpParamLabels")
        form = QFormLayout(labels_group)
        form.setSpacing(9)

        self.le_param_peg = QLineEdit("Peg / Picchetto")
        self.le_param_progressive = QLineEdit("Progressive distances / Distanze progressive")
        self.le_param_ground = QLineEdit("Ground Level / Quota terreno")
        self.le_param_pipe = QLineEdit("Pipe Levels / Quote tubo")
        self.le_param_excavation = QLineEdit("Bottom Excavation Level / Fondo scavo")
        self.le_param_cut = QLineEdit("Cut / Sterro")
        self.le_param_fill = QLineEdit("Fill / Riporto")
        self.le_param_stockpile = QLineEdit("Stockpiles / Accumuli")

        self.lbl_param_peg = QLabel()
        self.lbl_param_progressive = QLabel()
        self.lbl_param_ground = QLabel()
        self.lbl_param_pipe = QLabel()
        self.lbl_param_excavation = QLabel()
        self.lbl_param_cut = QLabel()
        self.lbl_param_fill = QLabel()
        self.lbl_param_stockpile = QLabel()

        form.addRow(self.lbl_param_peg, self.le_param_peg)
        form.addRow(self.lbl_param_progressive, self.le_param_progressive)
        form.addRow(self.lbl_param_ground, self.le_param_ground)
        form.addRow(self.lbl_param_pipe, self.le_param_pipe)
        form.addRow(self.lbl_param_excavation, self.le_param_excavation)
        form.addRow(self.lbl_param_cut, self.le_param_cut)
        form.addRow(self.lbl_param_fill, self.le_param_fill)
        form.addRow(self.lbl_param_stockpile, self.le_param_stockpile)
        layout.addWidget(labels_group)

        insert_group = QGroupBox()
        insert_group.setObjectName("grpParamInsertion")
        insert_layout = QHBoxLayout(insert_group)
        self.lbl_insertion_x = QLabel()
        self.sb_insertion_x = QDoubleSpinBox()
        self.sb_insertion_x.setDecimals(3)
        self.sb_insertion_x.setRange(-999999999, 999999999)
        self.lbl_insertion_y = QLabel()
        self.sb_insertion_y = QDoubleSpinBox()
        self.sb_insertion_y.setDecimals(3)
        self.sb_insertion_y.setRange(-999999999, 999999999)
        self.btn_pick_insertion = QPushButton("...")
        self.btn_pick_insertion.setFixedWidth(44)
        insert_layout.addWidget(self.lbl_insertion_x)
        insert_layout.addWidget(self.sb_insertion_x)
        insert_layout.addWidget(self.lbl_insertion_y)
        insert_layout.addWidget(self.sb_insertion_y)
        insert_layout.addWidget(self.btn_pick_insertion)
        insert_layout.addStretch()
        layout.addWidget(insert_group)

        note = QLabel()
        note.setObjectName("Muted")
        note.setWordWrap(True)
        self.lbl_param_note = note
        layout.addWidget(note)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_auto_defaults = QPushButton()
        self.btn_params_reset = QPushButton()
        self.btn_params_save = QPushButton()
        self.btn_params_save.setObjectName("Primary")
        btn_row.addWidget(self.btn_auto_defaults)
        btn_row.addWidget(self.btn_params_reset)
        btn_row.addWidget(self.btn_params_save)
        layout.addLayout(btn_row)
        layout.addStretch()

        self.btn_params_save.clicked.connect(self._save_parameter_settings)
        self.btn_params_reset.clicked.connect(self._reset_parameter_settings)
        self.btn_auto_defaults.clicked.connect(self.apply_auto_defaults)

    def _build_tab_results(self):
        layout = QVBoxLayout(self.tab_results)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.web_view = None
        try:
            from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
            self.web_view = QWebEngineView()
        except ImportError:
            try:
                from qgis.PyQt.QtWebKitWidgets import QWebView
                self.web_view = QWebView()
            except ImportError:
                pass

        if self.web_view is None:
            self.web_view = QTextBrowser()
            self.web_view.setOpenExternalLinks(True)

        expanding = compat_enum(QSizePolicy, "Expanding", "Policy")
        self.web_view.setSizePolicy(expanding, expanding)
        layout.addWidget(self.web_view)
        self._show_placeholder()

    def _build_tab_info(self):
        layout = QVBoxLayout(self.tab_info)
        layout.setContentsMargins(0, 0, 0, 0)
        info_browser = QTextBrowser()
        info_browser.setOpenExternalLinks(True)
        info_browser.setHtml(INFO_HTML)
        layout.addWidget(info_browser)

    def _build_tab_help(self):
        layout = QVBoxLayout(self.tab_help)
        layout.setContentsMargins(0, 0, 0, 0)
        help_browser = QTextBrowser()
        help_browser.setOpenExternalLinks(True)
        help_browser.setHtml(HELP_HTML)
        layout.addWidget(help_browser)

    # ──────────────────────────────────────────────────────────────
    # Language toggle
    # ──────────────────────────────────────────────────────────────

    def _toggle_lang(self):
        self.lang = "en" if self.lang == "it" else "it"
        self.btn_lang.setText("IT" if self.lang == "en" else "EN")
        self._update_ui_lang()

    def _update_ui_lang(self):
        L = self.lang
        self.lbl_header.setText(_t(L, "Profili, Sezioni e Comuni", "Profiles, Sections & Municipalities"))
        self.lbl_subtitle.setText(
            _t(
                L,
                "Profili altimetrici, sezioni, sterri/riporti, comuni e download raster",
                "Elevation profiles, cross sections, cut/fill, municipalities and raster download",
            )
        )
        self.setWindowTitle(_t(L, "GeoFusion — Profili, Sezioni e Comuni",
                               "GeoFusion — Profiles, Sections & Municipalities"))
        self.btn_close.setText(_t(L, "Chiudi", "Close"))

        # Tab labels
        self.tabs.setTabText(self.TAB_PROFILO,   _t(L, "Profilo", "Profile"))
        self.tabs.setTabText(self.TAB_SEZIONI,   _t(L, "Sezioni", "Sections"))
        self.tabs.setTabText(self.TAB_COMUNI,    _t(L, "Comuni", "Municipalities"))
        self.tabs.setTabText(self.TAB_DOWNLOAD,  _t(L, "Download Raster", "Raster Download"))
        self.tabs.setTabText(self.TAB_PARAMETRI, _t(L, "Parametri", "Parameters"))
        self.tabs.setTabText(self.TAB_RISULTATI, _t(L, "Risultati", "Results"))
        self.tabs.setTabText(self.TAB_HELP,      "Help")
        self.tabs.setTabText(self.TAB_INFO,      "Info")

        # Tab Profilo
        grp_prof = self.tab_prof.findChild(QGroupBox, "grpProfSrc")
        if grp_prof:
            grp_prof.setTitle(_t(L, "Sorgente profilo", "Profile source"))
        self.lbl_source.setText(_t(L, "Sorgente:", "Source:"))
        self.lbl_samples_prof.setText(_t(L, "Campioni:", "Samples:"))
        self.lbl_provider_info.setText(
            _t(L, "ℹ Vedi limiti provider →", "ℹ See provider limits →")
        )
        self.btn_draw_prof.setText(_t(L, "Disegna asse", "Draw axis"))

        # Source combo items
        cur = self.cb_source.currentData()
        self.cb_source.blockSignals(True)
        self.cb_source.clear()
        self.cb_source.addItem(
            _t(L, "Open-Elevation API (SRTM/NASA)", "Open-Elevation API (SRTM/NASA)"),
            "provider_openelev"
        )
        self.cb_source.addItem(
            _t(L, "OpenTopoData API (SRTM 90m)", "OpenTopoData API (SRTM 90m)"),
            "provider_opentopo"
        )
        self.cb_source.addItem(
            _t(L, "Layer Raster (DEM/DTM locale)", "Raster Layer (local DEM/DTM)"),
            "raster"
        )
        # restore selection
        idx = self.cb_source.findData(cur)
        if idx >= 0:
            self.cb_source.setCurrentIndex(idx)
        self.cb_source.blockSignals(False)

        # Tab Sezioni
        grp_sez = self.tab_sez.findChild(QGroupBox, "grpSezInput")
        if grp_sez:
            grp_sez.setTitle(_t(L, "Dati e asse", "Data and axis"))
        self.lbl_axis.setText(_t(L, "Asse:", "Axis:"))

        cur_ax = self.cb_axis_source.currentData()
        self.cb_axis_source.blockSignals(True)
        self.cb_axis_source.clear()
        self.cb_axis_source.addItem(_t(L, "Disegna sulla mappa", "Draw on map"), "draw")
        self.cb_axis_source.addItem(_t(L, "Layer linea / Line layer", "Line layer"), "layer")
        idx_ax = self.cb_axis_source.findData(cur_ax)
        if idx_ax >= 0:
            self.cb_axis_source.setCurrentIndex(idx_ax)
        self.cb_axis_source.blockSignals(False)

        self.lbl_line_layer.setText(_t(L, "Layer linea:", "Line layer:"))
        self.btn_layer_sez.setText(_t(L, "Calcola da layer", "Calculate from layer"))
        self.lbl_start_elev.setText(_t(L, "Quota Inizio (m):", "Start Elev (m):"))
        self.lbl_end_elev.setText(_t(L, "Quota Fine (m):", "End Elev (m):"))
        self.lbl_grade.setText(_t(L, "Pend. (%):", "Grade (%):"))
        self.lbl_interval.setText(_t(L, "Interasse (m):", "Interval (m):"))
        self.lbl_halfwidth.setText(_t(L, "Semi-ampiezza (m):", "Half-width (m):"))
        self.lbl_samples_sez.setText(_t(L, "Campioni/sez.:", "Samples/sec.:"))
        self.chk_smooth.setText(_t(L, "Raccorda tangenti curve", "Smooth curve tangents"))
        self.btn_draw_sez.setText(_t(L, "Disegna asse", "Draw axis"))

        # Tab Comuni
        grp_com = self.tab_comuni.findChild(QGroupBox, "grpComuniSearch")
        if grp_com:
            grp_com.setTitle(_t(L, "Ricerca comune", "Municipality search"))
        self.le_comuni_search.setPlaceholderText(
            _t(L, "Nome comune... / Municipality name...", "Municipality name...")
        )
        self.btn_comuni_search.setText(_t(L, "Cerca", "Search"))
        self.btn_comuni_load.setText(
            _t(L, "Carica confine su mappa", "Load boundary on map")
        )
        self.btn_comuni_download_source.setText(
            _t(L, "Fonte download ISTAT", "ISTAT download source")
        )
        self.chk_clip_area.setText(
            _t(L, "Usa come area di ritaglio", "Use as clip area")
        )

        # Tab Download Raster
        grp_down = self.tab_download.findChild(QGroupBox, "grpDownloadArea")
        if grp_down:
            grp_down.setTitle(_t(L, "Area e sorgente raster", "Area and raster source"))
        self.btn_draw_area.setText(
            _t(L, "SHIFT + disegna area", "SHIFT + draw area")
        )
        if not self._download_area_points:
            self.lbl_download_area.setText(
                _t(
                    L,
                    "Nessuna area selezionata. Tieni premuto SHIFT, trascina un rettangolo sulla mappa e rilascia.",
                    "No area selected. Hold SHIFT, drag a rectangle on the map and release.",
                )
            )
        self.lbl_raster_source.setText(_t(L, "Sorgente:", "Source:"))
        self.btn_open_raster_source.setText(_t(L, "Apri fonte", "Open source"))
        self.btn_add_tinitaly_wcs.setText(_t(L, "Carica WCS TINITALY", "Load TINITALY WCS"))
        self.lbl_download_output.setText(_t(L, "Output GeoTIFF:", "Output GeoTIFF:"))
        self.btn_browse_download_output.setText(_t(L, "Sfoglia...", "Browse..."))
        self.chk_load_downloaded_raster.setText(
            _t(L, "Carica raster in QGIS dopo il download", "Load raster in QGIS after download")
        )
        self.btn_download_raster.setText(_t(L, "Scarica area raster", "Download raster area"))

        cur_src = self.cb_raster_source.currentData()
        self.cb_raster_source.blockSignals(True)
        self.cb_raster_source.clear()
        for key in ("tinitaly", "hrdtm5m"):
            self.cb_raster_source.addItem(source_label(key, L), key)
        idx_src = self.cb_raster_source.findData(cur_src)
        if idx_src >= 0:
            self.cb_raster_source.setCurrentIndex(idx_src)
        self.cb_raster_source.blockSignals(False)
        self._update_raster_source_info()

        italy_only = (L == "en")
        self.grp_download_area.setEnabled(not italy_only)
        self.lbl_download_lang_notice.setVisible(italy_only)
        if italy_only:
            self.lbl_download_lang_notice.setText(
                "⚠ TINITALY and HR-DTM-5m only cover Italian territory. "
                "This download tab is disabled in English; switch to Italiano "
                "(EN button, top right) to use it."
            )

        # Tab Parametri
        grp_params = self.tab_parametri.findChild(QGroupBox, "grpParamLabels")
        if grp_params:
            grp_params.setTitle(_t(L, "Etichette report e grafici", "Report and chart labels"))
        grp_ins = self.tab_parametri.findChild(QGroupBox, "grpParamInsertion")
        if grp_ins:
            grp_ins.setTitle(_t(L, "Punto di inserimento", "Insertion point"))
        self.lbl_param_peg.setText(_t(L, "Picchetto / Peg:", "Peg / Picchetto:"))
        self.lbl_param_progressive.setText(
            _t(L, "Distanze progressive / Progressive distances:", "Progressive distances / Distanze progressive:")
        )
        self.lbl_param_ground.setText(_t(L, "Quota terreno / Ground Level:", "Ground Level / Quota terreno:"))
        self.lbl_param_pipe.setText(_t(L, "Quote tubo / Pipe Levels:", "Pipe Levels / Quote tubo:"))
        self.lbl_param_excavation.setText(
            _t(L, "Fondo scavo / Bottom Excavation Level:", "Bottom Excavation Level / Fondo scavo:")
        )
        self.lbl_param_cut.setText(_t(L, "Sterro / Cut:", "Cut / Sterro:"))
        self.lbl_param_fill.setText(_t(L, "Riporto / Fill:", "Fill / Riporto:"))
        self.lbl_param_stockpile.setText(_t(L, "Accumuli / Stockpiles:", "Stockpiles / Accumuli:"))
        self.lbl_insertion_x.setText("X:")
        self.lbl_insertion_y.setText("Y:")
        self.lbl_param_note.setText(
            _t(
                L,
                "Queste etichette sono usate per impostare la grafia dei report e ricordano i parametri della schermata di riferimento. Include sterri, riporti e accumuli.",
                "These labels control report wording and mirror the reference screenshot parameters. Cut, fill and stockpiles are included.",
            )
        )
        self.btn_params_reset.setText(_t(L, "Ripristina default", "Reset defaults"))
        self.btn_auto_defaults.setText(_t(L, "Settaggi automatici", "Automatic settings"))
        self.btn_params_save.setText(_t(L, "Salva parametri", "Save parameters"))

        # Export buttons
        self.btn_export_png.setText(_t(L, "Esporta PNG", "Export PNG"))
        self.btn_export_pdf.setText(_t(L, "Esporta PDF", "Export PDF"))
        self.btn_export_csv.setText(_t(L, "Esporta CSV", "Export CSV"))
        self.btn_export_vectors.setText(_t(L, "Esporta GPKG", "Export GPKG"))
        self.btn_print_layout.setText(_t(L, "Stampa Layout", "Print Layout"))

        self.lbl_status.setText(_t(L, "Pronto.", "Ready."))
        self._toggle_prof_visibility()
        self._toggle_sez_visibility()

    # ──────────────────────────────────────────────────────────────
    # Visibility toggles
    # ──────────────────────────────────────────────────────────────

    def _toggle_prof_visibility(self):
        source = self.cb_source.currentData()
        self.row_raster_p.setVisible(source == "raster")

    def _toggle_sez_visibility(self):
        use_layer = self.cb_axis_source.currentData() == "layer"
        self.row_line_layer.setVisible(use_layer)
        self.btn_draw_sez.setVisible(not use_layer)

    def _update_raster_source_info(self):
        source_key = self.cb_raster_source.currentData() or "tinitaly"
        self.lbl_raster_source_info.setText(source_info_text(source_key, self.lang))

    def set_download_area(self, area_points, bbox_label):
        self._download_area_points = area_points
        self.lbl_download_area.setText(bbox_label)
        self.btn_download_raster.setEnabled(bool(area_points))

    def get_download_source(self):
        return self.cb_raster_source.currentData() or "tinitaly"

    def get_download_output_path(self):
        return self.le_download_output.text().strip()

    def set_download_output_path(self, path):
        self.le_download_output.setText(path or "")

    def get_parameter_labels(self):
        return {
            "peg": self.le_param_peg.text().strip() or "Peg / Picchetto",
            "progressive": self.le_param_progressive.text().strip() or "Progressive distances / Distanze progressive",
            "ground": self.le_param_ground.text().strip() or "Ground Level / Quota terreno",
            "pipe": self.le_param_pipe.text().strip() or "Pipe Levels / Quote tubo",
            "excavation": self.le_param_excavation.text().strip() or "Bottom Excavation Level / Fondo scavo",
            "cut": self.le_param_cut.text().strip() or "Cut / Sterro",
            "fill": self.le_param_fill.text().strip() or "Fill / Riporto",
            "stockpile": self.le_param_stockpile.text().strip() or "Stockpiles / Accumuli",
            "insertion_x": self.sb_insertion_x.value(),
            "insertion_y": self.sb_insertion_y.value(),
        }

    def _settings(self):
        s = QSettings()
        s.beginGroup(self.SETTINGS_GROUP)
        return s

    def _load_parameter_settings(self):
        s = self._settings()
        defaults = self.get_parameter_labels()
        edits = {
            "peg": self.le_param_peg,
            "progressive": self.le_param_progressive,
            "ground": self.le_param_ground,
            "pipe": self.le_param_pipe,
            "excavation": self.le_param_excavation,
            "cut": self.le_param_cut,
            "fill": self.le_param_fill,
            "stockpile": self.le_param_stockpile,
        }
        for key, edit in edits.items():
            edit.setText(str(s.value("labels/{0}".format(key), defaults[key])))
        try:
            self.sb_insertion_x.setValue(float(s.value("insertion/x", 0.0)))
            self.sb_insertion_y.setValue(float(s.value("insertion/y", 0.0)))
        except Exception:
            self.sb_insertion_x.setValue(0.0)
            self.sb_insertion_y.setValue(0.0)
        s.endGroup()

    def _save_parameter_settings(self):
        s = self._settings()
        labels = self.get_parameter_labels()
        for key in ("peg", "progressive", "ground", "pipe", "excavation", "cut", "fill", "stockpile"):
            s.setValue("labels/{0}".format(key), labels[key])
        s.setValue("insertion/x", labels["insertion_x"])
        s.setValue("insertion/y", labels["insertion_y"])
        s.endGroup()
        self.lbl_status.setText(_t(self.lang, "Parametri salvati.", "Parameters saved."))

    def _reset_parameter_settings(self):
        defaults = {
            "peg": "Peg / Picchetto",
            "progressive": "Progressive distances / Distanze progressive",
            "ground": "Ground Level / Quota terreno",
            "pipe": "Pipe Levels / Quote tubo",
            "excavation": "Bottom Excavation Level / Fondo scavo",
            "cut": "Cut / Sterro",
            "fill": "Fill / Riporto",
            "stockpile": "Stockpiles / Accumuli",
        }
        self.le_param_peg.setText(defaults["peg"])
        self.le_param_progressive.setText(defaults["progressive"])
        self.le_param_ground.setText(defaults["ground"])
        self.le_param_pipe.setText(defaults["pipe"])
        self.le_param_excavation.setText(defaults["excavation"])
        self.le_param_cut.setText(defaults["cut"])
        self.le_param_fill.setText(defaults["fill"])
        self.le_param_stockpile.setText(defaults["stockpile"])
        self.sb_insertion_x.setValue(0.0)
        self.sb_insertion_y.setValue(0.0)
        self._save_parameter_settings()

    def apply_auto_defaults(self):
        self.sb_samples_prof.setValue(250)
        self.sb_interval.setValue(25.0)
        self.sb_halfwidth.setValue(20.0)
        self.sb_samples_sez.setValue(41)
        self.chk_smooth.setChecked(True)
        self.le_start_elev.clear()
        self.le_end_elev.clear()
        self.le_grade_pct.clear()
        self._reset_parameter_settings()
        self.lbl_status.setText(
            _t(self.lang, "Settaggi automatici topografici applicati.", "Automatic surveying settings applied.")
        )

    # ──────────────────────────────────────────────────────────────
    # Populate dropdowns
    # ──────────────────────────────────────────────────────────────

    def populate_rasters(self):
        self.cb_raster_prof.clear()
        self.cb_raster_sez.clear()
        self.cb_line_layer.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer):
                self.cb_raster_prof.addItem(layer.name(), layer.id())
                self.cb_raster_sez.addItem(layer.name(), layer.id())
            elif isinstance(layer, QgsVectorLayer):
                geom_type = QgsWkbTypes.geometryType(layer.wkbType())
                if geom_type == QgsWkbTypes.LineGeometry:
                    self.cb_line_layer.addItem(layer.name(), layer.id())

    def get_selected_raster(self, for_sezioni=False):
        layer_id = (self.cb_raster_sez.currentData() if for_sezioni
                    else self.cb_raster_prof.currentData())
        if layer_id:
            return QgsProject.instance().mapLayer(layer_id)
        return None

    def get_selected_line_layer(self):
        layer_id = self.cb_line_layer.currentData()
        if layer_id:
            return QgsProject.instance().mapLayer(layer_id)
        return None

    # ──────────────────────────────────────────────────────────────
    # Comuni tab logic
    # ──────────────────────────────────────────────────────────────

    def do_comuni_search(self):
        from .core_comuni import search_comuni
        query = self.le_comuni_search.text().strip()
        if not query:
            return
        L = self.lang
        self.lbl_comuni_status.setText(_t(L, "Ricerca in corso...", "Searching..."))
        QApplication.processEvents()
        try:
            results = search_comuni(query, limit=15)
            self._comuni_results = results
            self.list_comuni.clear()
            if not results:
                self.lbl_comuni_status.setText(
                    _t(L, "Nessun risultato trovato.", "No results found.")
                )
                return
            for r in results:
                addr = r.get("address", {})
                region = addr.get("region") or addr.get("state") or ""
                province = addr.get("county") or addr.get("province") or ""
                label = r.get("name", "")
                if region:
                    label += f"  ({region}"
                    if province:
                        label += f" / {province}"
                    label += ")"
                item = QListWidgetItem(label)
                item.setData(QtCompat.UserRole, r)
                self.list_comuni.addItem(item)
            self.lbl_comuni_status.setText(
                _t(L, f"{len(results)} risultati trovati.", f"{len(results)} results found.")
            )
        except Exception as e:
            self.lbl_comuni_status.setText(
                _t(L, f"Errore: {e}", f"Error: {e}")
            )

    def _on_comune_selected(self, row):
        if row < 0 or row >= len(self._comuni_results):
            self._selected_comune = None
            self.btn_comuni_load.setEnabled(False)
            self.lbl_comune_info.setText("")
            return
        comune = self._comuni_results[row]
        self._selected_comune = comune
        self.btn_comuni_load.setEnabled(True)
        addr = comune.get("address", {})
        region = addr.get("region") or addr.get("state") or "—"
        province = addr.get("county") or addr.get("province") or "—"
        L = self.lang
        self.lbl_comune_info.setText(
            f"<b>{comune.get('name', '')}</b><br>"
            f"{_t(L, 'Regione', 'Region')}: {region}<br>"
            f"{_t(L, 'Provincia', 'Province')}: {province}<br>"
            f"{_t(L, 'Posizione', 'Position')}: {comune.get('lat', 0):.5f}, {comune.get('lon', 0):.5f}"
        )

    def do_load_boundary(self):
        if not self._selected_comune:
            return
        from .core_comuni import fetch_comune_boundary, create_boundary_layer
        L = self.lang
        comune = self._selected_comune
        self.lbl_comuni_status.setText(
            _t(L, "Scarico confine...", "Downloading boundary...")
        )
        QApplication.processEvents()
        try:
            feature = fetch_comune_boundary(
                comune.get("osm_type", "relation"),
                comune.get("osm_id", "")
            )
            if feature is None:
                self.lbl_comuni_status.setText(
                    _t(L, "Confine non disponibile su OSM.", "Boundary not available on OSM.")
                )
                return
            name = comune.get("name", "Comune")
            boundary_prefix = _t(L, "Confine", "Boundary")
            vl_boundary = create_boundary_layer(feature, f"{boundary_prefix} — {name}")
            vl_boundary.setFieldAlias(0, _t(L, "Nome", "Name"))
            vl_boundary.setFieldAlias(1, _t(L, "Nome esteso", "Full name"))
            self.lbl_comuni_status.setText(
                _t(L, f"Confine caricato: {name}", f"Boundary loaded: {name}")
            )
        except Exception as e:
            self.lbl_comuni_status.setText(
                _t(L, f"Errore: {e}", f"Error: {e}")
            )

    # ──────────────────────────────────────────────────────────────
    # Results display
    # ──────────────────────────────────────────────────────────────

    def _show_placeholder(self):
        L = self.lang
        html = f"""
        <html><head><style>
            body {{ margin:0; padding:40px; background:
                   radial-gradient(circle at top left, rgba(79,115,196,.14), transparent 28%),
                   linear-gradient(180deg, #0d1119 0%, #12151e 100%);
                   font-family:'Segoe UI','Inter',sans-serif; color:#566584;
                   display:flex; align-items:center; justify-content:center; min-height:300px; }}
            .placeholder {{ text-align:center; background:rgba(15,23,36,.88); border:1px solid #2d3757;
                            border-radius:18px; padding:30px 34px; min-width:320px;
                            box-shadow:0 16px 30px rgba(0,0,0,.18); }}
            .placeholder .icon {{ font-size:48px; margin-bottom:16px; opacity:.6; }}
            .placeholder h3 {{ color:#dbeafe; font-size:18px; margin:0 0 8px; }}
            .placeholder p  {{ color:#7f93b2; font-size:13px; margin:0; line-height:1.6; }}
        </style></head><body>
        <div class="placeholder">
            <div class="icon">📐</div>
            <h3>{_t(L, "Nessun risultato", "No results yet")}</h3>
            <p>{_t(L, "Disegna un asse sulla mappa o calcola da un layer.",
                "Draw an axis on the map or calculate from a layer.")}</p>
        </div></body></html>
        """
        self._set_html(html)

    def _set_html(self, html):
        if hasattr(self.web_view, "setHtml"):
            self.web_view.setHtml(html)
        else:
            self.web_view.setPlainText(html)

    def show_results(self, html_content):
        """Show results HTML in the Results tab and switch to it."""
        full_html = f"""<!DOCTYPE html>
        <html><head><style>
            body {{ margin:0; padding:0; background:
                   radial-gradient(circle at top left, rgba(79,115,196,.14), transparent 28%),
                   radial-gradient(circle at top right, rgba(245,158,11,.10), transparent 24%),
                   linear-gradient(180deg, #0d1119 0%, #12151e 100%);
                   font-family:'Segoe UI','Inter',sans-serif; color:#e2e8f0; line-height:1.55; }}
            .results-shell {{ max-width:1580px; margin:0 auto; padding:20px 20px 34px; }}
            svg {{ max-width:100%; height:auto; }}
            table {{ border-collapse:collapse; width:100%; font-size:12px; margin-top:8px; overflow:hidden;
                     border-radius:10px; }}
            th {{ background:linear-gradient(180deg, #20283d 0%, #182132 100%);
                  color:#a9c4ef; padding:8px 10px; text-align:left;
                  border-bottom:2px solid #2d3757; font-weight:600; font-size:11px;
                  text-transform:uppercase; letter-spacing:.5px; }}
            td {{ padding:6px 10px; border-bottom:1px solid #2d3757; color:#e2e8f0; }}
            tr:nth-child(even) {{ background:rgba(30,36,55,0.45); }}
            tr:hover {{ background:rgba(79,115,196,0.10); }}
            .section-title {{ font-size:18px; font-weight:700; color:#f1f5f9;
                              margin:24px 0 12px; padding:0 0 8px 10px;
                              border-bottom:1px solid rgba(79,115,196,.30);
                              border-left:4px solid #4f73c4; letter-spacing:.2px; }}
            .summary-card {{ background:
                             linear-gradient(180deg, rgba(32,40,61,.96) 0%, rgba(21,28,42,.98) 100%);
                             border:1px solid rgba(61,79,118,.82); border-radius:14px;
                             padding:15px 18px; margin-bottom:14px; font-size:13px;
                             line-height:1.75; box-shadow:0 14px 28px rgba(0,0,0,.18); }}
            .summary-card.crs-warn {{ border-color:#f59e0b; box-shadow:0 0 0 1px rgba(245,158,11,.14) inset; }}
            .summary-card.crs-ok {{ border-color:#1d4ed8; }}
            .summary-card strong {{ color:#f1f5f9; }}
            .report-list {{ margin:8px 0 0 16px; padding:0; }}
            .report-list li {{ margin:2px 0; color:#cbd5e1; }}
            .badge {{ display:inline-block; padding:2px 10px; border-radius:12px;
                      font-size:11px; font-weight:600; }}
            .badge-cut  {{ background:rgba(239,68,68,.15); color:#ef4444; }}
            .badge-fill {{ background:rgba(34,197,94,.15);  color:#22c55e; }}
            .badge-info {{ background:rgba(79,115,196,.18); color:#8fb3ff; }}
            .chart-container {{ background:linear-gradient(180deg, #0f1724 0%, #0c121c 100%);
                                border:1px solid #2d3757;
                                border-radius:14px; padding:14px; margin-bottom:14px;
                                overflow-x:auto; }}
            .accordion-list {{ display:flex; flex-direction:column; gap:12px; margin:12px 0 18px; }}
            .accordion-item {{ background:linear-gradient(180deg, rgba(15,23,36,.98) 0%, rgba(10,14,22,.98) 100%);
                               border:1px solid rgba(54,70,104,.88); border-radius:16px;
                               box-shadow:0 14px 26px rgba(0,0,0,.18); overflow:hidden; }}
            .accordion-head {{ width:100%; display:flex; justify-content:space-between; align-items:center;
                               gap:12px; border:none; padding:14px 16px; text-align:left; cursor:pointer;
                               background:transparent; color:inherit; }}
            .accordion-head:hover {{ background:rgba(79,115,196,.08); }}
            .accordion-head.static-head {{ cursor:default; }}
            .accordion-title {{ display:block; font-size:15px; font-weight:700; color:#f8fafc; letter-spacing:.2px; }}
            .accordion-meta {{ display:block; font-size:12px; color:#8ba3c7; line-height:1.5; text-align:right; }}
            .accordion-body {{ display:none; padding:0 16px 16px; border-top:1px solid rgba(61,79,118,.45); }}
            .section-browser {{ display:grid; grid-template-columns:minmax(280px,320px) minmax(0,1fr);
                                gap:16px; align-items:start; margin:14px 0 22px; }}
            .section-card-list {{ display:flex; flex-direction:column; gap:12px; max-height:920px;
                                  overflow:auto; padding-right:4px; }}
            .section-result-card {{ width:100%; border:1px solid rgba(54,70,104,.88); border-radius:16px;
                                    background:linear-gradient(180deg, rgba(15,23,36,.98) 0%, rgba(10,14,22,.98) 100%);
                                    color:inherit; text-align:left; cursor:pointer; padding:14px 15px;
                                    box-shadow:0 14px 26px rgba(0,0,0,.18); transition:.18s ease; }}
            .section-result-card:hover {{ transform:translateY(-1px); border-color:#4f73c4;
                                          box-shadow:0 14px 28px rgba(79,115,196,.16); }}
            .section-result-card.is-active {{ border-color:#60a5fa; box-shadow:0 0 0 1px rgba(96,165,250,.24) inset,
                                              0 18px 32px rgba(37,99,235,.12); }}
            .section-result-card.is-empty {{ opacity:.82; }}
            .section-result-title {{ display:block; font-size:15px; font-weight:700; color:#f8fafc; letter-spacing:.2px; }}
            .section-result-subtitle {{ display:block; margin-top:4px; font-size:12px; color:#dbeafe; }}
            .section-result-meta {{ display:block; margin-top:6px; font-size:12px; color:#8ba3c7; line-height:1.55; }}
            .section-result-chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }}
            .section-result-chip {{ display:inline-flex; align-items:center; padding:4px 8px; border-radius:999px;
                                    background:rgba(79,115,196,.16); color:#cfe0ff; font-size:11px; font-weight:600; }}
            .section-detail-stack {{ min-width:0; }}
            .section-detail-panel {{ display:none; background:linear-gradient(180deg, #101824 0%, #0d1520 100%);
                                     border:1px solid #334155; border-radius:16px; padding:16px; margin:0;
                                     box-shadow:0 14px 26px rgba(0,0,0,.16); }}
            .section-detail-panel.is-empty {{ padding:18px; }}
            .sections-grid {{ display:flex; flex-wrap:wrap; gap:10px; padding:8px 0; }}
            .section-card {{ flex:0 0 auto; text-align:center; width:140px; }}
            .section-card svg {{ border:1px solid #2d3757; border-radius:6px; background:#0e1118; }}
            .section-card .label {{ font-size:11px; color:#8ba3c7; margin-top:4px; line-height:1.4; }}
            .chart-card-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
                                gap:14px; margin:12px 0 16px; }}
            .chart-card {{ background:linear-gradient(180deg, rgba(15,23,36,.98) 0%, rgba(10,14,22,.98) 100%);
                           border:1px solid rgba(54,70,104,.88); border-radius:16px;
                           padding:13px; cursor:pointer; transition:.18s ease;
                           box-shadow:0 14px 26px rgba(0,0,0,.18); }}
            .chart-card:hover {{ transform:translateY(-2px); border-color:#4f73c4;
                                 box-shadow:0 14px 28px rgba(79,115,196,.16); }}
            .chart-card.is-empty {{ cursor:default; opacity:.7; }}
            .chart-card-title {{ font-size:15px; font-weight:700; color:#f8fafc; margin-bottom:8px;
                                 letter-spacing:.2px; }}
            .chart-card-preview {{ margin-bottom:10px; }}
            .chart-card-meta {{ font-size:12px; color:#8ba3c7; line-height:1.5; }}
            .chart-detail {{ display:none; background:linear-gradient(180deg, #101824 0%, #0d1520 100%);
                             border:1px solid #334155; border-radius:16px; padding:16px; margin:0 0 16px;
                             box-shadow:0 14px 26px rgba(0,0,0,.16); }}
            .chart-detail-header {{ margin-bottom:10px; color:#8ba3c7; font-size:13px; }}
            .chart-detail-body {{ display:flex; flex-wrap:wrap; gap:14px; align-items:flex-start; }}
            .chart-detail-graphic {{ flex:1 1 520px; min-width:320px; }}
            .chart-detail-table {{ flex:0 0 280px; min-width:240px; }}
            .chart-detail-table table {{ margin-top:0; }}
            .chart-detail-table th {{ width:52%; }}
            .chart-detail-table td, .chart-detail-table th {{ font-size:12px; }}
            @media (max-width: 1120px) {{
                .section-browser {{ grid-template-columns:1fr; }}
                .section-card-list {{ max-height:none; }}
            }}
        </style></head><body>
            <div class="results-shell">{html_content}</div>
        </body></html>"""
        self._set_html(full_html)
        self.tabs.setCurrentIndex(self.TAB_RISULTATI)

    def enable_exports(self, enabled):
        self.btn_export_png.setEnabled(enabled)
        self.btn_export_pdf.setEnabled(enabled)
        self.btn_export_csv.setEnabled(enabled)
        self.btn_export_vectors.setEnabled(enabled)
        self.btn_print_layout.setEnabled(enabled)
