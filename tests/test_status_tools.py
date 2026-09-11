"""pause_entity / enable_entity: SetEntityStatusIntent compile branch, drafts, refusals, and
apply — over the in-memory fake client (mirrors the UpdateCampaignBudgetIntent tests in
test_rails.py). The fake_gads (real v25 proto) counterpart lives in test_client_api.py.
"""
import json

import pytest

from mcp_google_ads_safe import audit, rails

CID = "1234567890"
CAMPAIGN_RN = f"customers/{CID}/campaigns/424242424"


def _assert_refused_audit(audit_file, code):
    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    refused = [e for e in events if e["phase"] == "refused"]
    assert len(refused) == 1
    assert refused[0]["code"] == code


# --- compile(SetEntityStatusIntent) -----------------------------------------------------

def test_compile_pause_enabled_campaign_builds_plan(fake_client):
    compiled = rails.compile(
        rails.SetEntityStatusIntent(CID, "campaign", "424242424", "PAUSED"))
    assert compiled.preview["tool"] == "pause_entity"
    assert compiled.preview["new_status"] == "PAUSED"
    assert compiled.preview["current_status"] == "ENABLED"
    assert compiled.preview["resource_name"] == CAMPAIGN_RN
    assert compiled.preview["digest"] == rails.plan_digest(compiled.plan)

    plan = compiled.plan
    assert isinstance(plan, rails.EntityMutationPlan)
    assert plan.kind == "entity"
    assert plan.mutate_customer_id == CID
    op = plan.operations[0]
    assert op.service == "CampaignService"
    assert op.update_mask == ["status"]
    assert op.operation == {"update": {"resource_name": CAMPAIGN_RN, "status": "PAUSED"}}
    assert compiled.fingerprint == {"resource_name": CAMPAIGN_RN, "status_current": "ENABLED"}


def test_compile_enable_paused_ad_group_builds_plan(fake_client):
    ad_group_rn = f"customers/{CID}/adGroups/9988776655"
    fake_client.status_info = {"exists": True, "resource_name": ad_group_rn, "status": "PAUSED"}
    compiled = rails.compile(
        rails.SetEntityStatusIntent(CID, "ad_group", "9988776655", "ENABLED"))
    assert compiled.preview["tool"] == "enable_entity"
    plan = compiled.plan
    op = plan.operations[0]
    assert op.service == "AdGroupService"
    assert op.operation == {"update": {"resource_name": ad_group_rn, "status": "ENABLED"}}


# --- refusals: each asserted by .code AND a phase-"refused" audit event -----------------

def test_not_found_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    fake_client.status_info["exists"] = False
    with pytest.raises(rails.RailViolation) as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    assert exc.value.code == "NOT_FOUND"
    _assert_refused_audit(audit_file, "NOT_FOUND")


def test_entity_removed_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    fake_client.status_info["status"] = "REMOVED"
    with pytest.raises(rails.RailViolation) as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    assert exc.value.code == "ENTITY_REMOVED"
    _assert_refused_audit(audit_file, "ENTITY_REMOVED")


def test_already_in_status_pause_direction(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    fake_client.status_info["status"] = "PAUSED"
    with pytest.raises(rails.RailViolation, match="already PAUSED") as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    assert exc.value.code == "ALREADY_IN_STATUS"
    _assert_refused_audit(audit_file, "ALREADY_IN_STATUS")


def test_already_in_status_enable_direction(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    fake_client.status_info["status"] = "ENABLED"  # default, explicit for clarity
    with pytest.raises(rails.RailViolation, match="already ENABLED") as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "ENABLED")
    assert exc.value.code == "ALREADY_IN_STATUS"
    _assert_refused_audit(audit_file, "ALREADY_IN_STATUS")


def test_unsupported_entity_type_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    with pytest.raises(rails.RailViolation) as exc:
        rails.set_entity_status_draft(CID, "keyword", "1", "PAUSED")
    assert exc.value.code == "UNSUPPORTED_ENTITY"
    _assert_refused_audit(audit_file, "UNSUPPORTED_ENTITY")


def test_bad_status_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    with pytest.raises(rails.RailViolation) as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "REMOVED")
    assert exc.value.code == "BAD_STATUS"
    _assert_refused_audit(audit_file, "BAD_STATUS")


def test_writes_disabled_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "false")
    with pytest.raises(rails.RailViolation) as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    assert exc.value.code == "WRITES_DISABLED"
    _assert_refused_audit(audit_file, "WRITES_DISABLED")


def test_not_allowlisted_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},5555555555")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "5555555555")  # CID not write-allowlisted
    with pytest.raises(rails.RailViolation, match="not allowlisted") as exc:
        rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    assert exc.value.code == "NOT_ALLOWLISTED"
    _assert_refused_audit(audit_file, "NOT_ALLOWLISTED")


def test_cross_customer_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    fake_client.status_info["resource_name"] = "customers/9999999999/campaigns/1"
    with pytest.raises(rails.RailViolation, match="cross-customer") as exc:
        rails.set_entity_status_draft(CID, "campaign", "1", "PAUSED")
    assert exc.value.code == "CROSS_CUSTOMER"
    _assert_refused_audit(audit_file, "CROSS_CUSTOMER")


# --- happy-path draft -> apply dispatches the plan exactly once, audits "apply" ---------

def test_draft_then_apply_dispatches_plan_and_audits(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    out = rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    assert out["dry_run"] is True
    draft_id = out["draft_id"]
    plan = rails._DRAFTS[draft_id].plan

    apply_out = rails.apply_draft(draft_id)
    assert apply_out["applied"] is True
    assert fake_client.dispatch_calls == [plan]
    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    assert [e for e in events if e["phase"] == "apply"]
    # single-use
    with pytest.raises(rails.RailViolation, match="unknown or already-applied"):
        rails.apply_draft(draft_id)


# --- validate_fn drift ------------------------------------------------------------------

def test_validate_fn_drift_into_target_status_refuses_without_dispatch(
        fake_client, tmp_path, monkeypatch):
    # Ambiguity resolution (see report): re-running the same exists/REMOVED/ALREADY_IN_STATUS
    # checks against the FRESH read (brief step 9) means a drift where the account has already
    # moved into the target status surfaces as ALREADY_IN_STATUS, not a separate generic
    # "drifted" message -- it is still a fail-closed, non-dispatching, non-consuming refusal.
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    draft_id = out["draft_id"]
    fake_client.status_info["status"] = "PAUSED"  # someone else already paused it

    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(draft_id)
    assert exc.value.code == "ALREADY_IN_STATUS"
    assert fake_client.dispatch_calls == []          # NOT dispatched
    assert draft_id in rails._DRAFTS                 # NOT consumed by the refusal

    # discriminating: no drift -> applies
    fake_client.status_info["status"] = "ENABLED"
    apply_out = rails.apply_draft(draft_id)
    assert apply_out["applied"] is True


def test_validate_fn_generic_drift_on_resource_name_change(fake_client, tmp_path, monkeypatch):
    # Exercises the final generic fingerprint-compare fallback specifically: status stays
    # ENABLED (so it is neither the target PAUSED nor REMOVED -- the per-field checks above it
    # pass), but resource_name changed underneath the draft.
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.set_entity_status_draft(CID, "campaign", "424242424", "PAUSED")
    draft_id = out["draft_id"]
    fake_client.status_info["resource_name"] = f"customers/{CID}/campaigns/999999999"

    with pytest.raises(rails.RailViolation, match="drifted"):
        rails.apply_draft(draft_id)
    assert fake_client.dispatch_calls == []
    assert draft_id in rails._DRAFTS
