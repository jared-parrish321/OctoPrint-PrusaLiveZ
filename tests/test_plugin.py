"""The OctoPrint side of the plugin: API commands, safety rules, events and history.

The plugin runs for real against the stand-ins in ``fakes.py``: a fake MK3S
firmware that answers M850, and fake OctoPrint printer and settings objects.
"""

import json
import os

import pytest
from octoprint.events import Events
from werkzeug.exceptions import Forbidden

import octoprint_prusa_livez as plugin_module
from octoprint_prusa_livez import m850

LADDER_FILE = os.path.join(os.path.dirname(__file__), "data", "first_layer_ladder.gcode")


def sheet(plugin, index):
    return plugin._state()["sheets"][index]


def last_push(plugin):
    return plugin._plugin_manager.send_plugin_message.call_args[0][1]


# -- plumbing -----------------------------------------------------------------


def test_plugin_metadata(plugin):
    assert set(plugin.get_settings_defaults()) == {
        "big_jump", "nudge_cap", "ladder_step", "ladder_offsets", "notify_on_start"}
    assert plugin.is_template_autoescaped() is True
    assert [t["type"] for t in plugin.get_template_configs()] == ["sidebar", "sidebar", "generic"]
    assert plugin.get_assets() == {"js": ["js/prusa_livez.js"], "css": ["css/prusa_livez.css"]}
    (permission,) = plugin.get_additional_permissions()
    assert permission["key"] == "EDIT" and permission["dangerous"] is True
    update = plugin.get_update_information()["prusa_livez"]
    assert update["repo"] == "OctoPrint-PrusaLiveZ" and "{target_version}" in update["pip"]
    assert plugin.is_api_protected() is True
    assert "set_z" in plugin.get_api_commands()


def test_plugin_load_registers_hooks():
    plugin_module.__plugin_load__()
    assert set(plugin_module.__plugin_hooks__) == {
        "octoprint.plugin.softwareupdate.check_config",
        "octoprint.access.permissions",
        "octoprint.comm.protocol.gcode.received",
        "octoprint.comm.protocol.gcode.queuing",
    }


def test_startup_asks_for_firmware_when_connected(plugin, firmware):
    plugin.on_after_startup()
    assert firmware.received == ["M115"]


def test_startup_stays_quiet_when_disconnected(plugin, firmware):
    plugin._printer.operational = False
    plugin.on_after_startup()
    assert firmware.received == []


def test_startup_loads_history(plugin, tmp_path):
    entries = [{"id": str(i), "field": "z"} for i in range(30)]
    (tmp_path / "history.json").write_text(json.dumps(entries))
    plugin.on_after_startup()
    assert len(plugin._history) == plugin_module.HISTORY_LEN
    assert plugin._history[-1]["id"] == "29"


def test_corrupt_history_is_logged_not_fatal(plugin, tmp_path):
    (tmp_path / "history.json").write_text("{not json")
    plugin.on_after_startup()
    assert plugin._history == []
    plugin._logger.exception.assert_called_once()


def test_history_save_failure_is_logged(connected, api, tmp_path):
    connected.get_plugin_data_folder = lambda: str(tmp_path / "missing")
    status, _ = api("set_z", sheet=0, z=-0.85)
    assert status == 200
    connected._logger.exception.assert_called_once()


def test_user_name_comes_from_login(plugin, mocker):
    user = mocker.Mock()
    user.get_name.return_value = "jared"
    mocker.patch("flask_login.current_user", user)
    assert plugin._user() == "jared"


def test_user_is_none_outside_a_request(plugin):
    assert plugin._user() is None


# -- firmware and connection ------------------------------------------------


def test_connect_reads_all_sheets(connected, firmware):
    assert [c for c in firmware.received if c.startswith("M850")] == ["M850 S%d" % i for i in range(8)]
    state = connected._state()
    assert state["firmware"] == {"name": "Prusa-Firmware 3.14.1+8237_74a577bc0", "version": "3.14.1",
                                 "supported": True}
    assert state["sheets"][0] == {"index": 0, "initialized": True, "raw": -330, "z_reported": -0.825,
                                  "name": "Smooth1", "active": True, "z": -0.825}
    assert state["sheets"][1]["initialized"] is False
    assert state["sheets"][4]["name"] == "Satin"
    assert state["refreshing"] is False


