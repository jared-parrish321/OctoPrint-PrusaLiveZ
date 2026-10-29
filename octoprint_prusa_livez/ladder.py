"""Recognise a first-layer calibration print and find its bands.

The print is one layer: a rectangular pad with notches cut into its left edge.
The notches split the pad into bands along Y. Each notch shows up in the
external perimeter as a pair of horizontal segments that start at the pad's
left edge and run a few mm inward. The middle of each pair is a band edge.

Pure Python, no OctoPrint imports.
"""

import re

from .zshift import parse_words

OBJECT_HINT = "FirstLayerCalibration"
# Slicer comments. These use re.VERBOSE, so a real space is written as [ ].

# "; printing object FirstLayerCalibration-FirstLayer.stl id:0 copy 0"
_OBJECT_RE = re.compile(
    r"""
    ^ ; [ ] printing [ ] object [ ]
    (.+?)                               # the object name (as short as possible)
    (?: [ ] id: \d+ [ ] copy [ ] \d+ )?   # added by newer PrusaSlicer versions
    \s* $
    """,
    re.VERBOSE,
)

# ";TYPE:External perimeter"
_TYPE_RE = re.compile(
    r"""
    ^ ;TYPE: (.+) $
    """,
    re.VERBOSE,
)

# ";Z:0.2", the height of the current layer
_Z_RE = re.compile(
    r"""
    ^ ;Z: ( [-\d.]+ )
    """,
    re.VERBOSE,
)

MIN_NOTCH_DEPTH = 2.0  # mm a notch must reach into the pad
MAX_NOTCH_WIDTH = 4.0  # mm between the two sides of one notch
EDGE_TOL = 0.05


def default_offsets(bands, step=0.01):
    """Spread centred on 0: 9 bands, 0.01 step -> -0.04 ... +0.04."""
    mid = (bands - 1) / 2.0
    return [round((i - mid) * step, 4) for i in range(bands)]


def analyze(lines, max_layers=1):
    """Return a dict describing the ladder, or a dict with ``reason`` when not one.

    ``lines`` is any iterable of raw G-code lines (comments included).
    """
    objects = set()
    layer_changes = 0
    layer_z = None
    absolute = True
    absolute_e = False
    last_e = 0.0
    x = y = z = None
    current_type = None
    perimeter = []  # (x0, y0, x1, y1)
    extrude_pts = []
    extrude_z = set()

    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith(";"):
            m = _OBJECT_RE.match(line)
            if m:
                objects.add(m.group(1).strip())
            elif line.startswith(";LAYER_CHANGE"):
                layer_changes += 1
                if layer_changes > max_layers:
                    break  # a normal multi-layer print: stop reading early
            else:
                m = _TYPE_RE.match(line)
                if m:
                    current_type = m.group(1).strip()
                else:
                    m = _Z_RE.match(line)
                    if m and layer_z is None:
                        layer_z = float(m.group(1))
            continue

        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        command, words = parse_words(code)
        if command == "G90":
            absolute = True
        elif command == "G91":
            absolute = False
        elif command == "M82":
            absolute_e = True
        elif command == "M83":
            absolute_e = False
        elif command == "G92":
            if "E" in words:
                last_e = words["E"]
        elif command in ("G0", "G1"):
            if not absolute:
                continue
            nx = words.get("X", x)
            ny = words.get("Y", y)
            nz = words.get("Z", z)
            if "E" in words:
                e = words["E"] - last_e if absolute_e else words["E"]
                if absolute_e:
                    last_e = words["E"]
            else:
                e = 0.0
            if (
                e > 0
                and current_type not in (None, "Custom")
                and x is not None
                and y is not None
                and ("X" in words or "Y" in words)
            ):
                extrude_pts.append((x, y))
                extrude_pts.append((nx, ny))
                if nz is not None:
                    extrude_z.add(round(nz, 4))
                if current_type == "External perimeter":
                    perimeter.append((x, y, nx, ny))
            x, y, z = nx, ny, nz

    result = {
        "objects": sorted(objects),
        "layers": layer_changes,
        "detected": False,
        "named": any(OBJECT_HINT.lower() in o.lower() for o in objects),
    }
    if not extrude_pts:
        result["reason"] = "No object extrusion found"
        return result
    if layer_changes > max_layers or len(extrude_z) != 1:
        result["reason"] = "Not a single-layer print"
        return result
    if layer_z is None:
        layer_z = next(iter(extrude_z))

    xs = [p[0] for p in extrude_pts]
    ys = [p[1] for p in extrude_pts]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    width = bbox[2] - bbox[0]

    # horizontal perimeter segments touching the left edge, shorter than half the pad
    sides = []
    for x0, y0, x1, y1 in perimeter:
        if abs(y1 - y0) > EDGE_TOL:
            continue
        left = min(x0, x1)
        length = abs(x1 - x0)
        if abs(left - bbox[0]) <= EDGE_TOL and MIN_NOTCH_DEPTH <= length < width / 2.0:
            sides.append(y0)
    sides.sort()

    notches = []
    i = 0
    while i + 1 < len(sides):
        if sides[i + 1] - sides[i] <= MAX_NOTCH_WIDTH:
            notches.append(round((sides[i] + sides[i + 1]) / 2.0, 3))
            i += 2
        else:
            i += 1

    result.update(
        {
            "layer_z": layer_z,
            "bbox": [round(v, 3) for v in bbox],
            "boundaries": notches,
            "bands": len(notches) + 1,
        }
    )
    if not notches:
        result["reason"] = "No notches found on the left edge"
        return result
    result["detected"] = True
    return result


def analyze_file(path, **kwargs):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return analyze(handle, **kwargs)
