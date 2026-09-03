# Traccia — tool-using career agent

An agent that reads a conversation with a professional and decides what — if
anything — should change on their career profile, making only those changes
through the tools in `traccia_store.py` (which is untouched).

## How to run

```bash
pip install -r requirements.txt          # anthropic SDK, python-dotenv, pytest
cp .env.example .env                     # then put your ANTHROPIC_API_KEY in .env

# one conversation (the shape you will run on an unseen file)
python agent.py --profile data/profile.json --conversation data/conversation_C-204.json

# all four scenarios -> traces/s1..s4 (.json + .txt)
python run_all.py

# chat mode
python agent.py --chat

# tests (no API key needed)
python -m pytest -q
```

Model: `claude-sonnet-5` (override with `TRACCIA_MODEL`). Seed: `TRACCIA_SEED=4`.

## How it works

Two files, ~450 lines in total.

```
conversation ──> agent.py  run_agent()  ──> Claude (Messages API via anthropic SDK)
                       │                        │  tool_use blocks
                       ▼                        ▼
                 toolkit.py  Executor.run()  ── policy checks ──> traccia_store.call_tool()
                       │
                       └──> trace: every call, its arguments, result, error, retries
```

**`agent.py`** — the loop. Send system prompt + tool list + transcript; for
each `tool_use` block Claude returns, call `Executor.run()`, append the result,
repeat; stop when Claude replies with text (or after 16 turns). Batch mode and
chat mode share this loop. The system prompt is eight rules that map directly
onto the scenarios (ground everything, search before writing, retractions win,
contradictions go to a human, destructive actions need approval, verify
certifications, be quiet when there is nothing, be frugal).

**`toolkit.py`** — the safety rails. The store is permissive by design, so the
executor enforces what it won't, *before* a call reaches the store:

- every write must cite `evidence_message_ids` that exist in this run's
  conversations — no invented evidence;
- duplicate facts and duplicate achievement titles are refused, with an error
  that tells the model what to do instead;
- a certification can be `supported` only after `verify_certification`
  returned that name this run;
- `verify_certification` is retried (3 attempts, backoff) before its failure
  is passed to the model as an ordinary tool error;
- `overwrite_achievement` needs a token a *human* approved. The model can
  request approval but can never grant it. **In batch mode the harness
  simulates the human** (the trace says so on every such call); in chat mode
  it is a real y/N prompt on stdin; `--no-auto-approve` shows the refusal path;
- a call budget stops runaway loops; every call, refused or not, is logged.

`TOOLS` in `toolkit.py` is what Claude reads about each tool. The wording
carries the policy ("search before writing", "zero or multiple registry
matches ⇒ ask, don't store") — that is half of what steers tool choice.

## The four scenarios (`traces/`, produced by claude-sonnet-5)

- **S1** (C-204): 2 searches, reads A-001, calls the registry (one timeout,
  retried, then two candidate matches); recognises that "automating our motor
  claims intake process" is the existing achievement A-001 under another name
  and enriches it (confirmation → overwrite) with contribution, outcome
  (18→7 min, ~120 handlers), period (Nov 2025) and evidence M2/M4/M6/M8/M11;
  does not record the retracted "managed the engineering team" claim; asks
  one follow-up naming the two candidate certifications instead of storing
  the vague one. 7 calls, 1 write.
- **S2** (C-205 on S1 state): "entirely responsible for delivering it"
  conflicts with M4/M11, so nothing is written — it is parked via
  `flag_for_human_review`. The 60% figure is consistent with the stored
  18→7 min, so no change is needed. 4 calls, 0 writes.
- **S3** (C-206 on S1 state): nothing new — 3 read-only calls to answer "how
  is my profile looking", 0 writes, 0 flags, 0 follow-ups.
- **S4** (C-204 with `TRACCIA_FORCE_FAIL=1`): the registry fails all 3
  attempts (one call with `retries: 2`), no certification is stored, one
  follow-up asks for the exact name; A-001 is enriched as in S1 and the
  corrected coordination claim is stored as a skill (evidence M11). 8 calls,
  2 writes.

Runs are not byte-identical between executions — the model sometimes stores
the M11 coordination claim as a separate skill and sometimes folds it into the
achievement — but the invariants hold every time: no write without evidence,
no duplicate, no unverified certification, no overwrite without approval.

`store_after` in each trace is a raw `dump_store()`.

## Tests

Five assertions in `tests/test_agent.py`, no API key needed: invented or
missing evidence is refused; duplicates are refused; an overwrite without human
approval fails and leaves the record untouched; a dead registry becomes a
normal error after 2 retries and blocks a "supported" certification; the loop
reports an unknown-tool request back to the model instead of crashing (using a
two-line scripted fake model).

## Which model and why

`claude-sonnet-5`: strong multi-step tool use at low cost and latency, which
is what this task is — many small decisions, no long-form generation. A run of
all four scenarios is roughly 25 model calls.

## Known wrong or missing

- The model kept the original title "Claims workflow redesign" on A-001, where
  "Motor claims intake automation" is arguably better. Renaming is a product
  call, not one the agent should make alone.
- `search_profile` is lexical and does not index achievement outcomes, so some
  legitimate matches are missed; the prompt asks for a second query to soften
  this.
- Missing tool: **`update_achievement(fields)`** — a partial, reversible
  enrichment. The only way to add an outcome to an existing achievement is the
  irreversible `overwrite_achievement`, which forces human approval for what
  is really an additive edit. I used confirmation + overwrite with the full
  merged record instead.
- Missing tool: a way to **retract or supersede a profile fact** — policy can
  refuse duplicates, but nothing can mark an old fact stale.
- No token or cost accounting.

## AI tools used

Built with **Claude Code** (Anthropic): the loop, policy layer, tests and this
README were written iteratively with it against the assignment spec, then
reviewed and simplified by hand. No other AI tools.
