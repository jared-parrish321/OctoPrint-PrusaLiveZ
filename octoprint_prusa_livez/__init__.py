"""Prusa Live Z: read and write Live Adjust Z per sheet on Prusa MK3/MK2.5 firmware."""

import json
import os
import threading
import time
import uuid

import flask
import octoprint.plugin
from octoprint.access import ADMIN_GROUP
from octoprint.access.permissions import Permissions
from octoprint.events import Events

from . import ladder as ladder_mod
from . import m850
from .zshift import Ladder, ZShifter

REPLY_TIMEOUT = 20  # s; M850 queues behind buffered moves during a print
HISTORY_LEN = 20
NUDGE_STEP_MAX = 0.05
MIN_PHYSICAL_Z = 0.05  # never put the nozzle lower than this on any layer
LADDER_BAND_Z = (0.10, 0.35)


class PluginError(Exception):
    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


def _round(value, digits=4):
    return round(float(value) + 0.0, digits)


class PrusaLiveZPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        self._io_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._parser = m850.ReplyParser()
        self._waiter = None
        self._sheets = [None] * m850.MAX_SHEETS
        self._steps = m850.DEFAULT_STEPS_PER_MM
        self._firmware = {"name": None, "version": None, "supported": None}
        self._refreshing = False
        self._history = []
        self._shifter = ZShifter()
        self._shift_active = False
        self._job = None
        self._pending = None
        self._ladder_info = None
        self._ladder_path = None
        self._ladder_enabled = False
        self._ladder_offsets = []

    # -- OctoPrint plumbing ---------------------------------------------

    def on_after_startup(self):
        self._load_history()
        if self._printer.is_operational():
            # Plugin (re)loaded while connected: ask for the firmware again.
            self._printer.commands("M115", tags={"trigger:plugin_prusa_livez"})

    def get_settings_defaults(self):
        return {
            "big_jump": 0.10,
            "nudge_cap": 0.30,
            "ladder_step": 0.01,
            "ladder_offsets": [],
            "notify_on_start": True,
        }

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [
            {"type": "sidebar", "name": "Live Z", "icon": "fas fa-ruler-vertical",
             "template": "prusa_livez_sidebar.jinja2", "custom_bindings": True},
            {"type": "generic", "template": "prusa_livez_generic.jinja2", "custom_bindings": True},
        ]

    def get_assets(self):
        return {"js": ["js/prusa_livez.js"], "css": ["css/prusa_livez.css"]}

    def get_additional_permissions(self, *args, **kwargs):
        return [
            {
                "key": "EDIT",
                "name": "Edit Live Z",
                "description": "Change Live Z, sheet names and the active sheet, and nudge Z during a print",
                "roles": ["edit"],
                "dangerous": True,
                "default_groups": [ADMIN_GROUP],
            }
        ]

    def get_update_information(self):
        return {
            "prusa_livez": {
                "displayName": "Prusa Live Z",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "jared-parrish321",
                "repo": "OctoPrint-PrusaLiveZ",
                "current": self._plugin_version,
                "pip": "https://github.com/jared-parrish321/OctoPrint-PrusaLiveZ/archive/{target_version}.zip",
            }
        }

    # -- API --------------------------------------------------------------

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {
            "refresh": [],
            "set_z": ["sheet", "z"],
            "rename": ["sheet", "name"],
            "activate": ["sheet"],
            "undo": ["id"],
            "import": ["sheets"],
            "nudge": ["delta"],
            "nudge_reset": [],
            "ladder_config": [],
            "apply_result": [],
            "dismiss_result": [],
        }

    def on_api_get(self, request):
        if not Permissions.STATUS.can():
            flask.abort(403)
        return flask.jsonify(self._state())

    def on_api_command(self, command, data):
        if not Permissions.STATUS.can():
            flask.abort(403)
        if command != "refresh" and not self._can_edit():
            flask.abort(403)
        handler = getattr(self, "_api_" + command)
        try:
            result = handler(data) or {}
        except (PluginError, m850.M850Error) as exc:
            code = getattr(exc, "code", "invalid")
            return flask.make_response(flask.jsonify(error=str(exc), code=code), 409 if code == "confirm" else 400)
        result["state"] = self._state()
        return flask.jsonify(result)

    def _can_edit(self):
        permission = getattr(Permissions, "PLUGIN_PRUSA_LIVEZ_EDIT", None)
        return permission is not None and permission.can()

    def _user(self):
        try:
            from flask_login import current_user

            return current_user.get_name()
        except Exception:
            return None

    # -- API handlers ----------------------------------------------------

    def _api_refresh(self, data):
        self._require_supported()
        self._refresh_all()

    def _api_set_z(self, data):
        self._require_idle()
        sheet = m850.validate_sheet(data.get("sheet"))
        self._set_z(sheet, data.get("z"), confirm=bool(data.get("confirm")))

    def _api_rename(self, data):
        self._require_idle()
        sheet = m850.validate_sheet(data.get("sheet"))
        self._rename(sheet, data.get("name"))

    def _api_activate(self, data):
        self._require_idle()
        sheet = m850.validate_sheet(data.get("sheet"))
        self._activate(sheet)

    def _api_undo(self, data):
        self._require_idle()
        entry = next((h for h in self._history if h["id"] == data.get("id")), None)
        if entry is None:
            raise PluginError("That change is no longer in the history")
        if entry.get("undone"):
            raise PluginError("That change was already undone")
        if entry["field"] == "z":
            self._set_z(entry["sheet"], m850.raw_to_mm(entry["old"], self._steps), confirm=True,
                        raw=entry["old"], note="undo")
        elif entry["field"] == "name":
            self._rename(entry["sheet"], entry["old"], note="undo")
        elif entry["field"] == "active":
            self._activate(entry["old"], note="undo")
        entry["undone"] = True
        self._save_history()

    def _api_import(self, data):
        self._require_idle()
        sheets = data.get("sheets")
        if not isinstance(sheets, list):
            raise PluginError("Import needs a list of sheets")
        if not data.get("confirm"):
            raise PluginError("Importing overwrites names and Live Z. Confirm to continue.", "confirm")
        plan = []
        for item in sheets:
            if not isinstance(item, dict):
                raise PluginError("Every sheet must be an object")
            index = m850.validate_sheet(item.get("index"))
            if not item.get("initialized", True):
                continue
            raw = int(item["raw"]) if "raw" in item else m850.mm_to_raw(float(item["z"]), self._steps)
            if raw == m850.RAW_EMPTY or not m850.RAW_MIN <= raw <= m850.RAW_MAX:
                raise PluginError("Sheet {} has an invalid Live Z".format(index))
            name = m850.validate_label(item["name"]) if item.get("name") else None
            plan.append((index, raw, name))
        for index, raw, name in plan:
            if name is not None and self._sheet_name(index) != name:
                self._rename(index, name, note="import")
            current = self._sheets[index]
            if current is None or current["raw"] != raw:
                self._set_z(index, m850.raw_to_mm(raw, self._steps), confirm=True, raw=raw, note="import")

    def _api_nudge(self, data):
        if not (self._shift_active and self._job):
            raise PluginError("Nudges work only while a print streamed by OctoPrint is running")
        if self._job.get("ladder"):
            raise PluginError("Nudges are off during a calibration ladder print")
        if self._printer.is_paused() or self._printer.is_pausing():
            raise PluginError("Nudges are locked while the print is paused")
        try:
            delta = float(data.get("delta"))
        except (TypeError, ValueError):
            raise PluginError("Nudge must be a number")
        if abs(delta) > NUDGE_STEP_MAX + 1e-9:
            raise PluginError("One nudge can be at most {} mm".format(NUDGE_STEP_MAX))
        cap = float(self._settings.get(["nudge_cap"]))
        new = _round(self._shifter.nudge + delta)
        if abs(new) > cap + 1e-9:
            raise PluginError("The total nudge is capped at ±{:.2f} mm".format(cap))
        file_z = self._shifter.file_z
        if file_z is not None and file_z + new < MIN_PHYSICAL_Z:
            raise PluginError("That would put the nozzle {:.3f} mm above the bed".format(file_z + new))
        self._shifter.set_nudge(new)
        self._job["nudge"] = new
        self._logger.info("Mid-print Z nudge now %+.3f mm (by %s)", new, self._user())
        self._push()

    def _api_nudge_reset(self, data):
        if not self._shift_active:
            raise PluginError("No print is running")
        self._shifter.set_nudge(0.0)
        if self._job:
            self._job["nudge"] = 0.0
        self._push()

    def _api_ladder_config(self, data):
        if self._shift_active:
            raise PluginError("The ladder can't be changed during a print")
        if self._ladder_info is None or not self._ladder_info.get("detected"):
            raise PluginError("The selected file isn't a calibration ladder print")
        if "enabled" in data:
            self._ladder_enabled = bool(data["enabled"])
        if "offsets" in data:
            self._ladder_offsets = self._validate_offsets(data["offsets"])
            self._settings.set(["ladder_offsets"], self._ladder_offsets)
            self._settings.save()
        self._push()

    def _api_apply_result(self, data):
        self._require_idle()
        pending = self._pending
        if not pending:
            raise PluginError("There is no result waiting")
        if pending["kind"] == "ladder":
            try:
                band = int(data.get("band"))
            except (TypeError, ValueError):
                raise PluginError("Pick a band")
            if not 0 <= band < len(pending["offsets"]):
                raise PluginError("Pick a band from 1 to {}".format(len(pending["offsets"])))
            offset = pending["offsets"][band]
            note = "ladder band {}".format(band + 1)
        else:
            offset = pending["offset"]
            note = "mid-print nudge"
        sheet = pending["sheet"]
        current = self._sheets[sheet]
        if current is None:
            raise PluginError("Sheet {} hasn't been read yet".format(sheet + 1))
        new_mm = m850.raw_to_mm(current["raw"], self._steps) + offset
        if abs(offset) < 1e-9:
            self._pending = None
            self._push()
            return {"message": "Band {} is the current Live Z, nothing to change".format(band + 1)}
        self._set_z(sheet, new_mm, confirm=True, note=note)
        self._pending = None

    def _api_dismiss_result(self, data):
        self._pending = None
        self._push()

    # -- checks -----------------------------------------------------------

    def _require_supported(self):
        if not self._printer.is_operational():
            raise PluginError("The printer isn't connected")
        if self._firmware["supported"] is False:
            raise PluginError("This needs Prusa firmware 3.13.0 or later (found {})".format(
                self._firmware["name"] or "unknown firmware"))

    def _require_idle(self):
        self._require_supported()
        if self._firmware["supported"] is not True:
            raise PluginError("Still waiting for the printer's firmware report")
        busy = (
            self._printer.is_printing()
            or self._printer.is_paused()
            or self._printer.is_pausing()
            or self._printer.is_cancelling()
        )
        if busy or self._shift_active:
            raise PluginError("Live Z can only be changed while the printer is idle")

    def _validate_offsets(self, offsets):
        info = self._ladder_info or {}
        bands = info.get("bands", 0)
        if not isinstance(offsets, list) or len(offsets) != bands:
            raise PluginError("Need exactly {} offsets".format(bands))
        layer_z = info.get("layer_z", 0.2)
        out = []
        for i, value in enumerate(offsets):
            try:
                value = _round(value)
            except (TypeError, ValueError):
                raise PluginError("Band {} offset must be a number".format(i + 1))
            z = layer_z + value
            if not LADDER_BAND_Z[0] - 1e-9 <= z <= LADDER_BAND_Z[1] + 1e-9:
                raise PluginError("Band {} would print at {:.3f} mm; keep it between {} and {} mm".format(
                    i + 1, z, LADDER_BAND_Z[0], LADDER_BAND_Z[1]))
            out.append(value)
        return out

    # -- printer I/O ------------------------------------------------------

    def _request(self, command, sheet):
        """Send one M850 and wait for the sheet report. One at a time."""
        with self._io_lock:
            waiter = {"sheet": sheet, "event": threading.Event(), "result": None, "error": None,
                      "read": command == m850.cmd_read(sheet)}
            self._waiter = waiter
            try:
                self._printer.commands(command, tags={"trigger:plugin_prusa_livez"})
                if not waiter["event"].wait(REPLY_TIMEOUT):
                    raise PluginError("No reply from the printer to {}".format(command))
            finally:
                self._waiter = None
            if waiter["error"]:
                raise PluginError("Printer replied: {}".format(waiter["error"]))
            return waiter["result"]

    def _refresh_all(self):
        with self._state_lock:
            if self._refreshing:
                return
            self._refreshing = True
        self._push()
        try:
            for index in range(m850.MAX_SHEETS):
                self._request(m850.cmd_read(index), index)
        finally:
            self._refreshing = False
            self._push()

    def _refresh_async(self):
        def run():
            try:
                self._refresh_all()
            except Exception as exc:
                self._logger.warning("Reading sheets failed: %s", exc)
                self._push(error=str(exc))

        threading.Thread(target=run, name="prusa_livez_refresh", daemon=True).start()

    def _sheet_name(self, index):
        sheet = self._sheets[index]
        return sheet["name"] if sheet else None

    def _set_z(self, sheet, z_mm, confirm=False, raw=None, note=None):
        current = self._sheets[sheet]
        if current is None:
            current = self._request(m850.cmd_read(sheet), sheet)
        old_raw = current["raw"]
        if raw is None:
            raw = m850.target_raw(z_mm, previous_raw=old_raw, steps_per_mm=self._steps)
        if current["initialized"]:
            jump = abs(m850.raw_to_mm(raw - old_raw, self._steps))
            big = float(self._settings.get(["big_jump"]))
            if jump > big + 1e-9 and not confirm:
                raise PluginError("That changes Live Z by {:.3f} mm. Confirm to continue.".format(jump), "confirm")
        elif not confirm:
            raise PluginError("Sheet {} has no Live Z yet. Run First Layer Calibration on the printer, "
                              "or confirm to set a starting value.".format(sheet + 1), "confirm")
        if raw == old_raw and current["initialized"]:
            return
        result = self._request(m850.cmd_set_z(sheet, raw, self._steps), sheet)
        if result["raw"] != raw:
            raise PluginError("The printer stored {} steps instead of {}. Check the sheet on the LCD.".format(
                result["raw"], raw))
        self._record(sheet, "z", old_raw, raw, note)

    def _rename(self, sheet, name, note=None):
        name = m850.validate_label(name)
        current = self._sheets[sheet] or self._request(m850.cmd_read(sheet), sheet)
        old = current["name"]
        if old == name:
            return
        result = self._request(m850.cmd_rename(sheet, name), sheet)
        if result["name"] != name:
            raise PluginError("The printer stored the name as '{}'".format(result["name"]))
        self._record(sheet, "name", old, name, note)

    def _activate(self, sheet, note=None):
        current = self._sheets[sheet] or self._request(m850.cmd_read(sheet), sheet)
        if not current["initialized"]:
            raise PluginError("Sheet {} has no Live Z yet, so the firmware won't make it active".format(sheet + 1))
        old = next((s["index"] for s in self._sheets if s and s["active"]), None)
        if old == sheet:
            return
        result = self._request(m850.cmd_activate(sheet), sheet)
        if not result["active"]:
            raise PluginError("The printer didn't make sheet {} active".format(sheet + 1))
        for other in self._sheets:
            if other and other["index"] != sheet:
                other["active"] = False
        self._record(None, "active", old, sheet, note)

    # -- hooks -------------------------------------------------------------

    def on_gcode_received(self, comm, line, *args, **kwargs):
        try:
            event = self._parser.feed(line)
        except Exception:
            self._logger.exception("Failed to parse %r", line)
            return line
        if event is None:
            return line
        kind, value = event
        waiter = self._waiter
        if kind == "sheet":
            answers_waiter = bool(waiter and waiter["sheet"] == value["index"])
            # After a write the firmware echoes the Z it was sent, not raw / steps,
            # so only a read can tell us the steps per mm.
            self._store_sheet(value, learn=not answers_waiter or waiter["read"])
            if answers_waiter:
                waiter["result"] = self._sheets[value["index"]]
                waiter["event"].set()
            elif not self._refreshing:
                self._push()
        elif kind == "error" and waiter:
            waiter["error"] = value
            waiter["event"].set()
        return line

    def on_gcode_queuing(self, comm, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        if not self._shift_active or not tags or "source:file" not in tags:
            return None
        try:
            out = self._shifter.process(cmd)
        except Exception:
            self._logger.exception("Z shift failed on %r, sending it unchanged", cmd)
            return None
        if len(out) == 1 and out[0] == cmd:
            return None
        return out

    def _store_sheet(self, value, learn=True):
        steps = m850.learn_steps_per_mm(value["raw"], value["z_reported"]) if learn else None
        if steps and steps != self._steps:
            self._logger.info("Learned Z steps/mm from the printer: %s", steps)
            self._steps = steps
        sheet = dict(value)
        sheet["z"] = _round(m850.raw_to_mm(sheet["raw"], self._steps))
        self._sheets[sheet["index"]] = sheet
        if sheet["active"]:
            for other in self._sheets:
                if other and other["index"] != sheet["index"]:
                    other["active"] = False

    # -- events --------------------------------------------------------------

    def on_event(self, event, payload):
        payload = payload or {}
        if event == Events.FIRMWARE_DATA:
            name = (payload.get("data") or {}).get("FIRMWARE_NAME") or payload.get("name")
            is_prusa, version, supported = m850.parse_firmware(name)
            self._firmware = {"name": name, "version": ".".join(map(str, version)) if version else None,
                              "supported": supported}
            self._logger.info("Firmware %r, M850 supported: %s", name, supported)
            if supported:
                self._refresh_async()
            else:
                self._push()
        elif event == Events.DISCONNECTED:
            self._sheets = [None] * m850.MAX_SHEETS
            self._firmware = {"name": None, "version": None, "supported": None}
            self._parser = m850.ReplyParser()
            self._end_job()
            self._push()
        elif event == Events.FILE_SELECTED:
            threading.Thread(target=self._analyze_selected, args=(payload,),
                             name="prusa_livez_analyze", daemon=True).start()
        elif event == Events.FILE_DESELECTED:
            self._ladder_info = None
            self._ladder_path = None
            self._ladder_enabled = False
            self._push()
        elif event == Events.PRINT_STARTED:
            self._start_job(payload)
        elif event == Events.PRINT_RESUMED:
            self._shifter.mark_unknown()
        elif event in (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED):
            self._finish_job(event)

    def _analyze_selected(self, payload):
        self._ladder_info = None
        self._ladder_path = None
        self._ladder_enabled = False
        if payload.get("origin") == "local" and payload.get("path"):
            try:
                path = self._file_manager.path_on_disk("local", payload["path"])
                info = ladder_mod.analyze_file(path)
            except Exception:
                self._logger.exception("Couldn't analyze %s", payload.get("path"))
                info = None
            if info:
                self._ladder_info = info
                self._ladder_path = payload["path"]
                if info.get("detected"):
                    self._ladder_enabled = bool(info.get("named"))
                    saved = self._settings.get(["ladder_offsets"]) or []
                    if len(saved) == info["bands"]:
                        self._ladder_offsets = list(saved)
                    else:
                        step = float(self._settings.get(["ladder_step"]))
                        self._ladder_offsets = ladder_mod.default_offsets(info["bands"], step)
        self._push()

    def _active_sheet(self):
        return next((s for s in self._sheets if s and s["active"]), None)

    def _start_job(self, payload):
        sheet = self._active_sheet()
        use_ladder = (
            self._ladder_enabled
            and self._ladder_info
            and self._ladder_info.get("detected")
            and payload.get("origin") == "local"
            and payload.get("path") == self._ladder_path
        )
        self._shifter = ZShifter()
        if use_ladder:
            info = self._ladder_info
            self._shifter.set_ladder(Ladder(info["boundaries"], self._ladder_offsets, info["layer_z"], info["bbox"]))
        self._job = {
            "path": payload.get("path"),
            "origin": payload.get("origin"),
            "sheet": sheet["index"] if sheet else None,
            "ladder": bool(use_ladder),
            "offsets": list(self._ladder_offsets) if use_ladder else None,
            "nudge": 0.0,
            "started": time.time(),
        }
        # Only lines OctoPrint streams can be rewritten.
        self._shift_active = payload.get("origin") == "local"
        self._pending = None
        if self._settings.get_boolean(["notify_on_start"]):
            self._push(notice="start")
        else:
            self._push()

    def _finish_job(self, event):
        job = self._job
        self._end_job()
        if job and job["sheet"] is not None:
            if job["ladder"] and event != Events.PRINT_FAILED:
                self._pending = {"kind": "ladder", "sheet": job["sheet"], "offsets": job["offsets"],
                                 "finished": event == Events.PRINT_DONE}
            elif abs(job["nudge"]) > 1e-9:
                self._pending = {"kind": "nudge", "sheet": job["sheet"], "offset": job["nudge"],
                                 "finished": event == Events.PRINT_DONE}
        self._push()
        if self._firmware["supported"] and self._printer.is_operational():
            # Live Z may have been changed on the LCD during the print.
            self._refresh_async()

    def _end_job(self):
        self._shift_active = False
        self._shifter.set_nudge(0.0)
        self._shifter.set_ladder(None)
        self._job = None

    # -- history --------------------------------------------------------------

    def _history_path(self):
        return os.path.join(self.get_plugin_data_folder(), "history.json")

    def _load_history(self):
        try:
            with open(self._history_path(), encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                self._history = data[-HISTORY_LEN:]
        except FileNotFoundError:
            pass
        except Exception:
            self._logger.exception("Couldn't read the change history")

    def _save_history(self):
        try:
            path = self._history_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._history[-HISTORY_LEN:], handle, indent=1)
            os.replace(tmp, path)
        except Exception:
            self._logger.exception("Couldn't save the change history")

    def _record(self, sheet, field, old, new, note=None):
        entry = {
            "id": uuid.uuid4().hex[:12],
            "time": time.time(),
            "user": self._user(),
            "sheet": sheet,
            "field": field,
            "old": old,
            "new": new,
            "note": note,
        }
        self._history.append(entry)
        self._history = self._history[-HISTORY_LEN:]
        self._save_history()
        self._logger.info("Changed %s on sheet %s: %r -> %r (%s, by %s)", field, sheet, old, new,
                          note or "edit", entry["user"])
        self._push()

    # -- state --------------------------------------------------------------

    def _state(self):
        info = self._ladder_info
        return {
            "firmware": self._firmware,
            "steps_per_mm": self._steps,
            "sheets": [dict(s) if s else None for s in self._sheets],
            "refreshing": self._refreshing,
            "history": list(reversed(self._history)),
            "big_jump": self._settings.get_float(["big_jump"]),
            "nudge_cap": self._settings.get_float(["nudge_cap"]),
            "job": dict(self._job) if self._job else None,
            "shift_active": self._shift_active,
            "nudge": self._shifter.nudge if self._shift_active else 0.0,
            "pending": self._pending,
            "ladder": {
                "info": info,
                "path": self._ladder_path,
                "enabled": self._ladder_enabled,
                "offsets": self._ladder_offsets,
            },
        }

    def _push(self, **extra):
        message = {"type": "state", "state": self._state()}
        message.update(extra)
        self._plugin_manager.send_plugin_message(self._identifier, message)


__plugin_name__ = "Prusa Live Z"
__plugin_pythoncompat__ = ">=3.9,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PrusaLiveZPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.on_gcode_received,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.on_gcode_queuing,
    }
