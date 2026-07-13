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
core_elevation.py — Elevation profile logic.

Supports three sources:
  "provider_openelev"  → Open-Elevation API (SRTM, chunked 80 pts)
  "provider_opentopo"  → OpenTopoData API (srtm90m dataset)
  "raster"             → local QgsRasterLayer (DEM/DTM)

Public API:
  calculate_profile(line_points, source, raster_layer, sample_count, band) ->
  (points_data, total_dist)
  build_pickets(points, total_m) -> list
  generate_profile_svg(points, total_m, source, raster_name, labels) -> str
  generate_profile_html(points, total_m, source, raster_name) -> str
  export_profile_csv(points, filepath)
"""

import math
import json
import urllib.parse
import urllib.request
import datetime
import html as _html
import csv

from qgis.core import (
    QgsDistanceArea,
    QgsProject,
    QgsRaster,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
)

# ──────────────────────────────────────────────────────────────────────
# Internal fetch helpers
# ──────────────────────────────────────────────────────────────────────

_UA = "ProfiliSezioniComuni/1.0 (+info@sinocloud.it)"


def _open_https(url, timeout):
    """urlopen limited to https URLs — blocks file://, ftp:// and custom
    schemes."""
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError(f"URL non consentito / URL scheme not allowed: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(
        req, timeout=timeout
    )  # nosec B310 - schema https validato sopra


def _fetch_open_elevation(points_wgs84):
    """Open-Elevation API — max 80 pts per chunk."""
    values = []
    for start in range(0, len(points_wgs84), 80):
        chunk = points_wgs84[start : start + 80]
        locs = "|".join(f"{p['lat']:.7f},{p['lon']:.7f}" for p in chunk)
        query = urllib.parse.urlencode({"locations": locs})
        url = f"https://api.open-elevation.com/api/v1/lookup?{query}"
        try:
            with _open_https(url, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            results = payload.get("results") or []
            if len(results) != len(chunk):
                raise RuntimeError(
                    "Incomplete response from Open-Elevation API"
                )
            for r in results:
                elev = r.get("elevation")
                values.append(float(elev) if elev is not None else None)
        except Exception as e:
            raise RuntimeError(f"Open-Elevation error: {e}") from e
    return values


def _fetch_opentopo(points_wgs84):
    """OpenTopoData API (srtm90m) — max 100 pts per chunk."""
    values = []
    chunk_size = 100
    for start in range(0, len(points_wgs84), chunk_size):
        chunk = points_wgs84[start : start + chunk_size]
        locs = "|".join(f"{p['lat']:.7f},{p['lon']:.7f}" for p in chunk)
        query = urllib.parse.urlencode({"locations": locs})
        url = f"https://api.opentopodata.org/v1/srtm90m?{query}"
        try:
            with _open_https(url, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            if payload.get("status") != "OK":
                raise RuntimeError(
                    f"OpenTopoData status: {payload.get('status')}"
                )
            results = payload.get("results") or []
            if len(results) != len(chunk):
                raise RuntimeError("Incomplete response from OpenTopoData API")
            for r in results:
                elev = r.get("elevation")
                values.append(float(elev) if elev is not None else None)
        except Exception as e:
            raise RuntimeError(f"OpenTopoData error: {e}") from e
    return values


# ──────────────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────────────


def _build_samples(line_points, sample_count):
    """Build evenly-spaced sample list along the polyline."""
    da = QgsDistanceArea()
    da.setSourceCrs(
        QgsProject.instance().crs(), QgsProject.instance().transformContext()
    )

    total_dist = 0.0
    segments = []
    for i in range(len(line_points) - 1):
        p1, p2 = line_points[i], line_points[i + 1]
        dist = da.measureLine(p1, p2)
        segments.append(
            {
                "p1": p1,
                "p2": p2,
                "dist": dist,
                "cum_start": total_dist,
                "cum_end": total_dist + dist,
            }
        )
        total_dist += dist

    if total_dist <= 0:
        raise ValueError("The drawn line has zero length.")

    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    try:
        xform = QgsCoordinateTransform(
            QgsProject.instance().crs(),
            crs_wgs84,
            QgsProject.instance().transformContext(),
        )
    except Exception:
        xform = QgsCoordinateTransform(
            QgsProject.instance().crs(), crs_wgs84, QgsProject.instance()
        )

    step = total_dist / max(1, sample_count - 1)
    samples = []
    for i in range(sample_count):
        target = i * step
        if i == sample_count - 1:
            target = total_dist
        for seg in segments:
            if seg["cum_start"] <= target <= seg["cum_end"] or math.isclose(
                target, seg["cum_end"]
            ):
                seg_len = seg["dist"]
                ratio = (
                    (target - seg["cum_start"]) / seg_len
                    if seg_len > 0
                    else 0.0
                )
                x = seg["p1"].x() + ratio * (seg["p2"].x() - seg["p1"].x())
                y = seg["p1"].y() + ratio * (seg["p2"].y() - seg["p1"].y())
                pt_wgs = xform.transform(QgsPointXY(x, y))
                samples.append(
                    {
                        "x": x,
                        "y": y,
                        "lon": pt_wgs.x(),
                        "lat": pt_wgs.y(),
                        "distance_m": target,
                    }
                )
                break
    return samples, total_dist


def _raster_nodata_values(provider, band):
    values = set()
    for method_name in ("sourceNoDataValue", "srcNoDataValue"):
        method = getattr(provider, method_name, None)
        if method is None:
            continue
        try:
            raw = method(band)
            if raw is not None:
                values.add(float(raw))
        except Exception:
            pass
    return values


def _clean_raster_value(raw, nodata_values):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    for nodata in nodata_values:
        if math.isclose(value, nodata, rel_tol=0.0, abs_tol=1e-9):
            return None
    return value


# ──────────────────────────────────────────────────────────────────────
# Public: calculate_profile
# ──────────────────────────────────────────────────────────────────────


def calculate_profile(
    line_points, source, raster_layer=None, sample_count=100, band=1
):
    """
    Calculate elevation profile.

    Parameters
    ----------
    line_points : list[QgsPointXY]
    source : "provider_openelev" | "provider_opentopo" | "raster"
    raster_layer : QgsRasterLayer | None
    sample_count : int
    band : int

    Returns
    -------
    (points_data, total_dist)
    """
    if len(line_points) < 2:
        raise ValueError("At least two vertices are required for the profile.")

    samples, total_dist = _build_samples(line_points, sample_count)

    points_data = []

    if source in ("provider_openelev", "provider_opentopo"):
        fetch_fn = (
            _fetch_open_elevation
            if source == "provider_openelev"
            else _fetch_opentopo
        )
        elevations = fetch_fn(samples)
        for p, val in zip(samples, elevations):
            points_data.append(
                {
                    "x": p["x"],
                    "y": p["y"],
                    "lon": p["lon"],
                    "lat": p["lat"],
                    "distance_m": p["distance_m"],
                    "elevation": val,
                }
            )
    elif source == "raster":
        if not raster_layer:
            raise ValueError("No raster layer selected.")
        dp = raster_layer.dataProvider()
        project = QgsProject.instance()
        project_crs = project.crs()
        raster_crs = raster_layer.crs()
        xform_to_raster = None
        if (
            project_crs.isValid()
            and raster_crs.isValid()
            and project_crs != raster_crs
        ):
            try:
                xform_to_raster = QgsCoordinateTransform(
                    project_crs,
                    raster_crs,
                    project.transformContext(),
                )
            except Exception:
                xform_to_raster = QgsCoordinateTransform(
                    project_crs, raster_crs, project
                )
        raster_extent = raster_layer.extent()
        nodata_values = _raster_nodata_values(dp, band)
        for p in samples:
            pt_project = QgsPointXY(p["x"], p["y"])
            pt_raster = (
                xform_to_raster.transform(pt_project)
                if xform_to_raster
                else pt_project
            )
            result = None
            if raster_extent.contains(pt_raster):
                result = dp.identify(pt_raster, QgsRaster.IdentifyFormatValue)
            val = None
            if result and result.isValid():
                raw = result.results().get(band)
                val = _clean_raster_value(raw, nodata_values)
            points_data.append(
                {
                    "x": p["x"],
                    "y": p["y"],
                    "lon": p["lon"],
                    "lat": p["lat"],
                    "raster_x": pt_raster.x(),
                    "raster_y": pt_raster.y(),
                    "distance_m": p["distance_m"],
                    "elevation": val,
                }
            )
        if not any(p.get("elevation") is not None for p in points_data):
            raise RuntimeError(
                "Il raster selezionato non ha restituito quote valide lungo "
                "il tracciato. "
                "Controlla che il tracciato cada dentro l'estensione del DEM, "
                "che il DEM abbia "
                "un CRS valido e che la banda selezionata contenga quote. "
                "Project CRS: {0}; Raster CRS: {1}; Raster extent: {2}".format(
                    project_crs.authid() or project_crs.description(),
                    raster_crs.authid() or raster_crs.description(),
                    raster_extent.toString(),
                )
            )
    else:
        raise ValueError(f"Unknown source: {source!r}")

    return points_data, total_dist


# ──────────────────────────────────────────────────────────────────────
# Pickets helpers
# ──────────────────────────────────────────────────────────────────────


def _nice_step(max_value, target=10):
    max_value = float(max_value or 0)
    if max_value <= 0:
        return 1.0
    raw = max_value / target
    if raw <= 0:
        return 1.0
    mag = math.floor(math.log10(raw))
    mag_pow = 10**mag
    norm = raw / mag_pow
    if norm < 1.5:
        nice = 1.0
    elif norm < 3:
        nice = 2.0
    elif norm < 7:
        nice = 5.0
    else:
        nice = 10.0
    return nice * mag_pow


def _elevation_at(points, distance_m):
    if not points:
        return None
    d = float(distance_m or 0)
    valid = [p for p in points if p.get("elevation") is not None]
    if not valid:
        return None
    if d <= float(valid[0].get("distance_m") or 0):
        return valid[0]["elevation"]
    for a, b in zip(valid[:-1], valid[1:]):
        da = float(a.get("distance_m") or 0)
        db = float(b.get("distance_m") or 0)
        if da <= d <= db:
            if db <= da:
                return a["elevation"]
            f = (d - da) / (db - da)
            return (
                float(a["elevation"])
                + (float(b["elevation"]) - float(a["elevation"])) * f
            )
    return valid[-1]["elevation"]


def _point_at_distance(points, distance_m):
    if not points:
        return None
    d = float(distance_m or 0)
    if d <= float(points[0].get("distance_m") or 0):
        return points[0]
    for a, b in zip(points[:-1], points[1:]):
        da = float(a.get("distance_m") or 0)
        db = float(b.get("distance_m") or 0)
        if da <= d <= db:
            if db <= da:
                return a
            f = (d - da) / (db - da)
            return {
                "lon": float(a["lon"])
                + (float(b["lon"]) - float(a["lon"])) * f,
                "lat": float(a["lat"])
                + (float(b["lat"]) - float(a["lat"])) * f,
                "distance_m": d,
                "elevation": _elevation_at(points, d),
            }
    return points[-1]


def build_pickets(points, total_m):
    """Build a list of picket dicts at nice distances along the profile."""
    total_m = float(total_m or 0)
    if total_m <= 0 or not points:
        return []
    step = _nice_step(total_m, 10)
    distances = [0.0]
    d = step
    while d < total_m and len(distances) < 80:
        distances.append(float(d))
        d += step
    if not distances or abs(distances[-1] - total_m) > max(0.01, step * 0.2):
        distances.append(total_m)
    pickets = []
    for idx, dist in enumerate(distances):
        p = _point_at_distance(points, dist)
        if not p:
            continue
        pickets.append(
            {
                "id": f"P{idx:02d}",
                "progressive_m": float(dist),
                "partial_m": (
                    float(dist - distances[idx - 1]) if idx > 0 else 0.0
                ),
                "lon": float(p.get("lon", 0)),
                "lat": float(p.get("lat", 0)),
                "elevation": p.get("elevation"),
            }
        )
    return pickets


# ──────────────────────────────────────────────────────────────────────
# SVG generator
# ──────────────────────────────────────────────────────────────────────


def generate_profile_svg(
    points, total_m, source="raster", raster_name="", labels=None
):
    if not points:
        return "<svg><text x='10' y='20'>No data.</text></svg>"

    valid = [p for p in points if p.get("elevation") is not None]
    pickets = build_pickets(points, total_m)
    labels = labels or {}
    lbl_peg = labels.get("peg", "Picket")
    lbl_progressive = labels.get("progressive", "Progressive m")
    lbl_ground = labels.get("ground", "Elevation m")

    width, height = 1600, 1040
    chart_l, chart_t, chart_r, chart_b = 95, 105, 1505, 575
    table_t = 650

    min_z = float(min((p["elevation"] for p in valid), default=0.0))
    max_z = float(max((p["elevation"] for p in valid), default=1.0))
    z_pad = max((max_z - min_z) * 0.08, 1.0)
    y_min = min_z - z_pad
    y_max = max_z + z_pad
    z_span = max(y_max - y_min, 1.0)
    d_span = max(float(total_m), 1.0)

    def esc(v):
        return _html.escape("" if v is None else str(v), quote=True)

    def x_of(d):
        return chart_l + (float(d or 0) / d_span) * (chart_r - chart_l)

    def y_of(z):
        return chart_b - ((float(z) - y_min) / z_span) * (chart_b - chart_t)

    path_pts = [
        (x_of(p.get("distance_m")), y_of(p.get("elevation"))) for p in valid
    ]
    line_d = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
        for i, (x, y) in enumerate(path_pts)
    )
    fill_d = ""
    if path_pts:
        fill_d = (
            f"{line_d} L{path_pts[-1][0]:.1f},{chart_b} "
            f"L{path_pts[0][0]:.1f},{chart_b} Z"
        )

    y_ticks = []
    for i in range(6):
        t = i / 5
        z = y_min + z_span * t
        y = y_of(z)
        y_ticks.append(
            f'<line x1="{chart_l}" y1="{y:.1f}" x2="{chart_r}" y2="{y:.1f}" '
            f'class="grid"/>'
            f'<text x="28" y="{y + 4:.1f}" class="axis">{z:.1f} m</text>'
        )

    x_ticks = []
    x_step = _nice_step(total_m, 8)
    d = 0.0
    while d <= float(total_m) + 0.01 and len(x_ticks) < 24:
        x = x_of(d)
        label = f"{d / 1000:.2f} km" if total_m >= 1000 else f"{d:.0f} m"
        x_ticks.append(
            f'<line x1="{x:.1f}" y1="{chart_t}" x2="{x:.1f}" y2="{chart_b}" '
            f'class="grid"/>'
            f'<text x="{x:.1f}" y="{chart_b + 28}" class="axis '
            f'center">{esc(label)}</text>'
        )
        d += x_step

    picket_lines = []
    for p in pickets:
        x = x_of(p.get("progressive_m"))
        z = p.get("elevation")
        y = y_of(z) if z is not None else chart_b
        picket_lines.append(
            f'<line x1="{x:.1f}" y1="{chart_b}" x2="{x:.1f}" y2="{y:.1f}" '
            f'class="picket-line"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" class="picket-dot"/>'
            f'<text x="{x:.1f}" y="{chart_b + 48}" '
            f'class="picket-label">{esc(p.get("id"))}</text>'
        )

    table_rows = []
    usable = pickets[:14]
    row_h = 26
    for i, p in enumerate(usable):
        y = table_t + 50 + i * row_h
        bg = "#1e2437" if i % 2 == 0 else "#12151e"
        elev = p.get("elevation")
        elev_text = "" if elev is None else f"{float(elev):.2f}"
        table_rows.append(
            f'<rect x="70" y="{y - 18}" width="1460" height="{row_h}" '
            f'fill="{bg}"/>'
            f'<text x="92" y="{y}" class="cell bold">{esc(p.get("id"))}</text>'
            f'<text x="230" y="{y}" '
            f'class="cell">{p.get("progressive_m", 0):.2f}</text>'
            f'<text x="610" y="{y}" '
            f'class="cell">{p.get("partial_m", 0):.2f}</text>'
            f'<text x="790" y="{y}" class="cell">{elev_text}</text>'
            f'<text x="1010" y="{y}" class="cell">{p.get("lon", 0):.7f}</text>'
            f'<text x="1260" y="{y}" class="cell">{p.get("lat", 0):.7f}</text>'
        )
    if len(pickets) > len(usable):
        y = table_t + 50 + len(usable) * row_h
        table_rows.append(
            f'<text x="92" y="{y}" class="cell muted">'
            f"Other {len(pickets) - len(usable)} pickets exportable via "
            f"CSV.</text>"
        )

    dist_label = (
        f"{total_m / 1000:.3f} km" if total_m >= 1000 else f"{total_m:.1f} m"
    )
    project_title = (
        QgsProject.instance().title()
        or QgsProject.instance().baseName()
        or "Project"
    )
    created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    source_labels = {
        "provider_openelev": "Open-Elevation API (SRTM/NASA)",
        "provider_opentopo": "OpenTopoData API (SRTM 90m)",
        "raster": raster_name or "DEM/DTM",
    }
    provider_name = source_labels.get(source, raster_name or "DEM/DTM")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<defs>
  <linearGradient id="profileFill" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0%" stop-color="#4f73c4" stop-opacity="0.34"/>
    <stop offset="100%" stop-color="#4f73c4" stop-opacity="0.06"/>
  </linearGradient>
  <style>
    .title {{ font: 700 28px Arial, sans-serif; fill:#f1f5f9; }}
    .subtitle {{ font: 14px Arial, sans-serif; fill:#8ba3c7; }}
    .axis {{ font: 12px Arial, sans-serif; fill:#566584; }}
    .center {{ text-anchor:middle; }}
    .grid {{ stroke:#2d3757; stroke-width:1; }}
    .frame {{ fill:#12151e; stroke:#2d3757; stroke-width:1.5; }}
    .profile {{ fill:none; stroke:#4f73c4; stroke-width:4; stroke-linejoin:round; stroke-linecap:round; }}
    .profile-fill {{ fill:url(#profileFill); }}
    .picket-line {{ stroke:#2d3757; stroke-width:1; stroke-dasharray:5 7; opacity:.7; }}
    .picket-dot {{ fill:#ffffff; stroke:#4f73c4; stroke-width:2.5; }}
    .picket-label {{ font: 700 11px Arial, sans-serif; fill:#8ba3c7; text-anchor:middle; }}
    .thead {{ font: 700 13px Arial, sans-serif; fill:#8ba3c7; }}
    .cell {{ font: 12px Arial, sans-serif; fill:#e2e8f0; }}
    .bold {{ font-weight:700; }}
    .muted {{ fill:#566584; }}
    .footer {{ font: 11px Arial, sans-serif; fill:#566584; }}
  </style>
</defs>
<rect width="100%" height="100%" fill="#12151e"/>
<text x="70" y="48" class="title">Elevation Profile / Profilo Altimetrico</text>
<text x="70" y="75" class="subtitle">Project: {esc(project_title)} · Distance: {esc(dist_label)} · Min {min_z:.2f} m · Max {max_z:.2f} m · {created_at}</text>
<rect x="{chart_l}" y="{chart_t}" width="{chart_r - chart_l}" height="{chart_b - chart_t}" rx="4" class="frame"/>
{''.join(y_ticks)}
{''.join(x_ticks)}
{f'<path d="{fill_d}" class="profile-fill"/>' if fill_d else ''}
{f'<path d="{line_d}" class="profile"/>' if line_d else ''}
{''.join(picket_lines)}
<text x="70" y="625" class="title" style="font-size:20px">Pickets /
Fincatura Picchetti</text>
<rect x="70" y="{table_t}" width="1460" height="34" fill="#1e2437"/>
<text x="92" y="{table_t + 22}" class="thead">{esc(lbl_peg)}</text>
<text x="230" y="{table_t + 22}" class="thead">{esc(lbl_progressive)}</text>
<text x="610" y="{table_t + 22}" class="thead">Partial m</text>
<text x="790" y="{table_t + 22}" class="thead">{esc(lbl_ground)}</text>
<text x="1010" y="{table_t + 22}" class="thead">Longitude</text>
<text x="1260" y="{table_t + 22}" class="thead">Latitude</text>
{''.join(table_rows)}
<text x="70" y="992" class="footer">Source: {esc(provider_name)}</text>
</svg>
"""


