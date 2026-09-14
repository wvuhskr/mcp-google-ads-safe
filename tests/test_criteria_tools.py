import copy

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID

CA = f"customers/{CID}/campaigns/11"
AG = f"customers/{CID}/adGroups/22"


def row(kind="KEYWORD", negative=False, group=True):
    parent = AG if group else CA
    return {
        "resource_name": f"customers/{CID}/"
        + ("adGroupCriteria/22" if group else "campaignCriteria/11")
        + "~33",
        "ad_group" if group else "campaign": parent,
        "criterion_id": "33",
        "status": "ENABLED",
        "type": kind,
        "negative": negative,
        "keyword": {"text": "repair", "match_type": "EXACT"},
        "location": {"geo_target_constant": "geoTargetConstants/44"},
        "ad_schedule": {
            "day_of_week": "MONDAY",
            "start_hour": 9,
            "start_minute": "ZERO",
            "end_hour": 17,
            "end_minute": "ZERO",
        },
        "cpc_bid_micros": "1000000",
        "effective_cpc_bid_micros": "1000000",
    }


@pytest.fixture
def state(monkeypatch):
    state = {
        "rows": [],
        "parent": {"resource_name": AG, "campaign": CA, "status": "ENABLED"},
        "time_zone": "America/New_York",
    }
    monkeypatch.setattr(client, "criteria_state", lambda *a: copy.deepcopy(state))
    monkeypatch.setattr(
        client,
        "geo_constant_state",
        lambda cid, rn: {"resource_name": rn, "status": "ENABLED"},
    )
    return state


CASES = [
    (
        "draft_keywords",
        dict(
            ad_group_id="22", keywords=[{"text": "new repair", "match_type": "EXACT"}]
        ),
        None,
    ),
    ("remove_keywords", dict(ad_group_id="22", criterion_ids=["33"]), row()),
    (
        "update_keyword_bid",
        dict(ad_group_id="22", criterion_id="33", new_bid="2"),
        row(),
    ),
    (
        "add_negative_keywords",
        dict(campaign_id="11", keywords=["new repair"], match_type="PHRASE"),
        None,
    ),
    (
        "remove_negative_keywords",
        dict(campaign_id="11", criterion_ids=["33"]),
        row(negative=True, group=False),
    ),
    ("exclude_geo_target", dict(campaign_id="11", geo_target_id="55"), None),
    (
        "remove_geo_target",
        dict(campaign_id="11", geo_target_id="44"),
        row("LOCATION", group=False),
    ),
    (
        "set_campaign_schedule",
        dict(
            campaign_id="11",
            schedules=[
                {
                    "day_of_week": "MONDAY",
                    "start_hour": 9,
                    "start_minute": 0,
                    "end_hour": 17,
                    "end_minute": 0,
                }
            ],
        ),
        row("AD_SCHEDULE", group=False),
    ),
]


@pytest.mark.parametrize("name,args,existing", CASES)
def test_draft_apply_and_drift(fake_client, state, monkeypatch, name, args, existing):
    state["parent"]["resource_name"] = AG if "ad_group_id" in args else CA
    state["rows"] = [existing] if existing else []
    monkeypatch.setattr(client, "verify_post_apply", lambda checks: None)
    draft = getattr(tools, name)(customer_id=CID, **args)
    assert not fake_client.dispatch_calls
    state["parent"]["status"] = "PAUSED"
    with pytest.raises(rails.RailViolation):
        tools.confirm_and_apply(draft["draft_id"])
    assert not fake_client.dispatch_calls
    state["parent"]["status"] = "ENABLED"
    assert tools.confirm_and_apply(draft["draft_id"])["applied"]


