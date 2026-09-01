"""toolkit.py — the guarded tool layer between the model and traccia_store.

Everything the model wants to do goes through Executor.execute(). The executor:

  * validates arguments before they reach the store (the store barely does),
  * enforces grounding: every write must cite message ids that actually exist
    in the conversations seen this run,
  * blocks duplicate profile facts (update_profile_field would happily store them),
  * blocks "supported" certification facts unless the registry verified the name,
  * retries the flaky external registry with backoff,
  * intercepts request_confirmation so a human (real or simulated) approves
    destructive actions — the model itself can never approve anything,
  * writes a structured trace entry for every call, success or failure.

It never changes what the store tools do; it only decides whether and how
they get called.
"""

from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import traccia_store as store
from traccia_store import (
    ConfirmationRequired,
    ExternalServiceError,
    ToolError,
    call_tool,
)


class PolicyError(Exception):
    """The call was well-formed for the store but violates agent policy."""


# --------------------------------------------------------------------------
# Similarity helper for duplicate detection (the store stores duplicates
# without complaint, so we refuse them here).
# --------------------------------------------------------------------------

_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "i", "my", "our"}


def _toks(text: str) -> set:
    return {w.strip(".,;:()%").lower() for w in str(text).split()} - _STOP - {""}


def similar(a: str, b: str) -> bool:
    if a.strip().casefold() == b.strip().casefold():
        return True
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.6


WRITE_TOOLS = {"propose_achievement", "update_profile_field", "overwrite_achievement"}

EXTERNAL_MAX_ATTEMPTS = 3     # 1 call + 2 retries for verify_certification
EXTERNAL_BACKOFF = 0.3        # seconds, doubled per retry
DEFAULT_MAX_TOOL_CALLS = 25


