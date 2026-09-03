"""agent.py — Traccia profile-update agent.

Batch:  python agent.py --profile data/profile.json --conversation data/conversation_C-204.json
Chat:   python agent.py --chat

How it works, in one paragraph:
    The conversation is sent to Claude together with the tool list. Claude
    replies either with tool calls or with a final message. Every tool call is
    passed to Executor.run() in toolkit.py, which checks it against our policy
    and only then calls traccia_store. The result goes back to Claude and the
    loop repeats until Claude writes its final message. Every call is logged.
"""

import argparse
import json
import os

import anthropic
from dotenv import load_dotenv

import traccia_store as store

load_dotenv()  # picks up ANTHROPIC_API_KEY (and TRACCIA_* settings) from a local .env file
from toolkit import TOOLS, Executor

MODEL = os.environ.get("TRACCIA_MODEL", "claude-sonnet-5")
MAX_TURNS = 16  # safety cap on model round-trips per conversation

SYSTEM_PROMPT = """\
You are the profile-update agent for Traccia, a career platform. After each
conversation with a professional, you decide what — if anything — changes on
their profile, and you make only those changes, through tools.

The profile is a durable record other features depend on. A wrong or vague
entry is worse than a missing one. Follow these rules strictly:

1. GROUND EVERYTHING. Every write must cite evidence_message_ids — the exact
   messages where the professional themselves stated the fact. Never store
   anything they did not say; never sharpen a number or a title beyond their
   words; never treat the interviewer's questions as facts.
2. SEARCH BEFORE WRITING. People describe existing profile items under new
   names. Search the profile first; if a new story matches an existing
   achievement, enrich that achievement instead of creating a duplicate.
3. RETRACTIONS WIN. If the professional walks a claim back ("not exactly…"),
   record only the corrected version, never the original claim.
4. CONTRADICTIONS ARE NOT YOURS TO SETTLE. If a new statement conflicts with
   earlier, more specific evidence (e.g. sole-credit claims after describing a
   shared effort), do not update the profile — flag_for_human_review with the
   message ids involved.
5. DESTRUCTIVE ACTIONS NEED A HUMAN. overwrite_achievement is irreversible:
   first request_confirmation and proceed only if the response says
   approved=true. If approval is not granted, flag the proposed change for
   review instead.
6. CERTIFICATIONS: verify_certification first. Store as "supported" only when
   the registry returns exactly one match for what they named. Zero matches,
   several matches, or a registry outage: store nothing and ask one follow-up
   naming the candidates. The registry cannot confirm the person holds the
   certification — reflect real uncertainty in confidence.
7. BE QUIET WHEN THERE IS NOTHING. Small talk and questions about the platform
   change nothing: make no writes and few or no tool calls.
8. BE FRUGAL: one focused follow-up question at most; no redundant reads.

When you are done, reply with a short message to the professional summarising
exactly what you changed, what you held back and why. Do not invent changes
you did not make."""


# ---------------------------------------------------------------------------
# 1. Talking to Claude  (official SDK, no framework)
# ---------------------------------------------------------------------------

_client = None


