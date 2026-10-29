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

from .textscan import DIGITS, LETTERS, is_decimal, is_digits, is_int, is_single_digit, take_while

MAX_SHEETS = 8
DEFAULT_STEPS_PER_MM = 400
RAW_MIN = -3999  # Z_BABYSTEP_MIN on MK3/MK3S/MK2.5
RAW_MAX = 0  # Z_BABYSTEP_MAX
RAW_EMPTY = -1  # 0xFFFF read as int16
LABEL_MAX = 7

LABEL_CHARS = LETTERS + DIGITS + " "

UNINIT_PREFIX = "Sheet "
UNINIT_SUFFIX = " NOT INITIALIZED"
FIRMWARE_MARKER = "Prusa-Firmware "
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

        header_sheet = parse_uninit_header(text)
        if header_sheet is not None:
            self._pending_uninit = header_sheet
            return None

        data = parse_sheet_data(text)
        if data:
            sheet = data["sheet"]
            initialized = True
            if sheet is None:
                if self._pending_uninit is None:
                    return None
                sheet = self._pending_uninit
                initialized = False
            self._pending_uninit = None
            return (
                "sheet",
                {
                    "index": sheet,
                    "initialized": initialized and data["raw"] != RAW_EMPTY,
                    "raw": data["raw"],
                    "z_reported": data["z"],
                    "name": _clean_label(data["label"]),
                    "active": data["active"],
                },
            )

        self._pending_uninit = None
        if text.startswith("Invalid sheet ID"):
            return ("error", text)
        if text == "Z VALUE OUT OF RANGE":
            return ("error", "Z value out of range")
        return None


def parse_uninit_header(text):
    """``Sheet 5 NOT INITIALIZED`` -> 5, anything else -> None."""
    if not (text.startswith(UNINIT_PREFIX) and text.endswith(UNINIT_SUFFIX)):
        return None
    number = text[len(UNINIT_PREFIX) : -len(UNINIT_SUFFIX)]
    return int(number) if is_single_digit(number) else None


def parse_sheet_data(text):
    """Split a sheet report into its fields, or return None if it isn't one.

    ``Sheet 3 Z-0.8250 R-330 LTextur1 B60 P35 A1``. The ``Sheet 3`` part is
    missing on the data line after ``NOT INITIALIZED``. The name may contain
    spaces, so the fixed fields are peeled off both ends: ``Z`` and ``R`` from
    the front, ``B``, ``P`` and ``A`` from the back. What is left after ``L``
    is the name.
    """
    sheet = None
    if text.startswith("Sheet "):
        number, _, text = text[len("Sheet ") :].partition(" ")
        if not is_single_digit(number):
            return None
        sheet = int(number)

    pieces = text.rsplit(" ", 3)  # front, then B.., P.., A..
    if len(pieces) != 4:
        return None
    front, bed, pinda, active = pieces
    if not (_is_field(bed, "B", is_digits) and _is_field(pinda, "P", is_digits)):
        return None
    if active not in ("A0", "A1"):
        return None

    pieces = front.split(" ", 2)  # Z.., R.., L<name>
    if len(pieces) != 3:
        return None
    z, raw, label = pieces
    if not (_is_field(z, "Z", is_decimal) and _is_field(raw, "R", is_int)):
        return None
    if not label.startswith("L"):
        return None

    return {
        "sheet": sheet,
        "z": float(z[1:]),
        "raw": int(raw[1:]),
        "label": label[1:],
        "active": active == "A1",
    }


def _is_field(token, letter, check_value):
    """True when ``token`` is ``letter`` followed by a value ``check_value`` accepts."""
    return token.startswith(letter) and check_value(token[1:])


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
    if not 1 <= len(label) <= LABEL_MAX or any(c not in LABEL_CHARS for c in label):
        raise M850Error("Name must be 1 to 7 letters, digits or spaces")
    if "Z" in label:
        raise M850Error(
            "The firmware would read an upper-case Z in a name as a new Live Z. Use a lower-case z."
        )
    if _reads_as_activate(label):
        raise M850Error(
            "The firmware would read an upper-case A followed by a digit as 'make active'. "
            "Use a lower-case a."
        )
    return label


def _reads_as_activate(label):
    """True if the firmware would find ``A`` followed by a number in the label.

    The firmware reads the number after a parameter letter with strtol, which
    skips leading spaces, so ``A 1`` counts too.
    """
    for i, char in enumerate(label):
        if char == "A" and is_single_digit(label[i + 1 :].lstrip(" ")[:1]):
            return True
    return False


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
    version = firmware_version(name)
    if version is None:
        return "prusa" in name.lower(), None, False
    return True, version, version >= MIN_FIRMWARE


def firmware_version(name):
    """``... Prusa-Firmware 3.14.1+8237 ...`` -> (3, 14, 1), or None."""
    start = name.find(FIRMWARE_MARKER)
    while start != -1:
        number_start = start + len(FIRMWARE_MARKER)
        number_end = take_while(name, DIGITS + ".", number_start)
        parts = name[number_start:number_end].split(".")
        if len(parts) >= 3 and all(is_digits(part) for part in parts[:3]):
            return tuple(int(part) for part in parts[:3])
        start = name.find(FIRMWARE_MARKER, start + 1)
    return None
