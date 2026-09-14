"""Offline update-family behavior, scope, and real v25 message proofs."""
import copy
import json

import pytest

from mcp_google_ads_safe import audit, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_row, make_search_response, make_type


@pytest.fixture
def state(fake_client, monkeypatch):
    campaign = {"resource_name": f"customers/{CID}/campaigns/42", "status": "ENABLED",
                "name": "Before", "target_cpa": {"target_cpa_micros": "10000000"},
                "maximize_conversions": {"target_cpa_micros": "10000000"}}
    group = {"resource_name": f"customers/{CID}/adGroups/7", "campaign": campaign["resource_name"],
             "status": "ENABLED", "name": "Before", "target_cpa_micros": "15000000",
             "cpc_bid_micros": "1000000", "effective_cpc_bid_micros": "1000000",
             "effective_target_cpa_micros": "15000000", "effective_target_cpa_source": "AD_GROUP"}
    states = {"campaign": campaign, "ad_group": group}
    monkeypatch.setattr(client, "update_state", lambda cid, kind, eid: copy.deepcopy(states[kind]))
    return states


def draft_plan(out):
    return rails._DRAFTS[out["draft_id"]].plan


@pytest.mark.parametrize("kwargs,code", [
    ({}, "EMPTY_UPDATE"), ({"status": "REMOVED"}, "BAD_STATUS"),
    ({"status": False}, "BAD_STATUS"), ({"name": ""}, "BAD_NAME"),
    ({"name": False}, "BAD_NAME"), ({"target_cpa": 0}, None),
    ({"target_cpa": False}, "BAD_AMOUNT"), ({"target_cpa": "nan"}, None),
    ({"target_cpa": ""}, "BAD_AMOUNT"), ({"target_cpa": 51}, "CAP_EXCEEDED"),
    ({"target_cpa": 2, "target_roas": 2}, "CONTRADICTORY_FIELDS"),
    ({"target_cpa": 2, "clear_target_cpa": True}, "CONTRADICTORY_FIELDS"),
    ({"clear_target_cpa": 1}, "BAD_CLEAR"),
])
def test_campaign_bad_input(state, fake_client, kwargs, code):
    fake_client.strategy["type"] = "TARGET_CPA"
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", **kwargs)
    if code:
        assert exc.value.code == code
    assert not fake_client.dispatch_calls


def test_budget_positional_compatibility(fake_client):
    out = tools.update_campaign("42", "75", CID)
    assert out["preview"]["new_daily_budget"] == "75"
    assert draft_plan(out).operations[0].update_mask == ["amount_micros"]


@pytest.mark.parametrize("value", [False, 0, "", "nan"])
def test_budget_invalid_value(fake_client, value):
    with pytest.raises(rails.RailViolation):
        tools.update_campaign("42", value)


def test_compound_update_atomic_real_message(state, fake_client, fake_gads):
    fake_client.strategy["type"] = "MAXIMIZE_CONVERSIONS"
    plan = draft_plan(tools.update_campaign("42", "90", status="PAUSED", name="After",
                                           target_cpa="20.123456"))
    fake_gads.mutate_response = make_type("MutateGoogleAdsResponse")
    client._dispatch_entity(plan, False)
    call = fake_gads.mutate_calls[0]
    assert call["partial_failure"] is False
    assert len(call["operations"]) == 2
    campaign = call["operations"][0].campaign_operation
    assert campaign.update.maximize_conversions.target_cpa_micros == 20123456
    assert list(campaign.update_mask.paths) == [
        "status", "name", "maximize_conversions.target_cpa_micros"]
    assert campaign.update.name == "After"
    assert campaign.update.status.name == "PAUSED"
    assert call["operations"][1].campaign_budget_operation.update.amount_micros == 90000000


@pytest.mark.parametrize("strategy,kwargs,path", [
    ("MAXIMIZE_CONVERSIONS", {"clear_target_cpa": True}, "maximize_conversions.target_cpa_micros"),
    ("MAXIMIZE_CONVERSION_VALUE", {"clear_target_roas": True},
     "maximize_conversion_value.target_roas"),
    ("TARGET_CPA", {"target_cpa": 12}, "target_cpa.target_cpa_micros"),
    ("TARGET_ROAS", {"target_roas": 1000}, "target_roas.target_roas"),
])
def test_target_literal_masks_real_messages(state, fake_client, fake_gads, strategy, kwargs, path):
    fake_client.strategy["type"] = strategy
    plan = draft_plan(tools.update_campaign("42", **kwargs))
    op = client._build_mutate_operation(fake_gads, plan.operations[0]).campaign_operation
    assert list(op.update_mask.paths) == [path]
    if "clear" in next(iter(kwargs)):
        parent, leaf = path.split(".")
        assert getattr(getattr(op.update, parent), leaf) == 0


