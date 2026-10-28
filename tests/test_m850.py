import pytest

from octoprint_prusa_livez import m850


def feed_all(lines):
    parser = m850.ReplyParser()
    return [e for e in (parser.feed(line) for line in lines) if e]


def test_parse_active_sheet():
    events = feed_all(["Sheet 0 Z-0.8250 R-330 LSmooth1 B60 P35 A1\n"])
    assert events == [("sheet", {
        "index": 0, "initialized": True, "raw": -330, "z_reported": -0.825,
        "name": "Smooth1", "active": True,
    })]


def test_parse_label_with_spaces_and_padding():
    (kind, sheet), = feed_all(["Sheet 4 Z-1.0000 R-400 LSatin   B0 P0 A0"])
    assert sheet["name"] == "Satin"
    assert sheet["active"] is False
    (kind, sheet), = feed_all(["Sheet 2 Z-0.5000 R-200 Lmy pei B60 P35 A0"])
    assert sheet["name"] == "my pei"


def test_parse_uninitialized_two_lines():
    # firmware prints the header line, then the data line starting with " Z"
    events = feed_all(["Sheet 5 NOT INITIALIZED\n", " Z-0.0025 R-1 LCustom1 B255 P255 A0\n"])
    assert len(events) == 1
    kind, sheet = events[0]
    assert sheet["index"] == 5
    assert sheet["initialized"] is False


def test_orphan_data_line_ignored():
    assert feed_all([" Z-0.0025 R-1 LCustom1 B255 P255 A0"]) == []


def test_unrelated_line_resets_uninit_state():
    assert feed_all(["Sheet 5 NOT INITIALIZED", "ok", " Z-0.0025 R-1 Lx B0 P0 A0"]) == []


def test_errors():
    assert feed_all([" Z VALUE OUT OF RANGE"]) == [("error", "Z value out of range")]
    assert feed_all(["Invalid sheet ID. Allowed: 0..7"])[0][0] == "error"


def test_other_lines_ignored():
    assert feed_all(["ok", "T:215.0 /215.0 B:60.0 /60.0", "echo:busy: processing", ""]) == []


def test_learn_steps():
    assert m850.learn_steps_per_mm(-330, -0.825) == 400
    assert m850.learn_steps_per_mm(0, 0.0) is None
    assert m850.learn_steps_per_mm(-1, -0.0025) is None
    # after a write the firmware echoes the sent value (raw - 0.5) / steps
    assert m850.learn_steps_per_mm(-339, -0.8488) is None
    assert m850.learn_steps_per_mm(-339, -0.8475) == 400


@pytest.mark.parametrize("z, raw", [(-0.825, -330), (-0.8, -320), (0, 0), (-9.9975, -3999), (-0.0024, -1 + -1)])
def test_target_raw(z, raw):
    assert m850.target_raw(z) == raw


def test_target_raw_skips_empty_marker_in_direction_of_travel():
    assert m850.target_raw(-0.0025, previous_raw=0) == -2
    assert m850.target_raw(-0.0025, previous_raw=-2) == 0
    assert m850.target_raw(-0.0025, previous_raw=-40) == 0
    assert m850.target_raw(-0.0025) == -2


@pytest.mark.parametrize("z", [0.01, -10.0, "abc", None, float("nan")])
def test_target_raw_rejects(z):
    with pytest.raises(m850.M850Error):
        m850.target_raw(z)


@pytest.mark.parametrize("raw", [-330, -320, -3999, -2, 0, -1234, -7])
def test_set_z_command_lands_on_raw_after_firmware_truncation(raw):
    import struct

    cmd = m850.cmd_set_z(3, raw)
    assert cmd.startswith("M850 S3 Z")
    value = float(cmd.split("Z")[1])
    # firmware: float(z) * 400.0f, then truncated toward zero into int16
    f32 = struct.unpack("f", struct.pack("f", value))[0]
    product = struct.unpack("f", struct.pack("f", f32 * 400.0))[0]
    assert int(product) == raw


def test_set_z_command_refuses_empty_marker():
    with pytest.raises(m850.M850Error):
        m850.cmd_set_z(0, -1)


def test_commands():
    assert m850.cmd_read(7) == "M850 S7"
    assert m850.cmd_activate(2) == "M850 S2 A1"
    assert m850.cmd_rename(1, "PEI") == "M850 S1 LPEI"
    with pytest.raises(m850.M850Error):
        m850.cmd_read(8)


@pytest.mark.parametrize("name", ["Smooth1", "Textur1", "NylonPA", "my pei", "PEI", "abc"])
def test_label_ok(name):
    assert m850.validate_label(name) == name


@pytest.mark.parametrize("name", ["Zebra", "A1", "satA 2", "", "toolongname", "bad!", "x;y"])
def test_label_rejected(name):
    with pytest.raises(m850.M850Error):
        m850.validate_label(name)


def test_parse_firmware():
    assert m850.parse_firmware("Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin") == (True, (3, 14, 1), True)
    assert m850.parse_firmware("Prusa-Firmware 3.12.2 based on Marlin") == (True, (3, 12, 2), False)
    assert m850.parse_firmware("Marlin 2.1.2") == (False, None, False)
    assert m850.parse_firmware(None) == (False, None, False)
