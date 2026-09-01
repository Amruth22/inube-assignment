"""Four things that must never regress. Run with:  python -m pytest -q

Each test would fail if the agent started behaving badly in a way that
matters: inventing evidence, duplicating facts, overwriting without a human,
crashing on registry outages, or writing when there is nothing to write.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import traccia_store as store            # noqa: E402
from toolkit import Executor             # noqa: E402


@pytest.fixture()
def executor():
    store.load_profile(str(ROOT / "data" / "profile.json"))
    ex = Executor(approval_mode="deny")
    ex.allow_evidence(["M1", "M2", "M4"])
    return ex


def test_writes_without_real_evidence_are_refused(executor):
    """The store would accept both of these; the policy layer must not."""
    before = len(store.STORE.profile_facts)

    _, err = executor.execute("update_profile_field",
                              {"fact_type": "skill", "value": "Kubernetes"})
    assert err and "evidence" in err

    _, err = executor.execute("update_profile_field",
                              {"fact_type": "skill", "value": "Kubernetes",
                               "evidence_message_ids": ["M999"]})
    assert err and "M999" in err

    assert len(store.STORE.profile_facts) == before


def test_duplicate_facts_are_refused(executor):
    """'SQL' is already on the seeded profile; re-adding it must be blocked."""
    before = len(store.STORE.profile_facts)
    _, err = executor.execute("update_profile_field",
                              {"fact_type": "skill", "value": "sql",
                               "evidence_message_ids": ["M2"]})
    assert err and "duplicate" in err.lower()
    assert len(store.STORE.profile_facts) == before


def test_overwrite_needs_human_approval(executor):
    """With no human approval (approval_mode='deny'), an overwrite must fail
    and the achievement must be untouched — even with a pending token."""
    res, _ = executor.execute("request_confirmation",
                              {"action": "overwrite_achievement",
                               "target_id": "A-001", "reason": "test"})
    assert res["approved"] is False

    before = store.dump_store()["achievements"]["A-001"]
    _, err = executor.execute("overwrite_achievement",
                              {"achievement_id": "A-001",
                               "payload": {"title": "hacked"},
                               "confirmation_token": res["confirmation_token"]})
    assert err and "ConfirmationRequired" in err
    assert store.dump_store()["achievements"]["A-001"] == before


def test_registry_outage_is_survived_not_crashed(executor, monkeypatch):
    """A dead registry surfaces as a normal tool error after retries; the
    agent process must not raise, and no certification may be stored as
    supported afterwards."""
    monkeypatch.setenv("TRACCIA_FORCE_FAIL", "1")
    monkeypatch.setattr("toolkit.EXTERNAL_BACKOFF", 0.01)
    _, err = executor.execute("verify_certification", {"name": "cloud architecture"})
    assert err and "ExternalServiceError" in err
    assert executor.trace[-1]["retries"] == 2

    _, err = executor.execute("update_profile_field",
                              {"fact_type": "certification",
                               "value": "AWS Certified Solutions Architect",
                               "evidence_message_ids": ["M2"],
                               "status": "supported"})
    assert err and "verify_certification" in err


def test_quiet_conversation_writes_nothing():
    """End-to-end on C-206 (small talk only): zero writes, zero flags,
    zero follow-ups."""
    from agent import run_batch
    trace = run_batch(str(ROOT / "data" / "profile.json"),
                      str(ROOT / "data" / "conversation_C-206.json"),
                      model_name="mock", scenario="test-S3")
    assert trace["summary"]["writes"] == 0
    assert trace["store_after"]["review_queue"] == []
    assert trace["store_after"]["followups"] == []
    assert all(f["source"] == "seed"
               for f in trace["store_after"]["profile_facts"].values())
