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
core_confronto.py — Before/after comparison of plugin outputs.

Compares two GeoPackage files produced by this plugin (state before the
works vs state after the works) and, optionally, two DTM rasters of the
same area (DEM of Difference). All computations are deterministic raster
and vector math — no AI involved.
"""

import math
import os

# Layer name prefixes written by plugin._write_vector_package()
_PREFIX_SECTIONS_CENTERS = "centri_sezione"
_PREFIX_VOLUMES = "tratte_volumi"
_PREFIX_PROFILE_SAMPLES = "profilo_campioni"
_PREFIX_PROFILE_PICKETS = "profilo_picchetti"


def _t(lang, it, en):
    return en if lang == "en" else it


# ──────────────────────────────────────────────────────────────────────
# GeoPackage discovery and reading
# ──────────────────────────────────────────────────────────────────────


def list_output_gpkgs(directory):
    """Return the .gpkg files in *directory*, newest first."""
    if not directory or not os.path.isdir(directory):
        return []
    paths = [
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.lower().endswith(".gpkg")
    ]
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths


def _open_gpkg(path):
    from osgeo import ogr

    ds = ogr.Open(path)
    if ds is None:
        raise ValueError(
            f"GeoPackage non leggibile / unreadable GeoPackage: {path}"
        )
    return ds


def _read_layer_records(ds, layer_name, fields):
    """Read *fields* from every feature of *layer_name*; missing fields become
    None."""
    layer = ds.GetLayerByName(layer_name)
    if layer is None:
        return []
    defn = layer.GetLayerDefn()
    available = {
        defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())
    }
    records = []
    layer.ResetReading()
    for feature in layer:
        rec = {}
        for field in fields:
            rec[field] = (
                feature.GetField(field) if field in available else None
            )
        records.append(rec)
    return records


def read_gpkg_summary(path):
    """Extract profile/section data from a GeoPackage created by this plugin.

    Returns a dict with keys:
      path, name, sections, volumes, samples, pickets, kind
    """
    ds = _open_gpkg(path)
    summary = {
        "path": path,
        "name": os.path.basename(path),
        "sections": [],
        "volumes": [],
        "samples": [],
        "pickets": [],
    }
    for i in range(ds.GetLayerCount()):
        name = ds.GetLayer(i).GetName()
        low = name.lower()
        if low.startswith(_PREFIX_SECTIONS_CENTERS):
            summary["sections"] = _read_layer_records(
                ds,
                name,
                [
                    "sezione",
                    "prog_m",
                    "quota_min",
                    "quota_max",
                    "quota_prj",
                    "area_m2",
                    "cut_m2",
                    "fill_m2",
                ],
            )
        elif low.startswith(_PREFIX_VOLUMES):
            summary["volumes"] = _read_layer_records(
                ds,
                name,
                [
                    "da_sez",
                    "a_sez",
                    "dist_m",
                    "volume_m3",
                    "sterro_m3",
                    "riporto_m3",
                    "cum_sterro",
                    "cum_riporto",
                ],
            )
        elif low.startswith(_PREFIX_PROFILE_SAMPLES):
            summary["samples"] = _read_layer_records(
                ds,
                name,
                [
                    "prog_m",
                    "quota_m",
                ],
            )
        elif low.startswith(_PREFIX_PROFILE_PICKETS):
            summary["pickets"] = _read_layer_records(
                ds,
                name,
                [
                    "id",
                    "prog_m",
                    "quota_m",
                    "label",
                ],
            )
    ds = None

    if summary["sections"] or summary["volumes"]:
        summary["kind"] = "sezioni"
    elif summary["samples"] or summary["pickets"]:
        summary["kind"] = "profilo"
    else:
        summary["kind"] = "sconosciuto"
    return summary


# ──────────────────────────────────────────────────────────────────────
# Vector comparison (before/after GeoPackage)
# ──────────────────────────────────────────────────────────────────────


def _sorted_profile(samples):
    pts = [
        (float(s["prog_m"]), float(s["quota_m"]))
        for s in samples
        if s.get("prog_m") is not None and s.get("quota_m") is not None
    ]
    pts.sort(key=lambda p: p[0])
    return pts


def _interp_elevation(profile_pts, prog):
    """Linear interpolation of elevation at *prog*; None outside the range."""
    if not profile_pts:
        return None
    if prog < profile_pts[0][0] or prog > profile_pts[-1][0]:
        return None
    for (x1, z1), (x2, z2) in zip(profile_pts[:-1], profile_pts[1:]):
        if x1 <= prog <= x2:
            if x2 == x1:
                return z1
            f = (prog - x1) / (x2 - x1)
            return z1 + f * (z2 - z1)
    return profile_pts[-1][1]


def _totals_from_volumes(volumes):
    cut = sum(float(v.get("sterro_m3") or 0) for v in volumes)
    fill = sum(float(v.get("riporto_m3") or 0) for v in volumes)
    return cut, fill


def compare_gpkg_summaries(before, after, tolerance_m=5.0):
    """Compare two summaries from read_gpkg_summary().

    Sections are matched by nearest progressive distance within
    *tolerance_m*. Profiles are compared by linear interpolation of the
    "after" elevations at the "before" progressive stations.
    """
    result = {
        "before": before,
        "after": after,
        "tolerance_m": tolerance_m,
        "section_pairs": [],
        "unmatched_before": 0,
        "unmatched_after": 0,
        "profile_deltas": [],
        "profile_stats": None,
        "volume_totals": None,
    }

    # ── sections by progressive ──
    sec_b = [s for s in before["sections"] if s.get("prog_m") is not None]
    sec_a = [s for s in after["sections"] if s.get("prog_m") is not None]
    used_a = set()
    for sb in sec_b:
        prog_b = float(sb["prog_m"])
        best = None
        best_dist = None
        for idx, sa in enumerate(sec_a):
            if idx in used_a:
                continue
            d = abs(float(sa["prog_m"]) - prog_b)
            if d <= tolerance_m and (best_dist is None or d < best_dist):
                best, best_dist = idx, d
        if best is None:
            result["unmatched_before"] += 1
            continue
        used_a.add(best)
        sa = sec_a[best]

        def _delta(key):
            vb, va = sb.get(key), sa.get(key)
            if vb is None or va is None:
                return None
            return float(va) - float(vb)

        result["section_pairs"].append(
            {
                "sezione": sb.get("sezione"),
                "prog_m": prog_b,
                "prog_after_m": float(sa["prog_m"]),
                "quota_min_before": sb.get("quota_min"),
                "quota_min_after": sa.get("quota_min"),
                "d_quota_min": _delta("quota_min"),
                "d_quota_max": _delta("quota_max"),
                "d_area_m2": _delta("area_m2"),
                "d_cut_m2": _delta("cut_m2"),
                "d_fill_m2": _delta("fill_m2"),
            }
        )
    result["unmatched_after"] = len(sec_a) - len(used_a)

    # ── earthwork totals ──
    if before["volumes"] or after["volumes"]:
        cut_b, fill_b = _totals_from_volumes(before["volumes"])
        cut_a, fill_a = _totals_from_volumes(after["volumes"])
        result["volume_totals"] = {
            "cut_before": cut_b,
            "fill_before": fill_b,
            "cut_after": cut_a,
            "fill_after": fill_a,
            "d_cut": cut_a - cut_b,
            "d_fill": fill_a - fill_b,
            "d_balance": (cut_a - fill_a) - (cut_b - fill_b),
        }

    # ── profile elevations ──
    prof_b = _sorted_profile(before["samples"] or before["pickets"])
    prof_a = _sorted_profile(after["samples"] or after["pickets"])
    if prof_b and prof_a:
        deltas = []
        for prog, z_b in prof_b:
            z_a = _interp_elevation(prof_a, prog)
            if z_a is None:
                continue
            deltas.append(
                {
                    "prog_m": prog,
                    "z_before": z_b,
                    "z_after": z_a,
                    "dz": z_a - z_b,
                }
            )
        result["profile_deltas"] = deltas
        if deltas:
            dz_values = [d["dz"] for d in deltas]
            n = len(dz_values)
            mean = sum(dz_values) / n
            rms = math.sqrt(sum(v * v for v in dz_values) / n)
            result["profile_stats"] = {
                "n": n,
                "mean": mean,
                "min": min(dz_values),
                "max": max(dz_values),
                "rms": rms,
            }
    return result


# ──────────────────────────────────────────────────────────────────────
# Delta profile SVG chart
# ──────────────────────────────────────────────────────────────────────


def generate_delta_svg(deltas, lang="it", width=1700, height=420):
    """SVG chart of elevation change (after - before) along the axis."""
    if not deltas:
        return ""
    margin_l, margin_r, margin_t, margin_b = 90, 30, 40, 60
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    progs = [d["prog_m"] for d in deltas]
    dzs = [d["dz"] for d in deltas]
    x_min, x_max = min(progs), max(progs)
    span_x = (x_max - x_min) or 1.0
    abs_max = max(0.1, max(abs(min(dzs)), abs(max(dzs))) * 1.15)

    def px(prog):
        return margin_l + (prog - x_min) / span_x * plot_w

    def py(dz):
        return margin_t + plot_h / 2 - dz / abs_max * (plot_h / 2)

    zero_y = py(0)
    path_pts = " ".join(
        f"{px(d['prog_m']):.1f},{py(d['dz']):.1f}" for d in deltas
    )
    area_start = f"{px(deltas[0]['prog_m']):.1f},{zero_y:.1f}"
    area_end = f"{px(deltas[-1]['prog_m']):.1f},{zero_y:.1f}"
    area_pts = f"{area_start} {path_pts} {area_end}"

    grid = []
    n_ticks = 6
    for i in range(n_ticks + 1):
        gx = margin_l + plot_w * i / n_ticks
        prog = x_min + span_x * i / n_ticks
        grid.append(
            f'<line x1="{gx:.1f}" y1="{margin_t}" x2="{gx:.1f}" '
            f'y2="{margin_t + plot_h}" '
            'stroke="#2d3757" stroke-width="0.6"/>'
            f'<text x="{gx:.1f}" y="{margin_t + plot_h + 22}" fill="#8ba3c7" '
            f'font-size="12" text-anchor="middle">{prog:,.0f} m</text>'
        )
    for i in range(5):
        dz = abs_max - i * (2 * abs_max / 4)
        gy = py(dz)
        grid.append(
            f'<line x1="{margin_l}" y1="{gy:.1f}" x2="{margin_l + plot_w}" '
            f'y2="{gy:.1f}" '
            'stroke="#2d3757" stroke-width="0.6"/>'
            f'<text x="{margin_l - 10}" y="{gy + 4:.1f}" fill="#8ba3c7" '
            f'font-size="12" text-anchor="end">{dz:+.2f}</text>'
        )

    title = _t(
        lang,
        "Variazione quota (dopo - prima)",
        "Elevation change (after - before)",
    )
    y_label = _t(lang, "Δ quota (m)", "Δ elevation (m)")
    return f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="{width}" height="{height}"
     viewBox="0 0 {width} {height}" style="background:#12151e;">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#12151e"/>
  <text x="{margin_l}" y="24" fill="#f1f5f9" font-size="16" font-weight="700"
        font-family="Segoe UI,Arial,sans-serif">{title}</text>
  {''.join(grid)}
  <polygon points="{area_pts}" fill="rgba(79,115,196,0.22)"/>
  <line x1="{margin_l}" y1="{zero_y:.1f}" x2="{margin_l + plot_w}"
        y2="{zero_y:.1f}"
        stroke="#8ba3c7" stroke-width="1.2" stroke-dasharray="6,4"/>
  <polyline points="{path_pts}" fill="none" stroke="#4f73c4"
        stroke-width="1.8"/>
  <text x="20" y="{margin_t + plot_h / 2:.0f}" fill="#8ba3c7" font-size="12"
        transform="rotate(-90 20 {margin_t + plot_h / 2:.0f})"
        text-anchor="middle">{y_label}</text>
</svg>"""


