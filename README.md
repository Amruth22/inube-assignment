# Traccia: a tool-using career agent

This is my take-home for the AI Engineer role. The agent reads a conversation
between a professional and the Traccia bot, works out what (if anything) should
change on their career profile, and makes only those changes through the tools
in `traccia_store.py`. I did not touch that file.

## Running it

```bash
pip install -r requirements.txt          # anthropic SDK, python-dotenv, pytest
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env  # your key; .env is git-ignored

# one conversation (this is the shape you will run on an unseen file)
python agent.py --profile data/profile.json --conversation data/conversation_C-204.json

# all four scenarios -> traces/s1..s4 (.json + .txt)
python run_all.py

# chat mode
python agent.py --chat

# tests, no API key needed
python -m pytest -q
```

The model is `claude-sonnet-5`. Override it with `TRACCIA_MODEL`. The seed I
used for the traces is `TRACCIA_SEED=4`.

## How it works

Two files, about 450 lines between them.

```
conversation ──> agent.py  run_agent()  ──> model (Messages API via the SDK)
                       │                        │  tool_use blocks
                       ▼                        ▼
                 toolkit.py  Executor.run()  ── policy checks ──> traccia_store.call_tool()
                       │
                       └──> trace: every call, its arguments, result, error, retries
```

`agent.py` is the loop. It sends the system prompt, the tool list and the
transcript to the model. For every `tool_use` block that comes back it calls
`Executor.run()`, appends the result, and goes again. It stops when the model
answers with text, or after 16 turns. Batch mode and chat mode share this one
loop. The system prompt is eight rules, and each rule maps onto one of the
scenarios: ground everything in a message id, search before writing,
retractions win, contradictions go to a human, destructive actions need
approval, verify certifications, stay quiet when there is nothing, and be
frugal with calls.

`toolkit.py` is where the discipline lives. The store is permissive on purpose,
so the executor enforces what the store won't, before a call ever reaches it.
Every write has to cite `evidence_message_ids` that exist in this run's
conversations. Duplicate facts and duplicate achievement titles are refused
with an error that tells the model what to do instead. A certification can be
stored as `supported` only if `verify_certification` returned that exact name
in this run. The registry is retried three times with backoff, and if it is
still down the failure goes back to the model as an ordinary tool error.
`overwrite_achievement` needs a token a human approved. The model can ask for
approval but can never grant it. In batch mode the harness plays the human,
and every trace entry says so. In chat mode it is a real y/N prompt on stdin.
`--no-auto-approve` shows what happens when the human says no. A call budget
stops runaway loops, and every call is logged whether it was refused or not.

`TOOLS` in `toolkit.py` is what the model reads about each tool. I wrote those
descriptions myself rather than reusing the ones in the store, because the
wording carries a lot of the policy. "Search before writing" and "zero or
multiple registry matches means ask, don't store" live in those strings, and in
my experience that steers tool choice about as much as the system prompt does.

## The four scenarios

These are in `traces/`.

S1 (C-204) is the main case. The agent searches twice, reads A-001, and calls
the registry. The registry times out once, gets retried, then returns two
candidate matches. The agent recognises that "automating our motor claims
intake process" is the existing achievement A-001 under a different name and
enriches it (confirmation, then overwrite) with the contribution, the outcome
(18 to 7 minutes, about 120 handlers), the period (November 2025) and evidence
M2, M4, M6, M8 and M11. It does not record the retracted "managed the
engineering team" claim. It asks one follow-up naming the two candidate
certifications instead of storing the vague one. Seven calls, one write.

S2 (C-205 on the S1 state). "Entirely responsible for delivering it" conflicts
with M4 and M11, so nothing is written and the claim goes to
`flag_for_human_review`. The 60% figure is consistent with the stored 18 to 7
minutes, so there is nothing to change. Four calls, no writes.

S3 (C-206 on the S1 state). Nothing new. Three read-only calls to answer "how
is my profile looking", no writes, no flags, no follow-ups.

S4 (C-204 with `TRACCIA_FORCE_FAIL=1`). The registry fails all three attempts
inside one call. No certification is stored and one follow-up asks for the
exact name. A-001 is enriched exactly as in S1, and the corrected coordination
claim is stored as a skill with M11 as evidence. Eight calls, two writes.

Runs are not byte-identical from one execution to the next. Sometimes the model
stores the M11 coordination claim as a separate skill and sometimes it folds it
into the achievement. What holds every time is the set of invariants: no write
without evidence, no duplicate, no unverified certification, no overwrite
without approval.

`store_after` in each trace is a raw `dump_store()`.

## Tests

Five assertions in `tests/test_agent.py`, none of which need an API key.
Invented or missing evidence is refused. Duplicates are refused. An overwrite
without human approval fails and leaves the record untouched. A dead registry
turns into a normal error after two retries and blocks a `supported`
certification. And the loop reports an unknown tool request back to the model
instead of crashing, tested with a two-line scripted fake model.

## Model choice

I went with `claude-sonnet-5` because this task is many small decisions with
no long-form generation, and Sonnet handles multi-step tool use well at low
cost and latency. Extended thinking is off. The policy is in the prompt and the
executor, not in the model's reasoning, and without thinking each call is
faster and cheaper. A full run of all four scenarios is roughly 25 model calls.

## What I know is wrong or missing

The model kept the original title "Claims workflow redesign" on A-001, where
"Motor claims intake automation" is arguably better. I decided renaming is a
product call and not something the agent should do on its own.

`search_profile` is lexical and does not index achievement outcomes, so some
legitimate matches get missed. The prompt asks for a second query to soften
this, which is a workaround rather than a fix.

The tool I missed most is a partial `update_achievement(fields)`. Adding an
outcome to an existing achievement is an additive, reversible edit, but the
only way to do it is the irreversible `overwrite_achievement`, which drags in
human approval for something that should not need it. I used confirmation plus
overwrite with the full merged record instead.

There is also no way to retract or supersede a profile fact. The policy can
refuse duplicates, but nothing can mark an old fact as stale.

There is no token or cost accounting.

## AI tools used

I used an AI coding assistant for this. The loop, the policy layer, the tests
and the first draft of this README were written iteratively with it against the
assignment spec, then I reviewed and simplified them by hand. I can explain and
change any line of it.
