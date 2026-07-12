# -*- coding: utf-8 -*-
# Copyright (C) 2024 Dott. Sarino Alfonso Grande <info@sinocloud.it>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

"""
core_raster_download.py — Raster source metadata and area download helpers.
"""

import datetime
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)


TINITALY_ROOT = "http://tinitaly.pi.ingv.it/"
TINITALY_WCS_URL = "http://tinitaly.pi.ingv.it/TINItaly_1_1/wcs"
TINITALY_WCS_CAPABILITIES = (
    "http://tinitaly.pi.ingv.it/TINItaly_1_1/wcs?"
    "service=WCS&request=getCapabilities"
)
TINITALY_DOWNLOAD_PAGE = "http://tinitaly.pi.ingv.it/Download_Area1_1.html"
TINITALY_WCS_URI = (
    "cache=AlwaysCache&crs=EPSG:4326&format=GeoTIFF&"
    "identifier=TINItaly_DEM&url={0}"
).format(TINITALY_WCS_URL)
TINITALY_TILE_RE = re.compile(
    r"""href=["'](?P<href>data_1\.1/(?P<code>[ew]\d{5}_s10)/(?P=code)\.zip)["']""",
    re.IGNORECASE,
)
TINITALY_CRS = QgsCoordinateReferenceSystem("EPSG:32632")

ZENODO_RECORD_REQUESTED = "https://zenodo.org/records/18872933"
ZENODO_RECORD_LATEST = "https://zenodo.org/records/18921767"
ZENODO_FILE_URL = "https://zenodo.org/api/records/18921767/files/HRDTM5m/content"
ZENODO_VSICURL = "/vsicurl?max_retry=3&retry_delay=2&list_dir=no&url={0}".format(
    urllib.parse.quote(ZENODO_FILE_URL, safe=":/")
)
ZENODO_VSICURL_LEGACY = "/vsicurl/{0}".format(ZENODO_FILE_URL)

ISTAT_ADMIN_BOUNDARIES_PAGE = (
    "https://www.istat.it/statistiche-per-temi/focus/"
    "informazioni-territoriali-e-cartografiche/unita-amministrative/"
)


RASTER_SOURCES = {
    "tinitaly": {
        "label_it": "TINITALY 1.1 DEM 10 m (ZIP ufficiali INGV)",
        "label_en": "TINITALY 1.1 DEM 10 m (official INGV ZIP tiles)",
        "short": "TINITALY 1.1",
        "resolution": "10 m",
        "license": "CC BY 4.0",
        "url": TINITALY_DOWNLOAD_PAGE,
        "service_url": TINITALY_WCS_CAPABILITIES,
        "citation": (
            "Tarquini S., Isola I., Favalli M., Battistini A., Dotta G. (2023). "
            "TINITALY, a digital elevation model of Italy with a 10 meters cell "
            "size (Version 1.1). Istituto Nazionale di Geofisica e Vulcanologia "
            "(INGV). https://doi.org/10.13127/tinitaly/1.1"
        ),
        "note": (
            "The official download area provides tiled ZIP files. The plugin "
            "selects the ZIP tiles intersecting the drawn area, builds a temporary "
            "VRT and clips the requested GeoTIFF. WCS loading remains optional."
        ),
    },
    "hrdtm5m": {
        "label_it": "HR-DTM-5m Italia (Zenodo/CNR, 5 m)",
        "label_en": "Italy HR-DTM-5m (Zenodo/CNR, 5 m)",
        "short": "HR-DTM-5m",
        "resolution": "5 m",
        "license": "CC BY 4.0",
        "url": ZENODO_RECORD_LATEST,
        "requested_url": ZENODO_RECORD_REQUESTED,
        "file_url": ZENODO_FILE_URL,
        "size": "22.09 GB",
        "citation": (
            "Panza M., Muto M., Rossi M., Alvioli M., Iovine G., Marchesini I. "
            "(2026). 5m Resolution Digital Terrain Model for Italy, version 1.2. "
            "Zenodo. https://doi.org/10.5281/zenodo.18921767"
        ),
        "note": (
            "The user supplied Zenodo record 18872933 (version 1.1); the latest "
            "published version checked for this plugin is record 18921767 "
            "(version 1.2, 2026-03-09). GDAL /vsicurl range reads and cache are "
            "enabled to avoid downloading the full archive when the server and "
            "file layout allow windowed reads."
        ),
    },
}


