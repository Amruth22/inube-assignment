"""agent.py — Traccia profile-update agent.

Batch:  python agent.py --profile data/profile.json --conversation data/conversation_C-204.json
Chat:   python agent.py --chat

The agent reads a conversation, decides what (if anything) should change on
the professional's profile, and makes those changes only through the guarded
tool layer in toolkit.py. See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import traccia_store as store
from model import ModelError, make_model
from toolkit import AGENT_TOOL_SPECS, Executor

MAX_STEPS = 16  # model turns per run; each turn may carry several tool calls

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


# --------------------------------------------------------------------------
# Conversation plumbing
# --------------------------------------------------------------------------

def load_conversation(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def transcript_block(conv: Dict[str, Any], label: str) -> str:
    lines = [f"--- {label}: conversation {conv.get('conversation_id', '?')} ---"]
    for m in conv["messages"]:
        lines.append(f"{m['id']} [{m['speaker']}]: {m['text']}")
    return "\n".join(lines)


def first_user_message(profile_header: Dict[str, Any], context_convs: List[dict],
                       target_conv: Dict[str, Any]) -> str:
    parts = [
        "Profile owner: "
        f"{profile_header.get('name')} ({profile_header.get('professional_id')}), "
        f"{profile_header.get('current_role')} at {profile_header.get('current_employer')}. "
        "Use the tools to inspect what is on the profile — do not assume.",
    ]
    for c in context_convs:
        parts.append(transcript_block(c, "EARLIER CONTEXT (already processed — do not "
                                         "re-apply, but its message ids are valid evidence)"))
    parts.append(transcript_block(target_conv, "PROCESS THIS conversation for profile updates"))
    parts.append("Decide what should change on the profile, apply it via tools, then "
                 "reply to the professional.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def run_loop(model, executor: Executor, messages: List[dict], printer) -> str:
    for _ in range(MAX_STEPS):
        try:
            resp = model.complete(system=SYSTEM_PROMPT, tools=AGENT_TOOL_SPECS,
                                  messages=messages)
        except ModelError as exc:
            printer(f"[fatal] model call failed: {exc}")
            return f"(run aborted: {exc})"

        blocks = resp.get("content") or []
        tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        texts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]

        if resp.get("stop_reason") != "tool_use" or not tool_uses:
            final = "\n".join(t for t in texts if t).strip()
            return final or "(model returned no final message)"

        messages.append({"role": "assistant", "content": blocks})
        results = []
        for tu in tool_uses:
            name = tu.get("name", "?")
            args = tu.get("input")
            if not isinstance(args, dict):
                args = {}
            result, error = executor.execute(name, args)
            shown = json.dumps(args, ensure_ascii=False)
            if len(shown) > 160:
                shown = shown[:160] + "…"
            if error:
                printer(f"[tool] {name}({shown}) -> ERROR {error}")
            else:
                printer(f"[tool] {name}({shown}) -> {_brief(result)}")
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.get("id", "?"),
                "content": json.dumps(error if error else result, default=str),
                **({"is_error": True} if error else {}),
            })
        messages.append({"role": "user", "content": results})
    return "(stopped: reached the maximum number of reasoning steps)"


def _brief(result: Any) -> str:
    s = json.dumps(result, ensure_ascii=False, default=str)
    return s if len(s) <= 200 else s[:200] + "…"


# --------------------------------------------------------------------------
# Batch mode
# --------------------------------------------------------------------------

def run_batch(profile_path: str, conversation_path: str,
              context_paths: Optional[List[str]] = None,
              model_name: str = "mock", scenario: str = "run",
              auto_approve: bool = True, fresh_store: bool = True,
              trace_out: Optional[str] = None,
              console_out: Optional[str] = None) -> Dict[str, Any]:
    lines: List[str] = []

    def printer(s: str) -> None:
        print(s)
        lines.append(s)

    with open(profile_path, "r", encoding="utf-8") as fh:
        profile_header = json.load(fh)
    if fresh_store:
        store.load_profile(profile_path)

    context_convs = [load_conversation(p) for p in (context_paths or [])]
    conv = load_conversation(conversation_path)
    pid = conv.get("professional_id", profile_header["professional_id"])
    if pid != profile_header["professional_id"]:
        raise SystemExit(f"conversation {conv.get('conversation_id')} belongs to {pid}, "
                         f"not to profile {profile_header['professional_id']}")

    executor = Executor(professional_id=pid,
                        approval_mode="auto" if auto_approve else "deny",
                        printer=printer)
    for c in context_convs + [conv]:
        executor.allow_evidence(m["id"] for m in c["messages"])

    printer(f"== {scenario}: {conv.get('conversation_id')} on {profile_header['name']} "
            f"({model_name}) ==")
    model = make_model(model_name, conv["messages"], profile_header["name"].split()[0])
    messages = [{"role": "user",
                 "content": first_user_message(profile_header, context_convs, conv)}]
    final = run_loop(model, executor, messages, printer)
    printer(f"agent> {final}")

    trace = {
        "scenario": scenario,
        "model": model.model_id,
        "seed": int(os.getenv("TRACCIA_SEED", "7")),
        "force_fail": os.getenv("TRACCIA_FORCE_FAIL") == "1",
        "conversation": conv.get("conversation_id"),
        "approval_mode": executor.approval_mode
                         + (" (human approval simulated by harness)"
                            if executor.approval_mode == "auto" else ""),
        "summary": executor.summary(),
        "calls": executor.trace,
        "final_message": final,
        "store_after": store.dump_store(),
    }
    if trace_out:
        Path(trace_out).parent.mkdir(parents=True, exist_ok=True)
        with open(trace_out, "w", encoding="utf-8") as fh:
            json.dump(trace, fh, indent=2, ensure_ascii=False)
        printer(f"(trace written to {trace_out})")
    if console_out:
        Path(console_out).parent.mkdir(parents=True, exist_ok=True)
        with open(console_out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return trace


# --------------------------------------------------------------------------
# Chat mode
# --------------------------------------------------------------------------

def run_chat(profile_path: str, model_name: str) -> None:
    with open(profile_path, "r", encoding="utf-8") as fh:
        profile_header = json.load(fh)
    store.load_profile(profile_path)
    first_name = profile_header["name"].split()[0]

    executor = Executor(professional_id=profile_header["professional_id"],
                        approval_mode="interactive", printer=print,
                        max_tool_calls=200)
    transcript: List[dict] = []
    llm_messages: List[dict] = [{"role": "user", "content": first_user_message(
        profile_header, [], {"conversation_id": "chat", "messages": []})}]

    print(f"Chat mode — you are {profile_header['name']}. Ctrl-D or 'quit' to leave.")
    n = 0
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            break
        n += 1
        msg = {"id": f"U{n}", "speaker": "professional", "text": text}
        transcript.append(msg)
        executor.allow_evidence([msg["id"]])

        if model_name == "mock":
            model = make_model("mock", [msg], first_name)
            messages: List[dict] = [{"role": "user", "content": f"{msg['id']}: {text}"}]
        else:
            model = make_model(model_name, [msg], first_name)
            llm_messages.append({"role": "user", "content":
                                 f"New message from the professional "
                                 f"({msg['id']}): {text}\n"
                                 "Process it for profile updates, then reply."})
            messages = llm_messages
        final = run_loop(model, executor, messages, print)
        if model_name != "mock":
            llm_messages.append({"role": "assistant", "content": final})
        print(f"agent> {final}")

    print("\nSession summary:", json.dumps(executor.summary(), indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def default_model() -> str:
    if os.environ.get("TRACCIA_MODEL"):
        return os.environ["TRACCIA_MODEL"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-sonnet-4-5"
    print("note: ANTHROPIC_API_KEY not set — using the offline mock planner "
          "(--model mock). Export a key to use the real model.", file=sys.stderr)
    return "mock"


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Traccia profile-update agent")
    ap.add_argument("--profile", default="data/profile.json")
    ap.add_argument("--conversation", help="conversation JSON to process")
    ap.add_argument("--context", action="append", default=[],
                    help="earlier conversation JSON, replayed as context only "
                         "(repeatable)")
    ap.add_argument("--model", default=None,
                    help="'mock' or an Anthropic model id (default: "
                         "claude-sonnet-4-5 if ANTHROPIC_API_KEY is set, else mock)")
    ap.add_argument("--scenario", default="run", help="label written into the trace")
    ap.add_argument("--trace-out", default=None, help="where to write the JSON trace")
    ap.add_argument("--console-out", default=None, help="where to mirror console output")
    ap.add_argument("--no-auto-approve", action="store_true",
                    help="do not simulate human approval of destructive actions")
    ap.add_argument("--chat", action="store_true", help="interactive chat mode")
    args = ap.parse_args(argv)

    model_name = args.model or default_model()
    if args.chat:
        run_chat(args.profile, model_name)
        return
    if not args.conversation:
        ap.error("--conversation is required unless --chat is given")
    run_batch(args.profile, args.conversation, context_paths=args.context,
              model_name=model_name, scenario=args.scenario,
              auto_approve=not args.no_auto_approve,
              trace_out=args.trace_out, console_out=args.console_out)


if __name__ == "__main__":
    main()