# ──────────────────────────────────────────────────────────────────────
# Interactive HTML with Chart.js
# ──────────────────────────────────────────────────────────────────────


def generate_profile_html(points, total_m, source="raster", raster_name=""):
    """Return a standalone HTML page with an interactive Chart.js elevation
    chart."""
    valid = [p for p in points if p.get("elevation") is not None]
    pickets = build_pickets(points, total_m)

    distances = [round(p["distance_m"], 2) for p in valid]
    elevations = [round(p["elevation"], 2) for p in valid]

    dist_label = (
        f"{total_m / 1000:.3f} km" if total_m >= 1000 else f"{total_m:.1f} m"
    )
    min_z = min(elevations, default=0)
    max_z = max(elevations, default=0)

    source_labels = {
        "provider_openelev": "Open-Elevation API (SRTM/NASA)",
        "provider_opentopo": "OpenTopoData API (SRTM 90m)",
        "raster": raster_name or "DEM/DTM",
    }
    src_label = source_labels.get(source, raster_name or "DEM/DTM")
    project_title = (
        QgsProject.instance().title()
        or QgsProject.instance().baseName()
        or "Project"
    )
    created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    dist_json = json.dumps(distances)
    elev_json = json.dumps(elevations)

    pickets_rows = ""
    for p in pickets:
        elev = p.get("elevation")
        elev_str = f"{float(elev):.2f}" if elev is not None else "—"
        pickets_rows += (
            f"<tr><td><b>{_html.escape(str(p.get('id', '')))}</b></td>"
            f"<td>{p.get('progressive_m', 0):.2f}</td>"
            f"<td>{p.get('partial_m', 0):.2f}</td>"
            f"<td>{elev_str}</td>"
            f"<td>{p.get('lon', 0):.7f}</td>"
            f"<td>{p.get('lat', 0):.7f}</td></tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"/>
<title>Elevation Profile</title>
<script
src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js">
</script>
<style>
  body {{ margin:0; padding:16px; background:#12151e; color:#e2e8f0;
         font-family:'Segoe UI',Arial,sans-serif; font-size:13px; }}
  h2 {{ color:#f1f5f9; margin:0 0 4px; }}
  .meta {{ color:#8ba3c7; font-size:12px; margin-bottom:16px; }}
  .chart-wrap {{ background:#0e1118; border:1px solid #2d3757;
                 border-radius:8px;
                 padding:16px; margin-bottom:16px; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; }}
  th {{ background:#1e2437; color:#8ba3c7; padding:7px 10px; text-align:left;
        border-bottom:2px solid #2d3757; font-size:11px;
        text-transform:uppercase; }}
  td {{ padding:5px 10px; border-bottom:1px solid #2d3757; color:#e2e8f0; }}
  tr:nth-child(even) td {{ background:rgba(30,36,55,0.5); }}
  tr:hover td {{ background:rgba(79,115,196,0.12); }}
</style>
</head>
<body>
<h2>Elevation Profile / Profilo Altimetrico</h2>
<div class="meta">
  Project: <b>{_html.escape(project_title)}</b> &nbsp;·&nbsp;
  Distance: <b>{_html.escape(dist_label)}</b> &nbsp;·&nbsp;
  Min: <b>{min_z:.2f} m</b> &nbsp;·&nbsp;
  Max: <b>{max_z:.2f} m</b> &nbsp;·&nbsp;
  Samples: <b>{len(points)}</b> &nbsp;·&nbsp;
  Source: <b>{_html.escape(src_label)}</b> &nbsp;·&nbsp;
  {created_at}
</div>
<div class="chart-wrap">
  <canvas id="profileChart" height="220"></canvas>
</div>
<script>
new Chart(document.getElementById('profileChart'), {{
  type: 'line',
  data: {{
    labels: {dist_json},
    datasets: [{{
      label: 'Elevation (m)',
      data: {elev_json},
      borderColor: '#4f73c4',
      backgroundColor: 'rgba(79,115,196,0.18)',
      borderWidth: 2.5,
      pointRadius: 0,
      fill: true,
      tension: 0.3,
    }}]
  }},
  options: {{
    responsive: true,
    animation: false,
    plugins: {{ legend: {{ labels: {{ color: '#8ba3c7' }} }} }},
    scales: {{
      x: {{ ticks: {{ color:'#8ba3c7', maxTicksLimit:12 }},
             grid: {{ color:'#2d3757' }},
             title: {{ display:true, text:'Distance (m)',
                      color:'#8ba3c7' }} }},
      y: {{ ticks: {{ color:'#8ba3c7' }},
             grid: {{ color:'#2d3757' }},
             title: {{ display:true, text:'Elevation (m)',
                      color:'#8ba3c7' }} }},
    }}
  }}
}});
</script>
<h3 style="color:#f1f5f9;margin:16px 0 8px;">Pickets / Picchetti</h3>
<table>
  <thead>
    <tr><th>ID</th><th>Progressive m</th><th>Partial m</th>
        <th>Elevation m</th><th>Longitude</th><th>Latitude</th></tr>
  </thead>
  <tbody>
    {pickets_rows}
  </tbody>
</table>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────
# CSV export
# ──────────────────────────────────────────────────────────────────────


def export_profile_csv(points, filepath):
    """Write profile points to a semicolon-delimited CSV file."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Distance_m", "Elevation_m", "Lon", "Lat"])
        for p in points:
            writer.writerow(
                [
                    f"{p['distance_m']:.2f}",
                    (
                        f"{p['elevation']:.2f}"
                        if p.get("elevation") is not None
                        else ""
                    ),
                    f"{p.get('lon', p.get('x', 0)):.7f}",
                    f"{p.get('lat', p.get('y', 0)):.7f}",
                ]
            )


# ──────────────────────────────────────────────────────────────────────
# Full results HTML (for in-dialog display)
# ──────────────────────────────────────────────────────────────────────


def generate_profile_results_html(
    points,
    total_m,
    source,
    raster_name,
    svg_content,
    labels=None,
    chart_html=None,
):
    """Build complete HTML with embedded chart + pickets table for the Results
    tab."""
    valid = [p for p in points if p.get("elevation") is not None]
    pickets = build_pickets(points, total_m)
    labels = labels or {}
    lbl_peg = _html.escape(labels.get("peg", "Picket"))
    lbl_progressive = _html.escape(labels.get("progressive", "Progressive"))
    lbl_ground = _html.escape(labels.get("ground", "Elevation"))

    min_z = min((p["elevation"] for p in valid), default=0)
    max_z = max((p["elevation"] for p in valid), default=0)
    dist_label = (
        f"{total_m / 1000:.3f} km" if total_m >= 1000 else f"{total_m:.1f} m"
    )
    project_title = (
        QgsProject.instance().title()
        or QgsProject.instance().baseName()
        or "Project"
    )

    source_labels = {
        "provider_openelev": "Open-Elevation API (SRTM/NASA)",
        "provider_opentopo": "OpenTopoData API (SRTM 90m)",
        "raster": raster_name or "DEM/DTM",
    }
    src_label = source_labels.get(source, raster_name or "DEM/DTM")
    created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    html = f"""
    <div class="summary-card">
        <strong>Project:</strong> {_html.escape(project_title)} &nbsp;·&nbsp;
        <strong>Distance:</strong> {_html.escape(dist_label)} &nbsp;·&nbsp;
        <strong>Min:</strong> {min_z:.2f} m &nbsp;·&nbsp;
        <strong>Max:</strong> {max_z:.2f} m &nbsp;·&nbsp;
        <strong>Samples:</strong> {len(points)} &nbsp;·&nbsp;
        <strong>Pickets:</strong> {len(pickets)}<br>
        <strong>Source:</strong> {_html.escape(src_label)} &nbsp;·&nbsp;
        <span style="color:#4a8090;">{created_at}</span>
    </div>

    <div class="section-title">Elevation Chart / Profilo Altimetrico</div>
    <div class="chart-container">
        {chart_html or svg_content}
    </div>

    <div class="section-title">Pickets Table / Fincatura Picchetti</div>
    <table>
        <thead>
            <tr>
                <th>{lbl_peg}</th><th>{lbl_progressive} (m)</th>
                <th>Partial (m)</th>
                <th>{lbl_ground} (m)</th><th>Longitude</th><th>Latitude</th>
            </tr>
        </thead>
        <tbody>
    """
    for p in pickets:
        elev = p.get("elevation")
        elev_str = f"{float(elev):.2f}" if elev is not None else "—"
        picket_id = _html.escape(str(p.get("id", "")))
        html += (
            f"<tr><td style='font-weight:700;'>{picket_id}</td>"
            f"<td>{p.get('progressive_m', 0):.2f}</td>"
            f"<td>{p.get('partial_m', 0):.2f}</td>"
            f"<td>{elev_str}</td>"
            f"<td>{p.get('lon', 0):.7f}</td>"
            f"<td>{p.get('lat', 0):.7f}</td></tr>\n"
        )
    html += "</tbody></table>"

    html += """
    <div class="section-title">All Samples / Tutti i Campioni</div>
    <table>
        <thead>
            <tr><th>#</th><th>Distance (m)</th><th>Elevation (m)</th>
                <th>Lon / X</th><th>Lat / Y</th></tr>
        </thead>
        <tbody>
    """
    for i, p in enumerate(points):
        elev = p.get("elevation")
        elev_str = f"{float(elev):.2f}" if elev is not None else "—"
        html += (
            f"<tr><td>{i + 1}</td><td>{p['distance_m']:.2f}</td>"
            f"<td>{elev_str}</td>"
            f"<td>{p.get('lon', p.get('x', 0)):.7f}</td>"
            f"<td>{p.get('lat', p.get('y', 0)):.7f}</td></tr>\n"
        )
    html += "</tbody></table>"
    return html
