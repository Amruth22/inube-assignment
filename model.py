"""model.py — the two model backends behind the same complete() interface.

AnthropicModel  calls the Messages API directly over httpx (no framework),
                with retry/backoff on 429/5xx/timeouts.
MockModel       a deterministic offline planner that follows the same policy
                the system prompt gives the real model. It exists so the
                scenarios and tests run with no API key and no network, and
                it is honestly labelled as a mock in every trace it produces.

Both return a dict shaped like an Anthropic response:
  {"stop_reason": "tool_use"|"end_turn", "content": [blocks]}
so agent.py runs one loop for either.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional


class ModelError(Exception):
    pass


# ==========================================================================
# Real model
# ==========================================================================

class AnthropicModel:
    def __init__(self, model_id: str = "claude-sonnet-4-5", max_tokens: int = 2000):
        import httpx  # imported here so the mock path needs no dependencies
        self._httpx = httpx
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ModelError("ANTHROPIC_API_KEY is not set; use --model mock or export a key")
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    def complete(self, system: str, tools: List[dict], messages: List[dict]) -> Dict[str, Any]:
        body = {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
        }
        delay = 1.0
        last_err = None
        for attempt in range(4):
            try:
                resp = self._httpx.post(
                    f"{self.base_url}/v1/messages",
                    headers={"x-api-key": self.api_key,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json=body, timeout=60.0)
                if resp.status_code == 200:
                    data = resp.json()
                    return {"stop_reason": data.get("stop_reason"),
                            "content": data.get("content", [])}
                if resp.status_code in (429, 500, 502, 503, 529):
                    last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                else:
                    raise ModelError(f"API error HTTP {resp.status_code}: {resp.text[:400]}")
            except (self._httpx.TimeoutException, self._httpx.TransportError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(delay)
            delay *= 2
        raise ModelError(f"model call failed after retries: {last_err}")


# ==========================================================================
# Offline planner
# ==========================================================================

CLAIM_RE = re.compile(
    r"\b(led|ran|wrote|mapped|built|deliver\w*|manag\w*|complet\w*|launch\w*|"
    r"reduc\w*|creat\w*|design\w*|certif\w*|award\w*|promot\w*|present\w*|"
    r"automat\w*|responsible|implement\w*|migrat\w*|defined?)\b", re.I)
RETRACTION_RE = re.compile(r"^\s*(not exactly|not really|no[,.]|actually,? no|well,? not)\b", re.I)
CONFLICT_RE = re.compile(
    r"\b(entirely|solely|single-?handedly?|fully|completely|100%)\s*"
    r"(responsible|owned|delivered|built|ran|in charge)\b", re.I)
MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{4}\b", re.I)
CERT_RE = re.compile(r"\bcertifications?\b", re.I)


def _cert_phrase(text: str) -> str:
    m = re.search(r"\b(?:a|an|my|the)\s+([\w -]{0,40}?certifications?)\b", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"((?:[\w-]+\s+){0,3}certifications?)\b", text, re.I)
    return m.group(1).strip() if m else "certification"

SKILL_PATTERNS = [
    (re.compile(r"\bworkshops?\b", re.I), "Workshop facilitation"),
    (re.compile(r"\bmapped\b.{0,40}\bprocess\b|\bprocess mapping\b", re.I), "Process mapping"),
    (re.compile(r"\bapi (payloads?|design|specs?)\b", re.I), "API design"),
    (re.compile(r"\bcoordinat\w+\b|\btracked dependencies\b", re.I), "Cross-team coordination"),
    (re.compile(r"\bstakeholders?\b", re.I), "Stakeholder management"),
]


def _is_claim(text: str) -> bool:
    return bool(CLAIM_RE.search(text)) or any(c.isdigit() for c in text)


def _is_concrete(text: str) -> bool:
    """A claim we would consider storing: it carries a figure or a date."""
    stripped = MONTH_RE.sub("", text)
    return any(c.isdigit() for c in stripped) or bool(MONTH_RE.search(text))


def _keywords(text: str, n: int = 8) -> str:
    stop = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "i",
            "our", "my", "was", "it", "also", "that"}
    words = [w.strip(".,;:()").lower() for w in text.split()]
    picked = [w for w in words if w and w not in stop and not w.startswith("'")]
    return " ".join(picked[:n])


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _analyse(prof_msgs: List[dict]) -> Dict[str, Any]:
    """Classify the professional's messages: solid claims, retracted claims,
    corrections, certification mentions, and conflict-with-evidence claims."""
    retracted, corrections = set(), []
    prev_claim_id = None
    for m in prof_msgs:
        if RETRACTION_RE.search(m["text"]) and prev_claim_id:
            retracted.add(prev_claim_id)
            corrections.append(m)
        elif _is_claim(m["text"]):
            prev_claim_id = m["id"]

    correction_ids = {m["id"] for m in corrections}
    certs, conflicts, core = [], [], []
    for m in prof_msgs:
        if m["id"] in retracted or m["id"] in correction_ids:
            continue
        if not _is_claim(m["text"]):
            continue
        if CERT_RE.search(m["text"]):
            certs.append(m)
        elif CONFLICT_RE.search(m["text"]):
            conflicts.append(m)
        else:
            core.append(m)
    return {"core": core, "certs": certs, "conflicts": conflicts,
            "retracted": retracted, "corrections": corrections}


def _extract_skills(msgs: List[dict]) -> List[tuple]:
    found, seen = [], set()
    for m in msgs:
        for rx, skill in SKILL_PATTERNS:
            if skill not in seen and rx.search(m["text"]):
                seen.add(skill)
                found.append((skill, m["id"]))
    return found


def _build_payload(core: List[dict], existing: Optional[dict],
                   skills: List[tuple]) -> Dict[str, Any]:
    outcome_bits, period = [], None
    for m in core:
        for sent in _split_sentences(m["text"]):
            mo = MONTH_RE.search(sent)
            if mo and not period:
                period = mo.group(0).title()
            if any(c.isdigit() for c in MONTH_RE.sub("", sent)):
                outcome_bits.append(sent)
    contribution = max(core, key=lambda m: len(m["text"]))["text"] if core else None
    first = core[0]["text"] if core else ""
    lead = re.sub(r"^i\s+", "", first, flags=re.I).rstrip(".")
    lead = lead[:1].upper() + lead[1:]

    payload = {
        "title": (existing or {}).get("title") or lead[:80],
        "description": ((existing or {}).get("description", "").rstrip() + " " + lead + ".").strip(),
        "contribution": contribution,
        "skills": sorted({s for s, _ in skills} | set((existing or {}).get("skills") or [])),
        "outcome": " ".join(outcome_bits) or (existing or {}).get("outcome"),
        "period": period or (existing or {}).get("period"),
        "evidence_message_ids": [m["id"] for m in core],
        "confidence": 0.85,
        "status": "supported",
    }
    return payload


def _plan(new_msgs: List[dict], profile_name: str):
    """Generator: yields ("tool", name, args) or ("final", text); receives
    {"error": bool, "data": ...} for each tool call via .send()."""
    prof_msgs = [m for m in new_msgs if m.get("speaker") == "professional"]
    a = _analyse(prof_msgs)
    acts: List[str] = []
    followup: Optional[str] = None

    if not (a["core"] or a["certs"] or a["conflicts"]):
        yield ("final",
               f"Nothing in this conversation changes the profile, so I have written "
               f"nothing. Happy to summarise what is currently on it, {profile_name} — "
               "and to answer your question: these conversations are only used to keep "
               "your own profile accurate; nothing is stored unless you actually said it.")
        return

    # 1) look before writing
    anchor = (a["core"] or a["conflicts"])[0]
    res = yield ("tool", "search_profile", {"query": _keywords(anchor["text"])})
    hits = (res["data"].get("results") if res and not res["error"] else []) or []
    ach_hits = [h for h in hits if h.get("record_type") == "achievement"]
    existing = None
    if ach_hits:
        res = yield ("tool", "get_achievement", {"achievement_id": ach_hits[0]["id"]})
        if res and not res["error"]:
            existing = res["data"]

    # 2) contradictions are parked, never written
    for m in a["conflicts"]:
        phrase = CONFLICT_RE.search(m["text"]).group(0)
        refs = [m["id"]] + ([existing["achievement_id"]] if existing else [])
        reason = (f"Message {m['id']} claims to be '{phrase}', which overstates or "
                  "conflicts with earlier, more specific statements on record. "
                  "Parked for human review instead of changing the profile.")
        res = yield ("tool", "flag_for_human_review", {"reason": reason, "refs": refs})
        if res and not res["error"]:
            acts.append("sent the ownership claim to a human reviewer rather than "
                        "changing the profile")

    # 3) the core story: enrich the existing achievement, or propose a new one
    skills = _extract_skills(a["core"] + a["corrections"])
    if a["core"]:
        payload = _build_payload(a["core"], existing, skills)
        if existing:
            aid = existing["achievement_id"]
            res = yield ("tool", "request_confirmation", {
                "action": "overwrite_achievement", "target_id": aid,
                "reason": f"Enrich {aid} with contribution, outcome and period stated in "
                          f"messages {', '.join(payload['evidence_message_ids'])}"})
            if res and not res["error"] and res["data"].get("approved"):
                res = yield ("tool", "overwrite_achievement", {
                    "achievement_id": aid, "payload": payload,
                    "confirmation_token": res["data"]["confirmation_token"]})
                if res and not res["error"]:
                    acts.append(f"updated your existing achievement ({aid}) with what you "
                                "personally did, the measured outcome and the go-live date")
            else:
                res = yield ("tool", "flag_for_human_review", {
                    "reason": f"Human approval not granted for enriching {aid}; proposed "
                              "update parked instead of overwriting.",
                    "refs": [aid] + payload["evidence_message_ids"]})
                if res and not res["error"]:
                    acts.append(f"parked the proposed update to {aid} for review "
                                "(no human approval)")
        elif _is_concrete(" ".join(m["text"] for m in a["core"])):
            res = yield ("tool", "propose_achievement", {"payload": payload})
            if res and not res["error"]:
                acts.append(f"added a new achievement ({res['data'].get('achievement_id')})")
        else:
            followup = ("Can you pin that down for me — when did it happen, and what "
                        "was the concrete outcome?")

    # 4) skills, one per call; duplicates are refused by policy and skipped
    for skill, msg_id in skills:
        res = yield ("tool", "update_profile_field", {
            "fact_type": "skill", "value": skill,
            "evidence_message_ids": [msg_id], "confidence": 0.8, "status": "supported"})
        if res and not res["error"]:
            acts.append(f"added the skill '{skill}'")

    # 5) certifications: verify; vague or unverifiable -> ask, don't store
    for m in a["certs"]:
        query = _cert_phrase(m["text"])
        res = yield ("tool", "verify_certification", {"name": query})
        if res and res["error"]:
            followup = (f"You mentioned a {query} — which one exactly (issuer and exact "
                        "name), and when did you complete it? The registry is currently "
                        "unreachable, so I could not verify it and have not added it yet.")
        else:
            matches = res["data"].get("matches", [])
            if len(matches) == 1:
                res2 = yield ("tool", "update_profile_field", {
                    "fact_type": "certification", "value": matches[0]["canonical_name"],
                    "evidence_message_ids": [m["id"]], "confidence": 0.7,
                    "status": "supported"})
                if res2 and not res2["error"]:
                    acts.append(f"added the certification '{matches[0]['canonical_name']}'")
            else:
                names = " or ".join(mm["canonical_name"] for mm in matches[:3]) or "which one"
                followup = (f"Which certification did you complete exactly — {names}? "
                            "And when? I have not added it until you confirm.")

    if followup:
        yield ("tool", "ask_followup", {"question": followup, "about": "certification"
               if a["certs"] else "vague claim"})
        acts.append("asked you one follow-up question")

    if acts:
        body = "; ".join(acts)
        yield ("final", f"Thanks {profile_name} — I have {body}. Everything written is "
                        "backed by your own words in this conversation; anything vague or "
                        "conflicting was held back.")
    else:
        yield ("final", f"Thanks {profile_name} — I checked the profile and made no "
                        "changes; nothing in this conversation was solid enough to store, "
                        "and anything questionable went to a human reviewer.")


class MockModel:
    """Deterministic offline planner. Same complete() contract as AnthropicModel."""

    def __init__(self, new_messages: List[dict], profile_name: str = "Maya"):
        self.model_id = "mock-planner"
        self._gen = _plan(new_messages, profile_name)
        self._started = False
        self._n = 0

    def complete(self, system: str, tools: List[dict], messages: List[dict]) -> Dict[str, Any]:
        payload = None
        last = messages[-1] if messages else {}
        if isinstance(last.get("content"), list):
            trs = [b for b in last["content"] if isinstance(b, dict)
                   and b.get("type") == "tool_result"]
            if trs:
                b = trs[-1]
                try:
                    data = json.loads(b.get("content") or "null")
                except (TypeError, ValueError):
                    data = b.get("content")
                payload = {"error": bool(b.get("is_error")), "data": data}
        try:
            step = self._gen.send(payload) if self._started else next(self._gen)
            self._started = True
        except StopIteration:
            step = ("final", "Done.")
        if step[0] == "tool":
            self._n += 1
            return {"stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": f"mock_{self._n}",
                                 "name": step[1], "input": step[2]}]}
        return {"stop_reason": "end_turn",
                "content": [{"type": "text", "text": step[1]}]}


def make_model(model_name: str, new_messages: List[dict], profile_name: str):
    if model_name == "mock":
        return MockModel(new_messages, profile_name)
    return AnthropicModel(model_name)