def ask_claude(messages):
    """One call to the Messages API. The SDK reads ANTHROPIC_API_KEY and
    retries rate limits / server errors on its own (max_retries below)."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    reply = _client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT,
                                    tools=TOOLS, messages=messages)
    return reply.to_dict()   # plain dicts, so the loop below is easy to read and test


# ---------------------------------------------------------------------------
# 2. The agent loop
# ---------------------------------------------------------------------------

def run_agent(executor, messages, ask=ask_claude):
    """Loop: ask Claude -> run its tool calls -> feed results back -> repeat."""
    for _ in range(MAX_TURNS):
        reply = ask(messages)
        tool_calls = [b for b in reply["content"] if b["type"] == "tool_use"]

        if not tool_calls:  # no tools requested: Claude is done, return its text
            return "".join(b.get("text", "") for b in reply["content"]
                           if b["type"] == "text").strip()

        messages.append({"role": "assistant", "content": reply["content"]})
        results = []
        for call in tool_calls:
            result, error = executor.run(call["name"], call.get("input") or {})
            print(f"[tool] {call['name']}({short(call.get('input'))}) -> "
                  f"{error or short(result)}")
            results.append({"type": "tool_result", "tool_use_id": call["id"],
                            "content": json.dumps(error or result, default=str),
                            "is_error": bool(error)})
        messages.append({"role": "user", "content": results})

    return "(stopped: too many turns without a final answer)"


def short(value, limit=150):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def build_prompt(profile, earlier_conversations, conversation):
    """The first user message: who the profile belongs to + the transcript(s)."""
    parts = [f"Profile owner: {profile['name']} ({profile['professional_id']}), "
             f"{profile['current_role']} at {profile['current_employer']}. "
             "Use the tools to inspect what is on the profile — do not assume."]
    for conv in earlier_conversations:
        parts.append(transcript(conv, "EARLIER CONTEXT (already processed — do not "
                                      "re-apply; its message ids are valid evidence)"))
    parts.append(transcript(conversation, "PROCESS THIS conversation for profile updates"))
    parts.append("Decide what should change, apply it via tools, then reply to the professional.")
    return "\n\n".join(parts)


def transcript(conv, label):
    lines = [f"--- {label}: conversation {conv.get('conversation_id', '?')} ---"]
    lines += [f"{m['id']} [{m['speaker']}]: {m['text']}" for m in conv["messages"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Batch mode: one conversation file in, one trace out
# ---------------------------------------------------------------------------

def run_conversation(profile_path, conversation_path, earlier=(), scenario="run",
                     approve="auto", reload_profile=True):
    with open(profile_path) as fh:
        profile = json.load(fh)
    if reload_profile:
        store.load_profile(profile_path)

    earlier_convs = [json.load(open(p)) for p in earlier]
    with open(conversation_path) as fh:
        conversation = json.load(fh)
    if conversation.get("professional_id", profile["professional_id"]) != profile["professional_id"]:
        raise SystemExit("conversation belongs to a different professional than the profile")

    executor = Executor(approve=approve)
    for conv in earlier_convs + [conversation]:           # only these ids count as evidence
        executor.known_message_ids.update(m["id"] for m in conv["messages"])

    print(f"== {scenario}: {conversation.get('conversation_id')} ({MODEL}) ==")
    messages = [{"role": "user", "content": build_prompt(profile, earlier_convs, conversation)}]
    final = run_agent(executor, messages)
    print(f"agent> {final}")

    return {
        "scenario": scenario,
        "model": MODEL,
        "seed": int(os.environ.get("TRACCIA_SEED", "7")),
        "force_fail": os.environ.get("TRACCIA_FORCE_FAIL") == "1",
        "conversation": conversation.get("conversation_id"),
        "approval": approve + (" (human approval simulated by the harness)" if approve == "auto" else ""),
        "summary": executor.summary(),
        "calls": executor.trace,
        "final_message": final,
        "store_after": store.dump_store(),
    }


# ---------------------------------------------------------------------------
# 4. Chat mode: type at it, watch the tool calls
# ---------------------------------------------------------------------------

def chat(profile_path):
    with open(profile_path) as fh:
        profile = json.load(fh)
    store.load_profile(profile_path)
    executor = Executor(approve="ask")  # destructive actions ask you on stdin
    messages = [{"role": "user", "content": build_prompt(
        profile, [], {"conversation_id": "chat", "messages": []})}]

    print(f"Chat mode — you are {profile['name']}. Type 'quit' to leave.")
    n = 0
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit"):
            break
        if not text:
            continue
        n += 1
        message_id = f"U{n}"
        executor.known_message_ids.add(message_id)
        messages.append({"role": "user", "content": f"{message_id} [professional]: {text}"})
        final = run_agent(executor, messages)
        messages.append({"role": "assistant", "content": final or "(no reply)"})
        print(f"agent> {final}")
    print("\nSession summary:", json.dumps(executor.summary(), indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Traccia profile-update agent")
    ap.add_argument("--profile", default="data/profile.json")
    ap.add_argument("--conversation", help="conversation JSON to process")
    ap.add_argument("--earlier", action="append", default=[],
                    help="earlier conversation JSON given as context only (repeatable)")
    ap.add_argument("--scenario", default="run", help="label written into the trace")
    ap.add_argument("--trace-out", help="where to write the JSON trace")
    ap.add_argument("--no-auto-approve", action="store_true",
                    help="do not simulate human approval of destructive actions")
    ap.add_argument("--chat", action="store_true", help="interactive chat mode")
    args = ap.parse_args()

    if args.chat:
        chat(args.profile)
        return
    if not args.conversation:
        ap.error("--conversation is required unless --chat is given")

    trace = run_conversation(args.profile, args.conversation, earlier=args.earlier,
                             scenario=args.scenario,
                             approve="never" if args.no_auto_approve else "auto")
    if args.trace_out:
        with open(args.trace_out, "w") as fh:
            json.dump(trace, fh, indent=2, ensure_ascii=False)
        print(f"(trace written to {args.trace_out})")


if __name__ == "__main__":
    main()
