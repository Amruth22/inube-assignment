"""toolkit.py — the safety rails between the model and traccia_store.

traccia_store's tools are deliberately permissive: update_profile_field will
store duplicates and unevidenced values, and verify_certification fails 40% of
the time. Executor.run() is the only path from the model to those tools, and it

  1. refuses writes whose evidence ids are not real messages from this run,
  2. refuses duplicate facts and duplicate achievements,
  3. refuses a 'supported' certification the registry did not confirm,
  4. retries the flaky registry, then reports the failure as a normal error,
  5. lets a human (real in chat mode, simulated in batch mode) approve
     destructive overwrites — the model itself can never approve,
  6. logs every call, refused or not, so the trace tells the whole story.

TOOLS at the bottom is what the model reads about each tool. The store ships its
own TOOL_SPECS, but the descriptions are our design surface, so we wrote them.
"""

import copy
import time

import traccia_store as store

WRITE_TOOLS = {"propose_achievement", "update_profile_field", "overwrite_achievement"}
REGISTRY_ATTEMPTS = 3      # 1 call + 2 retries for verify_certification
REGISTRY_BACKOFF = 0.3     # seconds, doubled after each failure


class PolicyError(Exception):
    """Well-formed for the store, but against our rules."""


def similar(a, b):
    """Crude duplicate check: same text, or one's words are a subset of the other's."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    return a.strip().lower() == b.strip().lower() or words_a <= words_b or words_b <= words_a


class Executor:
    def __init__(self, approve="auto", max_calls=25):
        self.approve = approve              # "auto" (simulate a human) | "ask" (stdin) | "never"
        self.max_calls = max_calls
        self.known_message_ids = set()      # the only ids a write may cite as evidence
        self.verified_certs = []            # names the registry confirmed this run
        self.trace = []                     # one entry per call, in order

    # ---- the single entry point ------------------------------------------

    def run(self, name, args):
        """Run one tool call. Returns (result, None) or (None, error_message)."""
        entry = {"n": len(self.trace) + 1, "tool": name, "arguments": copy.deepcopy(args),
                 "result": None, "error": None, "retries": 0}
        self.trace.append(entry)
        try:
            if len(self.trace) > self.max_calls:
                raise PolicyError("tool-call budget for this run is spent")
            self.check_policy(name, args)

            if name == "verify_certification":
                result = self.call_registry(args, entry)
            else:
                result = store.call_tool(name, args)

            if name == "request_confirmation":
                result = self.get_human_approval(result, args)
        except (PolicyError, store.ToolError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            return None, entry["error"]

        entry["result"] = result
        return result, None

    # ---- rules 1-3: what we refuse before the store even sees it -----------

    def check_policy(self, name, args):
        if name == "update_profile_field":
            self.check_evidence(args.get("evidence_message_ids"))
            kind, value = args.get("fact_type"), str(args.get("value", "")).strip()
            if not value:
                raise PolicyError("value must not be empty")
            for fact in store.STORE.profile_facts.values():
                if fact["fact_type"] == kind and similar(fact["value"], value):
                    raise PolicyError(f"'{value}' duplicates {fact['fact_id']} "
                                      f"('{fact['value']}') — do not re-add it")
            if kind == "certification" and args.get("status", "supported") == "supported" \
                    and not any(similar(value, c) for c in self.verified_certs):
                raise PolicyError("certification not confirmed by verify_certification "
                                  "this run — ask a follow-up instead of storing it")

        elif name == "propose_achievement":
            payload = args.get("payload") or {}
            self.check_evidence(payload.get("evidence_message_ids"))
            for ach in store.STORE.achievements.values():
                if similar(ach["title"], payload.get("title", "")):
                    raise PolicyError(f"looks like existing {ach['achievement_id']} "
                                      f"('{ach['title']}') — enrich it via "
                                      "request_confirmation + overwrite_achievement")

        elif name == "overwrite_achievement":
            evidence = (args.get("payload") or {}).get("evidence_message_ids")
            if evidence:
                self.check_evidence(evidence)

    def check_evidence(self, ids):
        if not isinstance(ids, list) or not ids:
            raise PolicyError("evidence_message_ids must be a non-empty list of message ids")
        unknown = [i for i in ids if i not in self.known_message_ids]
        if unknown:
            raise PolicyError(f"evidence ids {unknown} do not exist in this conversation "
                              "— do not invent evidence")

    # ---- rule 4: the flaky registry ----------------------------------------

    def call_registry(self, args, entry):
        delay = REGISTRY_BACKOFF
        for attempt in range(REGISTRY_ATTEMPTS):
            try:
                result = store.call_tool("verify_certification", args)
                self.verified_certs += [m["canonical_name"] for m in result.get("matches", [])]
                return result
            except store.ExternalServiceError as exc:
                if attempt == REGISTRY_ATTEMPTS - 1:
                    raise                      # give up: caller sees a normal error
                entry["retries"] += 1
                print(f"      (registry failed: {exc}; retry {entry['retries']} in {delay:.1f}s)")
                time.sleep(delay)
                delay *= 2

    # ---- rule 5: a human approves destructive actions ----------------------

    def get_human_approval(self, result, args):
        if self.approve == "ask":
            print(f"\n[approval needed] {args.get('action')} on {args.get('target_id')}: "
                  f"{args.get('reason')}")
            approved = input("approve? [y/N] ").strip().lower() == "y"
            note = "approved by human on stdin" if approved else "declined by human"
        elif self.approve == "auto":
            approved, note = True, "approved — human SIMULATED by the batch harness (see README)"
        else:
            approved, note = False, "no human available; not approved"
        if approved:
            store.approve_token(result["confirmation_token"])  # the "human clicks approve" step
        return {**result, "approved": approved, "message": note}

    # ---- rule 6: the story of the run, for the trace -----------------------

    def summary(self):
        changed = []
        for e in self.trace:
            if e["error"]:
                continue
            a = e["arguments"]
            if e["tool"] == "update_profile_field":
                changed.append(f"added {a.get('fact_type')} '{a.get('value')}'")
            elif e["tool"] == "propose_achievement":
                changed.append(f"created achievement '{a['payload'].get('title')}'")
            elif e["tool"] == "overwrite_achievement":
                changed.append(f"enriched {a.get('achievement_id')} (human-approved overwrite)")
            elif e["tool"] == "flag_for_human_review":
                changed.append(f"flagged for human review: {a.get('reason', '')[:90]}")
            elif e["tool"] == "ask_followup":
                changed.append(f"asked follow-up: {a.get('question', '')[:90]}")
        return {
            "tool_calls": len(self.trace),
            "writes": sum(1 for e in self.trace if e["tool"] in WRITE_TOOLS and not e["error"]),
            "errors": sum(1 for e in self.trace if e["error"]),
            "what_changed": "; ".join(changed) or "no changes",
        }


# ---------------------------------------------------------------------------
# What the model is told about each tool (Anthropic tool format)
# ---------------------------------------------------------------------------

STATUS = {"type": "string", "enum": ["supported", "needs_clarification", "conflicting"]}
IDS = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {"name": "search_profile",
     "description": "Search the existing profile (achievements and facts) by keywords. "
                    "ALWAYS call this before any write: the professional often describes "
                    "work that is already on the profile under a different name. Lexical "
                    "match only — try a second short query before concluding something is absent.",
     "input_schema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "3-8 keywords, not a sentence"}}}},

    {"name": "get_achievement",
     "description": "Read one achievement in full, including its current evidence. Call "
                    "before enriching it or comparing it against a new claim.",
     "input_schema": {"type": "object", "required": ["achievement_id"], "properties": {
         "achievement_id": {"type": "string"}}}},

    {"name": "propose_achievement",
     "description": "Create a NEW achievement card. Only for work not already on the profile "
                    "(search first). evidence_message_ids must be the exact messages where the "
                    "professional stated each part; never cite the interviewer's questions.",
     "input_schema": {"type": "object", "required": ["payload"], "properties": {
         "payload": {"type": "object",
                     "required": ["title", "description", "evidence_message_ids", "status"],
                     "properties": {
                         "title": {"type": "string"},
                         "description": {"type": "string"},
                         "contribution": {"type": "string", "description": "what THEY personally did"},
                         "skills": {"type": "array", "items": {"type": "string"}},
                         "outcome": {"type": "string", "description": "only figures they stated"},
                         "period": {"type": "string"},
                         "evidence_message_ids": IDS,
                         "confidence": {"type": "number"},
                         "status": STATUS}}}}},

    {"name": "update_profile_field",
     "description": "Add ONE short profile fact: a skill, role, certification, project or "
                    "career_event. The backend stores anything, so the burden is on you: search "
                    "first, one fact per call, cite evidence_message_ids, never re-add something "
                    "already there. A certification may only be 'supported' after "
                    "verify_certification confirmed that exact name.",
     "input_schema": {"type": "object", "required": ["fact_type", "value", "evidence_message_ids"],
                      "properties": {
                          "fact_type": {"type": "string", "enum": sorted(store.VALID_FACT_TYPES)},
                          "value": {"type": "string", "description": "short noun phrase"},
                          "evidence_message_ids": IDS,
                          "confidence": {"type": "number"},
                          "status": STATUS}}},

    {"name": "overwrite_achievement",
     "description": "IRREVERSIBLY replace fields of an existing achievement (use to enrich it "
                    "with outcome/period/contribution). Needs a confirmation_token that a human "
                    "approved via request_confirmation. Pass the full merged record and keep "
                    "existing correct fields.",
     "input_schema": {"type": "object", "required": ["achievement_id", "payload", "confirmation_token"],
                      "properties": {
                          "achievement_id": {"type": "string"},
                          "payload": {"type": "object"},
                          "confirmation_token": {"type": "string"}}}},

    {"name": "request_confirmation",
     "description": "Ask a human to approve a destructive action (overwrite_achievement). "
                    "Returns a token; proceed only if the response says approved=true.",
     "input_schema": {"type": "object", "required": ["action", "target_id", "reason"],
                      "properties": {
                          "action": {"type": "string"},
                          "target_id": {"type": "string"},
                          "reason": {"type": "string", "description": "what changes and why"}}}},

    {"name": "verify_certification",
     "description": "Look a certification name up in an external registry. Outages are retried "
                    "for you; vague names return several candidates. It can NOT confirm the "
                    "person holds it. Zero or multiple matches, or an outage, means: do not "
                    "store the certification — ask a follow-up instead.",
     "input_schema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string"}}}},

    {"name": "flag_for_human_review",
     "description": "Park a claim for a human reviewer INSTEAD of writing it. Use when a new "
                    "statement contradicts earlier, more specific evidence, or when you cannot "
                    "safely decide. refs = the message ids and record ids involved.",
     "input_schema": {"type": "object", "required": ["reason", "refs"], "properties": {
         "reason": {"type": "string"}, "refs": IDS}}},

    {"name": "ask_followup",
     "description": "Ask the professional ONE focused question when a claim is too vague to "
                    "store (no name, no date, no outcome). At most one per conversation; do not "
                    "write the vague version first.",
     "input_schema": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string"}, "about": {"type": "string"}}}},
]
