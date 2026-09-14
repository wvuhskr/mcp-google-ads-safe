"""undo_change: reverse drafts built from the audit log, riding the normal rails."""
import copy
import json
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, tools, undo
from tests.conftest import TEST_CUSTOMER_ID as CID


def events():
    return [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]


def apply(draft):
    out = rails.apply_draft(draft["draft_id"])
    assert out["applied"] is True
    return out


@pytest.fixture
def campaign_state(fake_client, monkeypatch):
    campaign = {"resource_name": f"customers/{CID}/campaigns/42", "status": "ENABLED",
                "name": "Before", "advertising_channel_type": "SEARCH",
                "target_cpa": {"target_cpa_micros": "10000000"},
                "maximize_conversions": {"target_cpa_micros": "10000000"}}
    monkeypatch.setattr(client, "update_state", lambda cid, kind, eid: copy.deepcopy(campaign))
    monkeypatch.setattr(client, "verify_post_apply", lambda checks: None)
    fake_client.strategy["type"] = "MAXIMIZE_CONVERSIONS"
    return campaign


# --- gating -----------------------------------------------------------------------------

def test_undo_requires_writes_and_known_applied_draft(fake_client, monkeypatch):
    with pytest.raises(rails.RailViolation) as exc:
        tools.undo_change("nope")
    assert exc.value.code == "UNKNOWN_DRAFT"
    draft = tools.update_campaign("42", "75", CID)  # drafted, never applied
    with pytest.raises(rails.RailViolation) as exc:
        tools.undo_change(draft["draft_id"])
    assert exc.value.code == "NOT_APPLIED"
    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "false")
    with pytest.raises(rails.RailViolation) as exc:
        tools.undo_change(draft["draft_id"])
    assert exc.value.code == "WRITES_DISABLED"


def test_undo_refuses_unknown_outcome(fake_client):
    draft = tools.update_campaign("42", "75", CID)
    fake_client.dispatch_error = rails.UnknownWriteOutcome("wire", request_id="r", failure={})
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(draft["draft_id"])
    with pytest.raises(rails.RailViolation) as exc:
        tools.undo_change(draft["draft_id"])
    assert exc.value.code == "OUTCOME_UNKNOWN"


def test_undo_never_dispatches_and_refuses_a_second_undo(fake_client):
    draft = tools.update_campaign("42", "75", CID)
    apply(draft)
    n = len(fake_client.dispatch_calls)
    rev = tools.undo_change(draft["draft_id"])
    assert rev["dry_run"] is True and rev["undo_of"] == draft["draft_id"]
    assert len(fake_client.dispatch_calls) == n  # undo only drafts
    link = [e for e in events() if e["tool"] == "undo_change"]
    assert link[-1]["undo_of"] == draft["draft_id"] and link[-1]["draft_id"] == rev["draft_id"]
    with pytest.raises(rails.RailViolation) as exc:
        tools.undo_change(draft["draft_id"])
    assert exc.value.code == "ALREADY_UNDONE"


# --- budget / update -----------------------------------------------------------------------

def test_undo_budget_only_restores_previous_amount(fake_client):
    draft = tools.update_campaign("42", "75", CID)
    apply(draft)
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["new_daily_budget"] == "50"  # DEFAULT_BUDGET_INFO amount
    assert rev["original_tool"] == "update_campaign"


def test_undo_combined_update_restores_name_status_target_and_budget(campaign_state, fake_client):
    draft = tools.update_campaign("42", "80", CID, name="After", status="PAUSED", target_cpa=20)
    assert draft["preview"]["current_daily_budget"] == "50"
    apply(draft)
    rev = tools.undo_change(draft["draft_id"])
    req = rev["preview"]["requested_changes"]
    assert req == {"daily_budget": "50", "status": "ENABLED", "name": "Before", "target_cpa": "10"}


def test_undo_target_clear_restores_previous_target(campaign_state, fake_client):
    draft = tools.update_campaign("42", customer_id=CID, clear_target_cpa=True)
    apply(draft)
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["requested_changes"] == {"target_cpa": "10"}


