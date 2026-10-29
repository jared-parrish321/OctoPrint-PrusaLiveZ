"""Shift the Z of streamed G-code, for mid-print nudges and the calibration ladder.

The Prusa MK3 firmware has no host babystep (no M290), so instead of moving the
nozzle the printer's way, this rewrites the job's moves on their way out:
every absolute Z gets the current offset added, and when the offset changes,
a Z move is inserted before the next extrusion.

Pure Python, no OctoPrint imports. ``ZShifter.process(line)`` takes one G-code
command (comments already stripped, as OctoPrint sends them) and returns the
list of commands to send instead.
"""

from .textscan import LETTERS, number_end

EPS = 1e-6


def fmt(value):
    """Format a coordinate like a slicer does: up to 5 decimals, no trailing zeros."""
    text = "{:.5f}".format(value).rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def parse_words(line):
    """Return (command, {letter: float}) for a G-code line."""
    parts = line.strip().split(None, 1)
    if not parts:
        return None, {}
    command = parts[0].upper()
    words = {}
    if len(parts) > 1:
        words = _scan_words(parts[1])
    return command, words


def _scan_words(text):
    """Read ``X10 Y-2.5 E.8`` into ``{"X": 10.0, "Y": -2.5, "E": 0.8}``.

    Walks the text one character at a time, the way the firmware's own parser
    does: a letter, optional spaces, then a number. A letter with no number
    after it is skipped. A repeated letter keeps the last value.
    """
    words = {}
    pos = 0
    while pos < len(text):
        letter = text[pos]
        if letter in LETTERS:
            value_start = pos + 1
            while value_start < len(text) and text[value_start].isspace():
                value_start += 1
            value_end = number_end(text, value_start)
            if value_end > value_start:
                words[letter.upper()] = float(text[value_start:value_end])
                pos = value_end
                continue
        pos += 1
    return words


def build_move(command, words, order="XYZEF"):
    out = [command]
    for letter in order:
        if letter in words:
            out.append(letter + fmt(words[letter]))
    for letter, value in words.items():
        if letter not in order:
            out.append(letter + fmt(value))
    return " ".join(out)


class Ladder:
    """Calibration ladder: an offset per Y band of a single-layer print."""

    def __init__(self, boundaries, offsets, layer_z, bbox, margin=1.0):
        if len(offsets) != len(boundaries) + 1:
            raise ValueError("need one offset per band")
        self.boundaries = sorted(boundaries)
        self.offsets = list(offsets)
        self.layer_z = layer_z
        x0, y0, x1, y1 = bbox
        self.bbox = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)

    def inside(self, x, y):
        if x is None or y is None:
            return False
        x0, y0, x1, y1 = self.bbox
        return x0 <= x <= x1 and y0 <= y <= y1

    def band(self, y):
        for i, b in enumerate(self.boundaries):
            if y < b:
                return i
        return len(self.boundaries)

    def offset_at(self, y):
        return self.offsets[self.band(y)]

    def crossings(self, y0, y1):
        """Boundaries strictly between y0 and y1, in travel order."""
        lo, hi = min(y0, y1), max(y0, y1)
        hits = [b for b in self.boundaries if lo + EPS < b < hi - EPS]
        return hits if y1 >= y0 else list(reversed(hits))


