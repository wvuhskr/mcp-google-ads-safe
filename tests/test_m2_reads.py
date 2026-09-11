"""Offline M2 read contracts; synthetic rows, no Google credentials."""
import asyncio
import json
from enum import IntEnum
from unittest.mock import Mock

import pytest

from mcp_google_ads_safe import client, rails, tools
from mcp_google_ads_safe.app import mcp
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_row, make_search_response

NAMES = ("list_extensions", "get_policy_issues", "get_conversion_actions", "list_recommendations")


@pytest.mark.parametrize("name", [name for name in NAMES if name != "get_policy_issues"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("total", [None, "missing", 19])
def test_wrapper_preserves_page_and_unknown_totals(monkeypatch, name, explicit, total):
    cid = "9876543210" if explicit else CID
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},9876543210")
    envelope = {"rows": [{"opaque": "preserve"}], "next_page_token": "next",
                "pages_complete": False, "returned_count": 1, "query_limited": False}
    if total != "missing":
        envelope["total_results_count"] = total
    provider = Mock(return_value=envelope)
    monkeypatch.setattr(client, name, provider)
    kwargs = {"customer_id": cid} if explicit else {}
    assert getattr(tools, name)(page_token="exact-token", **kwargs) is envelope
    provider.assert_called_once_with(cid, "exact-token")


@pytest.mark.parametrize("name", [name for name in NAMES if name != "get_policy_issues"])
def test_read_gate_precedes_provider(monkeypatch, name):
    provider = Mock()
    monkeypatch.setattr(client, name, provider)
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)(customer_id="9999999999")
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID")
    with pytest.raises(rails.RailViolation):
        getattr(tools, name)()
    provider.assert_not_called()


