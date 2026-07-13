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
core_sections.py — Cross-sections and earthwork volume logic.

Public API:
  calculate_cross_sections(points, raster_layer, interval_m, half_width_m,
                           samples, design_start, design_end, design_grade,
                           smooth) -> dict
  generate_cross_sections_svg(data, project_title, created_at) -> str
  generate_cross_sections_html(data) -> str
  generate_cross_sections_results_html(data) -> str
"""

import math
import datetime
import html as _html

from qgis.core import (
    QgsProject,
    QgsRaster,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
)

# ──────────────────────────────────────────────────────────────────────
# Cross-sections calculation
# ──────────────────────────────────────────────────────────────────────


def calculate_cross_sections(
    line_points,
    raster_layer,
    interval_m,
    half_width_m,
    samples_per_section,
    design_start_elev,
    design_end_elev,
    design_grade_pct,
    smooth_tangent,
    band=1,
):
    """
    Calculate cross-sections along a polyline, sampling elevations from a
    local raster.

    Returns a dict with keys:
      alignment_length_m, n_sections, interval_m, half_width_m,
      ref_elevation, total_cut_m3, total_fill_m3,
      sections, volumes, road_metrics, curve_points
    """
    try:
        from pyproj import Geod
    except ImportError:
        raise ImportError("pyproj is required for cross-section calculations.")

    geod = Geod(ellps="WGS84")

    crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    try:
        xform_to_wgs = QgsCoordinateTransform(
            QgsProject.instance().crs(),
            crs_wgs,
            QgsProject.instance().transformContext(),
        )
        xform_from_wgs = QgsCoordinateTransform(
            crs_wgs,
            raster_layer.crs(),
            QgsProject.instance().transformContext(),
        )
    except Exception:
        xform_to_wgs = QgsCoordinateTransform(
            QgsProject.instance().crs(), crs_wgs, QgsProject.instance()
        )
        xform_from_wgs = QgsCoordinateTransform(
            crs_wgs, raster_layer.crs(), QgsProject.instance()
        )

    pts_wgs = []
    for p in line_points:
        pt_wgs = xform_to_wgs.transform(QgsPointXY(p.x(), p.y()))
        pts_wgs.append((pt_wgs.x(), pt_wgs.y()))

    segs = []
    total_len = 0.0
    for a, b in zip(pts_wgs[:-1], pts_wgs[1:]):
        _az1, _az2, dist = geod.inv(a[0], a[1], b[0], b[1])
        if dist > 0:
            segs.append(
                {
                    "a": a,
                    "b": b,
                    "d0": total_len,
                    "d1": total_len + dist,
                    "az": _az1,
                }
            )
            total_len += dist

    if not segs:
        raise ValueError("Invalid axis or zero-length line.")

    # Design grade logic
    design_start = design_start_elev
    design_end = design_end_elev
    design_grade = design_grade_pct
    if (
        design_start is not None
        and design_end is None
        and design_grade is not None
    ):
        design_end = design_start + (design_grade / 100.0) * total_len
    elif (
        None not in (design_start, design_end)
        and design_grade is None
        and total_len > 0
    ):
        design_grade = ((design_end - design_start) / total_len) * 100.0

    def _design_elevation_at(progressive_m):
        if design_start is not None and design_end is not None:
            t = max(0.0, min(1.0, float(progressive_m or 0.0) / total_len))
            return design_start + (design_end - design_start) * t
        if design_start is not None:
            return design_start
        return None

    def _point_at(dist_m):
        si = 0
        for i, s in enumerate(segs):
            if dist_m <= s["d1"] or i == len(segs) - 1:
                si = i
                break
        s = segs[si]
        frac = (
            0.0
            if s["d1"] <= s["d0"]
            else (dist_m - s["d0"]) / (s["d1"] - s["d0"])
        )
        lon = s["a"][0] + (s["b"][0] - s["a"][0]) * frac
        lat = s["a"][1] + (s["b"][1] - s["a"][1]) * frac
        az = float(s["az"])
        if smooth_tangent and len(segs) > 1:
            seg_len = s["d1"] - s["d0"]
            if seg_len > 0:
                local_frac = (dist_m - s["d0"]) / seg_len
                if local_frac > 0.8 and si < len(segs) - 1:
                    next_az = float(segs[si + 1]["az"])
                    diff = (next_az - az + 360) % 360
                    if diff > 180:
                        diff -= 360
                    t = (local_frac - 0.8) / 0.2
                    az = (az + t * diff) % 360
                elif local_frac < 0.2 and si > 0:
                    prev_az = float(segs[si - 1]["az"])
                    diff = (az - prev_az + 360) % 360
                    if diff > 180:
                        diff -= 360
                    t = local_frac / 0.2
                    az = (prev_az + t * diff) % 360
        return lon, lat, az

    dp = raster_layer.dataProvider()
    nodata_values = set()
    for method_name in ("sourceNoDataValue", "srcNoDataValue"):
        method = getattr(dp, method_name, None)
        if method is None:
            continue
        try:
            raw_nodata = method(band)
            if raw_nodata is not None:
                nodata_values.add(float(raw_nodata))
        except Exception:  # nosec B110
            pass

    def _sample_elevation(lon, lat):
        pt = xform_from_wgs.transform(QgsPointXY(lon, lat))
        res = dp.identify(pt, QgsRaster.IdentifyFormat.IdentifyFormatValue)
        if res.isValid():
            raw = res.results().get(band)
            try:
                value = float(raw)
                if not math.isfinite(value):
                    return None
                for nodata in nodata_values:
                    if math.isclose(value, nodata, rel_tol=0.0, abs_tol=1e-9):
                        return None
                return value
            except Exception:  # nosec B110
                pass
        return None

    n_sections = max(2, int(total_len / interval_m) + 1)
    sections = []
    for i in range(n_sections):
        d = min(i * interval_m, total_len)
        lon_c, lat_c, az = _point_at(d)
        perp_az = (az + 90.0) % 360.0
        offsets = [
            (j / (samples_per_section - 1) - 0.5) * 2 * half_width_m
            for j in range(samples_per_section)
        ]
        sec_pts = []
        for off in offsets:
            az_dir = perp_az if off >= 0 else (perp_az + 180.0) % 360.0
            dist = abs(off)
            if dist == 0:
                plon, plat = lon_c, lat_c
            else:
                plon, plat, _ = geod.fwd(lon_c, lat_c, az_dir, dist)
            z = _sample_elevation(plon, plat)
            sec_pts.append(
                {
                    "offset_m": round(off, 2),
                    "elevation": z,
                    "lon": plon,
                    "lat": plat,
                }
            )

        valid_pts = [
            (p["offset_m"], p["elevation"])
            for p in sec_pts
            if p["elevation"] is not None
        ]
        elevs = [p["elevation"] for p in sec_pts if p["elevation"] is not None]
        min_z = min(elevs) if elevs else None
        area_total = 0.0
        if len(valid_pts) >= 2:
            for k in range(len(valid_pts) - 1):
                dx = valid_pts[k + 1][0] - valid_pts[k][0]
                area_total += abs(dx) * (
                    (valid_pts[k][1] + valid_pts[k + 1][1]) / 2 - (min_z or 0)
                )

        cut_area = fill_area = 0.0
        design_elev = _design_elevation_at(d)
        if design_elev is not None and len(valid_pts) >= 2:
            ref = float(design_elev)
            for k in range(len(valid_pts) - 1):
                dx = abs(valid_pts[k + 1][0] - valid_pts[k][0])
                avg_z = (valid_pts[k][1] + valid_pts[k + 1][1]) / 2
                if avg_z > ref:
                    cut_area += dx * (avg_z - ref)
                else:
                    fill_area += dx * (ref - avg_z)

        sections.append(
            {
                "index": i + 1,
                "progressive_m": round(d, 2),
                "center_lon": lon_c,
                "center_lat": lat_c,
                "points": sec_pts,
                "min_elevation": round(min(elevs), 3) if elevs else None,
                "max_elevation": round(max(elevs), 3) if elevs else None,
                "design_elevation": (
                    round(design_elev, 3) if design_elev is not None else None
                ),
                "area_m2": round(area_total, 3),
                "cut_area_m2": round(cut_area, 3),
                "fill_area_m2": round(fill_area, 3),
            }
        )

    volumes = []
    total_cut = total_fill = 0.0
    for i in range(len(sections) - 1):
        s1, s2 = sections[i], sections[i + 1]
        seg_d = s2["progressive_m"] - s1["progressive_m"]
        vol = ((s1["area_m2"] + s2["area_m2"]) / 2) * seg_d
        cut_vol = ((s1["cut_area_m2"] + s2["cut_area_m2"]) / 2) * seg_d
        fill_vol = ((s1["fill_area_m2"] + s2["fill_area_m2"]) / 2) * seg_d
        total_cut += cut_vol
        total_fill += fill_vol
        volumes.append(
            {
                "from_section": s1["index"],
                "to_section": s2["index"],
                "distance_m": round(seg_d, 2),
                "volume_m3": round(vol, 2),
                "cut_m3": round(cut_vol, 2),
                "fill_m3": round(fill_vol, 2),
                "cumulative_cut_m3": round(total_cut, 2),
                "cumulative_fill_m3": round(total_fill, 2),
            }
        )

    grades_pct = []
    total_ascent = total_descent = 0.0
    for i in range(1, len(sections)):
        s0, s1 = sections[i - 1], sections[i]
        dz = None
        if s0["min_elevation"] is not None and s1["min_elevation"] is not None:
            z0 = (s0["min_elevation"] + s0["max_elevation"]) / 2
            z1 = (s1["min_elevation"] + s1["max_elevation"]) / 2
            dz = z1 - z0
            dd = s1["progressive_m"] - s0["progressive_m"]
            if dd > 0:
                grades_pct.append(dz / dd * 100.0)
        if dz is not None:
            if dz > 0:
                total_ascent += dz
            else:
                total_descent += abs(dz)

    road_metrics = {}
    if grades_pct:
        road_metrics["avg_grade_pct"] = round(
            sum(abs(g) for g in grades_pct) / len(grades_pct), 3
        )
        road_metrics["max_grade_pct"] = round(
            max((abs(g) for g in grades_pct), default=0), 3
        )
        road_metrics["min_grade_pct"] = round(
            min((abs(g) for g in grades_pct), default=0), 3
        )
    road_metrics["total_ascent_m"] = round(total_ascent, 2)
    road_metrics["total_descent_m"] = round(total_descent, 2)

    curve_radii = []
    for i in range(1, len(segs)):
        az0, az1 = segs[i - 1]["az"], segs[i]["az"]
        delta_deg = abs(((az1 - az0 + 180) % 360) - 180)
        if delta_deg < 0.5:
            continue
        d0 = segs[i - 1]["d1"] - segs[i - 1]["d0"]
        d1 = segs[i]["d1"] - segs[i]["d0"]
        chord = (d0 + d1) / 2
        delta_rad = math.radians(delta_deg)
        if math.sin(delta_rad / 2) > 1e-9:
            curve_radii.append(round(chord / (2 * math.sin(delta_rad / 2)), 1))

    road_metrics["curve_count"] = len(curve_radii)
    if curve_radii:
        road_metrics["min_radius_m"] = min(curve_radii)
        road_metrics["avg_radius_m"] = round(
            sum(curve_radii) / len(curve_radii), 1
        )

    cum = [0.0]
    for s in segs:
        cum.append(cum[-1] + (s["d1"] - s["d0"]))

    curve_points = []
    for i in range(1, len(pts_wgs) - 1):
        prev_seg = segs[i - 1]
        next_seg = segs[i]
        delta = ((next_seg["az"] - prev_seg["az"] + 180.0) % 360.0) - 180.0
        deflection = abs(delta)
        if deflection < 5.0:
            continue
        radius = None
        try:
            chord = (
                prev_seg["d1"]
                - prev_seg["d0"]
                + next_seg["d1"]
                - next_seg["d0"]
            ) / 2.0
            rad = math.radians(deflection)
            if chord > 0 and math.sin(rad / 2.0) > 1e-9:
                radius = chord / (2.0 * math.sin(rad / 2.0))
        except Exception:
            radius = None
        curve_points.append(
            {
                "index": len(curve_points) + 1,
                "vertex_index": i,
                "lon": pts_wgs[i][0],
                "lat": pts_wgs[i][1],
                "progressive_m": round(cum[i], 3),
                "deflection_deg": round(deflection, 3),
                "signed_deflection_deg": round(delta, 3),
                "incoming_azimuth": round(prev_seg["az"], 3),
                "outgoing_azimuth": round(next_seg["az"], 3),
                "turn": "right/destra" if delta > 0 else "left/sinistra",
                "estimated_radius_m": (
                    round(radius, 3) if radius is not None else None
                ),
            }
        )

    return {
        "alignment_length_m": round(total_len, 2),
        "n_sections": len(sections),
        "interval_m": interval_m,
        "half_width_m": half_width_m,
        "ref_elevation": design_start,
        "design_profile": {
            "start_elevation": design_start,
            "end_elevation": design_end,
            "grade_pct": (
                round(design_grade, 3) if design_grade is not None else None
            ),
        },
        "total_cut_m3": round(total_cut, 2),
        "total_fill_m3": round(total_fill, 2),
        "sections": sections,
        "volumes": volumes,
        "road_metrics": road_metrics,
        "curve_points": curve_points,
    }


# ──────────────────────────────────────────────────────────────────────
# SVG generator
# ──────────────────────────────────────────────────────────────────────


def generate_cross_sections_svg(result, project_title, created_at):
    sections = result.get("sections") or []
    volumes = result.get("volumes") or []
    curves = result.get("curve_points") or []
    width, height = 1700, 1100

    def esc(v):
        return _html.escape("" if v is None else str(v), quote=True)

    valid_sections = [
        s
        for s in sections
        if s.get("min_elevation") is not None
        and s.get("max_elevation") is not None
    ]
    progs = [float(s.get("progressive_m") or 0) for s in valid_sections]
    elevs = [
        (float(s.get("min_elevation")) + float(s.get("max_elevation"))) / 2.0
        for s in valid_sections
    ]
    chart_l, chart_t, chart_r, chart_b = 80, 120, 1620, 410
    long_svg = (
        '<text x="90" y="255" class="muted">Insufficient elevation data for '
        'longitudinal profile.</text>')

    if len(valid_sections) >= 2:
        min_x = 0.0
        max_x = max(progs) if progs else 1.0
        min_y = min(elevs)
        max_y = max(elevs)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)

        def x_of(d):
            return chart_l + ((float(d) - min_x) / span_x) * (
                chart_r - chart_l
            )

        def y_of(z):
            return chart_b - ((float(z) - min_y) / span_y) * (
                chart_b - chart_t
            )

        def long_point(i, s):
            cmd = "M" if i == 0 else "L"
            x = x_of(s.get("progressive_m"))
            y = y_of(elevs[i])
            return f"{cmd}{x:.1f},{y:.1f}"

        line_d = " ".join(
            long_point(i, s) for i, s in enumerate(valid_sections)
        )
        section_marks = []
        for i, s in enumerate(valid_sections):
            x = x_of(s.get("progressive_m"))
            strong = i % 5 == 0 or i == len(valid_sections) - 1
            section_marks.append(
                f'<line x1="{x:.1f}" y1="{chart_t}" x2="{x:.1f}" '
                f'y2="{chart_b}"'
                f' class="{"section-strong" if strong else "section"}"/>'
            )
        curve_marks = []
        for c in curves:
            x = x_of(c.get("progressive_m") or 0)
            curve_marks.append(
                f'<line x1="{x:.1f}" y1="{chart_t}" x2="{x:.1f}" '
                f'y2="{chart_b}" class="curve-line"/>'
                f'<circle cx="{x:.1f}" cy="{chart_t + 12}" r="7" '
                f'class="curve-dot"/>'
                f'<text x="{x:.1f}" y="{chart_t - 8}" '
                f'class="curve-label">C{esc(c.get("index"))}</text>'
            )
        axis_labels = []
        for t in (0, 0.25, 0.5, 0.75, 1):
            d = min_x + span_x * t
            x = x_of(d)
            axis_labels.append(
                f'<text x="{x:.1f}" y="{chart_b + 28}" class="axis '
                f'center">{d:.0f} m</text>'
            )
        long_svg = (
            f'<rect x="{chart_l}" y="{chart_t}" width="{chart_r - chart_l}"'
            f' height="{chart_b - chart_t}" class="frame"/>'
            f'{"".join(section_marks)}'
            f'<path d="{line_d}" class="long-line"/>'
            f'{"".join(curve_marks)}'
            f'{"".join(axis_labels)}'
            f'<text x="{chart_l}" y="{chart_t - 18}" class="section-title">'
            f"Longitudinal Profile with Cross Sections / Profilo "
            f"Longitudinale con Sezioni</text>"
        )

    thumbs = []
    thumb_w, thumb_h = 250, 135
    start_x, start_y = 80, 500
    gap_x, gap_y = 28, 44
    for idx, sec in enumerate(sections[:18]):
        pts = [
            p
            for p in (sec.get("points") or [])
            if p.get("elevation") is not None
        ]
        col = idx % 6
        row = idx // 6
        x0 = start_x + col * (thumb_w + gap_x)
        y0 = start_y + row * (thumb_h + gap_y)
        if len(pts) < 2:
            thumbs.append(
                f'<text x="{x0}" y="{y0 + 45}" class="muted">Sec. '
                f'{esc(sec.get("index"))}: no elev.</text>'
            )
            continue
        offsets = [float(p.get("offset_m") or 0) for p in pts]
        elev = [float(p.get("elevation")) for p in pts]
        if sec.get("design_elevation") is not None:
            elev.append(float(sec.get("design_elevation")))
        mn_x, mx_x = min(offsets), max(offsets)
        mn_y, mx_y = min(elev), max(elev)
        sp_x = max(mx_x - mn_x, 1.0)
        sp_y = max(mx_y - mn_y, 1.0)

        def tx(o):
            return x0 + 12 + ((float(o) - mn_x) / sp_x) * (thumb_w - 24)

        def ty(z):
            return (
                y0 + thumb_h - 28 - ((float(z) - mn_y) / sp_y) * (thumb_h - 46)
            )

        def thumb_point(i, p):
            cmd = "M" if i == 0 else "L"
            x = tx(p.get("offset_m"))
            y = ty(p.get("elevation"))
            return f"{cmd}{x:.1f},{y:.1f}"

        d = " ".join(thumb_point(i, p) for i, p in enumerate(pts))
        ref = ""
        if sec.get("design_elevation") is not None:
            y = ty(sec.get("design_elevation"))
            ref = (
                f'<line x1="{x0 + 12}" y1="{y:.1f}" x2="{x0 + thumb_w - 12}"'
                f' y2="{y:.1f}" class="design-line"/>'
            )
        thumbs.append(
            f'<g><rect x="{x0}" y="{y0}" width="{thumb_w}" height="{thumb_h}" '
            f'class="thumb"/>'
            f'<path d="{d}" class="section-line"/>{ref}'
            f'<text x="{x0 + 12}" y="{y0 + 18}" class="thumb-label">'
            f'Sec. {esc(sec.get("index"))} · '
            f'{float(sec.get("progressive_m") or 0):.0f} m</text>'
            f'<text x="{x0 + 12}" y="{y0 + thumb_h - 8}" class="thumb-meta">'
            f'A={float(sec.get("area_m2") or 0):.2f} m² · '
            f'S={float(sec.get("cut_area_m2") or 0):.2f}'
            f' · R={float(sec.get("fill_area_m2") or 0):.2f}</text></g>'
        )

    volume_rows = []
    table_y = 990
    for i, v in enumerate(volumes[:4]):
        y = table_y + 28 + i * 22
        volume_rows.append(
            f'<text x="92" y="{y}" class="cell">{esc(v.get("from_section"))} '
            f'→ {esc(v.get("to_section"))}</text>'
            f'<text x="260" y="{y}" class="cell '
            f'right">{float(v.get("distance_m") or 0):.1f}</text>'
            f'<text x="430" y="{y}" class="cell '
            f'right">{float(v.get("volume_m3") or 0):.1f}</text>'
            f'<text x="610" y="{y}" class="cell '
            f'right">{float(v.get("cut_m3") or 0):.1f}</text>'
            f'<text x="790" y="{y}" class="cell '
            f'right">{float(v.get("fill_m3") or 0):.1f}</text>'
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}">
<defs>
  <style>
    .title {{ font: 700 28px Arial, sans-serif; fill:#f1f5f9; }}
    .subtitle {{ font: 14px Arial, sans-serif; fill:#8ba3c7; }}
    .section-title {{ font: 700 17px Arial, sans-serif;
                      fill:#f1f5f9; }}
    .axis {{ font: 12px Arial, sans-serif; fill:#8ba3c7; }}
    .center {{ text-anchor:middle; }}
    .frame,.thumb {{ fill:#0e1118; stroke:#2d3757; stroke-width:1.2; }}
    .section {{ stroke:#4f73c4; stroke-width:1; stroke-dasharray:3 5;
                opacity:.75; }}
    .section-strong {{ stroke:#f97316; stroke-width:1.6;
                       stroke-dasharray:5 4; }}
    .long-line {{ fill:none; stroke:#4f73c4; stroke-width:4;
                  stroke-linecap:round; stroke-linejoin:round; }}
    .curve-line {{ stroke:#f97316; stroke-width:2;
                   stroke-dasharray:6 4; }}
    .curve-dot {{ fill:#f97316; stroke:#111827; stroke-width:1.5; }}
    .curve-label {{ font:700 11px Arial, sans-serif; fill:#9a3412;
                    text-anchor:middle; }}
    .section-line {{ fill:none; stroke:#4f73c4; stroke-width:2; }}
    .design-line {{ stroke:#f59e0b; stroke-width:1.5;
                    stroke-dasharray:5 3; }}
    .thumb-label {{ font:700 12px Arial, sans-serif; fill:#e2e8f0; }}
    .thumb-meta,.muted {{ font:11px Arial, sans-serif;
                          fill:#8ba3c7; }}
    .cell {{ font:12px Arial, sans-serif; fill:#e2e8f0; }}
    .right {{ text-anchor:end; }}
  </style>
</defs>
<rect width="100%" height="100%" fill="#12151e"/>
<text x="70" y="52" class="title">Cross Sections and Road Profile /
Sezioni Trasversali</text>
<text x="70" y="80" class="subtitle">
  Project: {esc(project_title)} · Axis
  {float(result.get("alignment_length_m") or 0):.2f} m ·
  Sections {len(sections)} ·
  Cut {float(result.get("total_cut_m3") or 0):.2f} m³ ·
  Fill {float(result.get("total_fill_m3") or 0):.2f} m³ ·
  {esc(created_at)}
</text>
{long_svg}
<text x="80" y="470" class="section-title">Cross Sections / Sezioni
Trasversali</text>
{"".join(thumbs)}
<text x="80" y="965" class="section-title">Volume Summary / Prime
Tratte Volume</text>
<text x="92" y="{table_y + 4}" class="axis">Sections</text>
<text x="260" y="{table_y + 4}" class="axis right">Dist. m</text>
<text x="430" y="{table_y + 4}" class="axis right">Vol. m³</text>
<text x="610" y="{table_y + 4}" class="axis right">Cut m³</text>
<text x="790" y="{table_y + 4}" class="axis right">Fill m³</text>
{"".join(volume_rows)}
</svg>
"""


