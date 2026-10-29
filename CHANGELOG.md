# Changelog

## Unreleased

- Fix: after writing Live Z, the firmware echoes the value it was sent, and the plugin could learn a wrong steps-per-mm from that echo (for example 399 after writing -0.5 mm). The next write then landed one step off and was reported as a verification error. Steps per mm are now learned only from reads.
- Python 3.10 or later is required (3.9 reached end of life in October 2025).
- Tests for the OctoPrint side of the plugin, snapshot and property-based tests, and a CI coverage job (about 98% line coverage).

## 1.0.0

- First release: sheet table, Live Z editing, rename, active sheet switch, read-back verification, change history with undo, export and import, Control tab status, print-start notice, mid-print Z nudge, calibration ladder.