class Executor:
    def __init__(self, professional_id: str = "P-1007",
                 approval_mode: str = "auto",
                 max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
                 printer=None):
        self.professional_id = professional_id
        # auto        -> simulate the human clicking approve (documented in README)
        # interactive -> ask on stdin (chat mode)
        # deny        -> tokens stay pending; overwrites will be refused
        self.approval_mode = approval_mode
        self.max_tool_calls = max_tool_calls
        self.printer = printer or (lambda s: None)
        self.allowed_evidence: set = set()
        self.verified_certs: List[str] = []
        self.trace: List[Dict[str, Any]] = []
        self.changes: List[str] = []
        self.writes = 0
        self.errors = 0

    # -- evidence registry --------------------------------------------------

    def allow_evidence(self, message_ids) -> None:
        self.allowed_evidence.update(message_ids)

    # -- policy checks ------------------------------------------------------

    def _check_evidence(self, ids: Any, where: str) -> None:
        if not isinstance(ids, list) or not ids:
            raise PolicyError(f"{where}: evidence_message_ids must be a non-empty list "
                              "of message ids from this conversation")
        unknown = [i for i in ids if i not in self.allowed_evidence]
        if unknown:
            raise PolicyError(f"{where}: evidence ids {unknown} do not exist in any "
                              "conversation seen this session — do not invent evidence")

    def _precheck(self, name: str, args: Dict[str, Any]) -> None:
        if name == "update_profile_field":
            value = args.get("value")
            if not isinstance(value, str) or not value.strip():
                raise PolicyError("update_profile_field: value must be a non-empty string")
            if len(value) > 200:
                raise PolicyError("update_profile_field: value too long — store a short "
                                  "fact, put the story in an achievement")
            self._check_evidence(args.get("evidence_message_ids"), "update_profile_field")
            fact_type = args.get("fact_type")
            for f in store.STORE.profile_facts.values():
                if (f["professional_id"] == self.professional_id
                        and f["fact_type"] == fact_type
                        and similar(f["value"], value)):
                    raise PolicyError(
                        f"update_profile_field: '{value}' duplicates existing "
                        f"{fact_type} {f['fact_id']} ('{f['value']}') — do not re-add it")
            if fact_type == "certification" and args.get("status", "supported") == "supported":
                if not any(similar(value, c) or c.lower() in value.lower()
                           for c in self.verified_certs):
                    raise PolicyError(
                        "update_profile_field: a certification may only be stored as "
                        "'supported' after verify_certification returned exactly that "
                        "name this session; otherwise ask a follow-up or use status "
                        "'needs_clarification'")

        elif name == "propose_achievement":
            payload = args.get("payload")
            if not isinstance(payload, dict):
                raise PolicyError("propose_achievement: payload must be an object")
            self._check_evidence(payload.get("evidence_message_ids"), "propose_achievement")
            title = payload.get("title", "")
            for a in store.STORE.achievements.values():
                if a["professional_id"] == self.professional_id and similar(a["title"], title):
                    raise PolicyError(
                        f"propose_achievement: title looks like existing achievement "
                        f"{a['achievement_id']} ('{a['title']}') — enrich that one via "
                        "request_confirmation + overwrite_achievement instead of "
                        "creating a duplicate")

        elif name == "overwrite_achievement":
            payload = args.get("payload")
            if not isinstance(payload, dict):
                raise PolicyError("overwrite_achievement: payload must be an object")
            ev = payload.get("evidence_message_ids")
            if ev is not None:
                existing = set()
                aid = args.get("achievement_id")
                if aid in store.STORE.achievements:
                    existing = set(store.STORE.achievements[aid]["evidence_message_ids"])
                unknown = [i for i in ev if i not in self.allowed_evidence | existing]
                if unknown:
                    raise PolicyError(f"overwrite_achievement: evidence ids {unknown} "
                                      "do not exist — do not invent evidence")

    # -- confirmation handling ---------------------------------------------

    def _handle_confirmation(self, result: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        token = result["confirmation_token"]
        if self.approval_mode == "auto":
            store.approve_token(token)
            result = dict(result, approved=True,
                          message="approved (human approval SIMULATED by harness "
                                  "--auto-approve; see README)")
        elif self.approval_mode == "interactive":
            print(f"\n[approval needed] {args.get('action')} on {args.get('target_id')}: "
                  f"{args.get('reason')}")
            answer = input("approve? [y/N] ").strip().lower()
            if answer == "y":
                store.approve_token(token)
                result = dict(result, approved=True, message="approved by human on stdin")
            else:
                result = dict(result, approved=False, message="human declined")
        # 'deny': leave pending — overwrite will fail with ConfirmationRequired
        return result

    # -- the single entry point --------------------------------------------

    def execute(self, name: str, args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Run one tool call. Returns (result, None) or (None, error_string).

        Every call, including refused ones, lands in self.trace.
        """
        entry: Dict[str, Any] = {"n": len(self.trace) + 1, "tool": name,
                                 "arguments": copy.deepcopy(args),
                                 "result": None, "error": None, "retries": 0}
        self.trace.append(entry)

        if len(self.trace) > self.max_tool_calls:
            entry["error"] = "BudgetExceeded: tool-call budget for this run is spent"
            self.errors += 1
            return None, entry["error"]

        try:
            if not isinstance(args, dict):
                raise PolicyError("arguments must be an object")
            self._precheck(name, args)

            if name == "verify_certification":
                result = self._call_external(name, args, entry)
            else:
                result = call_tool(name, args)

            if name == "request_confirmation":
                result = self._handle_confirmation(result, args)

        except (PolicyError, ToolError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            self.errors += 1
            return None, entry["error"]

        entry["result"] = self._truncate(result)
        self._record_change(name, args, result)
        return result, None

    def _call_external(self, name: str, args: Dict[str, Any], entry: Dict[str, Any]):
        delay = EXTERNAL_BACKOFF
        for attempt in range(EXTERNAL_MAX_ATTEMPTS):
            try:
                result = call_tool(name, args)
                if result.get("match_count", 0) >= 1:
                    self.verified_certs.extend(m["canonical_name"] for m in result["matches"])
                return result
            except ExternalServiceError as exc:
                if attempt == EXTERNAL_MAX_ATTEMPTS - 1:
                    raise
                entry["retries"] += 1
                self.printer(f"      (registry failed: {exc}; retry {entry['retries']} "
                             f"in {delay:.1f}s)")
                time.sleep(delay)
                delay *= 2

    # -- bookkeeping --------------------------------------------------------

    def _record_change(self, name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        if name in WRITE_TOOLS:
            self.writes += 1
        if name == "update_profile_field":
            self.changes.append(f"added {args.get('fact_type')} '{args.get('value')}' "
                                f"({result.get('fact_id')})")
        elif name == "propose_achievement":
            self.changes.append(f"created achievement {result.get('achievement_id')} "
                                f"'{args['payload'].get('title')}'")
        elif name == "overwrite_achievement":
            self.changes.append(f"enriched achievement {args.get('achievement_id')} "
                                "(human-approved overwrite)")
        elif name == "flag_for_human_review":
            self.changes.append(f"flagged for human review ({result.get('review_id')}): "
                                f"{args.get('reason')}")
        elif name == "ask_followup":
            self.changes.append(f"asked follow-up: {args.get('question')}")

    @staticmethod
    def _truncate(result: Any, limit: int = 800) -> Any:
        s = json.dumps(result, default=str)
        if len(s) <= limit:
            return result
        return {"truncated": True, "preview": s[:limit]}

    def summary(self, what_changed_default: str = "no changes") -> Dict[str, Any]:
        return {
            "tool_calls": len(self.trace),
            "writes": self.writes,
            "errors": self.errors,
            "what_changed": "; ".join(self.changes) or what_changed_default,
        }


# --------------------------------------------------------------------------
# The tool schemas we SHOW THE MODEL. The store ships its own TOOL_SPECS but
# the assignment makes the descriptions our design surface, so these spell
# out when each tool should and should not be picked. Anthropic tool format.
# --------------------------------------------------------------------------

AGENT_TOOL_SPECS: List[Dict[str, Any]] = [
    {"name": "search_profile",
     "description": ("Search the existing profile (achievements and facts) by keywords. "
                     "ALWAYS call this before any write: the professional often describes "
                     "work that is already on the profile under a different name. "
                     "Lexical match only — try 2 short keyword queries before concluding "
                     "something is absent."),
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "3-8 keywords, no full sentences"}},
         "required": ["query"]}},
    {"name": "get_achievement",
     "description": ("Read one achievement in full, including its current evidence. Call "
                     "before enriching or comparing against a new claim."),
     "input_schema": {"type": "object", "properties": {
         "achievement_id": {"type": "string"}}, "required": ["achievement_id"]}},
    {"name": "propose_achievement",
     "description": ("Create a NEW achievement card. Only for work not already on the "
                     "profile (search first). evidence_message_ids must cite the exact "
                     "messages where the professional stated each part; never cite the "
                     "interviewer's questions as evidence of fact."),
     "input_schema": {"type": "object", "properties": {
         "payload": {"type": "object", "properties": {
             "title": {"type": "string"},
             "description": {"type": "string"},
             "contribution": {"type": "string", "description": "what THEY personally did"},
             "skills": {"type": "array", "items": {"type": "string"}},
             "outcome": {"type": "string", "description": "only figures they actually stated"},
             "period": {"type": "string"},
             "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
             "confidence": {"type": "number"},
             "status": {"type": "string",
                        "enum": ["supported", "needs_clarification", "conflicting"]}},
             "required": ["title", "description", "evidence_message_ids", "status"]}},
         "required": ["payload"]}},
    {"name": "update_profile_field",
     "description": ("Add ONE short profile fact: a skill, role, certification, project or "
                     "career_event. The backend stores anything, so the burden is on you: "
                     "search first, one fact per call, cite evidence_message_ids, and never "
                     "re-add something already on the profile. A certification may only be "
                     "'supported' after verify_certification confirmed that exact name."),
     "input_schema": {"type": "object", "properties": {
         "fact_type": {"type": "string",
                       "enum": ["skill", "role", "certification", "project", "career_event"]},
         "value": {"type": "string", "description": "short noun phrase, max ~8 words"},
         "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
         "confidence": {"type": "number"},
         "status": {"type": "string",
                    "enum": ["supported", "needs_clarification", "conflicting"]}},
         "required": ["fact_type", "value", "evidence_message_ids"]}},
    {"name": "overwrite_achievement",
     "description": ("IRREVERSIBLY replace fields of an existing achievement (use to enrich "
                     "it with outcome/period/contribution). Requires a confirmation_token "
                     "that a human has approved via request_confirmation. Preserve existing "
                     "correct fields; pass the full merged record."),
     "input_schema": {"type": "object", "properties": {
         "achievement_id": {"type": "string"},
         "payload": {"type": "object"},
         "confirmation_token": {"type": "string"}},
         "required": ["achievement_id", "payload", "confirmation_token"]}},
    {"name": "request_confirmation",
     "description": ("Ask a human to approve a destructive action (currently: "
                     "overwrite_achievement). Returns a token; only proceed with the "
                     "overwrite if the response says approved=true."),
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string"},
         "target_id": {"type": "string"},
         "reason": {"type": "string", "description": "what will change and why, one sentence"}},
         "required": ["action", "target_id", "reason"]}},
    {"name": "verify_certification",
     "description": ("Look a certification name up in an external registry. Unreliable "
                     "(timeouts/503s are retried for you) and vague names return several "
                     "candidates. It can NOT confirm the person actually holds it. Zero or "
                     "multiple matches, or an outage, means: do not store the certification "
                     "as supported — ask a follow-up instead."),
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}}, "required": ["name"]}},
    {"name": "flag_for_human_review",
     "description": ("Park a claim for a human reviewer INSTEAD of writing it. Use when a "
                     "new statement contradicts earlier, more specific evidence (e.g. an "
                     "ownership claim the professional previously walked back), or when you "
                     "cannot safely decide. refs = message ids and record ids involved."),
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string"},
         "refs": {"type": "array", "items": {"type": "string"}}},
         "required": ["reason", "refs"]}},
    {"name": "ask_followup",
     "description": ("Ask the professional ONE focused question when a claim is too vague "
                     "to store (no name, no date, no outcome). Ask at most one per "
                     "conversation; do not write the vague version first."),
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "about": {"type": "string"}}, "required": ["question"]}},
]


# --------------------------------------------------------------------------
# Store snapshot/restore helpers (for chaining scenarios: S2/S3 start from
# S1's end state). Pure bookkeeping around the provided in-memory store.
# --------------------------------------------------------------------------

def snapshot_store() -> Dict[str, Any]:
    return store.dump_store()


def restore_store(snap: Dict[str, Any]) -> None:
    store.STORE.professionals = copy.deepcopy(snap["professionals"])
    store.STORE.achievements = copy.deepcopy(snap["achievements"])
    store.STORE.profile_facts = copy.deepcopy(snap["profile_facts"])
    store.STORE.review_queue = copy.deepcopy(snap["review_queue"])
    store.STORE.followups = copy.deepcopy(snap["followups"])
