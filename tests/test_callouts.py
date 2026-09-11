"""Offline callout creation coverage using real installed Google Ads v25 messages."""
import copy

import pytest

from mcp_google_ads_safe import client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

CAMPAIGN = f'customers/{CID}/campaigns/88'
GROUP = f'customers/{CID}/adGroups/77'


@pytest.fixture
def callouts(monkeypatch, fake_gads):
    data = {
        'campaign': {'resource_name': CAMPAIGN, 'name': 'Search', 'status': 'ENABLED',
                     'advertising_channel_type': 'SEARCH',
                     'advertising_channel_sub_type': 'UNSPECIFIED',
                     'campaign_budget': f'customers/{CID}/campaignBudgets/5'},
        'ad_group': {'resource_name': GROUP, 'status': 'PAUSED', 'type_': 'SEARCH_STANDARD',
                     'campaign': CAMPAIGN},
        'links': [], 'saved': {}, 'queries': [],
    }

    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        data['queries'].append(query)
        if "resource_name = 'customers/" in query:
            entity = query.split(' FROM ')[1].split()[0]
            rn = query.split("resource_name = '")[1].split("'")[0]
            return [{entity: copy.deepcopy(data['saved'][rn])}] if rn in data['saved'] else []
        if 'FROM campaign_asset' in query or 'FROM ad_group_asset' in query:
            return copy.deepcopy(data['links'])
        if 'FROM ad_group ' in query:
            return [{'ad_group': copy.deepcopy(data['ad_group'])}]
        if 'FROM campaign ' in query:
            return [{'campaign': copy.deepcopy(data['campaign'])}]
        raise AssertionError(query)

    monkeypatch.setattr(client, 'gaql_all', read)
    monkeypatch.setattr(client, 'account_info', lambda cid: {
        'rows': [{'customer': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'}}],
        'pages_complete': True, 'returned_count': 1, 'total_results_count': 1})
    return data, fake_gads


def draft(target='campaign', values=None, **changes):
    args = {'callouts': values or ['24/7 Service'],
            target + '_id': '88' if target == 'campaign' else '77'}
    args.update(changes)
    return tools.create_callouts(**args)


def land(data, fake, plan):
    response = make_type('MutateGoogleAdsResponse')
    for index, op in enumerate(plan.operations):
        entity = op.service[:-7]
        result_name = ''.join(['_' + c.lower() if c.isupper() else c for c in entity]).lstrip('_') + '_result'
        if entity == 'Asset':
            rn = f'customers/{CID}/assets/{100 + index}'
            saved = copy.deepcopy(op.operation['create'])
            saved['resource_name'] = rn
            saved['type_'] = 'CALLOUT'
        else:
            asset_rn = f'customers/{CID}/assets/{99 + index}'
            parent = '88' if entity == 'CampaignAsset' else '77'
            prefix = 'campaignAssets' if entity == 'CampaignAsset' else 'adGroupAssets'
            rn = f'customers/{CID}/{prefix}/{parent}~{99 + index}~11'
            saved = copy.deepcopy(op.operation['create'])
            saved['resource_name'] = rn
            saved['asset'] = asset_rn
        data['saved'][rn] = saved
        response.mutate_operation_responses.append({result_name: {'resource_name': rn}})
    fake.mutate_response = response


@pytest.mark.parametrize('target', ['campaign', 'ad_group'])
def test_real_v25_atomic_paused_batch_and_verified_readback(callouts, target):
    data, fake = callouts
    d = draft(target, ['24/7 Service', 'Free Estimates'])
    plan = rails._DRAFTS[d['draft_id']].plan
    land(data, fake, plan)
    result = rails.apply_draft(d['draft_id'])
    assert result['applied'] and result['verified']
    call = fake.mutate_calls[0]
    assert len(call['operations']) == 4 and call['partial_failure'] is False
    for asset, link in zip(call['operations'][::2], call['operations'][1::2]):
        assert asset.asset_operation.create.resource_name.endswith(('/-1', '/-2'))
        linked = (link.campaign_asset_operation.create if target == 'campaign'
                  else link.ad_group_asset_operation.create)
        assert linked.status.name == 'PAUSED' and linked.field_type.name == 'CALLOUT'


@pytest.mark.parametrize('callout_values,reason', [
    ([], 'nonempty list'), (['x'] * 11, 'at most 10'), ('bad', 'nonempty list'),
    ([1], 'plain text'), ([''], 'plain text'), ([' x'], 'plain text'),
    (['x '], 'plain text'), (['{x}'], 'plain text'), (['x\n'], 'plain text'),
    (['界' * 13], 'character limit'), (['Same', 'same'], 'duplicate callouts'),
])
def test_content_and_batch_refusal_reaches_relevant_rule(callouts, callout_values, reason):
    _, fake = callouts
    with pytest.raises(rails.RailViolation, match=reason):
        tools.create_callouts(callouts=callout_values, campaign_id='88')
    assert not fake.mutate_calls


@pytest.mark.parametrize('changes', [
    {'campaign_id': '88', 'ad_group_id': '77'},
    {'campaign_id': None, 'ad_group_id': None},
])
def test_target_exclusivity_refusal(callouts, changes):
    _, fake = callouts
    with pytest.raises(rails.RailViolation, match='exactly one campaign_id or ad_group_id'):
        tools.create_callouts(callouts=['x'], **changes)
    assert not fake.mutate_calls


@pytest.mark.parametrize('value', ['x', '界' * 12 + 'x'])
def test_valid_text_boundaries_are_accepted(callouts, value):
    _, fake = callouts
    result = tools.create_callouts(callouts=[value], campaign_id='88')
    assert result['dry_run'] and result['preview']['callouts'] == [value]
    assert not fake.mutate_calls


def test_existing_identical_and_blocked_terms_refused(callouts, monkeypatch):
    data, fake = callouts
    data['links'] = [{'campaign_asset': {
        'resource_name': f'customers/{CID}/campaignAssets/88~9~11', 'campaign': CAMPAIGN,
        'asset': f'customers/{CID}/assets/9', 'field_type': 'CALLOUT', 'status': 'PAUSED'},
        'asset': {'resource_name': f'customers/{CID}/assets/9', 'type_': 'CALLOUT',
                  'callout_asset': {'callout_text': '24/7 Service'}}}]
    with pytest.raises(rails.RailViolation, match='already associated'):
        draft()
    data['links'] = []
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ('service',))
    with pytest.raises(rails.RailViolation, match='blocked term'):
        draft()
    assert not fake.mutate_calls


def test_valid_broader_existing_callout_is_snapshotted_without_blocking_plain_draft(callouts):
    data, fake = callouts
    data['links'] = [{'campaign_asset': {
        'resource_name': f'customers/{CID}/campaignAssets/88~9~11', 'campaign': CAMPAIGN,
        'asset': f'customers/{CID}/assets/9', 'field_type': 'CALLOUT', 'status': 'ENABLED'},
        'asset': {'resource_name': f'customers/{CID}/assets/9', 'type_': 'CALLOUT',
                  'final_urls': ['https://example.com'], 'tracking_url_template': '{lpurl}',
                  'callout_asset': {'callout_text': '24/7 Service', 'start_date': '2026-09-01',
                                    'end_date': '2026-09-30',
                                    'ad_schedule_targets': [{'day_of_week': 'MONDAY'}]}}}]
    d = draft()
    content = rails._DRAFTS[d['draft_id']].fingerprint['links'][0]['content']
    assert d['dry_run'] and content['plain'] is False
    assert not fake.mutate_calls


@pytest.mark.parametrize('damage', ['asset_extra', 'asset_oneof', 'link_enabled', 'link_extra',
                                     'forward', 'dangling', 'reuse', 'mixed_target',
                                     'mixed_family', 'kind_mismatch', 'unrelated'])
def test_closed_payload_and_temporary_graph_refusal(callouts, damage):
    _, fake = callouts
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    asset, link = plan.operations
    if damage == 'asset_extra':
        asset.operation['create']['tracking_url_template'] = 'x'
    elif damage == 'asset_oneof':
        asset.operation['create']['sitelink_asset'] = {'link_text': 'x'}
    elif damage == 'link_enabled':
        link.operation['create']['status'] = 'ENABLED'
    elif damage == 'link_extra':
        link.operation['create']['source'] = 'ADVERTISER'
    elif damage == 'forward':
        plan.operations.reverse()
    elif damage == 'dangling':
        link.operation['create']['asset'] = f'customers/{CID}/assets/-2'
    elif damage == 'reuse':
        plan.operations.append(copy.deepcopy(link))
    elif damage == 'mixed_target':
        second_asset, second_link = copy.deepcopy(asset), copy.deepcopy(link)
        second_asset.operation['create']['resource_name'] = f'customers/{CID}/assets/-2'
        second_link.operation['create']['asset'] = f'customers/{CID}/assets/-2'
        second_link.operation['create']['campaign'] = f'customers/{CID}/campaigns/89'
        plan.operations.extend((second_asset, second_link))
    elif damage == 'mixed_family':
        asset.operation['create'] = {'resource_name': f'customers/{CID}/assets/-1',
                                     'final_urls': ['https://example.com'],
                                     'sitelink_asset': {'link_text': 'Book'}}
    elif damage == 'kind_mismatch':
        link.operation['create']['field_type'] = 'SITELINK'
    else:
        plan.operations.append(rails.safe_create_operation('AdGroupService', {
            'name': 'x', 'campaign': CAMPAIGN, 'type_': 'SEARCH_STANDARD'}))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert not fake.mutate_calls


@pytest.mark.parametrize('field,value', [('status', 'REMOVED'), ('name', 'Renamed Search'),
                                         ('campaign_budget', f'customers/{CID}/campaignBudgets/6')])
def test_confirmation_parent_campaign_drift_refuses_without_consuming(callouts, field, value):
    data, fake = callouts
    d = draft('ad_group')
    data['campaign'][field] = value
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.mutate_calls


def test_incomplete_safety_scan_refuses(callouts, monkeypatch):
    _, fake = callouts
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: (_ for _ in ()).throw(
        rails.RailViolation('incomplete', code='SCAN_INCOMPLETE')))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'SCAN_INCOMPLETE' and not fake.mutate_calls