def source_label(source_key, lang="it"):
    meta = RASTER_SOURCES.get(source_key) or RASTER_SOURCES["tinitaly"]
    return meta.get("label_en" if lang == "en" else "label_it", meta["short"])


def source_info_text(source_key, lang="it"):
    meta = RASTER_SOURCES.get(source_key) or RASTER_SOURCES["tinitaly"]
    if lang == "en":
        return (
            "{label} | Resolution: {res} | License: {lic}\n"
            "Source: {url}\nCitation: {cit}\nNote: {note}"
        ).format(
            label=meta.get("label_en"),
            res=meta.get("resolution"),
            lic=meta.get("license"),
            url=meta.get("url"),
            cit=meta.get("citation"),
            note=meta.get("note"),
        )
    return (
        "{label} | Risoluzione: {res} | Licenza: {lic}\n"
        "Fonte: {url}\nCitazione: {cit}\nNota: {note}"
    ).format(
        label=meta.get("label_it"),
        res=meta.get("resolution"),
        lic=meta.get("license"),
        url=meta.get("url"),
        cit=meta.get("citation"),
        note=meta.get("note"),
    )


def bbox_wgs84_from_points(area_points):
    if not area_points:
        raise ValueError("No download area has been drawn.")

    project = QgsProject.instance()
    crs_wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    source_crs = project.crs()
    try:
        xform = QgsCoordinateTransform(source_crs, crs_wgs, project.transformContext())
    except Exception:
        xform = QgsCoordinateTransform(source_crs, crs_wgs, project)

    xs = []
    ys = []
    for p in area_points:
        pt = xform.transform(p)
        xs.append(pt.x())
        ys.append(pt.y())

    if not xs or not ys:
        raise ValueError("The drawn download area is empty.")

    rect = QgsRectangle(min(xs), min(ys), max(xs), max(ys))
    if rect.isNull() or rect.isEmpty():
        raise ValueError("The drawn download area is empty.")
    return rect


def bbox_text(rect):
    return (
        "WGS84 bbox: {xmin:.7f}, {ymin:.7f}, {xmax:.7f}, {ymax:.7f}"
    ).format(
        xmin=rect.xMinimum(),
        ymin=rect.yMinimum(),
        xmax=rect.xMaximum(),
        ymax=rect.yMaximum(),
    )


def processing_extent(rect):
    return "{xmin},{xmax},{ymin},{ymax} [EPSG:4326]".format(
        xmin=rect.xMinimum(),
        xmax=rect.xMaximum(),
        ymin=rect.yMinimum(),
        ymax=rect.yMaximum(),
    )


def _emit_progress(progress_callback, percent, message, **kwargs):
    if progress_callback is None:
        return True
    result = progress_callback(percent=percent, message=message, **kwargs)
    return result is not False


def _format_tile_code(tile_code):
    return tile_code.replace("_s10", "")


def _tinitaly_tile_x(prefix, suffix):
    value = int(suffix)
    if prefix.lower() == "e":
        return 1000000 + value * 10000
    if value == 10:
        return 1000000
    return value * 10000


def tinitaly_tile_extent(tile_code):
    match = re.match(r"^([ew])(\d{3})(\d{2})_s10$", tile_code, re.IGNORECASE)
    if not match:
        raise ValueError("Invalid TINITALY tile code: {0}".format(tile_code))
    prefix, northing_code, easting_code = match.groups()
    x0 = _tinitaly_tile_x(prefix, easting_code)
    y0 = int(northing_code) * 10000
    return QgsRectangle(x0 - 50, y0 - 50, x0 + 50050, y0 + 50000)


def _rect_intersects(a, b):
    x_overlap = a.xMinimum() < b.xMaximum() and a.xMaximum() > b.xMinimum()
    y_overlap = a.yMinimum() < b.yMaximum() and a.yMaximum() > b.yMinimum()
    return x_overlap and y_overlap


