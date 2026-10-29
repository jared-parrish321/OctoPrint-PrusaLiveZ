"""Snapshot of the full calibration ladder rewrite.

The first run stores the rewritten G-code in __snapshots__/. Later runs fail
if any line changes. If a change is intended, review the diff and update
the snapshot with ``pytest --snapshot-update``.
"""

import os

from octoprint_prusa_livez import ladder
from octoprint_prusa_livez.zshift import Ladder, ZShifter

LADDER_FILE = os.path.join(os.path.dirname(__file__), "data", "first_layer_ladder.gcode")


def test_ladder_rewrite_matches_snapshot(snapshot):
    info = ladder.analyze_file(LADDER_FILE)
    shifter = ZShifter()
    shifter.set_ladder(Ladder(info["boundaries"], ladder.default_offsets(info["bands"]),
                              info["layer_z"], info["bbox"]))
    out = []
    with open(LADDER_FILE) as handle:
        for raw in handle:
            line = raw.split(";", 1)[0].strip()
            if line:
                out.extend(shifter.process(line))
    assert "\n".join(out) == snapshot
