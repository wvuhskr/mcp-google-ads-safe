"""Regression tests for the 2026-09-14 public-readiness review fixes.

One test per finding; each fails if the corresponding guard is removed."""
import copy
import json
import threading
from pathlib import Path

import pytest
from google.api_core import exceptions as api_exceptions

from mcp_google_ads_safe import app, audit, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.test_protocol_errors import boundary, payload


@pytest.fixture
def campaign_state(fake_client, monkeypatch):
    campaign = {"resource_name": f"customers/{CID}/campaigns/42", "status": "PAUSED",
                "name": "Before", "advertising_channel_type": "SEARCH",
                "target_cpa": {"target_cpa_micros": "10000000"},
                "maximize_conversions": {"target_cpa_micros": "10000000"}}
    monkeypatch.setattr(client, "update_state", lambda cid, kind, eid: copy.deepcopy(campaign))
    fake_client.strategy["type"] = "MAXIMIZE_CONVERSIONS"
    return campaign


# 1. PMax target clear is refused ------------------------------------------------------

def test_pmax_target_cpa_clear_refused(campaign_state, fake_client):
    campaign_state["advertising_channel_type"] = "PERFORMANCE_MAX"
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", clear_target_cpa=True)
    assert exc.value.code == "PMAX_TARGET_CLEAR"
    assert fake_client.dispatch_calls == []


def test_pmax_target_cpa_raise_still_allowed(campaign_state):
    campaign_state["advertising_channel_type"] = "PERFORMANCE_MAX"
    out = tools.update_campaign("42", target_cpa=20)
    assert out["dry_run"] is True


def test_search_target_cpa_clear_still_allowed(campaign_state):
    out = tools.update_campaign("42", clear_target_cpa=True)
    assert out["dry_run"] is True


# 2. Draft store is serialized ---------------------------------------------------------

def test_concurrent_apply_dispatches_exactly_once(fake_client, monkeypatch):
    import time
    out = rails.update_campaign_budget_draft(CID, "77", "75")
    real_dispatch = client._dispatch

    def slow_dispatch(plan):
        time.sleep(0.2)  # widen the peek -> pop -> dispatch window
        return real_dispatch(plan)

    monkeypatch.setattr(client, "_dispatch", slow_dispatch)
    outcomes = []

    def run():
        try:
            outcomes.append(rails.apply_draft(out["draft_id"]))
        except rails.RailViolation as e:
            outcomes.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(fake_client.dispatch_calls) == 1
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert sum(isinstance(o, rails.RailViolation) for o in outcomes) == 1


# 3. Credential YAML key allowlist -----------------------------------------------------

