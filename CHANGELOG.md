# Changelog

## Unreleased

- Parsing no longer uses regular expressions. M850 replies, G-code words, sheet names, the firmware version and slicer comments are read by small hand-written functions (`textscan.py`). Behaviour is unchanged; new unit tests cover the edge cases.

## 1.0.0

- First release: sheet table, Live Z editing, rename, active sheet switch, read-back verification, change history with undo, export and import, Control tab status, print-start notice, mid-print Z nudge, calibration ladder.