def test_firmware_name_from_the_m115_fields(plugin, firmware):
    plugin.on_event(Events.FIRMWARE_DATA, {"data": {"FIRMWARE_NAME": "Prusa-Firmware 3.13.2"}})
    assert plugin._firmware["version"] == "3.13.2"
    assert sheet(plugin, 2)["name"] == "Textur1"


@pytest.mark.parametrize("name", ["Prusa-Firmware 3.12.2", "Marlin 2.1.2", None])
def test_old_or_other_firmware_is_read_only(plugin, firmware, permissions, name):
    plugin.on_event(Events.FIRMWARE_DATA, {"name": name})
    assert firmware.received == []
    assert plugin._firmware["supported"] is False
    with pytest.raises(plugin_module.PluginError, match="3.13.0 or later"):
        plugin._api_refresh({})


def test_refresh_failure_is_reported(plugin, firmware, monkeypatch):
    monkeypatch.setattr(plugin_module, "REPLY_TIMEOUT", 0.01)
    firmware.silent = True
    plugin.on_event(Events.FIRMWARE_DATA, {"name": "Prusa-Firmware 3.14.1"})
    plugin._logger.warning.assert_called_once()
    assert "No reply from the printer" in last_push(plugin)["error"]
    assert plugin._refreshing is False


def test_refresh_does_not_run_twice(connected, firmware):
    connected._refreshing = True
    before = len(firmware.received)
    connected._refresh_all()
    assert len(firmware.received) == before


def test_disconnect_forgets_the_printer(connected):
    connected.on_event(Events.DISCONNECTED, None)
    state = connected._state()
    assert state["sheets"] == [None] * 8
    assert state["firmware"]["supported"] is None


def test_printer_error_reply_is_raised(connected, firmware):
    firmware.override["M850 S3"] = ["Invalid sheet ID. Allowed: 0..7"]
    with pytest.raises(plugin_module.PluginError, match="Printer replied: Invalid sheet ID"):
        connected._request("M850 S3", 3)


def test_steps_per_mm_is_learned_from_replies(connected):
    connected.on_gcode_received(None, "Sheet 7 Z-0.0800 R-160 LCustom2 B0 P0 A0\n")
    assert connected._steps == 2000
    connected._logger.info.assert_any_call("Learned Z steps/mm from the printer: %s", 2000)


# -- API: access ----------------------------------------------------------


def test_api_get_returns_state(connected, permissions, api):
    with api.app.test_request_context():
        body = connected.on_api_get(None).get_json()
    assert body["sheets"][0]["name"] == "Smooth1"


def test_api_needs_status_permission(connected, permissions, api):
    permissions.STATUS.can.return_value = False
    with api.app.test_request_context():
        with pytest.raises(Forbidden):
            connected.on_api_get(None)
        with pytest.raises(Forbidden):
            connected.on_api_command("refresh", {})


def test_editing_needs_edit_permission(connected, permissions, api, firmware):
    permissions.PLUGIN_PRUSA_LIVEZ_EDIT.can.return_value = False
    with pytest.raises(Forbidden):
        api("set_z", sheet=0, z=-0.85)
    assert firmware.sheets[0]["raw"] == -330
    status, _ = api("refresh")  # reading is still allowed
    assert status == 200


def test_editing_refused_when_permission_is_missing(connected, permissions, api):
    permissions.PLUGIN_PRUSA_LIVEZ_EDIT = None
    with pytest.raises(Forbidden):
        api("rename", sheet=0, name="PEI")


# -- API: Live Z ------------------------------------------------------------