def test_credential_yaml_refuses_logging_key(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(client.GoogleAdsClient, "load_from_dict",
                        staticmethod(lambda cfg, version=None: seen.append(cfg) or object()))
    path = tmp_path / "creds.yaml"
    path.write_text("developer_token: X\nlogging:\n  version: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported key"):
        _ORIGINAL_GADS(str(path))
    assert seen == []
    path.write_text("developer_token: X\nlogin_customer_id: '1'\n", encoding="utf-8")
    _ORIGINAL_GADS(str(path))
    assert seen and set(seen[0]) == {"developer_token", "login_customer_id", "use_proto_plus"}


# conftest's autouse fixture replaces client.gads with a refuser; keep the real one, captured
# at collection time, so the credential-file guard itself can be exercised offline.
_ORIGINAL_GADS = client.gads


# 4. Any GoogleAPICallError from the RPC is an unknown outcome ------------------------

@pytest.mark.parametrize("exc_type", [api_exceptions.Aborted, api_exceptions.Unknown,
                                      api_exceptions.Cancelled, api_exceptions.ResourceExhausted])
def test_api_call_errors_audit_unknown_not_error(fake_client, exc_type):
    fake_client.dispatch_error = exc_type("offline")
    out = rails.update_campaign_budget_draft(CID, "77", "75")
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(out["draft_id"])
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    assert events[-1]["phase"] == "unknown"
    assert not [e for e in events if e["phase"] == "error"]


@pytest.mark.parametrize("exc_type", [api_exceptions.InvalidArgument, api_exceptions.PermissionDenied,
                                      api_exceptions.NotFound, api_exceptions.Unauthenticated])
def test_definite_rejections_audit_error_not_unknown(fake_client, exc_type):
    # Google answered and refused: nothing landed, so this is "error", not "unknown".
    fake_client.dispatch_error = exc_type("offline")
    out = rails.update_campaign_budget_draft(CID, "77", "75")
    with pytest.raises(exc_type):
        rails.apply_draft(out["draft_id"])
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    assert events[-1]["phase"] == "error"
    assert not [e for e in events if e["phase"] == "unknown"]


# 5. Consuming refusals are audited ----------------------------------------------------

def test_unknown_expired_tampered_refusals_are_audited(fake_client):
    def phases():
        return [json.loads(line)["code"] for line in Path(audit.AUDIT_PATH).read_text().splitlines()
                if json.loads(line)["phase"] == "refused"]

    with pytest.raises(rails.RailViolation):
        rails.apply_draft("nope")
    assert phases() == ["UNKNOWN_DRAFT"]

    out = rails.update_campaign_budget_draft(CID, "77", "75")
    rails._DRAFTS[out["draft_id"]].created_at -= rails.draft_ttl_seconds() + 1
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(out["draft_id"])
    assert phases()[-1] == "DRAFT_EXPIRED"

    out = rails.update_campaign_budget_draft(CID, "77", "75")
    rails._DRAFTS[out["draft_id"]].plan.operations[0].operation["update"]["amount_micros"] = 1
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(out["draft_id"])
    assert phases()[-1] == "PLAN_TAMPERED"
    assert fake_client.dispatch_calls == []


# 6. Landed-but-unverified is never phase "error" (covered by test_update_tools) ------

# 7. Separate target-CPA cap -----------------------------------------------------------

def test_target_cpa_cap_independent_of_cpc_cap(campaign_state, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MAX_CPC", "50")
    monkeypatch.setenv("GOOGLE_ADS_MAX_TARGET_CPA", "5")
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", target_cpa=10)
    assert exc.value.code == "CAP_EXCEEDED" and "GOOGLE_ADS_MAX_TARGET_CPA" in str(exc.value)


def test_target_cpa_cap_falls_back_to_cpc_cap(campaign_state, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MAX_CPC", "5")
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", target_cpa=10)
    assert exc.value.code == "CAP_EXCEEDED"


# 8. remove_entity opt-in defaults off --------------------------------------------------

def test_remove_opt_in_default_off(fake_client, monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_ALLOW_REMOVE_ENTITY", raising=False)
    reads = []
    monkeypatch.setattr(client, "removal_entity_state",
                        lambda *a, **k: reads.append(a) or {})
    with pytest.raises(rails.RailViolation) as exc:
        tools.remove_entity("campaign", "88")
    assert exc.value.code == "REMOVE_DISABLED"
    assert reads == []  # refused before any account read


def test_remove_opt_in_rechecked_at_apply(fake_client, monkeypatch):
    # a draft made while the opt-in was on cannot be applied after it is turned off
    d = rails.Draft(id="rm1", tool="remove_entity", preview={}, fingerprint={},
                    plan=rails.EntityMutationPlan(CID, [rails.MutationOp(
                        "CampaignService", {"remove": f"customers/{CID}/campaigns/88"}, [])], True))
    d.digest = rails.plan_digest(d.plan)
    rails._DRAFTS[d.id] = d
    monkeypatch.setenv("GOOGLE_ADS_ALLOW_REMOVE_ENTITY", "false")
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d.id)
    assert exc.value.code == "REMOVE_DISABLED"
    assert d.id in rails._DRAFTS  # non-consuming refusal
    assert fake_client.dispatch_calls == []


# 9. Generic "null"/JSON-string guard covers every tool -------------------------------

@pytest.mark.parametrize("name,args", [
    ("pause_entity", {"entity_type": "campaign", "entity_id": "1", "customer_id": "null"}),
    ("update_campaign", {"campaign_id": "1", "daily_budget": "5", "customer_id": " null "}),
    ("get_campaign_performance", {"customer_id": "null"}),
    ("draft_keywords", {"ad_group_id": "1", "keywords": '["a"]'}),
])
def test_generic_guard_rejects_coercible_strings(fake_client, name, args):
    result = payload(boundary(app.mcp, name, args))
    assert result["code"] == "BAD_INPUT"
    assert fake_client.dispatch_calls == []


def test_generic_guard_lets_ordinary_strings_through():
    app._reject_coercible_strings({"name": "null hypothesis", "q": "x [y]", "n": "123",
                                   "id": "not json", "empty": "", "nested": {"a": "null"}})


# GAQL hygiene --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["2026-01-01' OR 1=1 --", "yesterday", "20260101"])
def test_report_dates_must_be_iso(fake_client, bad):
    with pytest.raises(rails.RailViolation) as exc:
        client.campaign_performance(CID, bad, "2026-01-31")
    assert exc.value.code == "BAD_DATE"


def test_geo_query_escapes_backslash(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "gaql", lambda q, cid, page_token=None: seen.append(q) or {"results": []})
    client.geo_targets(CID, "x\\'")
    assert "LIKE '%x\\\\\\'%'" in seen[0]