# ──────────────────────────────────────────────────────────────────────
# HTML generators
# ──────────────────────────────────────────────────────────────────────


def generate_cross_sections_html(data):
    """Compact HTML for embedding in Results tab."""
    dp = data.get("design_profile", {})
    has_ref = dp.get("start_elevation") is not None
    html = f"""
    <div style="background:#0f172a;border:1px solid #243244;
                color:#cbd5e1;padding:12px;
                border-radius:6px;margin-bottom:12px;font-size:13px;
                line-height:1.5;">
        <strong>Axis:</strong>
        {(data['alignment_length_m'] / 1000):.3f} km &nbsp;·&nbsp;
        <strong>Sections:</strong> {data['n_sections']} &nbsp;·&nbsp;
        <strong>Interval:</strong> {data['interval_m']} m &nbsp;·&nbsp;
        <strong>Width:</strong> {data['half_width_m'] * 2} m<br>
    """
    if has_ref:
        html += f"""
        Start Elev: <strong>{dp.get('start_elevation')} m</strong>
        &nbsp;·&nbsp;
        End Elev: <strong>{dp.get('end_elevation')} m</strong>&nbsp;·&nbsp;
        Grade: <strong>{dp.get('grade_pct')}%</strong><br>
        <span style="color:#ef4444;">Cut:
        <strong>{data['total_cut_m3']:,.0f} m³</strong>
        </span>&nbsp;·&nbsp;
        <span style="color:#22c55e;">Fill:
        <strong>{data['total_fill_m3']:,.0f} m³</strong></span>&nbsp;·&nbsp;
        <span style="color:#f59e0b;">Balance:
        <strong>
        {(data['total_fill_m3'] - data['total_cut_m3']):,.0f} m³</strong>
        </span>
        """
    html += "</div>"

    m = data.get("road_metrics", {})
    if m:
        c_count = m.get("curve_count", 0)
        c_text = (
            f"Curves: <strong>{c_count}</strong> · Min radius: "
            f"<strong>{m.get('min_radius_m', 'n/a')} m</strong>"
            if c_count > 0
            else "Straight alignment (no curves detected)"
        )
        html += f"""
        <div style="background:#0f172a;border:1px solid #243244;
                    color:#cbd5e1;padding:12px;
                    border-radius:6px;margin-bottom:12px;font-size:13px;
                    line-height:1.5;">
            <strong style="color:#d97706;">Road Metrics</strong><br>
            Avg grade:
            <strong>{m.get('avg_grade_pct', 'n/a')} %</strong> &nbsp;·&nbsp;
            Max:
            <strong>{m.get('max_grade_pct', 'n/a')} %</strong> &nbsp;·&nbsp;
            Min: <strong>{m.get('min_grade_pct', 'n/a')} %</strong><br>
            Total relief:
            <span style="color:#ef4444;">
            ▲ {m.get('total_ascent_m', 0):.1f} m</span>
            &nbsp;<span style="color:#22c55e;">
            ▼ {m.get('total_descent_m', 0):.1f} m</span><br>
            {c_text}
        </div>
        """

    svgs = ""
    for sec in data["sections"][:12]:
        pts = [p for p in sec["points"] if p["elevation"] is not None]
        if len(pts) < 2:
            svgs += (
                f'<div style="text-align:center;font-size:11px;'
                f'color:#64748b;width:120px;">Sec.{sec["index"]}<br>no '
                f'data</div>'
            )
            continue
        w, h, padX, padY = 120, 70, 6, 6
        offsets = [p["offset_m"] for p in pts]
        elevs = [p["elevation"] for p in pts]
        minX, maxX = min(offsets), max(offsets)
        minY, maxY = min(elevs), max(elevs)
        spanX = (maxX - minX) or 1
        spanY = (maxY - minY) or 1

        def xOf(o):
            return padX + (o - minX) / spanX * (w - 2 * padX)

        def yOf(e):
            return padY + (1 - (e - minY) / spanY) * (h - 2 * padY)

        def mini_point(i):
            cmd = "M" if i == 0 else "L"
            x = xOf(pts[i]["offset_m"])
            y = yOf(pts[i]["elevation"])
            return f"{cmd}{x:.1f},{y:.1f}"

        d_path = " ".join(mini_point(i) for i in range(len(pts)))
        fill_d = (
            f"{d_path} L{xOf(offsets[-1]):.1f},{h - padY} "
            f"L{xOf(offsets[0]):.1f},{h - padY} Z"
        )
        ref_line = ""
        if has_ref and sec.get("design_elevation") is not None:
            try:
                ry = yOf(float(sec["design_elevation"]))
                ref_line = (
                    f'<line x1="{padX}" y1="{ry:.1f}" x2="{w - padX}" '
                    f'y2="{ry:.1f}"'
                    f' stroke="#f59e0b" stroke-width="1" stroke-dasharray="3 '
                    f'2"/>'
                )
            except Exception:  # nosec B110
                pass
        svgs += f"""
        <div style="flex-shrink:0;text-align:center;width:120px;">
            <svg width="{w}" height="{h}"
            style="border:1px solid #334155;border-radius:5px;
            background:#111827;display:block;">
                <path d="{fill_d}" fill="rgba(77,159,255,0.15)"
                stroke="none"/>
                <path d="{d_path}" fill="none" stroke="#4d9fff"
                stroke-width="1.5"/>
                {ref_line}
            </svg>
            <div style="font-size:11px;color:#64748b;margin-top:2px;">
                Sec. {sec["index"]} · {sec["progressive_m"]:.0f} m<br>
                {min(elevs):.1f}–{max(elevs):.1f} m
            </div>
        </div>
        """

    html += (
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;padding:0 0 10px '
        f'0;">{svgs}')
    if data["n_sections"] > 12:
        html += (
            f'<div style="font-size:11px;color:#64748b;align-self:center;">'
            f'+ {data["n_sections"] - 12} more sections in table</div>'
        )
    html += "</div>"

    if data.get("volumes"):
        rows = ""
        for v in data["volumes"]:
            s1d = data["sections"][v["from_section"] - 1]
            s2d = data["sections"][v["to_section"] - 1]
            grade_str = "<td style='text-align:center;'>—</td>"
            if (
                None not in (s1d["min_elevation"], s2d["min_elevation"])
                and v["distance_m"] > 0
            ):
                z1 = (s1d["min_elevation"] + s1d["max_elevation"]) / 2
                z2 = (s2d["min_elevation"] + s2d["max_elevation"]) / 2
                g = (z2 - z1) / v["distance_m"] * 100
                col = "#ef4444" if g >= 0 else "#22c55e"
                grade_str = (
                    f'<td style="text-align:right;color:{col};">{g:.2f}%</td>'
                )
            rows += f"""<tr>
                <td style="text-align:center;padding:4px;">
                {v["from_section"]}→{v["to_section"]}</td>
                <td style="text-align:right;padding:4px;">
                {v["distance_m"]:.1f}</td>
                {grade_str}
                <td style="text-align:right;padding:4px;">
                {(s1d.get("area_m2") or 0):.2f}</td>
                <td style="text-align:right;padding:4px;">
                {(s2d.get("area_m2") or 0):.2f}</td>
                <td style="text-align:right;font-weight:600;padding:4px;">
                {v["volume_m3"]:,.1f}</td>
            """
            if has_ref:
                rows += (
                    f'<td style="text-align:right;color:#ef4444;'
                    f'padding:4px;">{v["cut_m3"]:,.1f}</td>'
                    f'<td style="text-align:right;color:#22c55e;'
                    f'padding:4px;">{v["fill_m3"]:,.1f}</td>'
                    f'<td style="text-align:right;color:#ef4444;'
                    f'padding:4px;">{v["cumulative_cut_m3"]:,.0f}</td>'
                    f'<td style="text-align:right;color:#22c55e;'
                    f'padding:4px;">{v["cumulative_fill_m3"]:,.0f}</td>'
                )
            rows += "</tr>"
        header_ref = (
            (
                '<th style="color:#ef4444;border-bottom:1px solid '
                '#cbd5e1;">Cut (m³)</th>'
                '<th style="color:#22c55e;border-bottom:1px solid '
                '#cbd5e1;">Fill (m³)</th>'
                '<th style="color:#ef4444;border-bottom:1px solid '
                '#cbd5e1;">Cum.Cut</th>'
                '<th style="color:#22c55e;border-bottom:1px solid '
                '#cbd5e1;">Cum.Fill</th>'
            )
            if has_ref
            else ""
        )
        html += f"""
        <table style="width:100%;border-collapse:collapse;font-size:12px;
        background:#0f172a;color:#cbd5e1;border:1px solid #243244;">
            <thead><tr>
                <th style="padding:6px 4px;text-align:center;
                border-bottom:1px solid #cbd5e1;">Sections</th>
                <th style="padding:6px 4px;text-align:right;
                border-bottom:1px solid #cbd5e1;">Dist (m)</th>
                <th style="padding:6px 4px;text-align:right;
                border-bottom:1px solid #cbd5e1;">Grade %</th>
                <th style="padding:6px 4px;text-align:right;
                border-bottom:1px solid #cbd5e1;">A₁ (m²)</th>
                <th style="padding:6px 4px;text-align:right;
                border-bottom:1px solid #cbd5e1;">A₂ (m²)</th>
                <th style="padding:6px 4px;text-align:right;
                border-bottom:1px solid #cbd5e1;">Vol (m³)</th>
                {header_ref}
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
        """
    return html


