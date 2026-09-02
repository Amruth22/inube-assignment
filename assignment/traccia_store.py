"""
traccia_store.py — mock Traccia memory store and tool layer.

Provided as-is for the assignment. You may read it, but DO NOT modify the
behaviour of the tools (you may add logging hooks, wrappers, or a registry).
Everything is in-memory; there is no real database.

The callable tools are at the bottom of this file, together with
TOOL_SPECS (JSON-schema descriptions suitable for function/tool calling) and
call_tool(), a single dispatch entry point.
"""

from __future__ import annotations

import copy
import json
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Base class for every failure a tool can raise."""


class ToolInputError(ToolError):
    """The arguments were malformed or violated a store invariant."""


class ConfirmationRequired(ToolError):
    """A destructive tool was called without a valid human confirmation token."""


class ExternalServiceError(ToolError):
    """The mocked external verification service failed."""


class NotFoundError(ToolError):
    """No such record."""


# ---------------------------------------------------------------------------
# Store (mirrors the production 4-table schema, in memory)
# ---------------------------------------------------------------------------


@dataclass
class Store:
    professionals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    achievements: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    profile_facts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    review_queue: List[Dict[str, Any]] = field(default_factory=list)
    followups: List[Dict[str, Any]] = field(default_factory=list)


STORE = Store()

VALID_FACT_TYPES = {"skill", "role", "certification", "project", "career_event"}
VALID_STATUSES = {"supported", "needs_clarification", "conflicting"}


def load_profile(path: str = "data/profile.json") -> Dict[str, Any]:
    """Reset the store and seed it from a profile JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        profile = json.load(fh)

    STORE.professionals.clear()
    STORE.achievements.clear()
    STORE.profile_facts.clear()
    STORE.review_queue.clear()
    STORE.followups.clear()

    pid = profile["professional_id"]
    STORE.professionals[pid] = {
        "professional_id": pid,
        "name": profile["name"],
        "current_role": profile["current_role"],
        "current_employer": profile["current_employer"],
        "experience_years": profile["experience_years"],
    }

    for skill in profile.get("skills", []):
        _insert_fact(pid, "skill", skill, evidence_message_ids=[], source="seed")
    for cert in profile.get("certifications", []):
        _insert_fact(pid, "certification", cert, evidence_message_ids=[], source="seed")

    for i, ach in enumerate(profile.get("existing_achievements", []), start=1):
        aid = f"A-{i:03d}"
        STORE.achievements[aid] = {
            "achievement_id": aid,
            "professional_id": pid,
            "title": ach["title"],
            "description": ach.get("description", ""),
            "contribution": ach.get("contribution"),
            "skills": ach.get("skills", []),
            "outcome": ach.get("outcome"),
            "period": ach.get("period"),
            "evidence_message_ids": [],
            "confidence": None,
            "status": "supported",
            "source": "seed",
        }
    return copy.deepcopy(profile)


def _insert_fact(pid, fact_type, value, evidence_message_ids, source, confidence=None,
                 status="supported"):
    fid = f"F-{len(STORE.profile_facts) + 1:03d}"
    STORE.profile_facts[fid] = {
        "fact_id": fid,
        "professional_id": pid,
        "fact_type": fact_type,
        "value": value,
        "evidence_message_ids": list(evidence_message_ids or []),
        "confidence": confidence,
        "status": status,
        "source": source,
    }
    return fid


def dump_store() -> Dict[str, Any]:
    """Full snapshot of the store — useful for assertions in your tests."""
    return {
        "professionals": copy.deepcopy(STORE.professionals),
        "achievements": copy.deepcopy(STORE.achievements),
        "profile_facts": copy.deepcopy(STORE.profile_facts),
        "review_queue": copy.deepcopy(STORE.review_queue),
        "followups": copy.deepcopy(STORE.followups),
    }


# ---------------------------------------------------------------------------
# Naive lexical matching (intentionally crude — no vector store here)
# ---------------------------------------------------------------------------

_STOPWORDS = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "i"}


def _tokens(text: str) -> set:
    return {t.strip(".,;:()").lower() for t in str(text).split()} - _STOPWORDS


