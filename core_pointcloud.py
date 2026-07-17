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
core_pointcloud.py — Minimal, dependency-free LAS 1.2 point cloud writer.

Only the Python standard library (struct) is used: no laspy/PDAL/GDAL
Python bindings are required, so the plugin has no extra runtime
dependency beyond QGIS itself. The resulting .las file uses Point Data
Record Format 0 and can be opened directly by QGIS (native "pdal"
point cloud provider), CloudCompare, QGIS, or any LAS 1.2 reader.
"""

import datetime
import math
import struct

_LAS_HEADER_SIZE = 227
_POINT_RECORD_FORMAT = 0
_POINT_RECORD_LENGTH = 20
_XYZ_SCALE = 0.001  # millimetre precision, plenty for terrain profiles


def write_las_point_cloud(points, filepath):
    """
    Write a minimal, valid LAS 1.2 file (point format 0) from a flat list
    of (x, y, z) tuples in the project CRS (or any consistent CRS —
    LAS coordinates are plain doubles, no CRS is embedded).

    Non-finite coordinates (None/NaN/inf) are silently skipped.

    Returns the number of points written. Raises ValueError if fewer
    than 1 valid point is available.
    """
    clean = []
    for x, y, z in points:
        if x is None or y is None or z is None:
            continue
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            continue
        clean.append((float(x), float(y), float(z)))

    if not clean:
        raise ValueError("No valid 3D points to write to the point cloud.")

    xs = [p[0] for p in clean]
    ys = [p[1] for p in clean]
    zs = [p[2] for p in clean]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)

    # Offsets at the minimum keep the scaled integers small and positive.
    off_x, off_y, off_z = min_x, min_y, min_z

    now = datetime.datetime.now()
    day_of_year = now.timetuple().tm_yday

    system_id = _padded_ascii("OTHER", 32)
    gen_software = _padded_ascii("ProfiliSezioniComuni", 32)

    header = struct.pack(
        "<4sHH" "16s" "BB" "32s32s" "HH" "H" "II" "BH" "I" "5I"
        "ddd" "ddd" "dddddd",
        b"LASF",
        0,  # File Source ID
        0,  # Global Encoding
        b"\x00" * 16,  # Project ID GUID (unused)
        1,
        2,  # Version 1.2
        system_id,
        gen_software,
        day_of_year,
        now.year,
        _LAS_HEADER_SIZE,  # Header size
        _LAS_HEADER_SIZE,  # Offset to point data (no VLRs)
        0,  # Number of variable length records
        _POINT_RECORD_FORMAT,
        _POINT_RECORD_LENGTH,
        len(clean),  # Number of point records (legacy)
        len(clean), 0, 0, 0, 0,  # Number of points by return
        _XYZ_SCALE, _XYZ_SCALE, _XYZ_SCALE,
        off_x, off_y, off_z,
        max_x, min_x,
        max_y, min_y,
        max_z, min_z,
    )
    if len(header) != _LAS_HEADER_SIZE:
        raise RuntimeError(
            "Internal LAS header size mismatch: {0} bytes.".format(
                len(header)
            )
        )

    with open(filepath, "wb") as fh:
        fh.write(header)
        for x, y, z in clean:
            ix = int(round((x - off_x) / _XYZ_SCALE))
            iy = int(round((y - off_y) / _XYZ_SCALE))
            iz = int(round((z - off_z) / _XYZ_SCALE))
            fh.write(
                struct.pack(
                    "<iiiHBBbBH",
                    _clamp_int32(ix),
                    _clamp_int32(iy),
                    _clamp_int32(iz),
                    0,  # Intensity
                    0b00001001,  # return number=1, number of returns=1
                    2,  # Classification: 2 = ground
                    0,  # Scan angle rank
                    0,  # User data
                    0,  # Point source ID
                )
            )

    return len(clean)


def _clamp_int32(value):
    return max(-2147483648, min(2147483647, value))


def _padded_ascii(text, length):
    raw = text.encode("ascii", "replace")[:length]
    return raw + b"\x00" * (length - len(raw))
