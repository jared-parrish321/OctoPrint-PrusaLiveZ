import pytest

from octoprint_prusa_livez import ladder, m850
from octoprint_prusa_livez.zshift import parse_words


@pytest.mark.parametrize("line, expected", [
    ("G1 X10 Y-2.5 E.8 F1200", ("G1", {"X": 10.0, "Y": -2.5, "E": 0.8, "F": 1200.0})),
    ("g1 x1 z 0.2", ("G1", {"X": 1.0, "Z": 0.2})),  # lower case, space before the number
    ("G1 X Y5", ("G1", {"Y": 5.0})),  # a letter with no number is skipped
    ("G1 X1E2", ("G1", {"X": 1.0, "E": 2.0})),  # no space between words
    ("G1 X1 X2", ("G1", {"X": 2.0})),  # the last value wins
    ("G1 X5.", ("G1", {"X": 5.0})),
    ("M115 U3.11.0", ("M115", {"U": 3.11})),  # stops at the second dot
    ("G28", ("G28", {})),
    ("", (None, {})),
])
def test_parse_words(line, expected):
    assert parse_words(line) == expected


def feed(*lines):
    parser = m850.ReplyParser()
    return [e for e in (parser.feed(line) for line in lines) if e]


def test_sheet_name_with_spaces_and_empty_name():
    (_, sheet), = feed("Sheet 3 Z-0.8250 R-330 Lmy pei B60 P35 A1")
    assert sheet["name"] == "my pei" and sheet["index"] == 3
    (_, sheet), = feed("Sheet 1 Z0 R0 L B255 P255 A0")
    assert sheet["name"] == ""


@pytest.mark.parametrize("text", [
    "Sheet 10 Z-0.8250 R-330 LA B60 P35 A1",  # two-digit sheet
    "Sheet 3 Z-0.8250 R-330 LA B60 P35 A2",  # A must be 0 or 1
    "Sheet 3 Z-0.8250 R-330 B60 P35 A1",  # no L field
    "Sheet 3 Z-0.82.50 R-330 LA B60 P35 A1",
    "Sheet 3 R-330 Z-0.8250 LA B60 P35 A1",  # fields out of order
    "Sheet 12 NOT INITIALIZED",
])
def test_reply_rejects(text):
    assert feed(text, " Z-0.0025 R-1 LCustom1 B255 P255 A0") == []


@pytest.mark.parametrize("name, version", [
    ("FIRMWARE_NAME:Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin", (3, 14, 1)),
    ("Prusa-Firmware 3.13.0.4", (3, 13, 0)),
    ("Prusa-Firmware 3.14", None),
    ("Prusa-Firmware 3..1 Prusa-Firmware 3.12.2", (3, 12, 2)),  # first valid one
    ("Marlin 2.1.2", None),
])
def test_firmware_version(name, version):
    assert m850.parse_firmware(name)[1] == version


@pytest.mark.parametrize("label", ["A1", "xA 2", "PA0", "Satiné"])
def test_label_refused(label):
    with pytest.raises(m850.M850Error):
        m850.validate_label(label)


@pytest.mark.parametrize("label", ["PEI", "NylonPA", "A", "Textur1"])
def test_label_allows_a_without_digit(label):
    assert m850.validate_label(label) == label


@pytest.mark.parametrize("line, name", [
    ("; printing object FirstLayerCalibration-FirstLayer.stl id:0 copy 0", "FirstLayerCalibration-FirstLayer.stl"),
    ("; printing object Pad  ", "Pad"),  # older PrusaSlicer, no id
    ("; printing object my id:x copy 0", "my id:x copy 0"),  # not a real id suffix
])
def test_object_name(line, name):
    assert ladder.analyze([line + "\n"])["objects"] == [name]
