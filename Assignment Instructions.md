# Traccia — Tool-Using Career Agent

**Take-home assignment · AI Engineer**
Please spend **no more than 3 hours.** We would rather see a small, careful system than a large unfinished one. 
If you run out of time, stop and write down what you left out.

---

## The problem

Traccia is a platform for career professionals and job seekers. 
The platform uses a bot that maintains the professional's or a job seeker's career profile up to date by talking to them and eliciting updates about their career and their achievements.
This profile is an inimmutable record of the professional / job seeker. This profile powers various other features in the platform and cannot be vague.
After each conversation with the professional / job seeker, the AI model powering the bot has to decide if anything should change on the underlying current profile based on the conversation.
That decision is harder than it sounds. People overstate things, then correct themselves. They mention work that is already on their profile under a different name. They say "a cloud certification" without mentioning exactly which one. 
A system that writes down everything it hears will quietly fill the profile with things that are not true. This leads to an inaccurate profile which should not be the case.
Your job is to build the part that decides exactly what is to be updated on the profile based on the conversation just had.


## What you are building

A Python program that reads a conversation and updates Maya Rao's profile by calling tools.
We give you the profile store and the tools. You write everything above them: the agent loop, the decision about which tool to call, the arguments, the validation, and the error handling.
You are **not** building a database, a frontend, authentication or a deployment.

---

## What we give you

```
traccia_store.py          the profile store and the tools
data/profile.json         Maya Rao's current profile (P-1007)
data/conversation_C-204.json
data/conversation_C-205.json
data/conversation_C-206.json
```

`load_profile()` sets up the store. `call_tool(name, arguments)` is the one entry point — wrap it however you like, but do not change what it does. `dump_store()` gives you a snapshot.

### The tools

| Tool | What it does |
|---|---|
| `search_profile(query)` | Searches what is already on the profile |
| `get_achievement(id)` | Reads one achievement in full |
| `propose_achievement(payload)` | Adds an achievement. Rejects it if you do not say which messages support it |
| `update_profile_field(...)` | Adds a skill, role, certification, project or career event |
| `overwrite_achievement(...)` | Replaces an achievement. Irreversible, and needs human approval |
| `request_confirmation(...)` | Asks a human to approve a destructive action |
| `verify_certification(name)` | Checks a certification against an outside registry |
| `flag_for_human_review(...)` | Parks something for a person to look at |
| `ask_followup(question)` | Asks the professional one question |

Two of these behave awkwardly on purpose, which is realistic:

- `update_profile_field` barely validates anything. It will store nonsense and duplicates if you let it.
- `verify_certification` fails about 40% of the time, and for a vague name it returns several possible matches rather than one answer.

**You do not write the tools.** Do not change what they do. You do own everything else, including the descriptions and schemas you show the model — how a tool is described drives whether it gets picked, so treat that as part of your design.

If you find you are missing a tool, do not force it. Say so in the README: which tool, what it would do, and what you did instead.

---

## Run it on these four cases

Save the output of each.

| | Input | |
|---|---|---|
| **S1** | profile + C-204 | The main case |
| **S2** | S1 state, then C-205 | A later claim that contradicts an earlier one |
| **S3** | S1 state, then C-206 | A conversation with nothing new in it |
| **S4** | S1 with `TRACCIA_FORCE_FAIL=1` | The certification registry is down the whole run |

Set `TRACCIA_SEED` if you want repeatable failures, and say in your README which seed you used.

Your batch runner must take the conversation file as an argument, not hardcode these three:

```
$ python agent.py --profile data/profile.json --conversation data/conversation_C-204.json
```

We will run it on a conversation you have not seen.

---

## What to send us

**1. The code**, as a Git repo or a ZIP.

**2. A log per case**, in `traces/` — `s1.json` through `s4.json`, plus whatever your console printed (`s1.txt`) if that is easier to read.

We need to follow what happened without running it. Roughly this shape:

```json
{
  "scenario": "S1",
  "model": "claude-sonnet-4-5",
  "seed": 7,
  "summary": {
    "tool_calls": 9,
    "writes": 4,
    "errors": 1,
    "what_changed": "Enriched A-001 with outcome and period; added 3 skills; asked 1 follow-up about the certification"
  },
  "calls": [
    {
      "n": 1,
      "tool": "search_profile",
      "arguments": { "query": "motor claims intake automation" },
      "result": { "result_count": 2, "results": ["A-001", "F-004"] },
      "error": null,
      "retries": 0
    }
  ],
  "final_message": "Thanks Maya — I have updated ...",
  "store_after": { }
}
```

The `what_changed` line and `store_after` (just `dump_store()`) matter most — they let us see the end state at a glance. Truncate long results if you like, but do not drop the arguments.

**3. A few tests.** Not a framework — three or four assertions that would fail if the agent started behaving badly. Pick the ones you think matter.

**4. A README** covering:

- how to run it
- how it works, briefly
- which model you used and why
- what you know is wrong or missing
- which AI coding tools you used, and for what

Keep the README short. We will ask the rest in person.

---

## Also build a chat mode

As well as running the saved files, let us type at it:

```
$ python agent.py --chat
you> I also presented the results to the board last month.
[tool] search_profile(query="board presentation") -> 0 results
agent> Noted. Which month, and what came out of it?
```

Show the tool calls as they happen. An `input()` loop is fine — no web server, no UI.

We will type messages you have not seen into this on Monday and talk about what your agent does with them.

---

## Rules

- 3 hours maximum.
- **Plain Python.** No LangChain, LangGraph, CrewAI or similar. Call the model API directly and write the loop yourself — we want to see your control flow, not a framework's.
- Pydantic or similar for structured output is welcome. So are `httpx`, `tenacity`, `pytest`.
- Any model you like, commercial or open.
- Command line only. No frontend, no auth, no database, no deployment.
- Mocking something is fine if you say so.
- Use coding assistants if you want. Tell us what you used, and be ready to explain and change any line of it.
- No real personal or employer data.

We give no extra credit for a nice interface, a cloud deployment, or a multi-agent architecture.

---

## What we are looking at

| | |
|---|---|
| **Tool choices** | Did it check the profile before writing? Did it call tools when it did not need to? |
| **Grounded arguments** | Is every claim tied to a specific message? Did it invent anything it was not told? |
| **Failure handling** | Timeouts, errors, bad model output, malformed arguments |
| **Care with destructive actions** | What the agent is allowed to do without a human |
| **Logs** | Can we debug a run from your log alone? |
| **Tests** | Would they catch a real regression? |
| **Clarity** | Short README, honest about limitations |

---

## The day of the discussion

30 minutes. You demo it briefly, we walk through your logs together, we type a few new messages into your chat mode, and we work through one change request. Bring questions.
