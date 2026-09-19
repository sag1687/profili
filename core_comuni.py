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
core_comuni.py — Italian municipality search and boundary loader.

Boundaries come straight from ISTAT's official "Confini delle unita'
amministrative a fini statistici" archive (non-generalised, native CRS
EPSG:32632) rather than OpenStreetMap: the archive is downloaded once
(~100 MB) and cached locally, then every search/fetch reads it through
GDAL's /vsizip/ virtual filesystem with no further network round-trip.
This is the same "big ZIP, /vsizip/ per-item access" pattern already
used for the TINITALY raster tiles in core_raster_download.py.

The municipality name/region/province lookup is read from the archive once
per session into a plain in-memory list (build_comuni_index()) rather than
re-opening the three shapefiles on every keystroke: with ~7900 municipalities
the one-off scan takes a fraction of a second, while filtering the resulting
list is effectively instant. A static JSON bundled with the plugin was
considered instead, but ISTAT revises this archive yearly, so a bundled copy
would silently go stale — deriving the index from the same freshly-downloaded
archive keeps it always correct without extra maintenance.

Public API:
  ensure_istat_archive(cache_dir, progress_callback=None) -> (zip_path, stamp)
  build_comuni_index(zip_path, stamp) -> list[dict]
  search_comuni(index, query, limit=15) -> list[dict]
  fetch_comune_boundary(zip_path, stamp, pro_com) -> (QgsGeometry, dict) | None
  create_boundary_layer(qgeometry, attrs, layer_name) -> QgsVectorLayer
"""

import datetime
import os
import unicodedata

from qgis.core import (
    QgsFeature,
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsGeometry,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)

from .core_raster_download import download_file

ISTAT_BASE_URL = (
    "https://www.istat.it/storage/cartografia/confini_amministrativi/"
    "non_generalizzati"
)


# ──────────────────────────────────────────────────────────────────────
# Archive download / cache
# ──────────────────────────────────────────────────────────────────────


def _istat_candidate_stamps():
    """Yield (year, "ddmmyyyy") stamps to try, most recent first.

    ISTAT publishes one archive per year, dated 1 January of that year,
    but the file itself is only uploaded a little while into the year
    (historically anywhere from January to April) — so the current
    year's archive may not exist yet. The previous two years are tried
    as a fallback.
    """
    current_year = datetime.date.today().year
    for year in (current_year, current_year - 1, current_year - 2):
        yield year, f"0101{year}"


def _istat_zip_url(year, stamp):
    return f"{ISTAT_BASE_URL}/{year}/Limiti{stamp}.zip"


def _com_shapefile_path(zip_path, stamp):
    return f"/vsizip/{zip_path}/Com{stamp}/Com{stamp}_WGS84.shp"


def _reg_shapefile_path(zip_path, stamp):
    return f"/vsizip/{zip_path}/Reg{stamp}/Reg{stamp}_WGS84.shp"


def _provcm_shapefile_path(zip_path, stamp):
    return f"/vsizip/{zip_path}/ProvCM{stamp}/ProvCM{stamp}_WGS84.shp"


def ensure_istat_archive(cache_dir, progress_callback=None):
    """
    Make sure the official ISTAT administrative-boundaries archive is
    available locally, downloading it once and reusing the cached copy
    on every later call (download_file() skips the transfer once the
    local file size matches the server's Content-Length).

    Returns (zip_path, stamp) where stamp is the "ddmmyyyy" vintage
    identifier used to build the archive's internal shapefile paths.
    """
    os.makedirs(cache_dir, exist_ok=True)

    errors = []
    for year, stamp in _istat_candidate_stamps():
        zip_path = os.path.join(cache_dir, f"Limiti{stamp}.zip")
        try:
            download_file(
                _istat_zip_url(year, stamp),
                zip_path,
                progress_callback,
                0,
                100,
                "ISTAT — confini amministrativi",
            )
            return zip_path, stamp
        except OSError as e:
            # Network/HTTP errors (incl. 404 for a year not published yet,
            # both urllib.error.URLError/HTTPError subclass OSError) are
            # expected here and worth retrying with the previous year's
            # archive. Anything else (a real bug) propagates immediately
            # instead of being silently swallowed by a bare "except".
            errors.append(f"{year}: {e}")
            continue
    raise RuntimeError(
        "Impossibile scaricare i confini ISTAT / Could not download the "
        "ISTAT boundaries archive: " + "; ".join(errors)
    )


# ──────────────────────────────────────────────────────────────────────
# Search
# ──────────────────────────────────────────────────────────────────────


def _normalize(text):
    """Case- and accent-insensitive comparison key."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(
        c for c in decomposed if not unicodedata.combining(c)
    ).casefold().strip()


def _code_name_map(zip_path, stamp, path_fn, code_field, name_field):
    layer = QgsVectorLayer(path_fn(zip_path, stamp), "lookup", "ogr")
    if not layer.isValid():
        return {}
    return {
        feat[code_field]: feat[name_field] for feat in layer.getFeatures()
    }