@pytest.mark.parametrize("value", [0, False, .001, 1001, "inf", ""])
def test_roas_bounds(state, fake_client, value):
    fake_client.strategy["type"] = "TARGET_ROAS"
    with pytest.raises(rails.RailViolation):
        tools.update_campaign("42", target_roas=value)


def test_roas_not_currency_cap(state, fake_client, monkeypatch):
    fake_client.strategy["type"] = "TARGET_ROAS"
    monkeypatch.setenv("GOOGLE_ADS_MAX_CPC", "1")
    plan = draft_plan(tools.update_campaign("42", target_roas=200))
    assert plan.operations[0].operation["update"]["target_roas"] == {"target_roas": 200.0}


@pytest.mark.parametrize("kind", ["campaign", "ad_group"])
def test_removed(state, kind):
    state[kind]["status"] = "REMOVED"
    with pytest.raises(rails.RailViolation, match="removed"):
        getattr(tools, f"update_{kind}")("42", name="After")


def test_apply_drift_and_cap_recheck(state, fake_client, monkeypatch):
    fake_client.strategy["type"] = "TARGET_CPA"
    out = tools.update_campaign("42", target_cpa=20)
    state["campaign"]["name"] = "Changed"
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(out["draft_id"])
    assert exc.value.code == "STATE_DRIFT"
    state["campaign"]["name"] = "Before"
    monkeypatch.setenv("GOOGLE_ADS_MAX_CPC", "10")
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(out["draft_id"])
    assert exc.value.code == "CAP_EXCEEDED"
    assert out["draft_id"] in rails._DRAFTS
    assert not fake_client.dispatch_calls


@pytest.fixture
def portfolio(state, fake_client, monkeypatch):
    owner = "999"
    fake_client.strategy.update(type="TARGET_CPA", portfolio_resource_name=f"customers/{CID}/biddingStrategies/77",
                                owner_customer_id=owner, strategy_id="77")
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},{owner}")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", f"{CID},{owner}")
    monkeypatch.setenv("GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT", "true")
    scope = {"strategy": {"resource_name": "customers/999/biddingStrategies/77",
                           "type": "TARGET_CPA", "non_removed_campaign_count": "1",
                           "target_cpa": {"target_cpa_micros": "10000000"}},
             "attachments": [{"customer_id": CID, "id": "42", **state["campaign"]}]}
    monkeypatch.setattr(client, "portfolio_state", lambda *args: copy.deepcopy(scope))
    return scope


def test_portfolio_owner_and_preview(portfolio, fake_gads):
    out = tools.update_campaign("42", target_cpa=20)
    plan = draft_plan(out)
    assert plan.mutate_customer_id == "999"
    assert out["preview"]["affected_campaigns"] == portfolio["attachments"]
    op = client._build_mutate_operation(fake_gads, plan.operations[0]).bidding_strategy_operation
    assert op.update.resource_name == "customers/999/biddingStrategies/77"
    assert op.update.target_cpa.target_cpa_micros == 20000000


@pytest.mark.parametrize("change", ["count", "owner", "permission", "flag", "attachment", "scan"])
def test_portfolio_refusals_rechecked_at_apply(portfolio, fake_client, monkeypatch, change):
    out = tools.update_campaign("42", target_cpa=20)
    if change == "count":
        portfolio["strategy"]["non_removed_campaign_count"] = "2"
    elif change == "owner":
        portfolio["strategy"]["resource_name"] = f"customers/{CID}/biddingStrategies/77"
    elif change == "permission":
        monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "999")
    elif change == "flag":
        monkeypatch.setenv("GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT", "false")
    elif change == "attachment":
        portfolio["attachments"][0]["id"] = "43"
    else:
        def incomplete(*args):
            raise rails.RailViolation("truncated", code="SCAN_INCOMPLETE")
        monkeypatch.setattr(client, "portfolio_state", incomplete)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(out["draft_id"])
    assert not fake_client.dispatch_calls
    assert out["draft_id"] in rails._DRAFTS