def test_set_z_writes_and_verifies(api, firmware):
    status, body = api("set_z", sheet=0, z=-0.85)
    assert status == 200
    assert firmware.sheets[0]["raw"] == -340
    assert firmware.received[-1] == "M850 S0 Z-0.851250"
    assert body["state"]["sheets"][0]["z"] == -0.85
    (entry,) = body["state"]["history"]
    assert (entry["sheet"], entry["field"], entry["old"], entry["new"]) == (0, "z", -330, -340)


def test_big_jump_needs_confirmation(api, firmware):
    status, body = api("set_z", sheet=0, z=-1.2)
    assert status == 409 and body["code"] == "confirm"
    assert "0.375 mm" in body["error"]
    assert firmware.sheets[0]["raw"] == -330
    status, _ = api("set_z", sheet=0, z=-1.2, confirm=True)
    assert status == 200 and firmware.sheets[0]["raw"] == -480


def test_uninitialized_marker_is_skipped(api, firmware):
    api("set_z", sheet=0, z=-0.005, confirm=True)
    status, _ = api("set_z", sheet=0, z=-0.0025)
    assert status == 200
    assert firmware.sheets[0]["raw"] == 0  # -1 would mark the sheet empty


def test_uncalibrated_sheet_needs_confirmation(api, firmware):
    status, body = api("set_z", sheet=1, z=-0.8)
    assert status == 409 and "has no Z offset yet" in body["error"]
    status, body = api("set_z", sheet=1, z=-0.8, confirm=True)
    assert status == 200 and body["state"]["sheets"][1]["initialized"] is True


def test_same_value_sends_nothing(api, firmware):
    before = len(firmware.received)
    status, _ = api("set_z", sheet=0, z=-0.825)
    assert status == 200 and len(firmware.received) == before


def test_sheet_not_yet_read_is_read_first(api, firmware):
    api.plugin._sheets[5] = None
    status, _ = api("set_z", sheet=5, z=-0.5, confirm=True)
    assert status == 200
    assert firmware.received[-2:] == ["M850 S5", "M850 S5 Z-0.501250"]


def test_write_that_does_not_stick_is_an_error(api, firmware):
    firmware.override["M850 S0 Z-0.851250"] = ["Sheet 0 Z-0.8000 R-320 LSmooth1 B60 P35 A1"]
    status, body = api("set_z", sheet=0, z=-0.85)
    assert status == 400 and "stored -320 steps instead of -340" in body["error"]
    assert body.get("state") is None


@pytest.mark.parametrize("data, message", [
    ({"sheet": 0, "z": "abc"}, "must be a number"),
    ({"sheet": 0, "z": -20}, "must be between"),
    ({"sheet": 9, "z": -0.8}, "from 0 to 7"),
    ({"sheet": "x", "z": -0.8}, "from 0 to 7"),
])
def test_set_z_rejects_bad_input(api, data, message):
    status, body = api("set_z", **data)
    assert status == 400 and message in body["error"]


@pytest.mark.parametrize("flag", ["printing", "paused", "pausing", "cancelling"])
def test_no_changes_unless_idle(api, firmware, flag):
    setattr(api.plugin._printer, flag, True)
    for command, data in [("set_z", {"sheet": 0, "z": -0.85}), ("rename", {"sheet": 0, "name": "PEI"}),
                          ("activate", {"sheet": 2})]:
        status, body = api(command, **data)
        assert status == 400 and "only be changed while the printer is idle" in body["error"]
    assert firmware.sheets[0]["raw"] == -330 and firmware.active == 0


def test_no_changes_when_disconnected(api):
    api.plugin._printer.operational = False
    status, body = api("set_z", sheet=0, z=-0.85)
    assert status == 400 and "isn't connected" in body["error"]


def test_no_changes_before_firmware_report(plugin, permissions):
    import flask

    with flask.Flask(__name__).test_request_context():
        response = plugin.on_api_command("set_z", {"sheet": 0, "z": -0.85})
    assert response.status_code == 400
    assert "waiting for the printer's firmware report" in response.get_json()["error"]