def generate_cross_sections_results_html(data):
    """Full HTML for the Results tab (summary + sections + volumes + detail
    table)."""
    dp = data.get("design_profile", {})
    has_ref = dp.get("start_elevation") is not None
    created_at = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    html = f"""
    <div class="summary-card">
        <strong>Axis:</strong>
        {(data['alignment_length_m'] / 1000):.3f} km &nbsp;·&nbsp;
        <strong>Sections:</strong> {data['n_sections']} &nbsp;·&nbsp;
        <strong>Interval:</strong> {data['interval_m']} m &nbsp;·&nbsp;
        <strong>Width:</strong> {data['half_width_m'] * 2} m
    """
    if has_ref:
        html += f"""<br>
        Start: <strong>{dp.get('start_elevation')} m</strong> &nbsp;·&nbsp;
        End: <strong>{dp.get('end_elevation')} m</strong> &nbsp;·&nbsp;
        <span class="badge badge-cut">
        Cut: {data['total_cut_m3']:,.0f} m³</span> &nbsp;
        <span class="badge badge-fill">
        Fill: {data['total_fill_m3']:,.0f} m³</span> &nbsp;
        <span class="badge badge-info">Balance:
        {(data['total_fill_m3'] - data['total_cut_m3']):,.0f} m³</span>
        """
    html += f"<br><span style='color:#64748b;'>{created_at}</span></div>"

    m = data.get("road_metrics", {})
    if m:
        c_count = m.get("curve_count", 0)
        html += f"""
        <div class="summary-card">
            <strong style="color:#f59e0b;">Road Metrics / Metriche
            Stradali</strong><br>
            Avg grade:
            <strong>{m.get('avg_grade_pct', 'n/a')} %</strong> &nbsp;·&nbsp;
            Max:
            <strong>{m.get('max_grade_pct', 'n/a')} %</strong> &nbsp;·&nbsp;
            Min: <strong>{m.get('min_grade_pct', 'n/a')} %</strong><br>
            Relief:
            <span style="color:#ef4444;">
            ▲ {m.get('total_ascent_m', 0):.1f} m</span>
            &nbsp;<span style="color:#22c55e;">
            ▼ {m.get('total_descent_m', 0):.1f} m</span>
            &nbsp;·&nbsp; Curves: <strong>{c_count}</strong>
        """
        if c_count > 0:
            html += (
                f" &nbsp;·&nbsp; R min: "
                f"<strong>{m.get('min_radius_m', 'n/a')} m</strong>"
                f" &nbsp;·&nbsp; R avg: "
                f"<strong>{m.get('avg_radius_m', 'n/a')} m</strong>"
            )
        html += "</div>"

    html += (
        '<div class="section-title">Cross Sections / Sezioni Trasversali</div>'
    )
    html += '<div class="sections-grid">'
    for sec in data["sections"]:
        pts = [p for p in sec["points"] if p["elevation"] is not None]
        if len(pts) < 2:
            html += (
                f'<div class="section-card"><div class="label">Sec. '
                f'{sec["index"]}<br>no data</div></div>')
            continue
        w, h, px, py = 140, 80, 6, 6
        offsets = [p["offset_m"] for p in pts]
        elevs_sec = [p["elevation"] for p in pts]
        mnX, mxX = min(offsets), max(offsets)
        mnY, mxY = min(elevs_sec), max(elevs_sec)
        sX = (mxX - mnX) or 1
        sY = (mxY - mnY) or 1

        def xo(o):
            return px + (o - mnX) / sX * (w - 2 * px)

        def yo(e):
            return py + (1 - (e - mnY) / sY) * (h - 2 * py)

        def sec_point(i):
            cmd = "M" if i == 0 else "L"
            x = xo(pts[i]["offset_m"])
            y = yo(pts[i]["elevation"])
            return f"{cmd}{x:.1f},{y:.1f}"

        d_path = " ".join(sec_point(i) for i in range(len(pts)))
        fill_d = (
            f"{d_path} L{xo(offsets[-1]):.1f},{h - py} "
            f"L{xo(offsets[0]):.1f},{h - py} Z")
        ref_line = ""
        if has_ref and sec.get("design_elevation") is not None:
            try:
                ry = yo(float(sec["design_elevation"]))
                if py <= ry <= h - py:
                    ref_line = (
                        f'<line x1="{px}" y1="{ry:.1f}" x2="{w - px}" '
                        f'y2="{ry:.1f}"'
                        f' stroke="#f59e0b" stroke-width="1" '
                        f'stroke-dasharray="3 2"/>'
                    )
            except Exception:  # nosec B110
                pass
        html += f"""
        <div class="section-card">
            <svg width="{w}" height="{h}">
                <path d="{fill_d}" fill="rgba(52,211,153,0.15)"
                stroke="none"/>
                <path d="{d_path}" fill="none" stroke="#34d399"
                stroke-width="1.5"/>
                {ref_line}
            </svg>
            <div class="label">Sec. {sec["index"]} ·
            {sec["progressive_m"]:.0f} m<br>
            {mnY:.1f}–{mxY:.1f} m</div>
        </div>
        """
    html += "</div>"

    if data.get("volumes"):
        html += (
            '<div class="section-title">Volumes / Tabella Volumi</div>'
            "<table><thead><tr>"
            "<th>Sections</th><th>Dist (m)</th><th>Grade %</th>"
            "<th>A₁ (m²)</th><th>A₂ (m²)</th><th>Vol (m³)</th>"
        )
        if has_ref:
            html += (
                '<th style="color:#ef4444;">Cut (m³)</th>'
                '<th style="color:#22c55e;">Fill (m³)</th>'
                '<th style="color:#ef4444;">Cum.Cut</th>'
                '<th style="color:#22c55e;">Cum.Fill</th>'
            )
        html += "</tr></thead><tbody>"
        for v in data["volumes"]:
            s1 = data["sections"][v["from_section"] - 1]
            s2 = data["sections"][v["to_section"] - 1]
            grade_str = "<td>—</td>"
            if (
                None not in (s1["min_elevation"], s2["min_elevation"])
                and v["distance_m"] > 0
            ):
                z1 = (s1["min_elevation"] + s1["max_elevation"]) / 2
                z2 = (s2["min_elevation"] + s2["max_elevation"]) / 2
                g = (z2 - z1) / v["distance_m"] * 100
                col = "#ef4444" if g >= 0 else "#22c55e"
                grade_str = f'<td style="color:{col};">{g:.2f}%</td>'
            html += (
                f'<tr><td>{v["from_section"]}→{v["to_section"]}</td>'
                f'<td>{v["distance_m"]:.1f}</td>{grade_str}'
                f'<td>{s1.get("area_m2", 0):.2f}</td>'
                f'<td>{s2.get("area_m2", 0):.2f}</td>'
                f'<td style="font-weight:600;">{v["volume_m3"]:,.1f}</td>'
            )
            if has_ref:
                html += (
                    f'<td style="color:#ef4444;">{v["cut_m3"]:,.1f}</td>'
                    f'<td style="color:#22c55e;">{v["fill_m3"]:,.1f}</td>'
                    f'<td style="color:#ef4444;">'
                    f'{v["cumulative_cut_m3"]:,.0f}</td>'
                    f'<td style="color:#22c55e;">'
                    f'{v["cumulative_fill_m3"]:,.0f}</td>'
                )
            html += "</tr>"
        html += "</tbody></table>"

    html += (
        '<div class="section-title">Section Detail / Dettaglio Sezioni</div>'
        "<table><thead><tr>"
        "<th>Sec.</th><th>Progressive (m)</th><th>Min (m)</th><th>Max "
        "(m)</th><th>Area (m²)</th>"
    )
    if has_ref:
        html += "<th>Cut (m²)</th><th>Fill (m²)</th>"
    html += "</tr></thead><tbody>"
    for s in data["sections"]:
        mn = (
            f"{s['min_elevation']:.2f}"
            if s["min_elevation"] is not None
            else "—"
        )
        mx = (
            f"{s['max_elevation']:.2f}"
            if s["max_elevation"] is not None
            else "—"
        )
        html += (
            f'<tr><td>{s["index"]}</td><td>{s["progressive_m"]:.2f}</td>'
            f'<td>{mn}</td><td>{mx}</td><td>{s.get("area_m2", 0):.2f}</td>'
        )
        if has_ref:
            html += (
                f'<td '
                f'style="color:#ef4444;">{s.get("cut_area_m2", 0):.2f}</td>'
                f'<td '
                f'style="color:#22c55e;">{s.get("fill_area_m2", 0):.2f}</td>'
            )
        html += "</tr>"
    html += "</tbody></table>"
    return html