@pytest.mark.parametrize("kwargs", [{"name": "After"}, {"daily_budget": 70}, {"status": "PAUSED"}])
def test_portfolio_cross_owner_compound_refused(portfolio, kwargs):
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", target_cpa=20, **kwargs)
    assert exc.value.code == "CROSS_CUSTOMER"


@pytest.mark.parametrize("strategy", ["MANUAL_CPM", "TARGET_CPA", "ENHANCED_CPC", "UNKNOWN"])
def test_ad_group_cpc_exact_strategy(state, fake_client, strategy):
    fake_client.strategy["type"] = strategy
    with pytest.raises(rails.RailViolation):
        tools.update_ad_group("7", cpc_bid=2)


@pytest.mark.parametrize("clear", [False, True])
def test_ad_group_cpa_verification_and_real_message(state, fake_client, fake_gads, clear, monkeypatch):
    fake_client.strategy["type"] = "MAXIMIZE_CONVERSIONS"
    kwargs = {"clear_target_cpa": True} if clear else {"target_cpa": 20}
    out = tools.update_ad_group("7", **kwargs)
    plan = draft_plan(out)
    op = client._build_mutate_operation(fake_gads, plan.operations[0]).ad_group_operation
    assert list(op.update_mask.paths) == ["target_cpa_micros"]
    assert op.update._pb.HasField("target_cpa_micros") is (not clear)
    def dispatch(plan):
        state["ad_group"]["effective_target_cpa_micros"] = "10000000" if clear else "20000000"
        state["ad_group"]["effective_target_cpa_source"] = "CAMPAIGN_BIDDING_STRATEGY" if clear else "AD_GROUP"
        return {"results": []}
    monkeypatch.setattr(client, "_dispatch", dispatch)
    assert rails.apply_draft(out["draft_id"])["verified"] is True


@pytest.mark.parametrize("failure", ["mismatch", "read_error"])
def test_post_write_failure_is_consumed_and_explicit(state, fake_client, monkeypatch, failure):
    out = tools.update_ad_group("7", cpc_bid=2)
    if failure == "read_error":
        def verify(checks):
            raise RuntimeError("read unavailable")
        monkeypatch.setattr(client, "verify_post_apply", verify)
    result = rails.apply_draft(out["draft_id"])
    assert result["applied"] is True and result["verified"] is False
    assert result["code"] == "POST_WRITE_VERIFICATION_FAILED"
    assert "do not retry" in result["next"]
    assert out["draft_id"] not in rails._DRAFTS
    assert len(fake_client.dispatch_calls) == 1
    with open(audit.AUDIT_PATH) as handle:
        events = [json.loads(line) for line in handle]
    assert events[-1]["phase"] == "apply_unverified"
    assert events[-1]["applied"] is True


def test_ad_group_cpc_verified_real_message(state, fake_client, fake_gads, monkeypatch):
    out = tools.update_ad_group("7", cpc_bid="2.25", status="PAUSED", name="After")
    op = client._build_mutate_operation(fake_gads, draft_plan(out).operations[0]).ad_group_operation
    assert op.update.cpc_bid_micros == 2250000
    assert list(op.update_mask.paths) == ["status", "name", "cpc_bid_micros"]
    def dispatch(plan):
        state["ad_group"]["effective_cpc_bid_micros"] = "2250000"
        return {}
    monkeypatch.setattr(client, "_dispatch", dispatch)
    assert rails.apply_draft(out["draft_id"])["verified"] is True


def test_post_checks_digest_tamper(state, fake_client):
    out = tools.update_ad_group("7", cpc_bid=2)
    draft_plan(out).post_checks[0]["expected"]["effective_cpc_bid_micros"] = "1"
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(out["draft_id"])
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize("service,mask", [("CampaignService", "bidding_strategy"),
                                           ("AdGroupService", "effective_cpc_bid_micros"),
                                           ("BiddingStrategyService", "name")])
def test_forbidden_masks(fake_gads, service, mask):
    op = rails.MutationOp(service, {"update": {"resource_name": "customers/1/campaigns/2"}}, [mask])
    with pytest.raises(rails.RailViolation) as exc:
        client._build_mutate_operation(fake_gads, op)
    assert exc.value.code == "BAD_MASK"