# -- API: names and the active sheet ----------------------------------------


def test_rename(api, firmware):
    status, body = api("rename", sheet=0, name="PEI")
    assert status == 200 and firmware.sheets[0]["name"] == "PEI"
    assert body["state"]["history"][0]["field"] == "name"


def test_rename_to_same_name_sends_nothing(api, firmware):
    before = len(firmware.received)
    api("rename", sheet=0, name="Smooth1")
    assert len(firmware.received) == before


def test_rename_refuses_names_the_firmware_would_misread(api, firmware):
    status, body = api("rename", sheet=0, name="Zebra")
    assert status == 400 and "upper-case Z" in body["error"]
    assert firmware.sheets[0]["name"] == "Smooth1"


def test_rename_that_does_not_stick_is_an_error(api, firmware):
    firmware.override["M850 S0 LPEI"] = ["Sheet 0 Z-0.8250 R-330 LSmooth1 B60 P35 A1"]
    status, body = api("rename", sheet=0, name="PEI")
    assert status == 400 and "stored the name as 'Smooth1'" in body["error"]


def test_activate(api, firmware):
    status, body = api("activate", sheet=2)
    assert status == 200 and firmware.active == 2
    sheets = body["state"]["sheets"]
    assert sheets[2]["active"] and not sheets[0]["active"]
    assert body["state"]["history"][0]["old"] == 0


def test_activate_already_active_sends_nothing(api, firmware):
    before = len(firmware.received)
    api("activate", sheet=0)
    assert len(firmware.received) == before


def test_activate_refuses_uncalibrated_sheet(api, firmware):
    status, body = api("activate", sheet=1)
    assert status == 400 and "won't make it active" in body["error"]


def test_activate_that_does_not_stick_is_an_error(api, firmware):
    firmware.override["M850 S2 A1"] = ["Sheet 2 Z-0.9300 R-372 LTextur1 B60 P35 A0"]
    status, body = api("activate", sheet=2)
    assert status == 400 and "didn't make sheet 3 active" in body["error"]


# -- API: undo and import ---------------------------------------------------


def test_undo_each_kind_of_change(api, firmware):
    api("set_z", sheet=0, z=-0.85)
    api("rename", sheet=0, name="PEI")
    _, body = api("activate", sheet=2)
    for entry in body["state"]["history"]:
        status, _ = api("undo", id=entry["id"])
        assert status == 200
    assert firmware.active == 0
    assert firmware.sheets[0]["name"] == "Smooth1"
    assert firmware.sheets[0]["raw"] == -330


def test_undo_errors(api):
    status, body = api("undo", id="nope")
    assert status == 400 and "no longer in the history" in body["error"]
    _, body = api("set_z", sheet=0, z=-0.85)
    entry_id = body["state"]["history"][0]["id"]
    api("undo", id=entry_id)
    status, body = api("undo", id=entry_id)
    assert status == 400 and "already undone" in body["error"]


def test_write_echo_does_not_change_steps_per_mm(api, firmware):
    # Writing -0.5 makes the firmware echo "Z-0.5013 R-200", which looks like
    # 399 steps/mm. Learning that made the next write land one step off.
    api("set_z", sheet=0, z=-0.5, confirm=True)
    assert api.plugin._steps == 400
    status, _ = api("set_z", sheet=0, z=-0.51)
    assert status == 200 and firmware.sheets[0]["raw"] == -204


def test_history_keeps_the_last_twenty(api):
    for i in range(25):
        api("set_z", sheet=0, z=-0.5 - i * 0.01, confirm=True)
    assert len(api.plugin._history) == plugin_module.HISTORY_LEN


