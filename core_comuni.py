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

Public API:
  search_comuni(query, limit) -> list[dict]
  fetch_comune_boundary(osm_type, osm_id) -> dict | None
  create_boundary_layer(geojson_feature, layer_name) -> QgsVectorLayer
"""

import json
import urllib.parse
import urllib.request

from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
)

_UA = "ProfiliSezioniComuni/1.0 QGIS-plugin (+info@sinocloud.it)"
_NOMINATIM_BASE = "https://nominatim.openstreetmap.org"


# ──────────────────────────────────────────────────────────────────────
# Nominatim helpers
# ──────────────────────────────────────────────────────────────────────


def _nominatim_get(endpoint, params):
    """Perform a Nominatim GET request and return parsed JSON."""
    query = urllib.parse.urlencode(params)
    url = f"{_NOMINATIM_BASE}/{endpoint}?{query}"
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError(f"URL non consentito / URL scheme not allowed: {url}")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "it,en",
        },
    )
    with urllib.request.urlopen(
        req, timeout=20
    ) as resp:  # nosec B310 - schema https validato sopra
        return json.loads(resp.read().decode("utf-8", "replace"))


# ──────────────────────────────────────────────────────────────────────
# search_comuni
# ──────────────────────────────────────────────────────────────────────


def search_comuni(query, limit=10):
    """
    Search Italian municipalities via Nominatim.

    Parameters
    ----------
    query : str
        Name (partial or full) of the municipality.
    limit : int
        Max results (default 10).

    Returns
    -------
    list[dict] with keys:
        name, display_name, osm_id, osm_type, lat, lon, address
    """
    if not query or not query.strip():
        return []

    params = {
        "q": query.strip(),
        "format": "jsonv2",
        "countrycodes": "it",
        "addressdetails": "1",
        "limit": str(limit),
        "featuretype": "settlement",
    }
    try:
        results = _nominatim_get("search", params)
    except Exception as e:
        raise RuntimeError(f"Nominatim search error: {e}") from e

    comuni = []
    for r in results:
        # filter to relevant types
        osm_type = r.get("osm_type", "")
        place_type = r.get("type", "")
        if place_type not in (
            "city",
            "town",
            "village",
            "hamlet",
            "municipality",
            "administrative",
            "suburb",
            "quarter",
        ):
            continue
        addr = r.get("address", {})
        comuni.append(
            {
                "name": r.get(
                    "name", r.get("display_name", "").split(",")[0].strip()
                ),
                "display_name": r.get("display_name", ""),
                "osm_id": str(r.get("osm_id", "")),
                "osm_type": osm_type,
                "lat": float(r.get("lat", 0)),
                "lon": float(r.get("lon", 0)),
                "address": addr,
            }
        )
    return comuni


# ──────────────────────────────────────────────────────────────────────
# fetch_comune_boundary
# ──────────────────────────────────────────────────────────────────────


def fetch_comune_boundary(osm_type, osm_id):
    """
    Fetch the GeoJSON polygon for a municipality from Nominatim.

    Parameters
    ----------
    osm_type : str   e.g. "relation", "way", "node"
    osm_id   : str

    Returns
    -------
    dict (GeoJSON Feature) or None
    """
    if not osm_id:
        return None

    # Map osm_type → Nominatim lookup type character
    type_char = {"relation": "R", "way": "W", "node": "N"}.get(
        (osm_type or "").lower(), "R"
    )

    params = {
        "osm_type": type_char,
        "osm_id": osm_id,
        "format": "geojson",
        "polygon_geojson": "1",
        "addressdetails": "1",
    }
    try:
        data = _nominatim_get("lookup", params)
    except Exception as e:
        raise RuntimeError(f"Nominatim lookup error: {e}") from e

    features = data.get("features") or []
    if not features:
        return None

    # Return first feature with a polygon geometry
    for feat in features:
        geom_type = (feat.get("geometry") or {}).get("type", "")
        if geom_type in ("Polygon", "MultiPolygon"):
            return feat

    return features[0] if features else None


# ──────────────────────────────────────────────────────────────────────
# create_boundary_layer
# ──────────────────────────────────────────────────────────────────────


def create_boundary_layer(geojson_feature, layer_name):
    """
    Create a temporary in-memory QgsVectorLayer from a GeoJSON feature.

    Style: green border (#22c55e), transparent fill.
    Adds the layer to the current QGIS project.

    Returns
    -------
    QgsVectorLayer
    """
    if not geojson_feature:
        raise ValueError("No GeoJSON feature provided.")

    geom_data = geojson_feature.get("geometry")
    if not geom_data:
        raise ValueError("GeoJSON feature has no geometry.")

    geom_type = geom_data.get("type", "")
    if geom_type == "Polygon":
        vl_type = "Polygon?crs=EPSG:4326"
    elif geom_type == "MultiPolygon":
        vl_type = "MultiPolygon?crs=EPSG:4326"
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type!r}")

    vl = QgsVectorLayer(
        f"{vl_type}&field=name:string(255)&field=display_name:string(500)",
        layer_name,
        "memory",
    )
    if not vl.isValid():
        raise RuntimeError("Failed to create in-memory vector layer.")

    pr = vl.dataProvider()
    feat = QgsFeature()

    geom_json = json.dumps(geom_data)
    qgeom = QgsGeometry.fromWkt(_geojson_geom_to_wkt(geom_data))
    if qgeom is None or qgeom.isEmpty():
        # fallback: use fromGeoJson
        try:
            qgeom = QgsGeometry.fromWkt(_geojson_to_wkt_fallback(geom_json))
        except Exception:  # nosec B110
            pass

    # Most reliable: parse via QGIS WKT
    # If both fail, try direct fromWkt approach with GeoJSON coords
    if qgeom is None or qgeom.isEmpty():
        raise RuntimeError("Could not parse boundary geometry.")

    feat.setGeometry(qgeom)
    props = geojson_feature.get("properties") or {}
    name = props.get("name") or layer_name
    display_name = props.get("display_name") or ""
    feat.setAttributes([name, display_name])
    pr.addFeature(feat)
    vl.updateExtents()

    # Apply style
    _apply_boundary_style(vl)

    QgsProject.instance().addMapLayer(vl)
    return vl


def _geojson_geom_to_wkt(geom_data):
    """Convert a GeoJSON geometry dict to WKT string."""
    geom_type = geom_data.get("type", "")
    coords = geom_data.get("coordinates")

    if geom_type == "Polygon":
        return "POLYGON(" + _rings_to_wkt(coords) + ")"
    elif geom_type == "MultiPolygon":
        parts = []
        for polygon in coords:
            parts.append("(" + _rings_to_wkt(polygon) + ")")
        return "MULTIPOLYGON(" + ",".join(parts) + ")"
    return ""


def _rings_to_wkt(rings):
    """Convert a list of rings (each a list of [lon, lat]) to WKT ring
    string."""
    ring_strs = []
    for ring in rings:
        pts = ",".join(f"{pt[0]:.7f} {pt[1]:.7f}" for pt in ring)
        ring_strs.append(f"({pts})")
    return ",".join(ring_strs)


def _geojson_to_wkt_fallback(geom_json_str):
    """Fallback: use QgsGeometry.fromWkt after building WKT from JSON."""
    data = json.loads(geom_json_str)
    return _geojson_geom_to_wkt(data)


def _apply_boundary_style(vl):
    """Apply a green-border, transparent-fill style to the boundary layer."""
    try:
        from qgis.core import QgsSingleSymbolRenderer, QgsFillSymbol

        symbol = QgsFillSymbol.createSimple(
            {
                "color": "0,0,0,0",  # transparent fill
                "outline_color": "#22c55e",
                "outline_width": "1.2",
                "outline_style": "solid",
            }
        )
        vl.setRenderer(QgsSingleSymbolRenderer(symbol))
    except Exception:  # nosec B110
        pass