def test_update_state_actual_enum_and_amount_conversion(fake_gads):
    row = make_row(**{"ad_group.id": 7, "ad_group.resource_name": f"customers/{CID}/adGroups/7",
                      "ad_group.status": 2, "ad_group.effective_cpc_bid_micros": 2000000,
                      "ad_group.effective_target_cpa_source": 5})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    state = client.update_state(CID, "ad_group", "7")
    assert state["status"] == "ENABLED"
    assert state["effective_cpc_bid_micros"] == "2000000"
    assert isinstance(state["effective_target_cpa_source"], str)


def test_portfolio_scans_owner_identity_not_client_resource(fake_gads):
    strategy = make_row(**{"bidding_strategy.resource_name": "customers/999/biddingStrategies/77",
                           "bidding_strategy.type": 9,
                           "bidding_strategy.non_removed_campaign_count": 1})
    campaign = make_row(**{"campaign.resource_name": f"customers/{CID}/campaigns/42",
                           "campaign.id": 42, "campaign.status": 2,
                           "campaign.bidding_strategy": f"customers/{CID}/biddingStrategies/77",
                           "accessible_bidding_strategy.owner_customer_id": 999,
                           "accessible_bidding_strategy.id": 77})
    fake_gads.search_responses[("999", "")] = make_search_response([strategy])
    fake_gads.search_responses[(CID, "")] = make_search_response([campaign])
    scope = client.portfolio_state("999", "77", {CID})
    assert len(scope["attachments"]) == 1
    assert scope["attachments"][0]["customer_id"] == CID
    assert len(fake_gads.search_requests) == 2
    assert all(req.search_settings.return_total_results_count for req in fake_gads.search_requests)


@pytest.mark.parametrize("kwargs", [{}, {"name": ""}, {"status": "REMOVED"},
                                   {"cpc_bid": 0}, {"cpc_bid": False}, {"cpc_bid": 51},
                                   {"target_cpa": 2, "cpc_bid": 2},
                                   {"target_cpa": 2, "clear_target_cpa": True}])
def test_ad_group_invalid_requests(state, fake_client, kwargs):
    with pytest.raises(rails.RailViolation):
        tools.update_ad_group("7", **kwargs)
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize("strategy,kwargs", [("TARGET_CPA", {"clear_target_cpa": True}),
                                             ("TARGET_ROAS", {"clear_target_roas": True}),
                                             ("MANUAL_CPC", {"target_cpa": 2})])
def test_required_or_incompatible_target(state, fake_client, strategy, kwargs):
    fake_client.strategy["type"] = strategy
    with pytest.raises(rails.RailViolation):
        tools.update_campaign("42", **kwargs)


def test_ad_group_maximize_requires_campaign_target(state, fake_client):
    fake_client.strategy["type"] = "MAXIMIZE_CONVERSIONS"
    state["campaign"]["maximize_conversions"] = {}
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_ad_group("7", target_cpa=2)
    assert exc.value.code == "BID_STRATEGY"


def test_ad_group_portfolio_cpc_refused(state, fake_client):
    fake_client.strategy["portfolio_resource_name"] = f"customers/{CID}/biddingStrategies/77"
    with pytest.raises(rails.RailViolation):
        tools.update_ad_group("7", cpc_bid=2)


def test_ad_group_portfolio_cpa_is_local_override(state, fake_client, monkeypatch):
    fake_client.strategy.update(type="TARGET_CPA", portfolio_resource_name=f"customers/{CID}/biddingStrategies/77",
                                owner_customer_id="999", strategy_id="77")
    monkeypatch.setattr(client, "accessible_target_state", lambda *args: {
        "target_cpa": {"target_cpa_micros": "10000000"}})
    out = tools.update_ad_group("7", target_cpa=20)
    assert draft_plan(out).mutate_customer_id == CID
    assert draft_plan(out).operations[0].service == "AdGroupService"


def test_budget_removed_after_draft(fake_client):
    out = tools.update_campaign("42", 75)
    fake_client.budget_info["campaign_status"] = "REMOVED"
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(out["draft_id"])
    assert exc.value.code == "ENTITY_REMOVED"
    assert not fake_client.dispatch_calls


