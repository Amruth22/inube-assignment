"""Five things that must never regress.  Run:  python -m pytest -q

No API key needed: four tests hit the policy layer directly, and the loop test
feeds the agent a scripted fake model instead of Claude.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import traccia_store as store          # noqa: E402
from agent import run_agent            # noqa: E402
from toolkit import Executor           # noqa: E402


@pytest.fixture()
def executor():
    store.load_profile(str(ROOT / "data" / "profile.json"))
    ex = Executor(approve="never")
    ex.known_message_ids |= {"M1", "M2", "M4"}
    return ex


def test_writes_without_real_evidence_are_refused(executor):
    """The store would accept both of these; our policy must not."""
    before = len(store.STORE.profile_facts)
    _, err = executor.run("update_profile_field", {"fact_type": "skill", "value": "Kubernetes"})
    assert err and "evidence" in err
    _, err = executor.run("update_profile_field", {"fact_type": "skill", "value": "Kubernetes",
                                                   "evidence_message_ids": ["M999"]})
    assert err and "M999" in err
    assert len(store.STORE.profile_facts) == before


def test_duplicate_facts_are_refused(executor):
    """'SQL' is already on the seeded profile; adding it again must be blocked."""
    before = len(store.STORE.profile_facts)
    _, err = executor.run("update_profile_field", {"fact_type": "skill", "value": "sql",
                                                   "evidence_message_ids": ["M2"]})
    assert err and "duplicates" in err
    assert len(store.STORE.profile_facts) == before


def test_overwrite_needs_human_approval(executor):
    """With no human (approve='never'), an overwrite fails even with a pending token."""
    res, _ = executor.run("request_confirmation", {"action": "overwrite_achievement",
                                                   "target_id": "A-001", "reason": "test"})
    assert res["approved"] is False
    before = store.dump_store()["achievements"]["A-001"]
    _, err = executor.run("overwrite_achievement", {"achievement_id": "A-001",
                                                    "payload": {"title": "hacked"},
                                                    "confirmation_token": res["confirmation_token"]})
    assert err and "ConfirmationRequired" in err
    assert store.dump_store()["achievements"]["A-001"] == before


def test_registry_outage_is_survived(executor, monkeypatch):
    """A dead registry becomes a normal error after retries, and no certification
    may be stored as 'supported' afterwards."""
    monkeypatch.setenv("TRACCIA_FORCE_FAIL", "1")
    monkeypatch.setattr("toolkit.REGISTRY_BACKOFF", 0.01)
    _, err = executor.run("verify_certification", {"name": "cloud architecture"})
    assert err and "ExternalServiceError" in err
    assert executor.trace[-1]["retries"] == 2
    _, err = executor.run("update_profile_field", {"fact_type": "certification",
                                                   "value": "AWS Certified Solutions Architect",
                                                   "evidence_message_ids": ["M2"]})
    assert err and "verify_certification" in err


def test_loop_survives_a_bad_tool_call(executor):
    """If the model asks for a tool that does not exist, the loop reports the
    error back to it and continues instead of crashing."""
    scripted = iter([
        {"content": [{"type": "tool_use", "id": "t1", "name": "delete_everything", "input": {}}]},
        {"content": [{"type": "text", "text": "Nothing changed."}]},
    ])
    final = run_agent(executor, messages=[], ask=lambda msgs: next(scripted))
    assert final == "Nothing changed."
    assert executor.trace[0]["error"] and "unknown tool" in executor.trace[0]["error"]
    assert executor.summary()["writes"] == 0
