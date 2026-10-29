"""Property-based tests: Hypothesis generates thousands of inputs per run
and checks rules that must hold for all of them."""

import string

from fakes import FakeFirmware
from hypothesis import given, settings
from hypothesis import strategies as st

from octoprint_prusa_livez import m850
from octoprint_prusa_livez.zshift import ZShifter, parse_words

GCODE_CHARS = string.ascii_letters + string.digits + " .-+\t"


@given(st.text())
def test_reply_parser_never_crashes(text):
    parser = m850.ReplyParser()
    parser.feed(text)
    parser.feed(" Z-0.0025 R-1 LCustom1 B255 P255 A0")


@given(st.text(alphabet=GCODE_CHARS))
def test_parse_words_never_crashes(line):
    command, words = parse_words(line)
    assert all(len(letter) == 1 and letter.isupper() for letter in words)


@settings(max_examples=300)
@given(st.integers(min_value=m850.RAW_MIN, max_value=m850.RAW_MAX))
def test_every_raw_value_survives_the_firmware_truncation(raw):
    """What cmd_set_z sends always lands on the wanted step in the firmware."""
    if raw == m850.RAW_EMPTY:
        return
    firmware = FakeFirmware()
    firmware.handle(m850.cmd_set_z(0, raw))
    assert firmware.sheets[0]["raw"] == raw


@given(st.floats(min_value=-9.9975, max_value=0.0), st.integers(m850.RAW_MIN, m850.RAW_MAX))
def test_target_raw_never_marks_a_sheet_empty(z_mm, previous):
    raw = m850.target_raw(z_mm, previous_raw=previous)
    assert raw != m850.RAW_EMPTY
    assert m850.RAW_MIN <= raw <= m850.RAW_MAX


@given(st.text(alphabet=string.ascii_letters + string.digits + " ", min_size=1, max_size=7))
def test_accepted_names_cannot_be_misread_by_the_firmware(label):
    try:
        accepted = m850.validate_label(label)
    except m850.M850Error:
        return
    assert "Z" not in accepted
    firmware = FakeFirmware()
    firmware.handle(m850.cmd_rename(1, accepted))
    assert firmware.sheets[1]["raw"] == -1  # the name didn't change Live Z
    assert firmware.active == 0  # or the active sheet


coordinate = st.floats(min_value=-500, max_value=500, allow_nan=False).map(lambda v: round(v, 3))


@given(st.lists(st.tuples(st.sampled_from("XYZE"), coordinate), min_size=1, max_size=5))
def test_zero_offset_never_changes_a_line(words):
    line = "G1 " + " ".join("{}{}".format(letter, value) for letter, value in words)
    shifter = ZShifter()
    shifter.process("G1 Z0.2")
    assert shifter.process(line) == [line]