@pytest.mark.parametrize("name,args,existing", CASES)
def test_real_v25_draft_apply_refusal_drift(
    fake_gads, monkeypatch, name, args, existing
):
    from tests.conftest import (
        _FakeSearchPager,
        make_row,
        make_search_response,
        make_type,
    )

    snapshot = {"status": "ENABLED"}

    def search(request):
        fake_gads.search_requests.append(request)
        q = request.query
        if "FROM ad_group_criterion" in q or "FROM campaign_criterion" in q:
            ent = (
                "ad_group_criterion"
                if "FROM ad_group_criterion" in q
                else "campaign_criterion"
            )
            data = copy.deepcopy(existing) if existing else None
            if data:
                # Only fields belonging to the real criterion message.
                if ent == "ad_group_criterion":
                    data.pop("location")
                    data.pop("ad_schedule")
                else:
                    data.pop("cpc_bid_micros")
                    data.pop("effective_cpc_bid_micros")
                    if data["type"] != "KEYWORD":
                        data.pop("keyword")
                    if data["type"] != "LOCATION":
                        data.pop("location")
                    if data["type"] != "AD_SCHEDULE":
                        data.pop("ad_schedule")
                if snapshot.get("applied") and name == "update_keyword_bid":
                    data["effective_cpc_bid_micros"] = "2000000"
                rows = [make_row(**{ent: data})]
            else:
                rows = []
        elif "FROM geo_target_constant" in q:
            rows = [
                make_row(
                    geo_target_constant={
                        "resource_name": "geoTargetConstants/55",
                        "status": "ENABLED",
                    }
                )
            ]
        elif "FROM customer" in q:
            rows = [make_row(customer={"time_zone": "America/New_York"})]
        elif "FROM ad_group" in q:
            rows = [
                make_row(
                    ad_group={
                        "resource_name": AG,
                        "campaign": CA,
                        "status": snapshot["status"],
                    }
                )
            ]
        elif "bidding_strategy_type" in q:
            rows = [
                make_row(
                    campaign={
                        "resource_name": CA,
                        "bidding_strategy_type": "MANUAL_CPC",
                    }
                )
            ]
        else:
            rows = [
                make_row(campaign={"resource_name": CA, "status": snapshot["status"]})
            ]
        return _FakeSearchPager(make_search_response(rows, total=len(rows)))

    monkeypatch.setattr(fake_gads._svc, "search", search)
    fake_gads.mutate_response = make_type("MutateGoogleAdsResponse")
    original = fake_gads._svc.mutate

    def mutate(request=None):
        snapshot["applied"] = True
        return original(request=request)

    monkeypatch.setattr(fake_gads._svc, "mutate", mutate)
    draft = getattr(tools, name)(customer_id=CID, **args)
    snapshot["status"] = "PAUSED"
    with pytest.raises(rails.RailViolation):
        tools.confirm_and_apply(draft["draft_id"])
    assert not fake_gads.mutate_calls
    snapshot["status"] = "REMOVED"
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)(customer_id=CID, **args)
    assert not fake_gads.mutate_calls
    snapshot["status"] = "ENABLED"
    result = tools.confirm_and_apply(draft["draft_id"])
    assert result["applied"]
    if name == "update_keyword_bid":
        assert result["verified"]
    (call,) = fake_gads.mutate_calls
    assert call["partial_failure"] is False and call["validate_only"] is False
    op = call["operations"][-1]
    if name == "draft_keywords":
        assert op.ad_group_criterion_operation.create.status.name == "PAUSED"
        assert op.ad_group_criterion_operation.create.keyword.text == "new repair"
    elif name == "update_keyword_bid":
        assert op.ad_group_criterion_operation.update.cpc_bid_micros == 2000000
        assert list(op.ad_group_criterion_operation.update_mask.paths) == [
            "cpc_bid_micros"
        ]
    elif name == "add_negative_keywords":
        assert op.campaign_criterion_operation.create.negative
    elif name == "exclude_geo_target":
        assert op.campaign_criterion_operation.create.location.geo_target_constant.endswith(
            "/55"
        )
        assert op.campaign_criterion_operation.create.negative
    elif name == "set_campaign_schedule":
        assert len(call["operations"]) == 2
        assert call["operations"][0].campaign_criterion_operation.remove.endswith(
            "11~33"
        )
        assert (
            op.campaign_criterion_operation.create.ad_schedule.start_minute.name
            == "ZERO"
        )
    else:
        removed = (
            op.ad_group_criterion_operation.remove
            if "ad_group_id" in args
            else op.campaign_criterion_operation.remove
        )
        assert removed.endswith("~33")
    assert all("LIMIT" not in request.query for request in fake_gads.search_requests)


