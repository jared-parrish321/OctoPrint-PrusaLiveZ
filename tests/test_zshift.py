import os

import pytest

from octoprint_prusa_livez import ladder
from octoprint_prusa_livez.zshift import Ladder, ZShifter, parse_words

DATA = os.path.join(os.path.dirname(__file__), "data", "first_layer_ladder.gcode")


def stream(path):
    """The commands OctoPrint would send for a file: comments stripped, blanks skipped."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            code = line.split(";", 1)[0].strip()
            if code:
                yield code


class Printer:
    """Tiny absolute-XYZ / relative-E motion model that records extrusions."""

    def __init__(self):
        self.x = self.y = self.z = None
        self.abs_e = False
        self.e_abs = 0.0
        self.extrusions = []  # (x0, y0, x1, y1, z, e)
        self.offset_z = 0.0  # G92 shifts

    def run(self, commands):
        for cmd in commands:
            self.step(cmd)
        return self

    def step(self, cmd):
        command, w = parse_words(cmd)
        if command == "M82":
            self.abs_e = True
        elif command == "M83":
            self.abs_e = False
        elif command == "G92":
            if "E" in w:
                self.e_abs = w["E"]
            if "Z" in w and self.z is not None:
                # logical position redefined; physical stays
                self.offset_z = self.z_phys() - w["Z"]
                self.z = w["Z"]
        elif command == "G28":
            self.x = self.y = None
            self.z = None
            self.offset_z = 0.0
        elif command in ("G0", "G1"):
            nx, ny, nz = w.get("X", self.x), w.get("Y", self.y), w.get("Z", self.z)
            e = 0.0
            if "E" in w:
                e = w["E"] - self.e_abs if self.abs_e else w["E"]
                if self.abs_e:
                    self.e_abs = w["E"]
            if e > 0 and ("X" in w or "Y" in w):
                self.extrusions.append((self.x, self.y, nx, ny, round(nz + self.offset_z, 6), e))
            self.x, self.y, self.z = nx, ny, nz

    def z_phys(self):
        return self.z + self.offset_z


def shifted(path, shifter):
    out = []
    for cmd in stream(path):
        out.extend(shifter.process(cmd))
    return out


@pytest.fixture(scope="module")
def info():
    return ladder.analyze_file(DATA)


def make_ladder(info, offsets):
    return Ladder(info["boundaries"], offsets, info["layer_z"], info["bbox"])


def test_zero_offsets_change_nothing(info):
    shifter = ZShifter()
    shifter.set_ladder(make_ladder(info, [0.0] * 9))
    original = list(stream(DATA))
    assert shifted(DATA, shifter) == original


def test_nudge_zero_changes_nothing():
    original = list(stream(DATA))
    assert shifted(DATA, ZShifter()) == original


def test_ladder_bands_print_at_their_own_height(info):
    offsets = ladder.default_offsets(9, 0.01)
    lad = make_ladder(info, offsets)
    shifter = ZShifter()
    shifter.set_ladder(lad)
    out = shifted(DATA, shifter)

    before = Printer().run(stream(DATA))
    after = Printer().run(out)

    # same filament overall
    assert sum(e for *_, e in after.extrusions) == pytest.approx(sum(e for *_, e in before.extrusions), abs=1e-4)

    checked = 0
    for x0, y0, x1, y1, z, e in after.extrusions:
        if x0 is None or y0 is None:
            continue  # first move after homing, start point unknown to the model
        mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
        if lad.inside(mid_x, mid_y):
            want = 0.2 + offsets[lad.band(mid_y)]
            assert z == pytest.approx(want, abs=1e-6), (x0, y0, x1, y1, z)
            # no piece crosses a band edge
            assert lad.band(min(y0, y1) + 1e-4) == lad.band(max(y0, y1) - 1e-4)
            checked += 1
        else:
            # intro line at the front stays at the normal first-layer height
            assert z == pytest.approx(0.2, abs=1e-6)
    assert checked > 400

    # every band was printed at its height
    seen = {round(z, 4) for *_, z, e in after.extrusions}
    for off in offsets:
        assert round(0.2 + off, 4) in seen


def test_ladder_pieces_join_up(info):
    """Split pieces still trace the original path end to end."""
    shifter = ZShifter()
    shifter.set_ladder(make_ladder(info, ladder.default_offsets(9, 0.01)))
    after = Printer().run(shifted(DATA, shifter))
    before = Printer().run(stream(DATA))
    ends_before = {(round(x1, 3), round(y1, 3)) for _, _, x1, y1, _, _ in before.extrusions}
    ends_after = {(round(x1, 3), round(y1, 3)) for _, _, x1, y1, _, _ in after.extrusions}
    assert ends_before <= ends_after


def test_ladder_end_gcode_untouched(info):
    shifter = ZShifter()
    shifter.set_ladder(make_ladder(info, [0.04] * 9))
    out = shifted(DATA, shifter)
    assert "G1 Z1.2 F720" in out
    assert "G1 Z49.2 F720" in out


def test_nudge_rewrites_absolute_z_and_inserts_before_extrusion():
    s = ZShifter()
    assert s.process("G90") == ["G90"]
    assert s.process("M83") == ["M83"]
    assert s.process("G1 Z0.2 F720") == ["G1 Z0.2 F720"]
    s.set_nudge(-0.03)
    assert s.process("G1 X10 Y10 F9000") == ["G1 X10 Y10 F9000"]  # travel: no change yet
    assert s.process("G1 X20 Y10 E1.5") == ["G1 Z0.17", "G1 X20 Y10 E1.5"]
    assert s.process("G1 X30 Y10 E1.5") == ["G1 X30 Y10 E1.5"]
    assert s.process("G1 Z0.4 F720") == ["G1 Z0.37 F720"]
    s.set_nudge(0.0)
    assert s.process("G1 X40 Y10 E1") == ["G1 Z0.4", "G1 X40 Y10 E1"]


def test_nudge_relative_mode_passes_through():
    s = ZShifter()
    for cmd in ("G90", "G1 Z1"):
        s.process(cmd)
    s.set_nudge(0.05)
    assert s.process("G91") == ["G91"]
    assert s.process("G1 Z2 E0") == ["G1 Z2 E0"]
    assert s.process("G90") == ["G90"]
    assert s.file_z == 3
    assert s.process("G1 Z3.2") == ["G1 Z3.25"]


def test_g92_z_keeps_applied_offset():
    s = ZShifter()
    for cmd in ("G90", "M83", "G1 Z0.2"):
        s.process(cmd)
    s.set_nudge(0.05)
    s.process("G1 X1 Y1 E1")  # applies the nudge
    assert s.process("G92 Z5") == ["G92 Z5.05"]
    assert s.file_z == 5


def test_homing_resets():
    s = ZShifter()
    for cmd in ("G90", "M83", "G1 Z0.2"):
        s.process(cmd)
    s.set_nudge(0.05)
    s.process("G1 X1 Y1 E1")
    assert s.process("G28") == ["G28"]
    assert s.emitted == 0.0
    assert s.process("G1 Z0.3") == ["G1 Z0.35"]


def test_mark_unknown_resends_z_after_resume():
    s = ZShifter()
    for cmd in ("G90", "M83", "G1 Z0.2", "G1 X1 Y1 E1"):
        s.process(cmd)
    s.mark_unknown()
    assert s.process("G1 X2 Y1 E1") == ["G1 Z0.2", "G1 X2 Y1 E1"]


def test_absolute_extrusion_split(info):
    lad = Ladder([10.0], [-0.02, 0.02], 0.2, (0, 0, 20, 20))
    s = ZShifter()
    s.set_ladder(lad)
    for cmd in ("G90", "M82", "G92 E0", "G1 Z0.2", "G1 X5 Y5"):
        s.process(cmd)
    out = s.process("G1 X5 Y15 E10 F1200")
    assert out == ["G1 Z0.18", "G1 X5 Y10 E5 F1200", "G1 Z0.22", "G1 X5 Y15 E10"]
    assert s.process("G1 X5 Y16 E11") == ["G1 X5 Y16 E11"]
