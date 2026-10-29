import pytest

from octoprint_prusa_livez import ladder, m850
from octoprint_prusa_livez.textscan import is_decimal, is_int, number_end
from octoprint_prusa_livez.zshift import parse_words


@pytest.mark.parametrize("text, end", [
    ("5", 1), ("5.", 2), ("5.25", 4), (".25", 3), ("-.8", 3), ("+12", 3),
    ("3.11.0", 4),  # stops at the second dot
    ("-", 0), (".", 0), ("x", 0), ("", 0),
])
def test_number_end(text, end):
    assert number_end(text, 0) == end


@pytest.mark.parametrize("text, ok", [
    ("-0.8250", True), ("12", True), ("0", True),
    ("", False), ("-", False), ("1.", False), (".5", False), ("1.2.3", False), ("+1", False),
])
def test_is_decimal(text, ok):
    assert is_decimal(text) is ok


def test_is_int():
    assert is_int("-330") and is_int("0")
    assert not is_int("-") and not is_int("3.0") and not is_int("")


@pytest.mark.parametrize("line, expected", [
    ("G1 X10 Y-2.5 E.8 F1200", ("G1", {"X": 10.0, "Y": -2.5, "E": 0.8, "F": 1200.0})),
    ("g1 x1 z 0.2", ("G1", {"X": 1.0, "Z": 0.2})),  # lower case, space before the number
    ("G1 X Y5", ("G1", {"Y": 5.0})),  # a letter with no number is skipped
    ("G1 X1E2", ("G1", {"X": 1.0, "E": 2.0})),  # no space between words
    ("G1 X1 X2", ("G1", {"X": 2.0})),  # the last value wins
    ("M115 U3.11.0", ("M115", {"U": 3.11})),
    ("G28", ("G28", {})),
    ("", (None, {})),
])
def test_parse_words(line, expected):
    assert parse_words(line) == expected


def test_parse_sheet_data_fields():
    assert m850.parse_sheet_data("Sheet 3 Z-0.8250 R-330 Lmy pei B60 P35 A1") == {
        "sheet": 3, "z": -0.825, "raw": -330, "label": "my pei", "active": True,
    }
    # the data line after NOT INITIALIZED has no "Sheet n" and the name can be empty
    assert m850.parse_sheet_data("Z0 R0 L B255 P255 A0")["sheet"] is None


@pytest.mark.parametrize("text", [
    "Sheet 10 Z-0.8250 R-330 LA B60 P35 A1",  # two-digit sheet
    "Sheet 3 Z-0.8250 R-330 LA B60 P35 A2",  # A must be 0 or 1
    "Sheet 3 Z-0.8250 R-330 B60 P35 A1",  # no L field
    "Sheet 3 Z-0.82.50 R-330 LA B60 P35 A1",
    "Sheet 3 R-330 Z-0.8250 LA B60 P35 A1",  # fields out of order
    "ok",
])
def test_parse_sheet_data_rejects(text):
    assert m850.parse_sheet_data(text) is None


def test_uninit_header():
    assert m850.parse_uninit_header("Sheet 5 NOT INITIALIZED") == 5
    assert m850.parse_uninit_header("Sheet 12 NOT INITIALIZED") is None
    assert m850.parse_uninit_header("Sheet 5 Z-0.8250 R-330 LA B60 P35 A1") is None


@pytest.mark.parametrize("name, version", [
    ("FIRMWARE_NAME:Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin", (3, 14, 1)),
    ("Prusa-Firmware 3.13.0.4", (3, 13, 0)),
    ("Prusa-Firmware 3.14", None),
    ("Prusa-Firmware 3..1 Prusa-Firmware 3.12.2", (3, 12, 2)),  # first valid one
    ("Marlin 2.1.2", None),
])
def test_firmware_version(name, version):
    assert m850.firmware_version(name) == version


@pytest.mark.parametrize("label", ["A1", "xA 2", "PA0"])
def test_label_refuses_activate(label):
    with pytest.raises(m850.M850Error):
        m850.validate_label(label)


@pytest.mark.parametrize("label", ["PEI", "NylonPA", "A", "Textur1"])
def test_label_allows_a_without_digit(label):
    assert m850.validate_label(label) == label


def test_label_refuses_non_ascii():
    with pytest.raises(m850.M850Error):
        m850.validate_label("Satiné")


@pytest.mark.parametrize("line, name", [
    ("; printing object FirstLayerCalibration-FirstLayer.stl id:0 copy 0", "FirstLayerCalibration-FirstLayer.stl"),
    ("; printing object Pad  ", "Pad"),  # older PrusaSlicer, no id
    ("; printing object my id:x copy 0", "my id:x copy 0"),  # not a real id suffix
    ("; stop printing object Pad id:0 copy 0", None),
    ("; printing object ", None),
])
def test_object_name(line, name):
    found = ladder._object_name(line)
    assert (found.strip() if found is not None else None) == name