@pytest.mark.parametrize("name,args,existing", CASES)
def test_refuses_before_read_when_disabled(
    fake_client, state, monkeypatch, name, args, existing
):
    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "false")
    monkeypatch.setattr(
        client, "criteria_state", lambda *a: pytest.fail("read before write gate")
    )
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)(customer_id=CID, **args)
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize(
    "strategy,code",
    [
        ("MAXIMIZE_CONVERSIONS", "BID_SMART"),
        ("TARGET_SPEND", "BID_POLICY"),
        ("FUTURE", "BID_UNRECOGNIZED"),
        ("", "BID_UNREADABLE"),
    ],
)
@pytest.mark.parametrize("group", [False, True])
def test_cpc_classification(fake_client, state, monkeypatch, strategy, code, group):
    state["rows"] = [row()]
    fake_client.strategy["type"] = strategy
    monkeypatch.setattr(
        client, "update_state", lambda *a: copy.deepcopy(state["parent"])
    )
    with pytest.raises(rails.RailViolation) as exc:
        if group:
            tools.update_ad_group("22", customer_id=CID, cpc_bid="2")
        else:
            tools.update_keyword_bid("22", "33", "2", customer_id=CID)
    assert exc.value.code == code
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize("name,args,existing", CASES)
def test_parent_confusion(fake_client, state, name, args, existing):
    state["parent"]["resource_name"] = f"customers/{CID}/campaigns/999"
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)(customer_id=CID, **args)
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize("name,args,existing", [CASES[1], CASES[2], CASES[4], CASES[6]])
def test_type_and_polarity_confusion(fake_client, state, name, args, existing):
    state["parent"]["resource_name"] = AG if "ad_group_id" in args else CA
    state["rows"] = [copy.deepcopy(existing)]
    state["rows"][0]["negative"] = not existing["negative"]
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)(customer_id=CID, **args)
    state["rows"][0]["negative"] = existing["negative"]
    state["rows"][0]["type"] = "AGE_RANGE"
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)(customer_id=CID, **args)
    assert not fake_client.dispatch_calls


def test_schedule_validation_and_duplicate_ids(fake_client, state):
    state["parent"]["resource_name"] = CA
    window = CASES[-1][1]["schedules"][0]
    for values in [
        [],
        [window, window],
        [dict(window, end_hour=8)],
        [dict(window, start_hour=True)],
        [dict(window, end_hour=24, end_minute=15)],
    ]:
        with pytest.raises(rails.RailViolation):
            tools.set_campaign_schedule("11", values, customer_id=CID)
    with pytest.raises(rails.RailViolation):
        tools.remove_negative_keywords("11", ["33", "33"], customer_id=CID)
    assert not fake_client.dispatch_calls


def test_create_duplicates_status_and_blocked_terms(fake_client, state, monkeypatch):
    state["rows"] = [row()]
    for values in [
        [{"text": "repair", "match_type": "EXACT"}],
        [{"text": "x", "match_type": "EXACT", "status": "ENABLED"}],
        [{"text": "x", "match_type": "EXACT"}] * 2,
    ]:
        with pytest.raises(rails.RailViolation):
            tools.draft_keywords("22", values, customer_id=CID)
    draft = tools.draft_keywords(
        "22", [{"text": "new repair", "match_type": "EXACT"}], customer_id=CID
    )
    monkeypatch.setattr(rails.settings, "blocked_terms", lambda: ("repair",))
    with pytest.raises(rails.RailViolation):
        tools.confirm_and_apply(draft["draft_id"])
    assert not fake_client.dispatch_calls