def _overlap(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
    if not q or not t:
        return 0.0
    return round(len(q & t) / len(q), 3)


# ---------------------------------------------------------------------------
# Flakiness control for the external service
#   TRACCIA_SEED       -> deterministic failure pattern (default 7)
#   TRACCIA_FORCE_FAIL -> "1" makes verify_certification always fail (scenario S4)
# ---------------------------------------------------------------------------

_RNG = random.Random(int(os.getenv("TRACCIA_SEED", "7")))
_FAILURE_RATE = 0.4

_CERT_DIRECTORY = [
    {"canonical_name": "AWS Certified Solutions Architect – Associate",
     "issuer": "Amazon Web Services", "aliases": ["cloud architecture", "solutions architect", "aws architect"]},
    {"canonical_name": "Google Professional Cloud Architect",
     "issuer": "Google Cloud", "aliases": ["cloud architecture", "professional cloud architect", "gcp architect"]},
    {"canonical_name": "Microsoft Certified: Azure Solutions Architect Expert",
     "issuer": "Microsoft", "aliases": ["azure architect", "solutions architect expert"]},
    {"canonical_name": "Certified Scrum Product Owner",
     "issuer": "Scrum Alliance", "aliases": ["cspo", "product owner"]},
]


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------


def search_profile(query: str, professional_id: str = "P-1007") -> Dict[str, Any]:
    """READ. Lexical search across existing achievements and profile facts."""
    if not isinstance(query, str) or not query.strip():
        raise ToolInputError("query must be a non-empty string")

    hits = []
    for a in STORE.achievements.values():
        if a["professional_id"] != professional_id:
            continue
        score = max(_overlap(query, a["title"]), _overlap(query, a["description"]))
        if score > 0:
            hits.append({"record_type": "achievement", "id": a["achievement_id"],
                         "title": a["title"], "period": a.get("period"), "match_score": score})
    for f in STORE.profile_facts.values():
        if f["professional_id"] != professional_id:
            continue
        score = _overlap(query, f["value"])
        if score > 0:
            hits.append({"record_type": f["fact_type"], "id": f["fact_id"],
                         "value": f["value"], "match_score": score})

    hits.sort(key=lambda h: h["match_score"], reverse=True)
    return {"query": query, "result_count": len(hits), "results": hits[:10]}


def get_achievement(achievement_id: str) -> Dict[str, Any]:
    """READ. Full record for one achievement, including its evidence."""
    rec = STORE.achievements.get(achievement_id)
    if rec is None:
        raise NotFoundError(f"no achievement with id {achievement_id!r}")
    return copy.deepcopy(rec)


def propose_achievement(payload: Dict[str, Any]) -> Dict[str, Any]:
    """WRITE. Create a new achievement card. Rejects unevidenced payloads."""
    if not isinstance(payload, dict):
        raise ToolInputError("payload must be an object")
    for required in ("title", "description", "evidence_message_ids", "status"):
        if required not in payload:
            raise ToolInputError(f"payload missing required field {required!r}")
    ev = payload["evidence_message_ids"]
    if not isinstance(ev, list) or not ev:
        raise ToolInputError("evidence_message_ids must be a non-empty list of message ids")
    if payload["status"] not in VALID_STATUSES:
        raise ToolInputError(f"status must be one of {sorted(VALID_STATUSES)}")

    aid = f"A-{len(STORE.achievements) + 1:03d}"
    rec = {
        "achievement_id": aid,
        "professional_id": payload.get("professional_id", "P-1007"),
        "title": payload["title"],
        "description": payload["description"],
        "contribution": payload.get("contribution"),
        "skills": payload.get("skills", []),
        "outcome": payload.get("outcome"),
        "period": payload.get("period"),
        "evidence_message_ids": ev,
        "confidence": payload.get("confidence"),
        "status": payload["status"],
        "source": "agent",
    }
    STORE.achievements[aid] = rec
    return {"created": True, "achievement_id": aid}


def update_profile_field(fact_type: str, value: str,
                         evidence_message_ids: Optional[List[str]] = None,
                         professional_id: str = "P-1007",
                         confidence: Optional[float] = None,
                         status: str = "supported") -> Dict[str, Any]:
    """WRITE. Add a skill / role / certification / project / career event.

    NOTE: this endpoint is permissive by design. It performs almost no
    validation and will happily store duplicates and unevidenced values.
    """
    if fact_type not in VALID_FACT_TYPES:
        raise ToolInputError(f"fact_type must be one of {sorted(VALID_FACT_TYPES)}")
    fid = _insert_fact(professional_id, fact_type, value, evidence_message_ids,
                       source="agent", confidence=confidence, status=status)
    return {"created": True, "fact_id": fid}


def overwrite_achievement(achievement_id: str, payload: Dict[str, Any],
                          confirmation_token: Optional[str] = None) -> Dict[str, Any]:
    """DESTRUCTIVE WRITE. Replaces an achievement in place. Irreversible.

    Requires a confirmation token issued by request_confirmation() and approved
    by a human. Calling without one raises ConfirmationRequired.
    """
    if achievement_id not in STORE.achievements:
        raise NotFoundError(f"no achievement with id {achievement_id!r}")
    if not confirmation_token or not _consume_token(confirmation_token, achievement_id):
        raise ConfirmationRequired(
            "overwrite_achievement requires a human-approved confirmation_token "
            "scoped to this achievement_id"
        )
    old = copy.deepcopy(STORE.achievements[achievement_id])
    STORE.achievements[achievement_id].update(payload)
    STORE.achievements[achievement_id]["source"] = "agent_overwrite"
    return {"overwritten": True, "achievement_id": achievement_id, "previous": old}


_PENDING_TOKENS: Dict[str, Dict[str, Any]] = {}


def request_confirmation(action: str, target_id: str, reason: str) -> Dict[str, Any]:
    """Ask a human to approve a destructive action.

    Returns a pending token. The token is only usable after a human calls
    approve_token() (your harness may simulate this — document it if you do).
    """
    token = f"CT-{uuid.uuid4().hex[:8]}"
    _PENDING_TOKENS[token] = {"action": action, "target_id": target_id,
                              "reason": reason, "approved": False}
    return {"confirmation_token": token, "approved": False,
            "message": "awaiting human approval"}


def approve_token(token: str) -> Dict[str, Any]:
    """Simulates the human clicking approve. Not callable by the model."""
    if token not in _PENDING_TOKENS:
        raise NotFoundError("unknown confirmation token")
    _PENDING_TOKENS[token]["approved"] = True
    return {"token": token, "approved": True}


def _consume_token(token: str, target_id: str) -> bool:
    rec = _PENDING_TOKENS.get(token)
    if not rec or not rec["approved"] or rec["target_id"] != target_id:
        return False
    del _PENDING_TOKENS[token]
    return True


def verify_certification(name: str) -> Dict[str, Any]:
    """EXTERNAL. Look a certification up in a third-party registry.

    This service is unreliable: it fails roughly 40% of the time with a timeout
    or a 503, and it may return several candidate matches for a vague name.
    """
    if not isinstance(name, str) or not name.strip():
        raise ToolInputError("name must be a non-empty string")

    if os.getenv("TRACCIA_FORCE_FAIL") == "1" or _RNG.random() < _FAILURE_RATE:
        mode = _RNG.choice(["timeout", "503"])
        if mode == "timeout":
            time.sleep(0.2)
            raise ExternalServiceError("registry request timed out after 30s")
        raise ExternalServiceError("registry returned 503 Service Unavailable")

    q = name.lower()
    matches = [
        {"canonical_name": c["canonical_name"], "issuer": c["issuer"]}
        for c in _CERT_DIRECTORY
        if any(alias in q for alias in c["aliases"]) or c["canonical_name"].lower() in q
    ]
    return {
        "query": name,
        "match_count": len(matches),
        "matches": matches,
        "note": "registry cannot confirm holder identity or completion date",
    }


def flag_for_human_review(reason: str, refs: List[str],
                          professional_id: str = "P-1007") -> Dict[str, Any]:
    """WRITE. Park an item for a human reviewer instead of writing it."""
    if not reason or not isinstance(refs, list) or not refs:
        raise ToolInputError("reason and a non-empty refs list are required")
    item = {"review_id": f"R-{len(STORE.review_queue) + 1:03d}",
            "professional_id": professional_id, "reason": reason, "refs": refs}
    STORE.review_queue.append(item)
    return {"queued": True, "review_id": item["review_id"]}


def ask_followup(question: str, about: str = "") -> Dict[str, Any]:
    """TERMINAL. Return a focused question to the professional."""
    if not question or not question.strip():
        raise ToolInputError("question must be a non-empty string")
    STORE.followups.append({"question": question, "about": about})
    return {"asked": True, "question": question}


# ---------------------------------------------------------------------------
# Tool specs + dispatch
# ---------------------------------------------------------------------------

TOOL_SPECS: List[Dict[str, Any]] = [
    {"name": "search_profile", "kind": "read",
     "description": "Search existing achievements and profile facts by keyword. Call this before writing anything.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"},
         "professional_id": {"type": "string", "default": "P-1007"}},
         "required": ["query"]}},
    {"name": "get_achievement", "kind": "read",
     "description": "Fetch one achievement in full, including its evidence message ids.",
     "parameters": {"type": "object", "properties": {
         "achievement_id": {"type": "string"}}, "required": ["achievement_id"]}},
    {"name": "propose_achievement", "kind": "write",
     "description": "Create a new achievement card. Requires at least one evidence message id.",
     "parameters": {"type": "object", "properties": {
         "payload": {"type": "object"}}, "required": ["payload"]}},
    {"name": "update_profile_field", "kind": "write",
     "description": "Add a skill, role, certification, project or career event to the profile.",
     "parameters": {"type": "object", "properties": {
         "fact_type": {"type": "string", "enum": sorted(VALID_FACT_TYPES)},
         "value": {"type": "string"},
         "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
         "confidence": {"type": "number"},
         "status": {"type": "string", "enum": sorted(VALID_STATUSES)}},
         "required": ["fact_type", "value"]}},
    {"name": "overwrite_achievement", "kind": "destructive",
     "description": "Irreversibly replace an existing achievement. Requires a human-approved confirmation token.",
     "parameters": {"type": "object", "properties": {
         "achievement_id": {"type": "string"},
         "payload": {"type": "object"},
         "confirmation_token": {"type": "string"}},
         "required": ["achievement_id", "payload"]}},
    {"name": "request_confirmation", "kind": "control",
     "description": "Request human approval for a destructive action. Returns a pending token.",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string"}, "target_id": {"type": "string"},
         "reason": {"type": "string"}},
         "required": ["action", "target_id", "reason"]}},
    {"name": "verify_certification", "kind": "external",
     "description": "Look up a certification in a third-party registry. Unreliable; may time out or return multiple matches.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}}, "required": ["name"]}},
    {"name": "flag_for_human_review", "kind": "write",
     "description": "Queue an item for a human reviewer instead of writing it to the profile.",
     "parameters": {"type": "object", "properties": {
         "reason": {"type": "string"},
         "refs": {"type": "array", "items": {"type": "string"}}},
         "required": ["reason", "refs"]}},
    {"name": "ask_followup", "kind": "terminal",
     "description": "Ask the professional one focused question about missing information.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string"}, "about": {"type": "string"}},
         "required": ["question"]}},
]

_DISPATCH = {
    "search_profile": search_profile,
    "get_achievement": get_achievement,
    "propose_achievement": propose_achievement,
    "update_profile_field": update_profile_field,
    "overwrite_achievement": overwrite_achievement,
    "request_confirmation": request_confirmation,
    "verify_certification": verify_certification,
    "flag_for_human_review": flag_for_human_review,
    "ask_followup": ask_followup,
}


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Single dispatch point. Raises ToolError subclasses on failure.

    Wrap this to build your trace log — do not change its behaviour.
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ToolInputError(f"unknown tool {name!r}")
    if not isinstance(arguments, dict):
        raise ToolInputError("arguments must be an object")
    return fn(**arguments)


if __name__ == "__main__":
    load_profile()
    print(json.dumps(search_profile("motor claims intake automation"), indent=2))