class ZShifter:
    def __init__(self):
        self.nudge = 0.0
        self.ladder = None
        self.reset_state()

    def reset_state(self):
        self.absolute_xyz = True
        self.absolute_e = False
        self.x = self.y = None
        self.file_z = None  # Z as the file intends it
        self.emitted = 0.0  # offset the printer currently has applied
        self.last_e = 0.0  # for absolute extrusion

    # -- control -------------------------------------------------------

    def set_nudge(self, offset):
        self.nudge = offset

    def set_ladder(self, ladder):
        self.ladder = ladder

    def mark_unknown(self):
        """After a pause script may have moved Z: re-send Z before next extrusion."""
        self.emitted = None

    def desired(self):
        """Offset wanted at the current position."""
        if self.ladder is not None:
            if (
                self.file_z is not None
                and abs(self.file_z - self.ladder.layer_z) < 1e-3
                and self.ladder.inside(self.x, self.y)
            ):
                return self.ladder.offset_at(self.y)
            return 0.0
        return self.nudge

    def physical_z(self, file_z, offset):
        return file_z + offset

    # -- processing ----------------------------------------------------

    def process(self, line):
        command, words = parse_words(line)
        if command is None:
            return [line]

        if command == "G90":
            self.absolute_xyz = True
            self.absolute_e = True
            return [line]
        if command == "G91":
            self.absolute_xyz = False
            self.absolute_e = False
            return [line]
        if command == "M82":
            self.absolute_e = True
            return [line]
        if command == "M83":
            self.absolute_e = False
            return [line]
        if command == "G28":
            # Homing: the printer's position is true again, nothing applied.
            self.x = self.y = self.file_z = None
            self.emitted = 0.0
            return [line]
        if command == "G92":
            return [self._g92(words, line)]
        if command in ("G0", "G1"):
            return self._move(command, words, line)
        if command in ("G2", "G3"):
            return self._arc(command, words, line)
        return [line]

    def _g92(self, words, line):
        if "E" in words:
            self.last_e = words["E"]
        if "X" in words:
            self.x = words["X"]
        if "Y" in words:
            self.y = words["Y"]
        if "Z" in words:
            self.file_z = words["Z"]
            # keep what is applied: tell the printer it is that much higher/lower
            applied = self.emitted or 0.0
            if abs(applied) < 1e-9:
                return line
            words = dict(words)
            words["Z"] = words["Z"] + applied
            return build_move("G92", words, order="XYZE")
        return line

    def _update_xy(self, words):
        if self.absolute_xyz:
            if "X" in words:
                self.x = words["X"]
            if "Y" in words:
                self.y = words["Y"]
        else:
            if "X" in words and self.x is not None:
                self.x += words["X"]
            if "Y" in words and self.y is not None:
                self.y += words["Y"]

    def _e_amount(self, words):
        if "E" not in words:
            return 0.0
        return words["E"] - self.last_e if self.absolute_e else words["E"]

    def _z_move(self, offset):
        return "G1 Z{}".format(fmt(self.physical_z(self.file_z, offset)))

    def _move(self, command, words, line):
        if not self.absolute_xyz:
            # relative Z keeps whatever offset is applied
            if "Z" in words and self.file_z is not None:
                self.file_z += words["Z"]
            self._update_xy(words)
            if "E" in words and self.absolute_e:
                self.last_e = words["E"]
            return [line]

        start = (self.x, self.y)
        if "Z" in words:
            self.file_z = words["Z"]
        self._update_xy(words)
        extruding = self._e_amount(words) > 0 and ("X" in words or "Y" in words)

        if (
            extruding
            and self.ladder is not None
            and start[0] is not None
            and start[1] is not None
            and "Z" not in words
        ):
            out = self._split_for_ladder(command, words, start, line)
        else:
            want = self.desired()
            if "Z" in words and self.file_z is not None:
                self.emitted = want
                if abs(want) < 1e-9:
                    out = [line]
                else:
                    words = dict(words)
                    words["Z"] = self.physical_z(self.file_z, want)
                    out = [build_move(command, words)]
            elif extruding and self.file_z is not None and _changed(self.emitted, want):
                out = [self._z_move(want), line]
                self.emitted = want
            else:
                out = [line]

        if "E" in words and self.absolute_e:
            self.last_e = words["E"]
        return out

    def _split_for_ladder(self, command, words, start, line):
        """Cut an extrusion at band edges and put each piece at its band's Z."""
        x0, y0 = start
        x1, y1 = self.x, self.y
        cuts = []
        if abs(y1 - y0) > EPS:
            cuts = [(b - y0) / (y1 - y0) for b in self.ladder.crossings(y0, y1)]
        cuts.append(1.0)

        # (end_t, offset) per piece, neighbours with the same offset merged
        pieces = []
        prev_t = 0.0
        for t in cuts:
            mid_t = (prev_t + t) / 2.0
            want = self._ladder_offset(x0 + (x1 - x0) * mid_t, y0 + (y1 - y0) * mid_t)
            if pieces and abs(pieces[-1][1] - want) < 1e-9:
                pieces[-1] = (t, want)
            else:
                pieces.append((t, want))
            prev_t = t

        if len(pieces) == 1:
            want = pieces[0][1]
            out = []
            if self.file_z is not None and _changed(self.emitted, want):
                out.append(self._z_move(want))
                self.emitted = want
            out.append(line)
            return out

        e_total = self._e_amount(words)
        out = []
        prev_t = 0.0
        e_done = 0.0
        for i, (t, want) in enumerate(pieces):
            if self.file_z is not None and _changed(self.emitted, want):
                out.append(self._z_move(want))
                self.emitted = want
            e_piece = e_total * (t - prev_t)
            piece = {"X": x0 + (x1 - x0) * t, "Y": y0 + (y1 - y0) * t}
            piece["E"] = self.last_e + e_done + e_piece if self.absolute_e else e_piece
            if i == 0 and "F" in words:
                piece["F"] = words["F"]
            out.append(build_move(command, piece))
            e_done += e_piece
            prev_t = t
        return out

    def _ladder_offset(self, x, y):
        if (
            self.file_z is not None
            and abs(self.file_z - self.ladder.layer_z) < 1e-3
            and self.ladder.inside(x, y)
        ):
            return self.ladder.offset_at(y)
        return 0.0

    def _arc(self, command, words, line):
        if not self.absolute_xyz:
            return [line]
        if "Z" in words:
            self.file_z = words["Z"]
        self._update_xy(words)
        want = self.desired()
        if "Z" in words and self.file_z is not None:
            self.emitted = want
            if abs(want) < 1e-9:
                out = [line]
            else:
                words = dict(words)
                words["Z"] = self.physical_z(self.file_z, want)
                out = [build_move(command, words, order="XYZIJREF")]
        elif self._e_amount(words) > 0 and self.file_z is not None and _changed(self.emitted, want):
            out = [self._z_move(want), line]
            self.emitted = want
        else:
            out = [line]
        if "E" in words and self.absolute_e:
            self.last_e = words["E"]
        return out


def _changed(emitted, want):
    return emitted is None or abs(emitted - want) > 1e-9