def build_comuni_index(zip_path, stamp):
    """
    Scan the cached ISTAT archive once and return every municipality as a
    plain list[dict] with keys: name, pro_com, region, province, sigla,
    plus a precomputed "_needle" (accent/case-insensitive name) used by
    search_comuni() to filter without touching the shapefile again.

    Call this once per session (after ensure_istat_archive()) and reuse the
    result for every search — that's what makes typing in the search box
    responsive despite the archive holding ~7900 municipalities.
    """
    layer = QgsVectorLayer(
        _com_shapefile_path(zip_path, stamp), "comuni_istat", "ogr"
    )
    if not layer.isValid():
        raise RuntimeError(
            "Impossibile aprire l'archivio ISTAT / Could not open the "
            "ISTAT boundaries archive."
        )

    reg_names = _code_name_map(
        zip_path, stamp, _reg_shapefile_path, "COD_REG", "DEN_REG"
    )
    prov_names = _code_name_map(
        zip_path, stamp, _provcm_shapefile_path, "COD_PROV", "DEN_UTS"
    )
    prov_sigle = _code_name_map(
        zip_path, stamp, _provcm_shapefile_path, "COD_PROV", "SIGLA"
    )

    index = []
    for feat in layer.getFeatures():
        name = feat["COMUNE"]
        cod_reg = feat["COD_REG"]
        cod_prov = feat["COD_PROV"]
        index.append(
            {
                "name": name,
                "pro_com": feat["PRO_COM"],
                "region": reg_names.get(cod_reg, ""),
                "province": prov_names.get(cod_prov, ""),
                "sigla": prov_sigle.get(cod_prov, ""),
                "_needle": _normalize(name),
            }
        )
    return index


def search_comuni(index, query, limit=15):
    """
    Filter a municipality index (as returned by build_comuni_index()) by
    name. Pure in-memory operation, no I/O.

    Parameters
    ----------
    index : list[dict] — as returned by build_comuni_index().
    query : str — name (partial or full) of the municipality.
    limit : int — max results (default 15).

    Returns
    -------
    list[dict] with keys: name, pro_com, region, province, sigla
    """
    if not query or not query.strip():
        return []

    needle = _normalize(query)
    matches = [entry for entry in index if needle in entry["_needle"]]
    matches.sort(key=lambda m: m["name"])
    return [
        {k: v for k, v in m.items() if k != "_needle"}
        for m in matches[:limit]
    ]


# ──────────────────────────────────────────────────────────────────────
# Boundary fetch + layer creation
# ──────────────────────────────────────────────────────────────────────


def fetch_comune_boundary(zip_path, stamp, pro_com):
    """
    Fetch a single municipality's polygon geometry + attributes from the
    cached ISTAT archive, looked up by its unique ISTAT "PRO_COM" code.

    Returns (QgsGeometry, dict) — geometry in the archive's native CRS
    (EPSG:32632) plus {"name", "pro_com", "crs_authid"} — or None if the
    code isn't found.
    """
    layer = QgsVectorLayer(
        _com_shapefile_path(zip_path, stamp), "comuni_istat", "ogr"
    )
    if not layer.isValid():
        raise RuntimeError(
            "Impossibile aprire l'archivio ISTAT / Could not open the "
            "ISTAT boundaries archive."
        )

    request = QgsFeatureRequest().setFilterExpression(
        f'"PRO_COM" = {int(pro_com)}'
    )
    for feat in layer.getFeatures(request):
        attrs = {
            "name": feat["COMUNE"],
            "pro_com": feat["PRO_COM"],
            "crs_authid": layer.crs().authid(),
        }
        return QgsGeometry(feat.geometry()), attrs

    return None


def create_boundary_layer(qgeometry, attrs, layer_name):
    """
    Create an in-memory QgsVectorLayer from a single municipality
    boundary (as returned by fetch_comune_boundary()), style it with a
    green border / transparent fill, and add it to the current project.

    Returns
    -------
    QgsVectorLayer
    """
    if qgeometry is None or qgeometry.isEmpty():
        raise ValueError("No boundary geometry to load.")

    attrs = attrs or {}
    crs_authid = attrs.get("crs_authid") or "EPSG:32632"
    wkb_type = "MultiPolygon" if qgeometry.isMultipart() else "Polygon"

    vl = QgsVectorLayer(
        f"{wkb_type}?crs={crs_authid}&field=comune:string(100)"
        "&field=pro_com:integer",
        layer_name,
        "memory",
    )
    if not vl.isValid():
        raise RuntimeError("Failed to create in-memory vector layer.")

    feat = QgsFeature()
    feat.setGeometry(qgeometry)
    feat.setAttributes(
        [attrs.get("name", ""), int(attrs.get("pro_com") or 0)]
    )
    vl.dataProvider().addFeature(feat)
    vl.updateExtents()

    _apply_boundary_style(vl)
    QgsProject.instance().addMapLayer(vl)
    return vl


def _apply_boundary_style(vl):
    """Apply a green-border, transparent-fill style to the boundary layer."""
    try:
        symbol = QgsFillSymbol.createSimple(
            {
                "color": "0,0,0,0",  # transparent fill
                "outline_color": "#22c55e",
                "outline_width": "1.2",
                "outline_style": "solid",
            }
        )
        vl.setRenderer(QgsSingleSymbolRenderer(symbol))
    except Exception:  # nosec B110 - styling is best-effort, non-critical
        pass
