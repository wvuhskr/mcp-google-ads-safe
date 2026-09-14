"""Offline errors through the SDK's actual request handler, not just Python calls."""
import asyncio
import inspect
import json
from pathlib import Path

import pytest
from mcp import types

from mcp_google_ads_safe import app, audit, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.test_final_fixes import mapped_error

NAMES = set('''attach_shared_set add_to_shared_set create_shared_negative_set draft_demand_gen_ad create_demand_gen_campaign set_listing_group_filter remove_asset_group_asset add_asset_group_assets update_asset_group create_asset_group create_pmax_campaign create_portfolio_bidding_strategy get_keyword_forecasts discover_keywords dismiss_recommendation apply_recommendation remove_entity remove_extension create_structured_snippets create_callouts draft_sitelinks draft_responsive_search_ad draft_campaign create_ad_group list_extensions get_policy_issues get_conversion_actions list_recommendations
health_check run_gaql update_campaign update_ad_group pause_entity enable_entity
confirm_and_apply undo_change get_account_info get_campaign_performance get_ad_performance
get_keyword_performance get_search_terms get_geo_performance get_negative_keywords
search_geo_targets get_entities list_accounts draft_keywords remove_keywords
update_keyword_bid add_negative_keywords remove_negative_keywords exclude_geo_target
remove_geo_target set_campaign_schedule upload_image_asset upload_text_asset create_custom_audience add_audience_targeting create_conversion_action set_conversion_action_primary_status'''.split())


def boundary(server, name, arguments):
    async def call():
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        if hasattr(server, '_handle_call_tool'):  # SDK 2 actual wire-result handler
            return await server._handle_call_tool(None, params)
        handler = server._mcp_server.request_handlers[types.CallToolRequest]
        return (await handler(types.CallToolRequest(method='tools/call', params=params))).root
    return asyncio.run(call())


def error_text(result):
    assert result.model_dump(by_alias=True)['isError'] is True
    return '\n'.join(block.text for block in result.content)


def payload(result):
    text = error_text(result)
    return json.loads(text[text.index('{'):])


@pytest.mark.parametrize('name,args', [
    ('update_campaign', {'campaign_id': '1', 'daily_budget': 75}),
    ('confirm_and_apply', {'draft_id': 'missing'}),
])
def test_writes_disabled_boundary(monkeypatch, fake_client, name, args):
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    result = payload(boundary(app.mcp, name, args))
    assert result['code'] == 'WRITES_DISABLED'
    assert 'GOOGLE_ADS_ENABLE_WRITES' in result['reason']
    assert not fake_client.dispatch_calls


def test_cap_boundary_and_direct_exception(monkeypatch, fake_client):
    monkeypatch.setenv('GOOGLE_ADS_MAX_DAILY_BUDGET', '100')
    args = {'campaign_id': '1', 'daily_budget': 500}
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign(**args)
    result = payload(boundary(app.mcp, 'update_campaign', args))
    assert result == {'code': 'CAP_EXCEEDED', 'reason': str(exc.value)}
    assert not fake_client.dispatch_calls


