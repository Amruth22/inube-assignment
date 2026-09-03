"""run_all.py — produce traces/s1..s4 for the four required scenarios.

S1  fresh profile + C-204
S2  S1 end state, then C-205   (a later claim contradicts earlier evidence)
S3  S1 end state, then C-206   (nothing new)
S4  fresh profile + C-204 with TRACCIA_FORCE_FAIL=1 (registry down all run)

Each scenario writes traces/<name>.json (the structured log) and
traces/<name>.txt (what the console printed).
"""

import contextlib
import copy
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
os.environ.setdefault("TRACCIA_SEED", "4")   # documented in the README

import traccia_store as store                # noqa: E402
from agent import run_conversation           # noqa: E402

PROFILE = "data/profile.json"
C204, C205, C206 = (f"data/conversation_C-{n}.json" for n in (204, 205, 206))


class Tee:
    """Print to the terminal and to a file at the same time."""
    def __init__(self, file):
        self.file, self.stdout = file, sys.stdout

    def write(self, text):
        self.stdout.write(text)
        self.file.write(text)

    def flush(self):
        self.stdout.flush()
        self.file.flush()


def run(name, **kwargs):
    os.makedirs("traces", exist_ok=True)
    with open(f"traces/{name}.txt", "w", encoding="utf-8") as log, contextlib.redirect_stdout(Tee(log)):
        trace = run_conversation(scenario=name.upper(), **kwargs)
    with open(f"traces/{name}.json", "w", encoding="utf-8") as fh:
        json.dump(trace, fh, indent=2, ensure_ascii=False)
    print(f"(trace written to traces/{name}.json)\n")


def restore(snapshot):
    """Put the in-memory store back to a dump_store() snapshot."""
    for field, value in snapshot.items():
        setattr(store.STORE, field, copy.deepcopy(value))


def main():
    run("s1", profile_path=PROFILE, conversation_path=C204)
    after_s1 = store.dump_store()

    restore(after_s1)
    run("s2", profile_path=PROFILE, conversation_path=C205, earlier=[C204], reload_profile=False)

    restore(after_s1)
    run("s3", profile_path=PROFILE, conversation_path=C206, earlier=[C204], reload_profile=False)

    os.environ["TRACCIA_FORCE_FAIL"] = "1"
    try:
        run("s4", profile_path=PROFILE, conversation_path=C204)
    finally:
        del os.environ["TRACCIA_FORCE_FAIL"]


if __name__ == "__main__":
    main()