def test_registered_inventory_and_call(fake_client, monkeypatch):
    import asyncio
    import json

    from mcp_google_ads_safe.app import mcp

    expected = {
            "attach_shared_set", "add_to_shared_set", "create_shared_negative_set", "draft_demand_gen_ad", "create_demand_gen_campaign", "set_listing_group_filter", "remove_asset_group_asset", "add_asset_group_assets", "update_asset_group", "create_asset_group", "create_pmax_campaign", "draft_campaign", "create_ad_group", "draft_responsive_search_ad", "draft_sitelinks", "create_callouts", "create_structured_snippets", "remove_extension", "upload_image_asset", "upload_text_asset", "create_custom_audience", "add_audience_targeting", "create_conversion_action", "set_conversion_action_primary_status", "create_portfolio_bidding_strategy", "apply_recommendation", "dismiss_recommendation",
        "list_extensions", "get_policy_issues", "get_conversion_actions", "list_recommendations",
        "discover_keywords", "get_keyword_forecasts", "health_check",
        "run_gaql", "undo_change",
        "update_campaign",
        "update_ad_group",
        "pause_entity",
        "remove_entity",
        "enable_entity",
        "confirm_and_apply",
        "get_account_info",
        "get_campaign_performance",
        "get_ad_performance",
        "get_keyword_performance",
        "get_search_terms",
        "get_geo_performance",
        "get_negative_keywords",
        "search_geo_targets",
        "get_entities",
        "list_accounts",
    } | {v[0] for v in CASES}

    async def check():
        inventory = await mcp.list_tools()
        assert {t.name for t in inventory} == expected
        assert len(inventory) == 60
        for tool in inventory:
            json.dumps(tool.input_schema)
        monkeypatch.setattr(client, "list_accounts", lambda: [{"customer_id": CID}])
        result = await mcp.call_tool("list_accounts", {})
        assert CID in str(result)

    asyncio.run(check())


@pytest.mark.parametrize("post_state", ["different_bid", "missing_keyword"])
def test_keyword_postcheck_failure_consumes_draft_and_blocks_redispatch(
    fake_client, state, monkeypatch, post_state
):
    """A landed keyword write with an inconclusive readback is never safe to retry."""
    state["rows"] = [row()]
    draft = tools.update_keyword_bid("22", "33", "2", customer_id=CID)

    def dispatch(plan):
        fake_client.dispatch_calls.append(plan)
        if post_state == "different_bid":
            state["rows"][0]["effective_cpc_bid_micros"] = "1500000"
        else:
            state["rows"] = []
        return {"results": []}

    monkeypatch.setattr(client, "_dispatch", dispatch)
    result = tools.confirm_and_apply(draft["draft_id"])

    assert result["applied"] is True
    assert result["verified"] is False
    assert result["code"] == "POST_WRITE_VERIFICATION_FAILED"
    assert draft["draft_id"] not in rails._DRAFTS
    assert len(fake_client.dispatch_calls) == 1
    with pytest.raises(rails.RailViolation, match="unknown or already-applied"):
        tools.confirm_and_apply(draft["draft_id"])
    assert len(fake_client.dispatch_calls) == 1


def test_list_accounts_filters(fake_gads, monkeypatch):
    from tests.conftest import make_row, make_search_response

    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", CID)
    fake_gads.search_responses[(CID, "")] = make_search_response(
        [
            make_row(
                customer={
                    "id": int(CID),
                    "descriptive_name": "Test only",
                    "currency_code": "USD",
                }
            )
        ],
        total=1,
    )
    assert tools.list_accounts() == [
        {"customer_id": CID, "descriptive_name": "Test only", "currency_code": "USD"}
    ]
    assert {r.customer_id for r in fake_gads.search_requests} == {CID}


