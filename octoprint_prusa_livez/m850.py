"""Parsing and building of Prusa firmware M850 (sheet parameters) commands.

Pure functions only, no OctoPrint imports, so the rules can be unit tested.

Facts from Prusa-Firmware 3.13.0+ (Firmware/Marlin_main.cpp, case 850):

* ``M850 S<n>`` prints ``Sheet <n> Z<mm> R<raw> L<label> B<bed> P<pinda> A<0|1>``.
  An uninitialized sheet prints ``Sheet <n> NOT INITIALIZED`` first and the
  data on the next line, which then starts with `` Z``.
* Live Z is stored as int16 microsteps (``R``). ``Z`` is ``R / steps_per_mm``.
* 0xFFFF (``R`` = -1) marks an uninitialized sheet, so -1 must never be written.
* Writing ``Z`` truncates ``z * steps_per_mm`` toward zero, so we send the
  midpoint of the wanted step to land exactly on it.
* Parameters are found with ``strchr`` on the whole command line, so a label
  is also searched for parameter letters. An upper-case ``Z`` in a label
  would overwrite Live Z, and ``A`` followed by a number would change the
  active sheet. ``B`` and ``P`` only touch fields the firmware never uses,
  ``S`` and ``L`` are always found earlier in the line. ``L`` must be the last
  parameter, because the label runs to the end of the line.
"""

import re

MAX_SHEETS = 8
DEFAULT_STEPS_PER_MM = 400
RAW_MIN = -3999  # Z_BABYSTEP_MIN on MK3/MK3S/MK2.5
RAW_MAX = 0  # Z_BABYSTEP_MAX
RAW_EMPTY = -1  # 0xFFFF read as int16
LABEL_MAX = 7

LABEL_RE = re.compile(r"^[A-Za-z0-9 ]{1,7}$")
# what the firmware would read as a parameter inside a label
_LABEL_Z_RE = re.compile(r"Z")
_LABEL_A_RE = re.compile(r"A\s*[+-]?\d")

_HEADER_UNINIT_RE = re.compile(r"^Sheet (\d) NOT INITIALIZED$")
_DATA_RE = re.compile(
    r"^(?:Sheet (?P<sheet>\d) )?Z(?P<z>-?\d+(?:\.\d+)?) R(?P<raw>-?\d+) "
    r"L(?P<label>.*) B(?P<bed>\d+) P(?P<pinda>\d+) A(?P<active>[01])$"
)
_INVALID_SHEET_RE = re.compile(r"^Invalid sheet ID")
_OUT_OF_RANGE_RE = re.compile(r"^Z VALUE OUT OF RANGE$")

_FW_RE = re.compile(r"Prusa-Firmware (\d+)\.(\d+)\.(\d+)")
MIN_FIRMWARE = (3, 13, 0)


class M850Error(ValueError):
    pass


class ReplyParser:
    """Feeds received serial lines, returns parsed events.

    ``feed(line)`` returns one of:
      * ``("sheet", dict)`` for a complete sheet report
      * ``("error", message)`` for an M850 error reply
      * ``None`` for anything else
    """

    def __init__(self):
        self._pending_uninit = None

    def feed(self, line):
        if line is None:
            return None
        text = line.strip()
        if not text:
            return None

        m = _HEADER_UNINIT_RE.match(text)
        if m:
            self._pending_uninit = int(m.group(1))
            return None

        m = _DATA_RE.match(text)
        if m:
            sheet = m.group("sheet")
            initialized = True
            if sheet is None:
                if self._pending_uninit is None:
                    return None
                sheet = self._pending_uninit
                initialized = False
            self._pending_uninit = None
            raw = int(m.group("raw"))
            return (
                "sheet",
                {
                    "index": int(sheet),
                    "initialized": initialized and raw != RAW_EMPTY,
                    "raw": raw,
                    "z_reported": float(m.group("z")),
                    "name": _clean_label(m.group("label")),
                    "active": m.group("active") == "1",
                },
            )

        self._pending_uninit = None
        if _INVALID_SHEET_RE.match(text):
            return ("error", text)
        if _OUT_OF_RANGE_RE.match(text):
            return ("error", "Z value out of range")
        return None


