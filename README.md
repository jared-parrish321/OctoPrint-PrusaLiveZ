# OctoPrint Prusa Live Z

Read and set **Live Adjust Z** for each steel sheet on a Prusa i3 MK3/MK3S/MK3S+ (and MK2.5/S) from OctoPrint, switch the active sheet, nudge Z during a print, and run a **calibration ladder** that prints each band of a first-layer test at its own height.

It talks to the printer only with `M850`, the sheet command added in Prusa firmware 3.13.0. It never sends `M500`, `M502` or `D3`.

## Features

| Feature | What it does |
| --- | --- |
| Sheet table | A sidebar panel shows all 8 sheets with name, Live Z and which one is active. It refreshes on connect and after every print. |
| Edit Live Z | Nudge buttons (±0.0025, ±0.01, ±0.05 mm) or type a value, then save. Values snap to the firmware's 0.0025 mm steps. |
| Rename and switch | Rename a sheet (up to 7 characters) and make any calibrated sheet the active one. |
| Verify | Every write is read back. If the printer didn't store exactly what was asked, you see an error. |
| History and undo | The last 20 changes, with who made them and an Undo button. |
| Export and import | Save all sheets to JSON, for example before a firmware flash or factory reset, and restore them later. |
| Control tab status | A one-line "Sheet: Textur1 · Live Z -0.825 mm" above the jog controls, plus a notice when a print starts. |
| Mid-print nudge | While OctoPrint streams a print, ±0.01 and ±0.025 mm buttons shift the Z of every following move. When the print ends, you can save the total into the sheet. |
| Calibration ladder | Select a notched first-layer test, give each band its own offset (default -0.04 to +0.04 mm in 0.01 steps), print it, pick the best band, and the plugin writes the new Live Z. |

## Requirements

* Prusa firmware **3.13.0 or later** (`M115` in the Terminal tab shows it). On older firmware the panel shows a notice and stays read-only.
* OctoPrint 1.10 or later, Python 3.9 or later.

## Install

In OctoPrint: Settings > Plugin Manager > Get More > "...from URL", then paste:

```
https://github.com/jared-parrish321/OctoPrint-PrusaLiveZ/archive/refs/heads/main.zip
```

Or from a shell on the Pi (OctoPi keeps OctoPrint in `~/oprint`):

```sh
~/oprint/bin/pip install https://github.com/jared-parrish321/OctoPrint-PrusaLiveZ/archive/refs/heads/main.zip
sudo service octoprint restart
```

Once a release is tagged, OctoPrint's Software Update offers new versions from this repo's GitHub releases.

**Back up first:** before the first change, send `M850 S0` to `M850 S7` in the Terminal tab and keep the replies, or use Export sheets once the panel shows them.

## How Live Z works on Prusa firmware

Think of 8 index cards in the printer's EEPROM, one per steel sheet. Each card holds a name and a Live Z "shim". One card is clipped to the front, and that is the active sheet. The firmware applies the active card's shim at the end of mesh bed levelling (`G80`), which is the start of every print.

| Command | Effect |
| --- | --- |
| `M850 S3` | Read sheet 3 (0 to 7) |
| `M850 S3 Z-0.825` | Set Live Z of sheet 3 in mm |
| `M850 S3 LPEI` | Rename sheet 3 |
| `M850 S3 A1` | Make sheet 3 active (ignored if it has no Live Z) |

The reply looks like `Sheet 3 Z-0.8250 R-330 LTextur1 B60 P35 A1`. `R` is the stored value in Z microsteps (400 per mm on the MK3S), and `A1` marks the active sheet.

`M851` (the Marlin "probe offset") does nothing useful here. Prusa firmware never applied it, and removed it in 3.14.1.

Details this plugin handles for you, all from the firmware source:

* **Truncation.** The firmware stores `(int)(z * 400)`, rounding toward zero, so `-0.825` can land on `-329`. The plugin sends the middle of the wanted step instead, then checks `R` in the reply.
* **The empty-sheet marker.** A stored value of -1 step (-0.0025 mm) means "not calibrated". If you landed exactly there, the printer would treat the sheet as empty and switch to another sheet at the next boot. The firmware's own LCD knob has the same trap. The plugin skips that one value: the step after 0.000 is -0.005.
* **Names.** The firmware looks for parameter letters anywhere in the line, including inside a name. An upper-case `Z` in a name would overwrite Live Z, and `A` followed by a digit would switch the active sheet, so those names are refused. Use lower case for those letters. `L` is always sent last, because the name runs to the end of the line.

## Mid-print nudge

The MK3 firmware has no G-code for live babystepping (no `M290`). Only the LCD knob can do it. OctoPrint, though, is the one feeding the print file to the printer, so the plugin rewrites Z on the way out. A -0.02 nudge turns every following `Z0.2` into `Z0.18`, and inserts a small Z move before the next extrusion so the change takes effect right away.

* It works only for prints OctoPrint streams, not prints from the printer's SD card.
* A nudge lands after the moves already buffered, roughly 1 to 2 seconds of printing.
* The total is capped at ±0.30 mm, and the nozzle is never sent below 0.05 mm. Nudges are locked while the print is paused.
* At the end, the panel offers to add the total to the sheet's Live Z. Nothing is written unless you click Write.

## Calibration ladder

When you select a single-layer print whose outline has notches on its left edge, the plugin finds the bands between the notches. Prusa's "FirstLayerCalibration" pad has 8 notches and 9 bands. If the object name matches, the ladder is switched on automatically. Otherwise it's offered but left off.

1. Check or edit the offsets in the sidebar. Band 1 is at the front of the bed, nearest you, and band 5 is your current Live Z.
2. Print. Every extrusion move gets `Z = 0.2 + its band's offset`. Lines that cross a band edge are split there.
3. When it finishes, pick the band with the best first layer. The panel shows the new Live Z, and **Write to sheet** stores it.

Band heights are kept between 0.10 and 0.35 mm.

## Safety rules

* No Live Z, name or sheet changes while a print is running, paused or cancelling. The server enforces this, not just the buttons.
* A change of more than 0.10 mm asks for confirmation.
* Editing needs the **Prusa Live Z: Edit Live Z** permission, which only the Admins group has by default. Viewing needs Status. The API never answers anonymous requests.
* The UI uses text-only bindings and has no password fields, so there's nothing for a browser to autofill.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install "OctoPrint>=1.10" pytest -e .
pytest
```

The tests replay a real PrusaSlicer first-layer calibration file (`tests/data/first_layer_ladder.gcode`) through the Z rewriter and check every band's height, the filament total and the band edges. They also check that `M850 Z` values survive the firmware's float truncation.

## Sources

* Prusa firmware `M850`: [Marlin_main.cpp](https://github.com/prusa3d/Prusa-Firmware/blob/v3.14.1/Firmware/Marlin_main.cpp) (`case 850`), sheet storage in [eeprom.h](https://github.com/prusa3d/Prusa-Firmware/blob/v3.14.1/Firmware/eeprom.h), and how Live Z is applied (`babystep_apply`) in [mesh_bed_calibration.cpp](https://github.com/prusa3d/Prusa-Firmware/blob/v3.14.1/Firmware/mesh_bed_calibration.cpp)
* [RepRap wiki: M850](https://reprap.org/wiki/G-code#M850:_Sheet_parameters)
* OctoPrint hooks: [gcode queuing and received](https://docs.octoprint.org/en/master/plugins/hooks.html), [permissions](https://docs.octoprint.org/en/master/plugins/hooks.html#octoprint-access-permissions), [SimpleApiPlugin](https://docs.octoprint.org/en/master/plugins/mixins.html#simpleapiplugin)

## License

MIT