def _transform_rect(rect, source_crs, target_crs):
    project = QgsProject.instance()
    try:
        xform = QgsCoordinateTransform(source_crs, target_crs, project.transformContext())
    except Exception:
        xform = QgsCoordinateTransform(source_crs, target_crs, project)
    points = [
        xform.transform(rect.xMinimum(), rect.yMinimum()),
        xform.transform(rect.xMinimum(), rect.yMaximum()),
        xform.transform(rect.xMaximum(), rect.yMinimum()),
        xform.transform(rect.xMaximum(), rect.yMaximum()),
    ]
    xs = [p.x() for p in points]
    ys = [p.y() for p in points]
    return QgsRectangle(min(xs), min(ys), max(xs), max(ys))


def tinitaly_tile_index():
    req = urllib.request.Request(
        _ensure_http_url(TINITALY_DOWNLOAD_PAGE),
        headers={"User-Agent": "ProfiliSezioniComuni/1.2 (+info@sinocloud.it)"},
    )
    with urllib.request.urlopen(req, timeout=45) as response:  # nosec B310 - schema http(s) validato sopra
        html = response.read().decode("utf-8", "replace")

    tiles = []
    seen = set()
    for match in TINITALY_TILE_RE.finditer(html):
        code = match.group("code")
        if code in seen:
            continue
        seen.add(code)
        href = match.group("href")
        tiles.append({
            "code": code,
            "url": urllib.parse.urljoin(TINITALY_ROOT, href),
            "extent": tinitaly_tile_extent(code),
        })
    if not tiles:
        raise RuntimeError("No TINITALY tile links were found on the official download page.")
    return tiles


def tinitaly_tiles_for_area(area_points):
    rect_wgs84 = bbox_wgs84_from_points(area_points)
    rect_tinitaly = _transform_rect(
        rect_wgs84,
        QgsCoordinateReferenceSystem("EPSG:4326"),
        TINITALY_CRS,
    )
    return [
        tile for tile in tinitaly_tile_index()
        if _rect_intersects(tile["extent"], rect_tinitaly)
    ]


def _configure_gdal_network():
    from osgeo import gdal

    options = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_MULTIRANGE": "YES",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "CPL_VSIL_CURL_USE_HEAD": "YES",
        "CPL_VSIL_CURL_CHUNK_SIZE": "1048576",
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": "134217728",
    }
    for key, value in options.items():
        gdal.SetConfigOption(key, value)
    return gdal


def _gdal_callback(progress_callback, start, end, label):
    started = time.time()

    def _callback(complete, message, _data):
        elapsed = max(time.time() - started, 0.001)
        percent = start + (end - start) * max(0.0, min(float(complete or 0), 1.0))
        eta = None
        if complete and complete > 0:
            eta = elapsed * (1.0 - complete) / complete
        keep_going = _emit_progress(
            progress_callback,
            percent,
            message or label,
            elapsed=elapsed,
            eta=eta,
        )
        return 1 if keep_going else 0

    return _callback


def _clip_raster_with_gdal(input_raster, rect, rect_srs, output_path, progress_callback, start=0, end=100):
    gdal = _configure_gdal_network()
    creation_options = ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]
    options = gdal.TranslateOptions(
        format="GTiff",
        projWin=[
            rect.xMinimum(),
            rect.yMaximum(),
            rect.xMaximum(),
            rect.yMinimum(),
        ],
        projWinSRS=rect_srs,
        noData=-9999,
        creationOptions=creation_options,
        callback=_gdal_callback(progress_callback, start, end, "Ritaglio raster / Raster clipping"),
    )
    result = gdal.Translate(output_path, input_raster, options=options)
    if result is None:
        raise RuntimeError("Raster clipping failed or was cancelled.")
    result = None
    return output_path