def _clean_label(label):
    # The firmware prints up to 7 raw EEPROM bytes; drop padding and junk.
    return "".join(c for c in label if 32 <= ord(c) < 127).rstrip()


def learn_steps_per_mm(raw, z_reported):
    """Steps per mm implied by a read reply, or None if it can't be told.

    A read prints Z as raw / steps to 4 decimals. After a write the firmware
    echoes the value it was sent instead, which doesn't divide evenly, so only
    accept a candidate that reproduces the printed Z.
    """
    if raw in (0, RAW_EMPTY) or z_reported == 0 or abs(raw) < 100:
        return None
    steps = int(round(raw / z_reported))
    if steps <= 0:
        return None
    if abs(raw / float(steps) - z_reported) > 0.00006:
        return None
    return steps


def mm_to_raw(z_mm, steps_per_mm=DEFAULT_STEPS_PER_MM):
    return int(round(z_mm * steps_per_mm))


def raw_to_mm(raw, steps_per_mm=DEFAULT_STEPS_PER_MM):
    return raw / float(steps_per_mm)


def snap_raw(raw, previous_raw=None):
    """Skip the 'uninitialized' marker (-1) in the direction of travel."""
    if raw != RAW_EMPTY:
        return raw
    if previous_raw is not None and previous_raw < RAW_EMPTY:
        # coming up from below, e.g. -0.005 -> -0.0025: go on to 0
        return 0
    return -2


def validate_sheet(sheet):
    try:
        sheet = int(sheet)
    except (TypeError, ValueError):
        raise M850Error("Sheet must be a number from 0 to 7")
    if not 0 <= sheet < MAX_SHEETS:
        raise M850Error("Sheet must be a number from 0 to 7")
    return sheet


def target_raw(z_mm, previous_raw=None, steps_per_mm=DEFAULT_STEPS_PER_MM):
    """Validate a Live Z value in mm and return the raw value to store."""
    try:
        z_mm = float(z_mm)
    except (TypeError, ValueError):
        raise M850Error("Live Z must be a number")
    if z_mm != z_mm:  # NaN
        raise M850Error("Live Z must be a number")
    raw = mm_to_raw(z_mm, steps_per_mm)
    if raw < RAW_MIN or raw > RAW_MAX:
        raise M850Error(
            "Live Z must be between {:.4f} and 0 mm".format(raw_to_mm(RAW_MIN, steps_per_mm))
        )
    return snap_raw(raw, previous_raw)


def validate_label(label):
    if label is None:
        raise M850Error("Name is empty")
    label = str(label).rstrip()
    if not LABEL_RE.match(label):
        raise M850Error("Name must be 1 to 7 letters, digits or spaces")
    if _LABEL_Z_RE.search(label):
        raise M850Error(
            "The firmware would read an upper-case Z in a name as a new Live Z. Use a lower-case z."
        )
    if _LABEL_A_RE.search(label):
        raise M850Error(
            "The firmware would read an upper-case A followed by a digit as 'make active'. "
            "Use a lower-case a."
        )
    return label


def cmd_read(sheet):
    return "M850 S{}".format(validate_sheet(sheet))


def cmd_set_z(sheet, raw, steps_per_mm=DEFAULT_STEPS_PER_MM):
    sheet = validate_sheet(sheet)
    if raw == RAW_EMPTY or raw < RAW_MIN or raw > RAW_MAX:
        raise M850Error("Raw Live Z {} is not allowed".format(raw))
    # firmware: zraw = (int16)(z * steps), truncated toward zero. Send the
    # middle of the step below raw so float rounding can't land one step off.
    value = (raw - 0.5) / float(steps_per_mm) if raw < 0 else 0.0
    return "M850 S{} Z{:.6f}".format(sheet, value)


def cmd_activate(sheet):
    return "M850 S{} A1".format(validate_sheet(sheet))


def cmd_rename(sheet, label):
    return "M850 S{} L{}".format(validate_sheet(sheet), validate_label(label))


def parse_firmware(name):
    """Return (is_prusa, version_tuple or None, supports_m850)."""
    if not name:
        return False, None, False
    m = _FW_RE.search(name)
    if not m:
        return "prusa" in name.lower(), None, False
    version = tuple(int(x) for x in m.groups())
    return True, version, version >= MIN_FIRMWARE