def test_adgroup_negative_create_and_explicit_status_refused(fake_gads):
    from tests.conftest import make_type

    fields = {
        "ad_group": AG,
        "negative": True,
        "keyword": {"text": "jobs", "match_type": "EXACT"},
    }
    fake_gads.mutate_response = make_type("MutateGoogleAdsResponse")
    client._dispatch(
        rails.EntityMutationPlan(
            CID, [rails.safe_create_operation("AdGroupCriterionService", fields)], True
        )
    )
    created = fake_gads.mutate_calls[0]["operations"][
        0
    ].ad_group_criterion_operation.create
    assert created.negative and created.status.name == "UNSPECIFIED"
    fields["status"] = "PAUSED"
    with pytest.raises(ValueError):
        client._dispatch(
            rails.EntityMutationPlan(
                CID,
                [rails.safe_create_operation("AdGroupCriterionService", fields)],
                True,
            )
        )
    assert len(fake_gads.mutate_calls) == 1


@pytest.mark.parametrize("name,args,existing", CASES)
def test_incomplete_scan_never_dispatches(fake_gads, monkeypatch, name, args, existing):
    from tests.conftest import make_row, make_search_response

    group = "ad_group_id" in args
    parent = (
        {"resource_name": AG, "campaign": CA, "status": "ENABLED"}
        if group
        else {"resource_name": CA, "status": "ENABLED"}
    )
    fake_gads.search_responses[(CID, "")] = make_search_response(
        [make_row(**{"ad_group" if group else "campaign": parent})], total=2
    )
    with pytest.raises(ValueError):
        getattr(tools, name)(customer_id=CID, **args)
    assert not fake_gads.mutate_calls


@pytest.mark.parametrize("name,args,existing", CASES[:5])
def test_keyword_policy_rechecked_for_every_action(
    fake_client, state, monkeypatch, name, args, existing
):
    state["parent"]["resource_name"] = AG if "ad_group_id" in args else CA
    state["rows"] = [existing] if existing else []
    draft = getattr(tools, name)(customer_id=CID, **args)
    monkeypatch.setattr(rails.settings, "blocked_terms", lambda: ("repair",))
    with pytest.raises(rails.RailViolation):
        tools.confirm_and_apply(draft["draft_id"])
    assert not fake_client.dispatch_calls


def test_schedule_six_windows_and_boundary(fake_client, state):
    state["parent"]["resource_name"] = CA
    windows = [
        dict(
            day_of_week="MONDAY",
            start_hour=i,
            start_minute=0,
            end_hour=i + 1,
            end_minute=0,
        )
        for i in range(7)
    ]
    tools.set_campaign_schedule("11", windows[:6], customer_id=CID)
    with pytest.raises(rails.RailViolation):
        tools.set_campaign_schedule("11", windows, customer_id=CID)
    tools.set_campaign_schedule(
        "11", [dict(windows[0], start_hour=23, end_hour=24)], customer_id=CID
    )


@pytest.mark.parametrize("negative", [False, True])
def test_geo_collisions(fake_client, state, negative):
    state["parent"]["resource_name"] = CA
    state["rows"] = [row("LOCATION", negative=negative, group=False)]
    with pytest.raises(rails.RailViolation):
        tools.exclude_geo_target("11", "geoTargetConstants/44", customer_id=CID)
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize("strategy", ["MANUAL_CPM", "MANUAL_CPV"])
def test_non_cpc_manual_refused(fake_client, state, strategy):
    state["rows"] = [row()]
    fake_client.strategy["type"] = strategy
    with pytest.raises(rails.RailViolation):
        tools.update_keyword_bid("22", "33", "2", customer_id=CID)
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize("bad", ["22 OR 1=1", True, 22.5, "２２", "0", "22~33"])
def test_strict_ids_before_read(fake_client, state, monkeypatch, bad):
    monkeypatch.setattr(
        client, "criteria_state", lambda *a: pytest.fail("invalid id read")
    )
    with pytest.raises(rails.RailViolation):
        tools.remove_keywords(bad, ["33"], customer_id=CID)
    assert not fake_client.dispatch_calls
