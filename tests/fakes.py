"""Stand-ins for the printer and for the parts of OctoPrint the plugin talks to.

``FakeFirmware`` answers M850 the way Prusa-Firmware 3.14.1 does
(Firmware/Marlin_main.cpp, case 850): parameters are found with strchr over
the whole line, Z is stored as (int)(z * 400) truncated toward zero, and a
stored value of -1 means "not initialized".
"""

import re
import struct

STEPS = 400.0
FIRMWARE_NAME = (
    "FIRMWARE_NAME:Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin "
    "FIRMWARE_URL:https://github.com/prusa3d/Prusa-Firmware PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa i3 MK3S"
)


def _f32(value):
    """Round a Python float to a C float, as the AVR firmware does."""
    return struct.unpack("f", struct.pack("f", value))[0]


def _strtod(text):
    match = re.match(r"\s*[-+]?(\d+\.?\d*|\.\d+)?", text)
    number = match.group(0).strip()
    return float(number) if number not in ("", "-", "+") else 0.0


def _strtol(text):
    match = re.match(r"\s*[-+]?\d+", text)
    return int(match.group(0)) if match else 0


class FakeFirmware:
    def __init__(self):
        names = ["Smooth1", "Smooth2", "Textur1", "Textur2", "Satin  ", "NylonPA", "Custom1", "Custom2"]
        self.sheets = [{"name": n, "raw": -1, "bed": 255, "pinda": 255} for n in names]
        self.sheets[0].update(raw=-330, bed=60, pinda=35)
        self.sheets[2].update(raw=-372, bed=60, pinda=35)
        self.active = 0
        self.received = []
        self.silent = False  # True: never answer, like a printer that hung
        self.override = {}  # command -> reply lines, to simulate odd firmware

    def handle(self, command):
        """Return the lines the printer would send back for one command."""
        self.received.append(command)
        if self.silent:
            return []
        if command in self.override:
            return list(self.override[command]) + ["ok"]
        word = command.split()[0].upper() if command.split() else ""
        if word == "M115":
            return [FIRMWARE_NAME, "ok"]
        if word == "M850":
            return self._m850(command) + ["ok"]
        return ["ok"]

    def _m850(self, cmd):
        def seen(letter):
            pos = cmd.find(letter)
            return None if pos < 0 else cmd[pos + 1 :]

        selected = seen("S")
        index = (_strtol(selected) & 0xFF) if selected is not None else self.active
        if index >= 8:
            return ["Invalid sheet ID. Allowed: 0..7"]
        sheet = self.sheets[index]
        z = seen("Z")
        if z is not None:
            z_value = _f32(_strtod(z))
            raw = int(_f32(z_value * STEPS))
            if raw < -3999 or raw > 0:
                return [" Z VALUE OUT OF RANGE"]
            sheet["raw"] = raw
        else:
            z_value = sheet["raw"] / STEPS
        label = seen("L")
        if label is not None and label.lstrip(" "):
            sheet["name"] = label.lstrip(" ")[:7]
        active = seen("A")
        initialized = sheet["raw"] != -1
        if active is not None and (_strtol(active) & 0xFF) and initialized:
            self.active = index
        prefix = "Sheet %d" % index
        lines = []
        if not initialized:
            lines.append(prefix + " NOT INITIALIZED")
            prefix = ""
        lines.append(prefix + " Z%.4f R%d L%s B%d P%d A%d" % (
            z_value, sheet["raw"], sheet["name"], sheet["bed"], sheet["pinda"], 1 if self.active == index else 0))
        return lines


class FakePrinter:
    """The bits of OctoPrint's printer the plugin uses. Replies arrive at once."""

    def __init__(self, firmware):
        self.firmware = firmware
        self.plugin = None
        self.operational = True
        self.printing = False
        self.paused = False
        self.pausing = False
        self.cancelling = False

    def is_operational(self):
        return self.operational

    def is_printing(self):
        return self.printing

    def is_paused(self):
        return self.paused

    def is_pausing(self):
        return self.pausing

    def is_cancelling(self):
        return self.cancelling

    def commands(self, commands, tags=None):
        if isinstance(commands, str):
            commands = [commands]
        for command in commands:
            for line in self.firmware.handle(command):
                self.plugin.on_gcode_received(None, line + "\n")


class FakeSettings:
    def __init__(self, defaults):
        self.values = dict(defaults)
        self.saved = 0

    def get(self, path):
        return self.values.get(path[0])

    def get_float(self, path):
        return float(self.values[path[0]])

    def get_boolean(self, path):
        return bool(self.values[path[0]])

    def set(self, path, value):
        self.values[path[0]] = value

    def save(self):
        self.saved += 1


class SyncThread:
    """Runs a thread's target straight away, so tests don't race."""

    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)