def test_undo_target_set_with_no_previous_target_clears(campaign_state, fake_client):
    campaign_state["target_cpa"] = {}
    campaign_state["maximize_conversions"] = {}
    draft = tools.update_campaign("42", customer_id=CID, target_cpa=20)
    apply(draft)
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["requested_changes"] == {"clear_target_cpa": "True"}
    assert rev["undo_notes"]


def test_undo_ad_group_cpc(fake_client, monkeypatch):
    group = {"resource_name": f"customers/{CID}/adGroups/7", "campaign": f"customers/{CID}/campaigns/42",
             "status": "ENABLED", "name": "G", "cpc_bid_micros": "1250000",
             "effective_cpc_bid_micros": "1250000", "target_cpa_micros": None,
             "effective_target_cpa_micros": None, "effective_target_cpa_source": "UNSPECIFIED"}
    monkeypatch.setattr(client, "update_state", lambda cid, kind, eid: copy.deepcopy(group))
    monkeypatch.setattr(client, "verify_post_apply", lambda checks: None)
    draft = tools.update_ad_group("7", CID, cpc_bid=2)
    apply(draft)
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["requested_changes"] == {"cpc_bid": "1.25"}


# --- status ------------------------------------------------------------------------------

def test_undo_pause_drafts_enable(fake_client):
    draft = tools.pause_entity("campaign", "424242424", CID)
    apply(draft)
    fake_client.status_info["status"] = "PAUSED"  # account now reflects the pause
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["tool"] == "enable_entity" and rev["preview"]["new_status"] == "ENABLED"


# --- criteria ----------------------------------------------------------------------------

AG = f"customers/{CID}/adGroups/22"
CA = f"customers/{CID}/campaigns/11"


@pytest.fixture
def criteria(fake_client, monkeypatch):
    state = {"rows": [], "parent": {"resource_name": AG, "campaign": CA, "status": "ENABLED"},
             "time_zone": "America/New_York"}
    monkeypatch.setattr(client, "criteria_state", lambda *a: copy.deepcopy(state))
    monkeypatch.setattr(client, "verify_post_apply", lambda checks: None)
    monkeypatch.setattr(client, "verify_created_results", lambda checks, result: None)
    return state


def test_undo_keyword_add_removes_created_ids(criteria, fake_client):
    draft = tools.draft_keywords("22", [{"text": "ac repair", "match_type": "EXACT"}], CID)
    fake_client.dispatch_result = {"results": [], "request_id": None,
                                   "resource_names": [f"{CID and 'customers/' + CID}/adGroupCriteria/22~901"]}
    apply(draft)
    criteria["rows"] = [{"resource_name": f"customers/{CID}/adGroupCriteria/22~901", "criterion_id": "901",
                         "ad_group": AG, "type": "KEYWORD", "negative": False, "status": "ENABLED",
                         "keyword": {"text": "ac repair", "match_type": "EXACT"}}]
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["tool"] == "remove_keywords"
    assert rev["preview"]["operations"][0]["operation"] == {"remove": f"customers/{CID}/adGroupCriteria/22~901"}


def test_undo_keyword_remove_readds_text_and_match(criteria, fake_client):
    criteria["rows"] = [{"resource_name": f"customers/{CID}/adGroupCriteria/22~901", "criterion_id": "901",
                         "ad_group": AG, "type": "KEYWORD", "negative": False, "status": "ENABLED",
                         "keyword": {"text": "ac repair", "match_type": "PHRASE"}}]
    draft = tools.remove_keywords("22", ["901"], CID)
    apply(draft)
    criteria["rows"] = []
    rev = tools.undo_change(draft["draft_id"])
    assert rev["preview"]["tool"] == "draft_keywords"
    created = rev["preview"]["operations"][0]["operation"]["create"]
    assert created["keyword"] == {"text": "ac repair", "match_type": "PHRASE"}
    assert rev["undo_notes"]


def test_undo_negative_remove_with_mixed_match_types_refuses(criteria, fake_client):
    criteria["parent"] = {"resource_name": CA, "status": "ENABLED"}
    criteria["rows"] = [
        {"resource_name": f"customers/{CID}/campaignCriteria/11~1", "criterion_id": "1", "campaign": CA,
         "type": "KEYWORD", "negative": True, "status": "ENABLED",
         "keyword": {"text": "free", "match_type": "EXACT"}},
        {"resource_name": f"customers/{CID}/campaignCriteria/11~2", "criterion_id": "2", "campaign": CA,
         "type": "KEYWORD", "negative": True, "status": "ENABLED",
         "keyword": {"text": "cheap", "match_type": "PHRASE"}}]
    draft = tools.remove_negative_keywords("11", ["1", "2"], CID)
    apply(draft)
    criteria["rows"] = []
    with pytest.raises(rails.RailViolation) as exc:
        tools.undo_change(draft["draft_id"])
    assert exc.value.code == "NOT_REVERSIBLE" and "mixed match types" in str(exc.value)