@pytest.mark.parametrize("name", [name for name in NAMES if name != "get_policy_issues"])
def test_provider_failures_propagate(fake_gads, monkeypatch, name):
    error = RuntimeError("synthetic Google search failure")
    monkeypatch.setattr(fake_gads._svc, "search", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        getattr(tools, name)()
    assert caught.value is error
    assert not fake_gads.mutate_calls


CASES = {
    "list_extensions": (
        "campaign_asset",
        "campaign_asset.resource_name campaign_asset.campaign campaign_asset.asset campaign_asset.field_type campaign_asset.status asset.resource_name asset.name asset.type asset.sitelink_asset.link_text asset.sitelink_asset.description1 asset.sitelink_asset.description2 asset.callout_asset.callout_text asset.structured_snippet_asset.header asset.structured_snippet_asset.values".split(),
        ["campaign_asset.status != 'REMOVED'"],
    ),
    "get_conversion_actions": (
        "conversion_action",
        "conversion_action.resource_name conversion_action.owner_customer conversion_action.id conversion_action.name conversion_action.type conversion_action.status conversion_action.category conversion_action.counting_type conversion_action.primary_for_goal conversion_action.value_settings.default_value conversion_action.click_through_lookback_window_days conversion_action.view_through_lookback_window_days conversion_action.attribution_model_settings.attribution_model".split(),
        ["conversion_action.status != 'REMOVED'"],
    ),
    "list_recommendations": (
        "recommendation",
        "recommendation.resource_name recommendation.type recommendation.impact recommendation.campaign recommendation.ad_group recommendation.dismissed".split(),
        ["recommendation.dismissed = FALSE"],
    ),
}


@pytest.mark.parametrize("name", [name for name in NAMES if name != "get_policy_issues"])
def test_canned_query_semantics_and_single_delegation(monkeypatch, name):
    envelope = {"rows": [], "next_page_token": "more", "total_results_count": None}
    gaql = Mock(return_value=envelope)
    monkeypatch.setattr(client, "gaql", gaql)
    assert getattr(client, name)(CID, "opaque-token") is envelope
    gaql.assert_called_once()
    query, cid, token = gaql.call_args.args
    assert (cid, token) == (CID, "opaque-token")
    resource, fields, filters = CASES[name]
    selected, rest = query.removeprefix("SELECT ").split(" FROM ")
    assert set(selected.split(", ")) == set(fields)
    assert rest.split(" WHERE ")[0] == resource
    assert set(rest.split(" WHERE ")[1].split(" AND ")) == set(filters)
    assert "LIMIT" not in query.upper()
    assert "metrics." not in query and "segments.date" not in query


ROWS = {
    "list_extensions": {
        "campaign_asset.resource_name": f"customers/{CID}/campaignAssets/1~2~3",
        "campaign_asset.status": "ENABLED",
        "asset.structured_snippet_asset.header": "Services",
        "asset.structured_snippet_asset.values": ["Heating", "Cooling"],
    },
    "get_policy_issues": {
        "ad_group_ad.resource_name": f"customers/{CID}/adGroupAds/1~2",
        "ad_group_ad.policy_summary.approval_status": "DISAPPROVED",
        "ad_group_ad.policy_summary.review_status": "REVIEWED",
        "ad_group_ad.policy_summary.policy_topic_entries": [{"topic": "SYNTHETIC", "type_": "PROHIBITED"}],
    },
    "get_conversion_actions": {
        "conversion_action.resource_name": f"customers/{CID}/conversionActions/7",
        "conversion_action.owner_customer": "customers/9876543210",
        "conversion_action.primary_for_goal": False,
        "conversion_action.value_settings.default_value": 12.5,
        "conversion_action.click_through_lookback_window_days": 30,
        "conversion_action.view_through_lookback_window_days": 1,
        "conversion_action.attribution_model_settings.attribution_model": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
    },
    "list_recommendations": {
        "recommendation.resource_name": f"customers/{CID}/recommendations/7",
        "recommendation.type_": "CAMPAIGN_BUDGET",
        "recommendation.dismissed": False,
        "recommendation.impact.base_metrics.clicks": 10.0,
        "recommendation.impact.potential_metrics.clicks": 12.0,
    },
}


@pytest.mark.parametrize("name", NAMES)
def test_real_v25_nested_serialization_and_continuation(fake_gads, monkeypatch, name):
    monkeypatch.setenv("GOOGLE_ADS_MAX_PAGES", "1")
    row = make_row(**ROWS[name])
    fake_gads.search_responses[(CID, "")] = make_search_response([row], next_token="raw-next", total=2)
    result = getattr(tools, name)()
    assert result["next_page_token"]
    assert result["total_results_count"] == 2
    assert len(result["rows"]) == 1
    actual = result["rows"][0]
    for path, value in ROWS[name].items():
        item = actual
        for part in path.split("."):
            item = item[part]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            assert item[0]["topic"] == "SYNTHETIC"
            assert item[0]["type_"] == 2  # v25 PolicyTopicEntryType.PROHIBITED
        else:
            proto_value = row
            for part in path.split("."):
                proto_value = getattr(proto_value, part)
            if isinstance(proto_value, IntEnum):
                assert item == int(proto_value)
            else:
                assert item == value or item == str(value)
    fake_gads.search_responses[(CID, "raw-next")] = make_search_response([row], total=2)
    final = getattr(tools, name)(page_token=result["next_page_token"])
    assert final["next_page_token"] is None
    assert [request.page_token for request in fake_gads.search_requests] == ["", "raw-next"]
    assert not fake_gads.mutate_calls and not fake_gads.reco_calls


def test_new_mcp_schemas_portable():
    async def check():
        inventory = {tool.name: tool for tool in await mcp.list_tools()}
        for name in NAMES:
            tool = inventory[name]
            schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
            json.dumps(schema)
            assert set(schema["properties"]) == {"customer_id", "page_token"}
            assert not schema.get("required")
            for field in schema["properties"].values():
                assert {v["type"] for v in field["anyOf"]} == {"string", "null"}
                assert field["default"] is None
    asyncio.run(check())
