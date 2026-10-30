# Changelog

## Unreleased

- Clearer wording: the value stored on each sheet is now called the **Z offset** everywhere (the LCD calls it Live adjust Z), and **Live Z** means changing Z during a print. The sidebar panel is titled "Prusa Live Z", and the edit permission is renamed "Edit sheets and Live Z". Its key is unchanged, so existing group settings keep working.
- Fix: after writing a Z offset, the firmware echoes the value it was sent, and the plugin could learn a wrong steps-per-mm from that echo (for example 399 after writing -0.5 mm). The next write then landed one step off and was reported as a verification error. Steps per mm are now learned only from reads.
- Tests for the OctoPrint side of the plugin, snapshot and property-based tests, and a CI coverage job (about 98% line coverage).
- The regular expressions are now written with `re.VERBOSE`, one piece per line with a comment and an example beside each, and they have descriptive names. Fixed-string checks use plain string methods. Behaviour is unchanged, and new unit tests cover the parsing edge cases.

## 1.0.0

- First release: sheet table, Live Z editing, rename, active sheet switch, read-back verification, change history with undo, export and import, Control tab status, print-start notice, mid-print Z nudge, calibration ladder.
