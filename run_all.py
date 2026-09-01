"""run_all.py — produce traces/s1..s4 for the four required scenarios.

S1  fresh profile + C-204
S2  S1 end state, then C-205   (contradiction of earlier evidence)
S3  S1 end state, then C-206   (nothing new)
S4  fresh profile + C-204 with TRACCIA_FORCE_FAIL=1 (registry down all run)

S2 and S3 start from a snapshot of S1's store, exactly as the assignment
describes. Human approval of destructive actions is simulated (--auto-approve
semantics); see README.
"""

from __future__ import annotations

import os
import sys

# Seed 4 is documented in the README: the first registry call fails once,
# succeeds on retry, and returns two candidate matches — so S1 exercises both
# the retry path and the ambiguity path, while S4 covers the full outage.
os.environ.setdefault("TRACCIA_SEED", "4")

from agent import default_model, run_batch          # noqa: E402
from toolkit import restore_store, snapshot_store   # noqa: E402

PROFILE = "data/profile.json"
C204 = "data/conversation_C-204.json"
C205 = "data/conversation_C-205.json"
C206 = "data/conversation_C-206.json"


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else default_model()

    run_batch(PROFILE, C204, model_name=model, scenario="S1",
              trace_out="traces/s1.json", console_out="traces/s1.txt")
    s1_state = snapshot_store()

    print()
    restore_store(s1_state)
    run_batch(PROFILE, C205, context_paths=[C204], model_name=model, scenario="S2",
              fresh_store=False, trace_out="traces/s2.json",
              console_out="traces/s2.txt")

    print()
    restore_store(s1_state)
    run_batch(PROFILE, C206, context_paths=[C204], model_name=model, scenario="S3",
              fresh_store=False, trace_out="traces/s3.json",
              console_out="traces/s3.txt")

    print()
    os.environ["TRACCIA_FORCE_FAIL"] = "1"
    try:
        run_batch(PROFILE, C204, model_name=model, scenario="S4",
                  trace_out="traces/s4.json", console_out="traces/s4.txt")
    finally:
        del os.environ["TRACCIA_FORCE_FAIL"]


if __name__ == "__main__":
    main()
