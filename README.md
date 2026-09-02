# Traccia — tool-using career agent

An agent that reads a conversation with a professional and decides what — if
anything — should change on their career profile, making only those changes
through the provided tools in `traccia_store.py`.

## How to run

```bash
pip install -r requirements.txt        # httpx (real model), pytest (tests)

# one conversation (the shape you will run on an unseen file)
python agent.py --profile data/profile.json --conversation data/conversation_C-204.json

# all four scenarios -> traces/s1..s4 (.json + .txt)
python run_all.py

# chat mode
python agent.py --chat

# tests
python -m pytest -q
```

With `ANTHROPIC_API_KEY` exported, the agent uses **claude-sonnet-4-5** via
the Messages API (raw `httpx`, no framework, no SDK). Without a key it falls
back to a deterministic **offline mock planner** (`--model mock`) and says so
on stderr. Seed: `TRACCIA_SEED=4` (documented below).

## How it works

```
conversation ──> agent.py (loop) ──> model (Anthropic API or mock planner)
                      │                      │ tool_use blocks
                      ▼                      ▼
               toolkit.Executor  ── policy checks ──> traccia_store.call_tool
                      │
                      └──> trace (every call, args, result, error, retries)
```

- **`agent.py`** — the loop: send system prompt + tool schemas + transcript,
  execute each `tool_use` the model returns, feed results back, stop on a
  final text message (capped at 16 turns). Batch and chat modes share it.
- **`toolkit.py`** — the part that keeps the profile honest. The store is
  deliberately permissive, so the executor enforces what it won't:
  - every write must cite `evidence_message_ids` that actually exist in the
    conversations of this run (no invented evidence);
  - near-duplicate facts and achievement titles are refused, with an error
    message that tells the model what to do instead;
  - a certification can only be stored `supported` after
    `verify_certification` confirmed that name this session;
  - `verify_certification` is retried (3 attempts, backoff) before its
    failure is surfaced to the model as an ordinary tool error;
  - `overwrite_achievement` only works with a token a *human* approved:
    the model can request confirmation but can never approve it;
  - a tool-call budget stops runaway loops; every call (including refused
    ones) is logged to the trace.
- **`model.py`** — two backends behind one `complete()` interface: the real
  API client with retry/backoff on 429/5xx/timeouts, and the mock planner.
- The tool descriptions shown to the model are rewritten in `toolkit.py`
  (`AGENT_TOOL_SPECS`) — they carry the policy ("search before writing",
  "zero or multiple registry matches ⇒ ask, don't store"), which is half of
  what steers tool choice.

**Human approval is simulated.** In batch mode the harness auto-approves
confirmation tokens (`request_confirmation` results are marked
`"approved (human approval SIMULATED by harness)"` in the traces); pass
`--no-auto-approve` to see the refusal path. In chat mode approval is a real
y/N prompt on stdin.

## The four scenarios (`traces/`)

`traces/s1..s4` were produced by **claude-sonnet-4-5**; `traces/mock/` holds
the same four runs from the offline planner for comparison.

- **S1** (C-204): 3 searches, then recognises that "automating our motor
  claims intake process" is the existing achievement A-001 under another
  name; enriches it (confirmation → overwrite) with contribution, outcome
  (18→7 min, ~120 handlers), period (Nov 2025), skills and evidence M2/M4/M6/M8;
  does not record the retracted "managed the engineering team" claim; asks
  one follow-up naming candidate certifications instead of storing the vague
  one. 7 calls, 1 write.
- **S2** (C-205 on S1 state): "entirely responsible for delivering it"
  conflicts with M4/M9–M11, so nothing is written — the claim is parked via
  `flag_for_human_review` citing those messages. The 60% figure is
  consistent with the stored 18→7 min, so no change is needed. 0 writes.
- **S3** (C-206 on S1 state): nothing new — 4 read-only calls to answer
  "how is my profile looking", 0 writes, 0 flags, 0 follow-ups. (The mock
  planner makes 0 calls here.)
- **S4** (C-204 with `TRACCIA_FORCE_FAIL=1`): same enrichment as S1; the
  registry fails all 3 attempts (logged as one call with `retries: 2`), no
  certification is stored, one follow-up asks for the exact name.

`store_after` in each trace is a raw `dump_store()`.

## Which model and why

`claude-sonnet-4-5`: strong multi-step tool use at low latency/cost, which is
what this task is — many small tool decisions, no long-form generation. The
offline mock planner follows the same written policy as the system prompt
and exists so the tests are deterministic and the code runs without a key.

## Known wrong or missing

- In S1 the real model asked the certification follow-up **without calling
  `verify_certification` first** (S4 did call it). Defensible — the name was
  too vague to store either way — but the system prompt says verify first,
  so the prompt/tool description could be tightened.
- The real model stored the new skills inside the achievement's `skills`
  list rather than as separate `skill` profile facts, so they are not
  findable via `search_profile` on facts. The mock does the opposite. Which
  is right is a product decision I would ask about.
- The mock planner's claim/retraction/conflict detection is regex-level; on
  genuinely unseen phrasing the real model is the answer, the mock only
  degrades to conservative behaviour (search, then ask or flag).
- `search_profile` is lexical and doesn't index achievement outcomes, so
  some legitimate matches are missed (the mock's S2 search finds nothing).
- Missing tool: **`update_achievement(fields)`** — a partial, reversible
  enrichment. The only way to add an outcome to an existing achievement is
  the irreversible `overwrite_achievement`, which forces a human approval
  for what is really an additive edit. I used confirmation + overwrite with
  the full merged record instead.
- Missing tool: a way to **supersede/retract a profile fact** — policy can
  refuse duplicates but nothing can mark an old fact stale.
- Chat mode with the mock treats each typed line independently; the real
  model keeps the whole session in context.
- No token/cost accounting; a run of all four scenarios is ~25 model calls.

## AI tools used

Built with **Claude Code** (Anthropic): the agent loop, policy layer, mock
planner, tests and this README were written iteratively with it against the
assignment spec, then reviewed and trimmed by hand. No other AI tools.
