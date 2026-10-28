import os

from octoprint_prusa_livez import ladder

DATA = os.path.join(os.path.dirname(__file__), "data", "first_layer_ladder.gcode")


def test_detects_calibration_print():
    info = ladder.analyze_file(DATA)
    assert info["detected"] is True
    assert info["named"] is True
    assert info["bands"] == 9
    assert info["layer_z"] == 0.2
    assert info["bbox"] == [95.41, 62.21, 154.59, 147.79]
    assert info["boundaries"] == [71.05, 80.75, 90.45, 100.15, 109.85, 119.55, 129.25, 138.95]


def test_default_offsets():
    assert ladder.default_offsets(9, 0.01) == [-0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04]
    assert ladder.default_offsets(3, 0.025) == [-0.025, 0.0, 0.025]


def test_rejects_multi_layer():
    lines = [
        ";LAYER_CHANGE", ";Z:0.2", "G1 Z0.2", "M83", ";TYPE:External perimeter",
        "G1 X10 Y10", "G1 X20 Y10 E1", ";LAYER_CHANGE", ";Z:0.4", "G1 Z0.4", "G1 X20 Y20 E1",
    ]
    info = ladder.analyze(lines)
    assert info["detected"] is False


def test_plain_pad_has_no_notches():
    lines = [
        ";LAYER_CHANGE", ";Z:0.2", "G90", "M83", "G1 Z0.2", ";TYPE:External perimeter",
        "G1 X10 Y10", "G1 X50 Y10 E1", "G1 X50 Y50 E1", "G1 X10 Y50 E1", "G1 X10 Y10 E1",
    ]
    info = ladder.analyze(lines)
    assert info["detected"] is False
    assert "notches" in info["reason"]