def _clip_remote_raster(source_key, rect, output_path, progress_callback):
    source_paths = (
        [ZENODO_VSICURL, ZENODO_VSICURL_LEGACY]
        if source_key == "hrdtm5m" else [input_for_source(source_key)]
    )
    last_error = None
    for source_path in source_paths:
        try:
            return _clip_raster_with_gdal(
                source_path,
                rect,
                "EPSG:4326",
                output_path,
                progress_callback,
                0,
                100,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError("Could not clip remote raster: {0}".format(last_error))


def _ensure_http_url(url):
    """Allow only http(s) URLs — blocks file://, ftp:// and similar schemes."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"URL non consentito / URL scheme not allowed: {url}")
    return url


def _download_file(url, output_path, progress_callback, start, end, label):
    headers = {"User-Agent": "ProfiliSezioniComuni/1.2 (+info@sinocloud.it)"}
    req = urllib.request.Request(_ensure_http_url(url), headers=headers)
    tmp_path = output_path + ".part"
    done = 0
    started = time.time()
    with urllib.request.urlopen(req, timeout=60) as response:  # nosec B310 - schema validato sopra
        total = int(response.headers.get("Content-Length") or 0)
        if os.path.exists(output_path):
            size = os.path.getsize(output_path)
            if size > 0 and (not total or size == total):
                _emit_progress(
                    progress_callback,
                    end,
                    "{0}: cache OK".format(label),
                    transferred=size,
                    total=size,
                    elapsed=0,
                    eta=0,
                )
                return output_path
        with open(tmp_path, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                elapsed = max(time.time() - started, 0.001)
                speed = done / elapsed
                eta = (total - done) / speed if total and speed > 0 else None
                fraction = (done / total) if total else 0
                percent = start + (end - start) * max(0.0, min(fraction, 1.0))
                if not _emit_progress(
                    progress_callback,
                    percent,
                    label,
                    transferred=done,
                    total=total,
                    speed=speed,
                    elapsed=elapsed,
                    eta=eta,
                ):
                    raise RuntimeError("Download cancelled.")
    os.replace(tmp_path, output_path)
    return output_path


def _tinitaly_zip_vsi_path(zip_path, tile_code):
    return "/vsizip/{0}/{1}/{1}.tif".format(zip_path, tile_code)


def _download_tinitaly_tiles_area(area_points, output_path, load_to_project, progress_callback):
    rect_wgs84 = bbox_wgs84_from_points(area_points)
    rect_tinitaly = _transform_rect(
        rect_wgs84,
        QgsCoordinateReferenceSystem("EPSG:4326"),
        TINITALY_CRS,
    )
    tiles = [
        tile for tile in tinitaly_tile_index()
        if _rect_intersects(tile["extent"], rect_tinitaly)
    ]
    if not tiles:
        raise RuntimeError(
            "No TINITALY tile intersects the drawn area. Check that the area is inside Italy."
        )

    cache_dir = os.path.join(os.path.dirname(output_path), "tinitaly_1_1_tiles")
    os.makedirs(cache_dir, exist_ok=True)

    _emit_progress(
        progress_callback,
        1,
        "Trovati {0} tile TINITALY / Found {0} TINITALY tiles".format(len(tiles)),
    )
    vsi_inputs = []
    download_start = 2.0
    download_end = 72.0
    per_tile = (download_end - download_start) / max(len(tiles), 1)
    for idx, tile in enumerate(tiles):
        tile_start = download_start + idx * per_tile
        tile_end = download_start + (idx + 1) * per_tile
        zip_name = os.path.basename(tile["url"])
        zip_path = os.path.join(cache_dir, zip_name)
        label = "TINITALY {0}/{1}: {2}".format(
            idx + 1,
            len(tiles),
            _format_tile_code(tile["code"]),
        )
        _download_file(tile["url"], zip_path, progress_callback, tile_start, tile_end, label)
        vsi_inputs.append(_tinitaly_zip_vsi_path(zip_path, tile["code"]))

    gdal = _configure_gdal_network()
    vrt_fd, vrt_path = tempfile.mkstemp(prefix="tinitaly_1_1_", suffix=".vrt")
    os.close(vrt_fd)
    try:
        vrt = gdal.BuildVRT(vrt_path, vsi_inputs)
        if vrt is None:
            raise RuntimeError("Could not build temporary VRT from TINITALY ZIP tiles.")
        vrt = None
        _clip_raster_with_gdal(
            vrt_path,
            rect_tinitaly,
            "EPSG:32632",
            output_path,
            progress_callback,
            72,
            100,
        )
    finally:
        if os.path.exists(vrt_path):
            try:
                os.remove(vrt_path)
            except OSError:
                pass

    write_download_receipt(output_path, "tinitaly", rect_wgs84)
    layer = None
    if load_to_project:
        layer = QgsRasterLayer(output_path, "{0} - area".format(RASTER_SOURCES["tinitaly"]["short"]))
    return output_path, rect_wgs84, layer


def create_download_area_layer(area_points, layer_name="Area download raster"):
    project = QgsProject.instance()
    crs_auth = project.crs().authid() or "EPSG:4326"
    vl = QgsVectorLayer(
        "Polygon?crs={0}&field=source:string(80)&field=created:string(32)".format(crs_auth),
        layer_name,
        "memory",
    )
    if not vl.isValid():
        raise RuntimeError("Could not create the download area layer.")

    feat = QgsFeature()
    feat.setGeometry(QgsGeometry.fromPolygonXY([area_points]))
    feat.setAttributes(["raster-download", datetime.datetime.now().isoformat(timespec="seconds")])
    pr = vl.dataProvider()
    pr.addFeature(feat)
    vl.updateExtents()

    try:
        symbol = QgsFillSymbol.createSimple({
            "color": "245,166,35,35",
            "outline_color": "#f5a623",
            "outline_width": "0.8",
        })
        vl.setRenderer(QgsSingleSymbolRenderer(symbol))
    except Exception:
        pass

    project.addMapLayer(vl)
    return vl


def tinitaly_wcs_layer():
    layer = QgsRasterLayer(TINITALY_WCS_URI, "TINITALY 1.1 DEM WCS", "wcs")
    if not layer.isValid():
        raise RuntimeError(
            "TINITALY WCS layer is not valid in this QGIS/GDAL installation. "
            "Use the official download page: {0}".format(TINITALY_DOWNLOAD_PAGE)
        )
    return layer


def input_for_source(source_key):
    if source_key == "tinitaly":
        return tinitaly_wcs_layer()
    if source_key == "hrdtm5m":
        return ZENODO_VSICURL
    raise ValueError("Unknown raster source: {0}".format(source_key))


def download_raster_area(source_key, area_points, output_path, load_to_project=True, progress_callback=None):
    if not output_path:
        raise ValueError("No output raster path selected.")
    if not output_path.lower().endswith((".tif", ".tiff")):
        output_path = output_path + ".tif"

    rect = bbox_wgs84_from_points(area_points)

    if source_key == "tinitaly":
        return _download_tinitaly_tiles_area(
            area_points,
            output_path,
            load_to_project,
            progress_callback,
        )

    if source_key == "hrdtm5m":
        _emit_progress(
            progress_callback,
            0,
            "Avvio ritaglio remoto Zenodo / Starting Zenodo remote clipping",
        )
        final_path = _clip_remote_raster(source_key, rect, output_path, progress_callback)
    else:
        input_raster = input_for_source(source_key)
        try:
            import processing
        except Exception as exc:
            raise RuntimeError("QGIS Processing is required for raster clipping: {0}".format(exc))

        params = {
            "INPUT": input_raster,
            "PROJWIN": processing_extent(rect),
            "OVERCRS": False,
            "NODATA": None,
            "OPTIONS": "COMPRESS=DEFLATE|TILED=YES|BIGTIFF=IF_SAFER",
            "DATA_TYPE": 0,
            "EXTRA": "",
            "OUTPUT": output_path,
        }
        result = processing.run("gdal:cliprasterbyextent", params)
        final_path = result.get("OUTPUT") or output_path

    _emit_progress(progress_callback, 100, "Scrittura ricevuta / Writing source receipt")

    write_download_receipt(final_path, source_key, rect)

    layer = None
    if load_to_project:
        name = "{0} - area".format(RASTER_SOURCES[source_key]["short"])
        layer = QgsRasterLayer(final_path, name)
    return final_path, rect, layer


def write_download_receipt(raster_path, source_key, rect):
    meta = RASTER_SOURCES[source_key]
    receipt_path = os.path.splitext(raster_path)[0] + "_fonte_licenza.txt"
    lines = [
        "Raster download receipt / Ricevuta download raster",
        "Created / Creato: {0}".format(datetime.datetime.now().isoformat(timespec="seconds")),
        "",
        "Source / Fonte: {0}".format(meta.get("label_it")),
        "Resolution / Risoluzione: {0}".format(meta.get("resolution")),
        "License / Licenza: {0}".format(meta.get("license")),
        "URL: {0}".format(meta.get("url")),
        "Citation / Citazione:",
        meta.get("citation"),
        "",
        bbox_text(rect),
        "",
        "Technical note / Nota tecnica:",
        meta.get("note", ""),
    ]
    if meta.get("requested_url"):
        lines.extend([
            "",
            "Requested record / Record richiesto: {0}".format(meta.get("requested_url")),
            "Latest record used / Record latest usato: {0}".format(meta.get("url")),
        ])
    with open(receipt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return receipt_path