@pytest.mark.parametrize('arguments', [
    {'name': 7, 'strategy_type': 'TARGET_CPA', 'target_cpa': '10'},
    {'name': 'Portfolio One', 'strategy_type': 7, 'target_cpa': '10'},
    {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA', 'target_cpa': '10',
     'customer_id': 1234567890},
    {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA', 'target_cpa': True},
    {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA', 'target_cpa': 'NaN'},
    {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA'},
    {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA', 'target_cpa': '10',
     'target_roas': '2'},
    {'name': 'Portfolio One', 'strategy_type': 'TARGET_ROAS', 'target_cpa': '10'},
])
def test_portfolio_invalid_actual_handler_refuses_without_dispatch(
        fake_client, arguments):
    response = boundary(app.mcp, 'create_portfolio_bidding_strategy', arguments)
    text = error_text(response)
    if '{' not in text:
        assert 'Error executing tool' in text and 'validation error' in text.lower()
    else:
        result = json.loads(text[text.index('{'):])
        assert result['code'] in {'RAIL_VIOLATION', 'BAD_AMOUNT', 'BAD_INPUT'}
    assert fake_client.dispatch_calls == []


def test_unknown_structured_details_and_unexpected_secret(monkeypatch):
    def fail():
        raise rails.UnknownWriteOutcome('known outcome', request_id='request-123',
                                       failure={'errors': [{'code': 'UNAVAILABLE'}]},
                                       cause=RuntimeError('secret-sentinel'))
    monkeypatch.setattr(client, 'preflight', fail)
    with pytest.raises(rails.UnknownWriteOutcome) as direct:
        tools.health_check()
    assert direct.value.request_id == 'request-123'
    result = payload(boundary(app.mcp, 'health_check', {}))
    assert result['code'] == 'UNKNOWN_WRITE_OUTCOME'
    assert result['request_id'] == 'request-123'
    assert result['failure'] == {'errors': [{'code': 'UNAVAILABLE'}]}
    assert 'verify account state before retrying' in result['reason']
    assert 'secret-sentinel' not in json.dumps(result)
    assert 'applied' not in result
    def crash():
        raise RuntimeError('secret-sentinel')
    monkeypatch.setattr(client, 'preflight', crash)
    text = error_text(boundary(app.mcp, 'health_check', {}))
    assert 'secret-sentinel' not in text
    assert 'Error executing tool' in text


def test_real_mapped_failure_consumes_draft_once_and_audits_unknown(fake_client):
    fake_client.dispatch_error = mapped_error('remapped')
    plan = rails.EntityMutationPlan(CID, [rails.MutationOp('CampaignBudgetService', {
        'update': {'resource_name': f'customers/{CID}/campaignBudgets/555',
                   'amount_micros': 75000000}}, ['amount_micros'])], True)
    draft = rails.create_draft('update_campaign', {}, plan, {})
    result = payload(boundary(app.mcp, 'confirm_and_apply', {'draft_id': draft['draft_id']}))
    assert result['code'] == 'UNKNOWN_WRITE_OUTCOME'
    assert 'verify account state before retrying' in result['reason']
    assert result['failure']['code'] == 503
    assert draft['draft_id'] not in rails._DRAFTS
    event = json.loads(Path(audit.AUDIT_PATH).read_text().splitlines()[-1])
    assert event['phase'] == 'unknown'
    assert event['failure']['code'] == result['failure']['code']
    assert event['failure']['details'] == result['failure']['details']
    assert result['failure']['errors'] == ['<non-JSON provider detail>']
    again = payload(boundary(app.mcp, 'confirm_and_apply', {'draft_id': draft['draft_id']}))
    assert again['code'] == 'UNKNOWN_DRAFT'
    assert len(fake_client.dispatch_calls) == 1
    # the refused re-apply is audited too (was silent before)
    tail = json.loads(Path(audit.AUDIT_PATH).read_text().splitlines()[-1])
    assert tail['phase'] == 'refused' and tail['code'] == 'UNKNOWN_DRAFT'


def test_exact_inventory_and_original_schemas():
    plain = app._Server('unwrapped-reference')
    for name in NAMES:
        plain.tool()(getattr(tools, name))
    wrapped_tools = asyncio.run(app.mcp.list_tools())
    reference = asyncio.run(plain.list_tools())
    assert {t.name for t in wrapped_tools} == NAMES
    assert len(wrapped_tools) == 60
    assert {t.name: t.model_dump() for t in wrapped_tools} == {
        t.name: t.model_dump() for t in reference}
    for name in NAMES:
        assert not hasattr(getattr(tools, name), '__wrapped__')


def test_async_registration_preserves_original():
    server = type(app.mcp)('async-reference')
    @server.tool()
    async def asynchronous(value: str = 'x') -> dict:
        raise rails.RailViolation(value)
    assert inspect.iscoroutinefunction(asynchronous)
    with pytest.raises(rails.RailViolation):
        asyncio.run(asynchronous())
    assert payload(boundary(server, 'asynchronous', {})) == {'code': 'RAIL_VIOLATION', 'reason': 'x'}


def test_successful_read_value_is_unchanged(fake_client):
    arguments = {'query': 'SELECT campaign.id FROM campaign'}
    result = boundary(app.mcp, 'run_gaql', arguments)
    assert not result.model_dump(by_alias=True)['isError']
    assert json.loads(result.content[0].text) == tools.run_gaql(**arguments)