def test_import_restores_sheets(api, firmware):
    backup = [
        {"index": 0, "raw": -300, "name": "PEI"},
        {"index": 1, "initialized": False, "raw": -1, "name": "Smooth2"},
        {"index": 3, "z": -0.6, "name": "Textur2"},
        {"index": 4, "raw": -400},
    ]
    status, body = api("import", sheets=backup)
    assert status == 409 and body["code"] == "confirm"
    status, body = api("import", sheets=backup, confirm=True)
    assert status == 200
    assert (firmware.sheets[0]["raw"], firmware.sheets[0]["name"]) == (-300, "PEI")
    assert firmware.sheets[1]["raw"] == -1
    assert firmware.sheets[3]["raw"] == -240
    assert firmware.sheets[4]["raw"] == -400
    assert {e["note"] for e in body["state"]["history"]} == {"import"}


@pytest.mark.parametrize("sheets, message", [
    ("nope", "needs a list"),
    (["nope"], "must be an object"),
    ([{"index": 0, "raw": -1}], "invalid Z offset"),
    ([{"index": 0, "raw": 5}], "invalid Z offset"),
    ([{"index": 0, "raw": -300, "name": "Zebra"}], "upper-case Z"),
])
def test_import_rejects_bad_backups(api, firmware, sheets, message):
    status, body = api("import", sheets=sheets, confirm=True)
    assert status == 400 and message in body["error"]
    assert firmware.sheets[0]["raw"] == -330


# -- calibration ladder and mid-print nudge ---------------------------------


def select_ladder(plugin):
    plugin.on_event(Events.FILE_SELECTED, {"origin": "local", "path": LADDER_FILE})


def start(plugin, path=LADDER_FILE, origin="local"):
    plugin._printer.printing = True
    plugin.on_event(Events.PRINT_STARTED, {"origin": origin, "path": path})


def finish(plugin, event=Events.PRINT_DONE):
    plugin._printer.printing = False
    plugin.on_event(event, {})


def test_selecting_the_ladder_file_turns_the_ladder_on(connected):
    select_ladder(connected)
    ladder = connected._state()["ladder"]
    assert ladder["info"]["detected"] and ladder["info"]["bands"] == 9
    assert ladder["enabled"] is True
    assert ladder["offsets"] == [-0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04]


def test_saved_offsets_are_reused(connected):
    saved = [0.0] * 9
    connected._settings.set(["ladder_offsets"], saved)
    select_ladder(connected)
    assert connected._ladder_offsets == saved


def test_sd_card_files_are_not_analyzed(connected):
    connected.on_event(Events.FILE_SELECTED, {"origin": "sdcard", "path": "cal.gco"})
    assert connected._ladder_info is None


def test_unreadable_file_is_logged(connected):
    connected.on_event(Events.FILE_SELECTED, {"origin": "local", "path": "/does/not/exist.gcode"})
    assert connected._ladder_info is None
    connected._logger.exception.assert_called_once()


def test_deselecting_clears_the_ladder(connected):
    select_ladder(connected)
    connected.on_event(Events.FILE_DESELECTED, {})
    assert connected._state()["ladder"]["info"] is None


def test_ladder_print_shifts_each_band(api):
    plugin = api.plugin
    select_ladder(plugin)
    start(plugin)
    state = last_push(plugin)
    assert state["notice"] == "start" and state["state"]["job"]["ladder"] is True
    zs = set()
    with open(LADDER_FILE) as handle:
        for raw in handle:
            cmd = raw.split(";", 1)[0].strip()
            if not cmd:
                continue
            out = plugin.on_gcode_queuing(None, "queuing", cmd, None, cmd.split()[0], tags={"source:file"})
            for line in out or [cmd]:
                if line.startswith("G1") and " Z" in line:
                    zs.add(round(float(line.split(" Z")[1].split()[0]), 3))
    assert {0.16, 0.2, 0.24} <= zs
    finish(plugin)
    pending = plugin._state()["pending"]
    assert pending["kind"] == "ladder" and pending["finished"] is True

    status, body = api("apply_result", band=6)  # band 7: +0.02
    assert status == 200 and body["state"]["pending"] is None
    assert api.plugin._sheets[0]["raw"] == -322


