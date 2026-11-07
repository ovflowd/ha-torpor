"""Runs torpord from a zipapp with shorter timers, so the end-to-end test takes seconds instead of minutes.

Arguments: the zipapp, then torpord's own arguments. Only the agent's tick, its pairing-key poll, the
brightness desk settle, the game settle and edge gap, and the mode reads' freshness, quiet window,
poll and retry get shorter, and the last five keep their order; everything else is the code the
systemd unit runs.
"""

from __future__ import annotations

import sys

sys.path.insert(0, sys.argv[1])

from torpor import agent, modes
from torpor.modules import brightness

agent.TICK_S = 1
agent.KEY_POLL_S = 1
brightness.DESK_SETTLE_S = 2
brightness.GAME_SETTLE_S = 1
modes.FRESH_S = 1
modes.QUIET_S = 2
brightness.EDGE_GAP_S = 3
modes.POLL_S = 4
modes.RETRY_S = 2

if __name__ == "__main__":
    sys.exit(agent.main(sys.argv[2:]))