# ──────────────────────────────────────────────────────────────────────
# HTML report for the vector comparison
# ──────────────────────────────────────────────────────────────────────


def _fmt(value, decimals=2, suffix=""):
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}{suffix}"


def generate_compare_html(result, lang="it", chart_html=""):
    L = lang
    before = result["before"]
    after = result["after"]
    parts = []

    main_title = _t(
        L, "Confronto prima / dopo lavori", "Before / after works comparison"
    )
    parts.append(f"<div class='section-title'>{main_title}</div>")
    tol_label = _t(
        L, "Tolleranza accoppiamento sezioni", "Section matching tolerance"
    )
    parts.append(
        "<div class='summary-card'>"
        f"<strong>{_t(L, 'Prima / Before', 'Before / Prima')}:</strong> "
        f"{before['name']} "
        f"<span class='badge badge-info'>{before['kind']}</span><br>"
        f"<strong>{_t(L, 'Dopo / After', 'After / Dopo')}:</strong> "
        f"{after['name']} "
        f"<span class='badge badge-info'>{after['kind']}</span><br>"
        f"{tol_label}: "
        f"{result['tolerance_m']:.1f} m"
        "</div>"
    )

    stats = result.get("profile_stats")
    if stats:
        sign_note = _t(
            L,
            "Δ positivo = terreno più alto dopo i lavori (riporto); Δ "
            "negativo = più basso (scavo).",
            "Positive Δ = ground higher after works (fill); negative Δ = "
            "lower (cut).",
        )
        elev_change_title = _t(
            L, "Variazione quote sul profilo", "Profile elevation change"
        )
        parts.append(
            "<div class='summary-card'>"
            f"<strong>{elev_change_title}</strong><br>"
            f"{_t(L, 'Punti confrontati', 'Compared stations')}: "
            f"<strong>{stats['n']}</strong> &nbsp;·&nbsp; "
            f"Δ {_t(L, 'media', 'mean')}: <strong>{stats['mean']:+.3f} "
            f"m</strong> &nbsp;·&nbsp; "
            f"Δ min: <strong>{stats['min']:+.3f} m</strong> &nbsp;·&nbsp; "
            f"Δ max: <strong>{stats['max']:+.3f} m</strong> &nbsp;·&nbsp; "
            f"RMS: <strong>{stats['rms']:.3f} m</strong><br>"
            f"<span style='color:#8ba3c7'>{sign_note}</span>"
            "</div>"
        )
    if chart_html:
        parts.append(f"<div class='chart-container'>{chart_html}</div>")

    totals = result.get("volume_totals")
    if totals:
        totals_title = _t(L, "Bilancio movimento terra", "Earthwork balance")
        balance_label = _t(
            L, "Bilancio (sterro - riporto)", "Balance (cut - fill)"
        )
        parts.append(f"<div class='section-title'>{totals_title}</div>")
        parts.append(
            "<table><tr>"
            f"<th></th><th>{_t(L, 'Prima', 'Before')}</th>"
            f"<th>{_t(L, 'Dopo', 'After')}</th><th>Δ</th></tr>"
            f"<tr><td><span class='badge "
            f"badge-cut'>{_t(L, 'Sterro', 'Cut')}</span></td>"
            f"<td>{_fmt(totals['cut_before'])} "
            f"m³</td><td>{_fmt(totals['cut_after'])} m³</td>"
            f"<td><strong>{totals['d_cut']:+,.2f} m³</strong></td></tr>"
            f"<tr><td><span class='badge "
            f"badge-fill'>{_t(L, 'Riporto', 'Fill')}</span></td>"
            f"<td>{_fmt(totals['fill_before'])} "
            f"m³</td><td>{_fmt(totals['fill_after'])} m³</td>"
            f"<td><strong>{totals['d_fill']:+,.2f} m³</strong></td></tr>"
            f"<tr><td>{balance_label}</td>"
            f"<td>{_fmt(totals['cut_before'] - totals['fill_before'])} m³</td>"
            f"<td>{_fmt(totals['cut_after'] - totals['fill_after'])} m³</td>"
            f"<td><strong>{totals['d_balance']:+,.2f} m³</strong></td></tr>"
            "</table>"
        )

    pairs = result.get("section_pairs")
    if pairs:
        pairs_title = _t(L, "Confronto sezioni", "Section comparison")
        parts.append(f"<div class='section-title'>{pairs_title}</div>")
        chainage_label = _t(L, "Prog. (m)", "Chainage (m)")
        rows = [
            "<table><tr>"
            f"<th>{_t(L, 'Sez.', 'Sec.')}</th><th>{chainage_label}</th>"
            f"<th>{_t(L, 'Quota min prima', 'Min elev before')}</th>"
            f"<th>{_t(L, 'Quota min dopo', 'Min elev after')}</th>"
            "<th>Δ quota min</th><th>Δ area (m²)</th>"
            f"<th>Δ {_t(L, 'sterro', 'cut')} (m²)</th>"
            f"<th>Δ {_t(L, 'riporto', 'fill')} (m²)</th></tr>"
        ]
        for p in pairs:
            d_qm = p["d_quota_min"]
            d_qm_txt = f"{d_qm:+.2f}" if d_qm is not None else "—"
            rows.append(
                f"<tr><td>S{int(p['sezione'] or 0):02d}</td>"
                f"<td>{p['prog_m']:.1f}</td>"
                f"<td>{_fmt(p['quota_min_before'])}</td>"
                f"<td>{_fmt(p['quota_min_after'])}</td>"
                f"<td><strong>{d_qm_txt}</strong></td>"
                f"<td>{_fmt(p['d_area_m2'])}</td>"
                f"<td>{_fmt(p['d_cut_m2'])}</td>"
                f"<td>{_fmt(p['d_fill_m2'])}</td></tr>"
            )
        rows.append("</table>")
        parts.append("".join(rows))
        if result["unmatched_before"] or result["unmatched_after"]:
            unmatched_msg = _t(
                L,
                f"Sezioni non accoppiate — prima: "
                f"{result['unmatched_before']}, "
                f"dopo: {result['unmatched_after']} (fuori tolleranza).",
                f"Unmatched sections — before: {result['unmatched_before']}, "
                f"after: {result['unmatched_after']} (outside tolerance).",
            )
            parts.append(f"<div class='summary-card'>{unmatched_msg}</div>")

    if not stats and not totals and not pairs:
        no_data_msg = _t(
            L,
            "Nessun dato confrontabile trovato nei due GeoPackage. Servono "
            "layer generati "
            "da questo plugin (campioni profilo, picchetti, centri sezione o "
            "tratte volumi).",
            "No comparable data found in the two GeoPackages. Layers "
            "generated by this plugin "
            "are required (profile samples, pickets, section centers or "
            "volume segments).",
        )
        parts.append(f"<div class='summary-card'>{no_data_msg}</div>")
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────
# DTM comparison (DEM of Difference) — deterministic raster math
# ──────────────────────────────────────────────────────────────────────


