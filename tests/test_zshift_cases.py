"""Z rewriting for the less common G-code: arcs, relative moves, G92 and absolute E."""

import os

from octoprint_prusa_livez import ladder
from octoprint_prusa_livez.zshift import Ladder, ZShifter, build_move, fmt

LADDER_FILE = os.path.join(os.path.dirname(__file__), "data", "first_layer_ladder.gcode")


def nudged(offset, *setup):
    shifter = ZShifter()
    for line in setup:
        shifter.process(line)
    shifter.set_nudge(offset)
    return shifter


def test_format_and_build_helpers():
    assert fmt(-0.0) == "0" and fmt(0.200001) == "0.2" and fmt(1e-7) == "0"
    assert build_move("G1", {"F": 1200, "X": 1, "Q": 2}) == "G1 X1 F1200 Q2"


def test_blank_and_other_commands_pass_through():
    shifter = nudged(-0.02)
    assert shifter.process("") == [""]
    assert shifter.process("M104 S215") == ["M104 S215"]


def test_arc_with_z_is_shifted():
    shifter = nudged(-0.02, "G1 Z0.2")
    assert shifter.process("G2 X10 Y10 Z0.4 I5 J5 E1") == ["G2 X10 Y10 Z0.38 I5 J5 E1"]


def test_arc_with_zero_offset_is_unchanged():
    shifter = nudged(0.0, "G1 Z0.2")
    assert shifter.process("G3 X10 Y10 Z0.4 I5 J5") == ["G3 X10 Y10 Z0.4 I5 J5"]


def test_extruding_arc_after_a_nudge_gets_a_z_move_first():
    shifter = nudged(0.0, "G1 Z0.2")
    shifter.set_nudge(0.01)
    assert shifter.process("G2 X10 Y10 I5 J5 E0.5") == ["G1 Z0.21", "G2 X10 Y10 I5 J5 E0.5"]
    assert shifter.process("G2 X0 Y0 I-5 J-5 E0.5") == ["G2 X0 Y0 I-5 J-5 E0.5"]


def test_relative_arc_and_moves_pass_through():
    shifter = nudged(-0.02, "G1 X0 Y0 Z0.2", "G91")
    assert shifter.process("G2 X1 Y1 I1 J0 E0.1") == ["G2 X1 Y1 I1 J0 E0.1"]
    assert shifter.process("G1 X1 Y1 Z0.2 E0.1") == ["G1 X1 Y1 Z0.2 E0.1"]
    assert (shifter.x, shifter.y, round(shifter.file_z, 3)) == (1.0, 1.0, 0.4)


def test_g90_switches_extrusion_to_absolute_too():
    shifter = nudged(0.0, "M83", "G91")
    shifter.process("G90")
    assert shifter.absolute_xyz is True and shifter.absolute_e is True  # as Marlin does


def test_homing_resets_the_applied_offset():
    shifter = nudged(-0.02, "G1 Z0.2")
    shifter.process("G1 X1 Y1 E0.1")
    assert shifter.emitted == -0.02
    shifter.process("G28")
    assert shifter.emitted == 0.0 and shifter.x is None and shifter.file_z is None


def test_g92_z_keeps_the_applied_offset():
    shifter = nudged(-0.02, "G1 Z0.2")
    shifter.process("G1 X1 Y1 E0.1")
    assert shifter.process("G92 Z5") == ["G92 Z4.98"]
    assert shifter.process("G92 X1 Y2 E0") == ["G92 X1 Y2 E0"]
    assert (shifter.x, shifter.y) == (1.0, 2.0)


def test_absolute_extrusion_is_tracked():
    shifter = nudged(0.0, "M82", "G92 E0", "G1 X0 Y0 Z0.2")
    shifter.set_nudge(0.01)
    assert shifter.process("G1 X10 Y0 E1.5") == ["G1 Z0.21", "G1 X10 Y0 E1.5"]
    assert shifter.last_e == 1.5
    assert shifter.process("G1 X20 Y0 E1.5") == ["G1 X20 Y0 E1.5"]  # travel, no Z move


def test_resume_resends_z_before_the_next_extrusion():
    shifter = nudged(-0.02, "G1 Z0.2")
    shifter.process("G1 X1 Y1 E0.1")
    shifter.mark_unknown()
    assert shifter.process("G1 X2 Y2 E0.1") == ["G1 Z0.18", "G1 X2 Y2 E0.1"]


def ladder_shifter(**changes):
    info = ladder.analyze_file(LADDER_FILE)
    offsets = ladder.default_offsets(info["bands"])
    shifter = ZShifter()
    shifter.set_ladder(Ladder(info["boundaries"], offsets, info["layer_z"], info["bbox"]))
    return shifter, info


def test_ladder_with_absolute_extrusion_splits_e_correctly():
    shifter, info = ladder_shifter()
    x = (info["bbox"][0] + info["bbox"][2]) / 2
    y0, y1 = info["bbox"][1] + 1, info["bbox"][3] - 1
    for line in ("M82", "G92 E10", "G1 Z0.2", "G1 X{} Y{}".format(x, y0)):
        shifter.process(line)
    out = shifter.process("G1 X{} Y{} E19 F1200".format(x, y1))
    moves = [line for line in out if " E" in line]
    assert len(moves) == 9  # one piece per band
    assert "F1200" in moves[0] and "F1200" not in moves[1]
    assert moves[-1].split(" E")[1] == "19"


def test_ladder_outside_the_pad_is_not_shifted():
    shifter, info = ladder_shifter()
    for line in ("G1 Z0.2", "G1 X10 Y10"):
        shifter.process(line)
    assert shifter.process("G1 X20 Y10 E0.5") == ["G1 X20 Y10 E0.5"]
    assert shifter.desired() == 0.0