def test_undo_keyword_bid_restores_previous(criteria, fake_client):
    fake_client.strategy["type"] = "MANUAL_CPC"
    criteria["rows"] = [{"resource_name": f"customers/{CID}/adGroupCriteria/22~901", "criterion_id": "901",
                         "ad_group": AG, "type": "KEYWORD", "negative": False, "status": "ENABLED",
                         "cpc_bid_micros": "1500000", "effective_cpc_bid_micros": "1500000",
                         "keyword": {"text": "ac repair", "match_type": "EXACT"}}]
    draft = tools.update_keyword_bid("22", "901", "3", CID)
    apply(draft)
    criteria["rows"][0]["cpc_bid_micros"] = "3000000"
    rev = tools.undo_change(draft["draft_id"])
    op = rev["preview"]["operations"][0]["operation"]["update"]
    assert op["cpc_bid_micros"] == 1500000 and op["resource_name"].endswith("22~901")


def test_undo_schedule_restores_previous_week(criteria, fake_client, monkeypatch):
    criteria["parent"] = {"resource_name": CA, "status": "ENABLED"}
    criteria["rows"] = [{"resource_name": f"customers/{CID}/campaignCriteria/11~5", "criterion_id": "5",
                         "campaign": CA, "type": "AD_SCHEDULE", "negative": False, "status": "ENABLED",
                         "ad_schedule": {"day_of_week": "MONDAY", "start_hour": 5, "start_minute": "ZERO",
                                         "end_hour": 22, "end_minute": "ZERO"}}]
    draft = tools.set_campaign_schedule("11", [{"day_of_week": "TUESDAY", "start_hour": 8, "start_minute": 0,
                                                "end_hour": 20, "end_minute": 0}], CID)
    apply(draft)
    criteria["rows"] = []
    rev = tools.undo_change(draft["draft_id"])
    created = [op["operation"]["create"]["ad_schedule"] for op in rev["preview"]["operations"]
               if "create" in op["operation"]]
    assert created == [{"day_of_week": "MONDAY", "start_hour": 5, "start_minute": "ZERO",
                        "end_hour": 22, "end_minute": "ZERO"}]


# --- creations and non-reversible ------------------------------------------------------

def test_undo_creation_pauses_only_if_enabled(fake_client, monkeypatch):
    audit.log_event("draft_campaign", "apply", {
        "draft_id": "cre1", "customer_id": CID, "preview": {"customer_id": CID},
        "result": {"resource_names": [f"customers/{CID}/campaigns/424242424"]}})
    fake_client.status_info["status"] = "PAUSED"
    out = tools.undo_change("cre1")
    assert out["reversible"] is False and "nothing to reverse" in out["reason"]
    fake_client.status_info["status"] = "ENABLED"
    rev = tools.undo_change("cre1")
    assert rev["preview"]["tool"] == "pause_entity"


@pytest.mark.parametrize("tool", ["remove_entity", "upload_image_asset", "apply_recommendation"])
def test_undo_reports_permanent_changes(fake_client, tool):
    audit.log_event(tool, "apply", {"draft_id": "perm1", "customer_id": CID, "preview": {}, "result": {}})
    out = tools.undo_change("perm1")
    assert out == {"reversible": False, "undo_of": "perm1", "original_tool": tool,
                   "reason": undo._NOT_REVERSIBLE[tool]}
    assert fake_client.dispatch_calls == []


def test_undo_tolerates_torn_audit_line(fake_client, monkeypatch):
    draft = tools.update_campaign("42", "75", CID)
    apply(draft)
    with open(audit.AUDIT_PATH, "a") as f:
        f.write("{not json\n")
    assert tools.undo_change(draft["draft_id"])["undo_of"] == draft["draft_id"]