def test_writes_disabled_refuses_before_any_read(callouts, monkeypatch):
    data, fake = callouts
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'WRITES_DISABLED'
    assert data['queries'] == [] and not fake.mutate_calls


@pytest.mark.parametrize('damage', ['count', 'kind', 'foreign_asset', 'negative_asset',
                                     'wrong_target', 'wrong_asset', 'wrong_field', 'content',
                                     'status', 'tracking', 'mobile', 'url', 'suffix', 'custom',
                                     'date', 'schedule'])
def test_result_or_readback_mismatch_is_consumed(callouts, damage):
    data, fake = callouts
    d = draft()
    plan = rails._DRAFTS[d['draft_id']].plan
    land(data, fake, plan)
    results = fake.mutate_response.mutate_operation_responses
    if damage == 'count':
        results.pop()
    elif damage == 'kind':
        results[0] = {'campaign_result': {'resource_name': CAMPAIGN}}
    elif damage == 'foreign_asset':
        results[0].asset_result.resource_name = 'customers/2/assets/100'
    elif damage == 'negative_asset':
        results[0].asset_result.resource_name = f'customers/{CID}/assets/-1'
    elif damage in {'wrong_target', 'wrong_asset', 'wrong_field'}:
        parts = {'wrong_target': '89~100~11', 'wrong_asset': '88~999~11',
                 'wrong_field': '88~100~13'}[damage]
        results[1].campaign_asset_result.resource_name = f'customers/{CID}/campaignAssets/{parts}'
    elif damage in {'content', 'tracking', 'mobile', 'url', 'suffix', 'custom', 'date', 'schedule'}:
        saved = data['saved'][f'customers/{CID}/assets/100']
        if damage == 'content':
            saved['callout_asset']['callout_text'] = 'Changed'
        elif damage == 'tracking':
            saved['tracking_url_template'] = '{lpurl}'
        elif damage == 'mobile':
            saved['final_mobile_urls'] = ['https://example.com/mobile']
        elif damage == 'url':
            saved['final_urls'] = ['https://example.com']
        elif damage == 'suffix':
            saved['final_url_suffix'] = 'x=1'
        elif damage == 'custom':
            saved['url_custom_parameters'] = [{'key': 'x', 'value': '1'}]
        elif damage == 'date':
            saved['callout_asset']['start_date'] = '2026-09-08'
        else:
            saved['callout_asset']['ad_schedule_targets'] = [{'day_of_week': 'MONDAY'}]
    else:
        data['saved'][f'customers/{CID}/campaignAssets/88~100~11']['status'] = 'ENABLED'
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified'] is False
    assert out['code'] == 'POST_WRITE_VERIFICATION_FAILED'
    assert d['draft_id'] not in rails._DRAFTS and len(fake.mutate_calls) == 1
    if damage in {'count', 'kind', 'foreign_asset', 'negative_asset',
                  'wrong_target', 'wrong_asset', 'wrong_field'}:
        assert not any("resource_name = 'customers/" in query for query in data['queries'])


def test_validate_only_serializes_without_readback(callouts):
    data, fake = callouts
    plan = rails._DRAFTS[draft()['draft_id']].plan
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    out = client._dispatch_entity(plan, True)
    assert out['validate_only'] is True
    assert not any("resource_name = 'customers/" in q for q in data['queries'])


def test_real_v25_default_output_fields_normalize_without_expanding_scope():
    asset = make_type('Asset')
    asset.callout_asset.callout_text = '24/7 Service'
    output = type(asset).to_dict(asset)
    assert output['callout_asset']['start_date'] == ''
    assert client.callout_content(output) == '24/7 Service'
    output['callout_asset']['end_date'] = '2026-09-08'
    with pytest.raises(rails.RailViolation):
        client.callout_content(output)
    assert client.existing_callout_content(output)['plain'] is False