def test_ladder_band_with_zero_offset_changes_nothing(api, firmware):
    select_ladder(api.plugin)
    start(api.plugin)
    finish(api.plugin)
    status, body = api("apply_result", band=4)
    assert status == 200 and "nothing to change" in body["message"]
    assert firmware.sheets[0]["raw"] == -330


@pytest.mark.parametrize("band, message", [(None, "Pick a band"), (9, "from 1 to 9")])
def test_ladder_result_needs_a_valid_band(api, band, message):
    select_ladder(api.plugin)
    start(api.plugin)
    finish(api.plugin)
    status, body = api("apply_result", band=band)
    assert status == 400 and message in body["error"]


def test_failed_ladder_print_offers_nothing(connected):
    select_ladder(connected)
    start(connected)
    finish(connected, Events.PRINT_FAILED)
    assert connected._pending is None


def test_ladder_settings(api):
    status, body = api("ladder_config", enabled=True)
    assert status == 400 and "isn't a calibration ladder" in body["error"]
    select_ladder(api.plugin)
    offsets = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
    status, body = api("ladder_config", enabled=False, offsets=offsets)
    assert status == 200
    assert body["state"]["ladder"]["enabled"] is False
    assert api.plugin._settings.values["ladder_offsets"] == offsets
    assert api.plugin._settings.saved == 1


@pytest.mark.parametrize("offsets, message", [
    ([0.0] * 8, "exactly 9 band changes"),
    (["x"] + [0.0] * 8, "Band 1 change must be a number"),
    ([0.0] * 8 + [0.5], "Band 9 would print at 0.700 mm"),
])
def test_ladder_settings_reject_bad_offsets(api, offsets, message):
    select_ladder(api.plugin)
    status, body = api("ladder_config", offsets=offsets)
    assert status == 400 and message in body["error"]


def test_ladder_settings_locked_during_print(api):
    select_ladder(api.plugin)
    start(api.plugin)
    status, body = api("ladder_config", enabled=False)
    assert status == 400 and "during a print" in body["error"]


def test_other_file_prints_without_ladder(connected):
    select_ladder(connected)
    start(connected, path="other.gcode")
    assert connected._job["ladder"] is False


def test_sd_print_does_not_shift(connected):
    start(connected, path="cal.gco", origin="sdcard")
    assert connected._shift_active is False
    assert connected.on_gcode_queuing(None, "queuing", "G1 Z0.2", None, "G1", tags={"source:file"}) is None


def test_start_notice_can_be_turned_off(connected):
    connected._settings.set(["notify_on_start"], False)
    start(connected, path="part.gcode")
    assert "notice" not in last_push(connected)


def test_nudge_during_print(api):
    plugin = api.plugin
    start(plugin, path="part.gcode")
    queue = lambda cmd: plugin.on_gcode_queuing(None, "queuing", cmd, None, "G1", tags={"source:file"})
    assert queue("G1 Z0.2 F720") is None  # no nudge yet: line goes out unchanged
    status, body = api("nudge", delta=-0.02)
    assert status == 200 and body["state"]["nudge"] == -0.02
    assert queue("G1 X10 Y10 E0.5") == ["G1 Z0.18", "G1 X10 Y10 E0.5"]
    assert queue("G1 Z0.4") == ["G1 Z0.38"]
    assert queue("G1 X5 Y5") is None
    assert plugin.on_gcode_queuing(None, "queuing", "G1 Z0.4", None, "G1", tags={"source:api"}) is None
    finish(plugin)
    pending = plugin._state()["pending"]
    assert (pending["kind"], pending["offset"]) == ("nudge", -0.02)
    status, _ = api("apply_result")
    assert status == 200 and plugin._sheets[0]["raw"] == -338


def test_nudge_reset(api):
    status, body = api("nudge_reset")
    assert status == 400 and "No print is running" in body["error"]
    start(api.plugin, path="part.gcode")
    api("nudge", delta=0.01)
    status, body = api("nudge_reset")
    assert status == 200 and body["state"]["job"]["nudge"] == 0.0
    finish(api.plugin)
    assert api.plugin._pending is None  # a zero nudge leaves nothing to save


