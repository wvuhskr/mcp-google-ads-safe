"""Offline smoke tests for the public MCP boundary, with no credentials or network."""
import asyncio
import json

from mcp_google_ads_safe import client, rails, tools  # noqa: F401 (registers MCP tools)
from mcp_google_ads_safe.app import mcp
from tests.conftest import TEST_CUSTOMER_ID as CID

EXPECTED_TOOLS = {
    "upload_image_asset", "upload_text_asset", "create_custom_audience", "add_audience_targeting", "create_conversion_action", "set_conversion_action_primary_status", "create_portfolio_bidding_strategy", "apply_recommendation", "dismiss_recommendation",
    "attach_shared_set", "add_to_shared_set", "create_shared_negative_set", "draft_demand_gen_ad", "create_demand_gen_campaign", "set_listing_group_filter", "remove_asset_group_asset", "add_asset_group_assets", "update_asset_group", "create_asset_group", "create_pmax_campaign", "draft_campaign", "create_ad_group", "draft_responsive_search_ad", "draft_sitelinks", "create_callouts", "create_structured_snippets", "remove_extension", "remove_entity",
    "list_extensions", "get_policy_issues", "get_conversion_actions", "list_recommendations",
    "discover_keywords", "get_keyword_forecasts", "health_check",
    "run_gaql",
    "update_campaign",
    "update_ad_group",
    "pause_entity",
    "enable_entity",
    "confirm_and_apply", "undo_change",
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
    "draft_keywords",
    "remove_keywords",
    "update_keyword_bid",
    "add_negative_keywords",
    "remove_negative_keywords",
    "exclude_geo_target",
    "remove_geo_target",
    "set_campaign_schedule",
}


def test_all_59_tools_publish_serializable_input_schemas():
    async def check():
        inventory = await mcp.list_tools()
        assert len(inventory) == 60
        assert {tool.name for tool in inventory} == EXPECTED_TOOLS
        for tool in inventory:
            json.dumps(tool.input_schema)

    asyncio.run(check())


def test_remove_extension_inventory_publishes_complete_contract():
    async def check():
        inventory = {tool.name: tool for tool in await mcp.list_tools()}
        tool = inventory['remove_extension']
        assert set(tool.input_schema['properties']) == {
            'asset_id', 'extension_type', 'campaign_id', 'ad_group_id', 'customer_id'}
        for required in ('SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET', 'positive numeric',
                         'exactly one', 'Confirmation', 'underlying asset', 'other connection',
                         'higher-level', 'list_extensions', 'run_gaql'):
            assert required in tool.description

    asyncio.run(check())


def test_remove_entity_inventory_publishes_complete_contract():
    async def check():
        inventory = {tool.name: tool for tool in await mcp.list_tools()}
        tool = inventory['remove_entity']
        assert set(tool.input_schema['properties']) == {
            'entity_type', 'entity_id', 'customer_id', 'ad_group_id'}
        for required in ('PAUSED', 'standard Search', 'responsive search ad',
                         'positive numeric', 'ad_group_id', 'Confirmation', 'permanent',
                         'budgets', 'assets', 'history', 'cannot be undone'):
            assert required in tool.description

    asyncio.run(check())


def test_structured_snippet_inventory_publishes_complete_nested_contract():
    async def check():
        inventory = {tool.name: tool for tool in await mcp.list_tools()}
        tool = inventory['create_structured_snippets']
        item = tool.input_schema['properties']['snippets']['items']
        assert item['type'] == 'object'
        for required in ('1..10', 'header', 'values', '3..10', '1..25', 'double-width',
                         'campaign', 'ad group', 'confirmation', 'PAUSED', 'Services',
                         'Featured hotels', 'Models'):
            assert required in tool.description

    asyncio.run(check())


def test_sitelink_inventory_publishes_nested_item_contract():
    async def check():
        inventory = {tool.name: tool for tool in await mcp.list_tools()}
        description = inventory['draft_sitelinks'].description
        for required in ('link_text', 'final_url', 'description1', 'description2',
                         '1..25', '1..35', 'Double-width', 'HTTP(S)', 'together'):
            assert required in description

    asyncio.run(check())


def test_callout_inventory_publishes_complete_contract():
    async def check():
        inventory = {tool.name: tool for tool in await mcp.list_tools()}
        tool = inventory['create_callouts']
        assert tool.input_schema['properties']['callouts']['items']['type'] == 'string'
        for required in ('Draft', '1..10', '1..25', 'Double-width', 'local cap',
                         'campaign', 'ad group', 'PAUSED', 'confirmation'):
            assert required in tool.description

    asyncio.run(check())


def test_representative_read_and_write_flow_crosses_mcp_boundary(
    fake_client, monkeypatch
):
    monkeypatch.setattr(
        client,
        "gaql",
        lambda query, customer_id, page_token=None: {
            "results": [{"campaign": {"id": "11"}}],
            "next_page_token": None,
            "total_results_count": 1,
        },
    )

    async def check():
        read_result = await mcp.call_tool(
            "run_gaql",
            {"query": "SELECT campaign.id FROM campaign", "customer_id": CID},
        )
        assert "campaign" in str(read_result)

        drafts_before = set(rails._DRAFTS)
        draft_result = await mcp.call_tool(
            "update_campaign",
            {"campaign_id": "77", "daily_budget": "75", "customer_id": CID},
        )
        (draft_id,) = set(rails._DRAFTS) - drafts_before
        draft_payload = json.loads(draft_result.content[0].text)
        assert draft_payload["draft_id"] == draft_id
        assert draft_payload["dry_run"] is True
        assert not fake_client.dispatch_calls

        apply_result = await mcp.call_tool(
            "confirm_and_apply", {"draft_id": draft_id}
        )
        apply_payload = json.loads(apply_result.content[0].text)
        assert apply_payload["applied"] is True
        assert apply_payload["draft_id"] == draft_id
        assert len(fake_client.dispatch_calls) == 1

    asyncio.run(check())