def compare_dtm_rasters(
    path_before, path_after, output_path, threshold_m=0.05, progress=None
):
    """Compute the difference raster (after - before) and change statistics.

    The "after" raster is resampled (bilinear) onto the grid of the
    "before" raster over their common extent. Volumes use the pixel area;
    for geographic CRS an approximate metric conversion is applied and
    flagged in the result. Returns a dict with statistics and the path of
    the GeoTIFF difference written to *output_path*.
    """
    from osgeo import gdal, osr
    import numpy as np

    gdal.UseExceptions()

    def _report(pct, msg=""):
        if progress:
            progress(percent=pct, message=msg)

    _report(5, "Apertura raster / Opening rasters")
    ds_before = gdal.Open(path_before)
    if ds_before is None:
        raise ValueError(
            f"Raster non leggibile / unreadable raster: {path_before}"
        )
    ds_after = gdal.Open(path_after)
    if ds_after is None:
        raise ValueError(
            f"Raster non leggibile / unreadable raster: {path_after}"
        )

    gt = ds_before.GetGeoTransform()
    proj = ds_before.GetProjection()
    if not proj:
        raise ValueError(
            "Il raster 'prima' non ha un CRS definito / 'before' raster has "
            "no CRS."
        )

    # Common extent in the CRS/grid of the "before" raster.
    def _extent(ds, transform=None):
        g = ds.GetGeoTransform()
        xs = [g[0], g[0] + g[1] * ds.RasterXSize]
        ys = [g[3], g[3] + g[5] * ds.RasterYSize]
        corners = [(x, y) for x in xs for y in ys]
        if transform:
            corners = [transform.TransformPoint(x, y)[:2] for x, y in corners]
        cx = [c[0] for c in corners]
        cy = [c[1] for c in corners]
        return min(cx), min(cy), max(cx), max(cy)

    srs_before = osr.SpatialReference(wkt=proj)
    proj_after = ds_after.GetProjection()
    transform = None
    if proj_after and not srs_before.IsSame(
        osr.SpatialReference(wkt=proj_after)
    ):
        srs_after = osr.SpatialReference(wkt=proj_after)
        try:
            srs_after.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            srs_before.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
        transform = osr.CoordinateTransformation(srs_after, srs_before)

    bx_min, by_min, bx_max, by_max = _extent(ds_before)
    ax_min, ay_min, ax_max, ay_max = _extent(ds_after, transform)
    ix_min, iy_min = max(bx_min, ax_min), max(by_min, ay_min)
    ix_max, iy_max = min(bx_max, ax_max), min(by_max, ay_max)
    if ix_min >= ix_max or iy_min >= iy_max:
        raise ValueError(
            "I due raster non si sovrappongono / the two rasters do not "
            "overlap."
        )

    _report(20, "Allineamento griglie / Aligning grids")
    warp_opts = dict(
        format="VRT",
        outputBounds=(ix_min, iy_min, ix_max, iy_max),
        xRes=abs(gt[1]),
        yRes=abs(gt[5]),
        dstSRS=proj,
        resampleAlg="bilinear",
    )
    vrt_after = gdal.Warp("", ds_after, **warp_opts)
    warp_opts["resampleAlg"] = "near"
    vrt_before = gdal.Warp("", ds_before, **warp_opts)

    band_b = vrt_before.GetRasterBand(1)
    band_a = vrt_after.GetRasterBand(1)
    nodata_b = band_b.GetNoDataValue()
    nodata_a = band_a.GetNoDataValue()
    cols = vrt_before.RasterXSize
    rows = vrt_before.RasterYSize

    out_gt = vrt_before.GetGeoTransform()
    cell_w, cell_h = abs(out_gt[1]), abs(out_gt[5])
    geographic = bool(srs_before.IsGeographic())
    if geographic:
        lat_center = (iy_min + iy_max) / 2.0
        pixel_area_m2 = (
            cell_w * 111320.0 * math.cos(math.radians(lat_center))
        ) * (cell_h * 110540.0)
    else:
        pixel_area_m2 = cell_w * cell_h

    if not output_path.lower().endswith((".tif", ".tiff")):
        output_path += ".tif"
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    nodata_out = -9999.0
    driver = gdal.GetDriverByName("GTiff")
    ds_out = driver.Create(
        output_path,
        cols,
        rows,
        1,
        gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    ds_out.SetGeoTransform(out_gt)
    ds_out.SetProjection(proj)
    band_out = ds_out.GetRasterBand(1)
    band_out.SetNoDataValue(nodata_out)

    # Block-wise difference so very large DTMs stay within memory.
    stats = {
        "n_valid": 0,
        "sum_dz": 0.0,
        "sum_sq": 0.0,
        "min": None,
        "max": None,
        "vol_fill_m3": 0.0,
        "vol_cut_m3": 0.0,
        "n_fill": 0,
        "n_cut": 0,
    }
    block_rows = max(1, min(rows, int(4e6 / max(cols, 1))))
    for row0 in range(0, rows, block_rows):
        nrows = min(block_rows, rows - row0)
        arr_b = band_b.ReadAsArray(0, row0, cols, nrows).astype("float64")
        arr_a = band_a.ReadAsArray(0, row0, cols, nrows).astype("float64")
        mask = np.isfinite(arr_b) & np.isfinite(arr_a)
        if nodata_b is not None:
            mask &= arr_b != nodata_b
        if nodata_a is not None:
            mask &= arr_a != nodata_a
        dz = arr_a - arr_b
        dz_out = np.where(mask, dz, nodata_out).astype("float32")
        band_out.WriteArray(dz_out, 0, row0)

        valid = dz[mask]
        if valid.size:
            stats["n_valid"] += int(valid.size)
            stats["sum_dz"] += float(valid.sum())
            stats["sum_sq"] += float((valid**2).sum())
            v_min, v_max = float(valid.min()), float(valid.max())
            stats["min"] = (
                v_min if stats["min"] is None else min(stats["min"], v_min)
            )
            stats["max"] = (
                v_max if stats["max"] is None else max(stats["max"], v_max)
            )
            pos = valid[valid > threshold_m]
            neg = valid[valid < -threshold_m]
            stats["vol_fill_m3"] += float(pos.sum()) * pixel_area_m2
            stats["vol_cut_m3"] += float(-neg.sum()) * pixel_area_m2
            stats["n_fill"] += int(pos.size)
            stats["n_cut"] += int(neg.size)
        _report(
            20 + int(70 * (row0 + nrows) / rows),
            "Calcolo differenza / Computing difference",
        )

    band_out.FlushCache()
    ds_out = None
    vrt_before = vrt_after = ds_before = ds_after = None

    n = stats["n_valid"]
    mean = stats["sum_dz"] / n if n else 0.0
    rms = math.sqrt(stats["sum_sq"] / n) if n else 0.0
    _report(100, "Completato / Done")
    return {
        "diff_path": output_path,
        "n_valid": n,
        "mean_dz": mean,
        "min_dz": stats["min"],
        "max_dz": stats["max"],
        "rms_dz": rms,
        "threshold_m": threshold_m,
        "pixel_area_m2": pixel_area_m2,
        "vol_fill_m3": stats["vol_fill_m3"],
        "vol_cut_m3": stats["vol_cut_m3"],
        "area_fill_m2": stats["n_fill"] * pixel_area_m2,
        "area_cut_m2": stats["n_cut"] * pixel_area_m2,
        "geographic_approx": geographic,
        "cols": cols,
        "rows": rows,
    }


def generate_dtm_compare_html(result, path_before, path_after, lang="it"):
    L = lang
    parts = []
    dtm_title = _t(
        L,
        "Confronto DTM (differenza dopo - prima)",
        "DTM comparison (after - before difference)",
    )
    parts.append(f"<div class='section-title'>{dtm_title}</div>")
    parts.append(
        "<div class='summary-card'>"
        f"<strong>{_t(L, 'Prima', 'Before')}:</strong> "
        f"{os.path.basename(path_before)}<br>"
        f"<strong>{_t(L, 'Dopo', 'After')}:</strong> "
        f"{os.path.basename(path_after)}<br>"
        f"<strong>{_t(L, 'Raster differenza', 'Difference raster')}:</strong> "
        f"{result['diff_path']}<br>"
        f"{_t(L, 'Celle valide', 'Valid cells')}: {result['n_valid']:,} "
        f"({result['cols']} × {result['rows']}) &nbsp;·&nbsp; "
        f"{_t(L, 'Soglia variazione', 'Change threshold')}: "
        f"±{result['threshold_m']:.2f} m"
        "</div>"
    )
    net_balance = result["vol_fill_m3"] - result["vol_cut_m3"]
    cut_vol_label = _t(
        L, "Volume scavo (Δ < -soglia)", "Cut volume (Δ < -threshold)"
    )
    fill_vol_label = _t(
        L, "Volume riporto (Δ > +soglia)", "Fill volume (Δ > +threshold)"
    )
    net_balance_label = _t(
        L, "Bilancio netto (riporto - scavo)", "Net balance (fill - cut)"
    )
    rows = [
        f"<table><tr><th></th><th>{_t(L, 'Valore', 'Value')}</th></tr>",
        f"<tr><td>Δ {_t(L, 'quota media', 'mean elevation')}</td>"
        f"<td><strong>{result['mean_dz']:+.3f} m</strong></td></tr>",
        f"<tr><td>Δ min "
        f"({_t(L, 'massimo abbassamento', 'largest lowering')})</td>"
        f"<td>{_fmt(result['min_dz'], 3)} m</td></tr>",
        f"<tr><td>Δ max "
        f"({_t(L, 'massimo innalzamento', 'largest raising')})</td>"
        f"<td>{_fmt(result['max_dz'], 3)} m</td></tr>",
        f"<tr><td>RMS</td><td>{result['rms_dz']:.3f} m</td></tr>",
        "<tr><td><span class='badge badge-cut'>"
        f"{cut_vol_label}</span></td>"
        f"<td><strong>{result['vol_cut_m3']:,.1f} m³</strong> "
        f"{_t(L, 'su', 'over')} {result['area_cut_m2']:,.0f} m²</td></tr>",
        "<tr><td><span class='badge badge-fill'>"
        f"{fill_vol_label}</span></td>"
        f"<td><strong>{result['vol_fill_m3']:,.1f} m³</strong> "
        f"{_t(L, 'su', 'over')} {result['area_fill_m2']:,.0f} m²</td></tr>",
        f"<tr><td>{net_balance_label}</td>"
        f"<td><strong>{net_balance:+,.1f} m³</strong></td></tr>",
        "</table>",
    ]
    parts.append("".join(rows))
    if result.get("geographic_approx"):
        crs_warning = _t(
            L,
            "Attenzione: CRS geografico — aree e volumi sono approssimati. "
            "Per valori "
            "accurati usare DTM in CRS metrico (es. UTM).",
            "Warning: geographic CRS — areas and volumes are approximate. For "
            "accurate "
            "figures use DTMs in a metric CRS (e.g. UTM).",
        )
        parts.append(
            f"<div class='summary-card' "
            f"style='color:#f5a623'>{crs_warning}</div>"
        )
    style_note = _t(
        L,
        "Il raster differenza è caricato in QGIS con scala di colori "
        "divergente: "
        "rosso = abbassamento (scavo), blu = innalzamento (riporto). Calcolo "
        "deterministico GDAL/NumPy, nessuna AI.",
        "The difference raster is loaded in QGIS with a diverging color ramp: "
        "red = lowering (cut), blue = raising (fill). Deterministic "
        "GDAL/NumPy "
        "computation, no AI.",
    )
    parts.append(f"<div class='summary-card'>{style_note}</div>")
    return "".join(parts)