def test_nudge_rules(api):
    status, body = api("nudge", delta=0.01)
    assert status == 400 and "only while OctoPrint is streaming a print" in body["error"]

    select_ladder(api.plugin)
    start(api.plugin)
    status, body = api("nudge", delta=0.01)
    assert status == 400 and "calibration ladder" in body["error"]
    finish(api.plugin)

    start(api.plugin, path="part.gcode")
    for delta, message in [("x", "must be a number"), (0.06, "at most 0.05 mm")]:
        status, body = api("nudge", delta=delta)
        assert status == 400 and message in body["error"]
    for _ in range(6):
        api("nudge", delta=0.05)
    status, body = api("nudge", delta=0.05)
    assert status == 400 and "capped at ±0.30 mm" in body["error"]

    api("nudge_reset")
    api.plugin.on_gcode_queuing(None, "queuing", "G1 Z0.2", None, "G1", tags={"source:file"})
    for _ in range(3):
        api("nudge", delta=-0.05)
    status, body = api("nudge", delta=-0.05)  # 0.2 - 0.2 = 0.0 mm
    assert status == 400 and "above the bed" in body["error"]

    api.plugin._printer.paused = True
    status, body = api("nudge", delta=0.01)
    assert status == 400 and "paused" in body["error"]


def test_resume_forgets_the_nozzle_position(connected, mocker):
    start(connected, path="part.gcode")
    spy = mocker.spy(connected._shifter, "mark_unknown")
    connected.on_event(Events.PRINT_RESUMED, {})
    spy.assert_called_once()


def test_result_rules(api):
    status, body = api("apply_result")
    assert status == 400 and "no result waiting" in body["error"]
    start(api.plugin, path="part.gcode")
    api("nudge", delta=0.01)
    finish(api.plugin)
    api.plugin._sheets[0] = None
    status, body = api("apply_result")
    assert status == 400 and "hasn't been read yet" in body["error"]
    status, body = api("dismiss_result")
    assert status == 200 and body["state"]["pending"] is None


def test_print_end_rereads_sheets(connected, firmware):
    start(connected, path="part.gcode")
    firmware.received.clear()
    finish(connected, Events.PRINT_CANCELLED)
    assert firmware.received == ["M850 S%d" % i for i in range(8)]


def test_disconnect_mid_print_ends_the_job(connected):
    start(connected, path="part.gcode")
    connected.on_event(Events.DISCONNECTED, {})
    assert connected._job is None and connected._shift_active is False


# -- serial hooks -------------------------------------------------------------


def test_received_lines_pass_through(connected):
    assert connected.on_gcode_received(None, "ok\n") == "ok\n"


def test_parser_crash_does_not_break_the_connection(connected, mocker):
    mocker.patch.object(connected._parser, "feed", side_effect=RuntimeError("boom"))
    assert connected.on_gcode_received(None, "weird\n") == "weird\n"
    connected._logger.exception.assert_called_once()


def test_sheet_changed_on_the_lcd_is_pushed(connected):
    connected._plugin_manager.reset_mock()
    connected.on_gcode_received(None, "Sheet 0 Z-0.9000 R-360 LSmooth1 B60 P35 A1\n")
    assert last_push(connected)["state"]["sheets"][0]["z"] == -0.9


def test_error_reply_without_a_request_is_ignored(connected):
    assert connected.on_gcode_received(None, "Invalid sheet ID. Allowed: 0..7\n") is not None


def test_shift_failure_sends_the_line_unchanged(connected, mocker):
    start(connected, path="part.gcode")
    mocker.patch.object(connected._shifter, "process", side_effect=RuntimeError("boom"))
    assert connected.on_gcode_queuing(None, "queuing", "G1 Z0.2", None, "G1", tags={"source:file"}) is None
    connected._logger.exception.assert_called_once()


def test_m850_constants_match_the_fake_firmware():
    assert m850.DEFAULT_STEPS_PER_MM == 400