def test_portfolio_unwritable_owner_at_draft(portfolio, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", CID)
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", target_cpa=20)
    assert exc.value.code == "PORTFOLIO_SCOPE"


def test_portfolio_read_only_attached_campaign(portfolio, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},999,888")
    portfolio["strategy"]["non_removed_campaign_count"] = "2"
    portfolio["attachments"].append({"customer_id": "888", "id": "43", "status": "PAUSED",
                                      "resource_name": "customers/888/campaigns/43"})
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("42", target_cpa=20)
    assert exc.value.code == "PORTFOLIO_SCOPE"


def test_portfolio_same_count_name_drift(portfolio, fake_client):
    out = tools.update_campaign("42", target_cpa=20)
    portfolio["attachments"][0]["name"] = "Scope changed"
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(out["draft_id"])
    assert exc.value.code == "STATE_DRIFT"
    assert not fake_client.dispatch_calls


def test_portfolio_full_pagination_and_mismatch(fake_gads):
    strategy = make_row(**{"bidding_strategy.resource_name": "customers/999/biddingStrategies/77",
                           "bidding_strategy.type": 9,
                           "bidding_strategy.non_removed_campaign_count": 2})
    def campaign(eid):
        return make_row(**{"campaign.id": eid, "campaign.resource_name": f"customers/{CID}/campaigns/{eid}",
                            "campaign.status": 2, "accessible_bidding_strategy.id": 77,
                            "accessible_bidding_strategy.owner_customer_id": 999})
    fake_gads.search_responses[("999", "")] = make_search_response([strategy])
    fake_gads.search_responses[(CID, "")] = make_search_response([campaign(42)], "page2", total=2)
    fake_gads.search_responses[(CID, "page2")] = make_search_response([campaign(43)], total=2)
    scope = client.portfolio_state("999", "77", {CID})
    assert len(scope["attachments"]) == 2
    assert [r.page_token for r in fake_gads.search_requests] == ["", "", "page2"]
    fake_gads.search_responses[(CID, "page2")] = make_search_response([], total=2)
    with pytest.raises(rails.RailViolation) as exc:
        client.portfolio_state("999", "77", {CID})
    assert exc.value.code == "SCAN_INCOMPLETE"


def test_unmasked_value_refused(fake_gads):
    op = rails.MutationOp("CampaignService", {"update": {
        "resource_name": f"customers/{CID}/campaigns/42", "status": "PAUSED", "name": "Hidden"}},
        ["status"])
    with pytest.raises(rails.RailViolation) as exc:
        client._build_mutate_operation(fake_gads, op)
    assert exc.value.code == "BAD_MASK"


def test_accessible_portfolio_target_read_actual_message(fake_gads):
    row = make_row(**{"accessible_bidding_strategy.target_cpa.target_cpa_micros": 15000000})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    state = client.accessible_target_state(CID, "77")
    assert state["target_cpa"]["target_cpa_micros"] == "15000000"


def test_postcheck_success_from_actual_read(fake_gads):
    row = make_row(**{"ad_group.resource_name": f"customers/{CID}/adGroups/7", "ad_group.status": 2,
                      "ad_group.effective_target_cpa_micros": 12000000,
                      "ad_group.effective_target_cpa_source": 5})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    client.verify_post_apply([{"customer_id": CID, "entity_type": "ad_group", "entity_id": "7",
                               "expected": {"effective_target_cpa_micros": "12000000",
                                            "effective_target_cpa_source": "CAMPAIGN_BIDDING_STRATEGY"}}])


@pytest.mark.parametrize("tool_name,entity_id,kwargs", [
    ("update_campaign", "42", {"target_cpa": "0.0000001"}),
    ("update_ad_group", "7", {"target_cpa": "0.0000001"}),
    ("update_ad_group", "7", {"cpc_bid": "0.0000001"}),
    ("update_campaign", "42", {"daily_budget": "0.0000001", "name": "After"}),
    ("update_campaign", "42", {"daily_budget": "0.0000001"}),
])
def test_unrepresentable_money_is_audited_refusal(state, fake_client, tool_name, entity_id, kwargs):
    fake_client.strategy["type"] = "MANUAL_CPC" if "cpc_bid" in kwargs else "TARGET_CPA"
    drafts_before = set(rails._DRAFTS)
    with pytest.raises(rails.RailViolation) as exc:
        getattr(tools, tool_name)(entity_id, **kwargs)
    assert exc.value.code == "BAD_AMOUNT"
    assert "cannot be represented" in str(exc.value)
    assert set(rails._DRAFTS) == drafts_before
    assert not fake_client.dispatch_calls
    with open(audit.AUDIT_PATH) as handle:
        events = [json.loads(line) for line in handle]
    assert len(events) == 1
    assert events[0]["phase"] == "refused"
    assert events[0]["tool"] == tool_name
    assert events[0]["code"] == "BAD_AMOUNT"
