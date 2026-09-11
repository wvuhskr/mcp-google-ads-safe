"""Offline sitelink creation coverage using real installed Google Ads v25 messages."""
import copy

import pytest

from mcp_google_ads_safe import client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

CAMPAIGN = f'customers/{CID}/campaigns/88'
GROUP = f'customers/{CID}/adGroups/77'


def item(**changes):
    return {'link_text': 'Book Service', 'final_url': 'https://example.com/book',
            'description1': 'Schedule cooling service',
            'description2': 'Choose a convenient time', **changes}


@pytest.fixture
def sitelinks(monkeypatch, fake_gads):
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
    args = {'sitelinks': values or [item()], target + '_id': '88' if target == 'campaign' else '77'}
    args.update(changes)
    return tools.draft_sitelinks(**args)


def land(data, fake, plan):
    response = make_type('MutateGoogleAdsResponse')
    for index, op in enumerate(plan.operations):
        entity = op.service[:-7]
        result_name = ''.join(['_' + c.lower() if c.isupper() else c for c in entity]).lstrip('_') + '_result'
        if entity == 'Asset':
            rn = f'customers/{CID}/assets/{100 + index}'
            saved = copy.deepcopy(op.operation['create'])
            saved['resource_name'] = rn
            saved['type_'] = 'SITELINK'
        else:
            asset_rn = f'customers/{CID}/assets/{99 + index}'
            parent = '88' if entity == 'CampaignAsset' else '77'
            prefix = 'campaignAssets' if entity == 'CampaignAsset' else 'adGroupAssets'
            rn = f'customers/{CID}/{prefix}/{parent}~{99 + index}~13'
            saved = copy.deepcopy(op.operation['create'])
            saved['resource_name'] = rn
            saved['asset'] = asset_rn
        data['saved'][rn] = saved
        response.mutate_operation_responses.append({result_name: {'resource_name': rn}})
    fake.mutate_response = response


@pytest.mark.parametrize('target', ['campaign', 'ad_group'])
def test_real_v25_atomic_paused_batch_and_verified_readback(sitelinks, target):
    data, fake = sitelinks
    d = draft(target, [item(), item(link_text='Financing', final_url='https://example.com/pay')])
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
        assert linked.status.name == 'PAUSED' and linked.field_type.name == 'SITELINK'


@pytest.mark.parametrize('changes', [
    {'sitelinks': []}, {'sitelinks': [item()] * 11}, {'sitelinks': 'bad'},
    {'sitelinks': [{'link_text': 'x', 'final_url': 'https://example.com', 'extra': 1}]},
    {'sitelinks': [{'link_text': 'x', 'final_url': 'https://example.com', 'description1': 'one'}]},
    {'sitelinks': [item(), item()]}, {'sitelinks': [item(link_text='界' * 13)]},
    {'sitelinks': [item(description1='界' * 18)]},
    {'sitelinks': [item()], 'campaign_id': '88', 'ad_group_id': '77'},
    {'sitelinks': [item()], 'campaign_id': None, 'ad_group_id': None},
])
def test_input_and_policy_refusal_before_dispatch(sitelinks, changes):
    data, fake = sitelinks
    with pytest.raises(rails.RailViolation):
        tools.draft_sitelinks(**changes)
    assert not fake.mutate_calls


def test_existing_identical_and_blocked_terms_refused(sitelinks, monkeypatch):
    data, fake = sitelinks
    data['links'] = [{'campaign_asset': {'resource_name': f'customers/{CID}/campaignAssets/88~9~13',
                       'campaign': CAMPAIGN, 'asset': f'customers/{CID}/assets/9',
                       'field_type': 'SITELINK', 'status': 'PAUSED'},
                      'asset': {'resource_name': f'customers/{CID}/assets/9', 'type_': 'SITELINK',
                                'final_urls': [item()['final_url']],
                                'sitelink_asset': {k: v for k, v in item().items() if k != 'final_url'}}}]
    with pytest.raises(rails.RailViolation, match='already associated'):
        draft()
    data['links'] = []
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ('cooling',))
    with pytest.raises(rails.RailViolation, match='blocked term'):
        draft()
    assert not fake.mutate_calls


def test_valid_broader_existing_sitelink_is_snapshotted_without_blocking_plain_draft(sitelinks):
    data, fake = sitelinks
    existing = item()
    data['links'] = [{'campaign_asset': {
        'resource_name': f'customers/{CID}/campaignAssets/88~9~13', 'campaign': CAMPAIGN,
        'asset': f'customers/{CID}/assets/9', 'field_type': 'SITELINK', 'status': 'ENABLED'},
        'asset': {'resource_name': f'customers/{CID}/assets/9', 'type_': 'SITELINK',
                  'final_urls': [existing['final_url'], 'https://example.com/alternate'],
                  'final_mobile_urls': ['https://example.com/mobile'],
                  'tracking_url_template': '{lpurl}?source=existing',
                  'final_url_suffix': 'existing=1', 'url_custom_parameters': [{'key': 'x', 'value': 'y'}],
                  'sitelink_asset': {**{k: v for k, v in existing.items() if k != 'final_url'},
                                     'start_date': '2026-09-01', 'end_date': '2026-09-30',
                                     'ad_schedule_targets': [{'day_of_week': 'MONDAY'}]}}}]
    d = draft(values=[existing])
    assert d['dry_run'] and rails._DRAFTS[d['draft_id']].fingerprint['links'][0]['content']['plain'] is False
    assert not fake.mutate_calls


@pytest.mark.parametrize('damage', ['asset_extra', 'asset_status', 'link_enabled', 'link_extra',
                                     'forward', 'dangling', 'reuse', 'mixed_target', 'unrelated'])
def test_closed_payload_and_temporary_graph_refusal(sitelinks, damage):
    _, fake = sitelinks
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    asset, link = plan.operations
    if damage == 'asset_extra':
        asset.operation['create']['tracking_url_template'] = 'x'
    elif damage == 'asset_status':
        asset.operation['create']['status'] = 'PAUSED'
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
        second_asset = copy.deepcopy(asset)
        second_asset.operation['create']['resource_name'] = f'customers/{CID}/assets/-2'
        second_link = copy.deepcopy(link)
        second_link.operation['create']['asset'] = f'customers/{CID}/assets/-2'
        second_link.operation['create']['campaign'] = f'customers/{CID}/campaigns/89'
        plan.operations.extend((second_asset, second_link))
    else:
        plan.operations.append(rails.safe_create_operation('AdGroupService', {
            'name': 'x', 'campaign': CAMPAIGN, 'type_': 'SEARCH_STANDARD'}))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert not fake.mutate_calls


@pytest.mark.parametrize('field,value', [('status', 'REMOVED'), ('status', 'PAUSED'),
                                         ('name', 'Renamed Search'),
                                         ('campaign_budget', f'customers/{CID}/campaignBudgets/6')])
def test_confirmation_drift_refuses_without_consuming(sitelinks, field, value):
    data, fake = sitelinks
    d = draft('ad_group')
    data['campaign'][field] = value
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.mutate_calls


def test_incomplete_safety_scan_refuses(sitelinks, monkeypatch):
    _, fake = sitelinks
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: (_ for _ in ()).throw(
        rails.RailViolation('incomplete', code='SCAN_INCOMPLETE')))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'SCAN_INCOMPLETE' and not fake.mutate_calls


@pytest.mark.parametrize('damage', ['count', 'kind', 'foreign_asset', 'negative_asset',
                                     'wrong_target', 'wrong_asset', 'wrong_field', 'content', 'status',
                                     'tracking', 'mobile', 'date', 'schedule'])
def test_result_or_readback_mismatch_is_consumed(sitelinks, damage):
    data, fake = sitelinks
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
        parts = {'wrong_target': '89~100~13', 'wrong_asset': '88~999~13',
                 'wrong_field': '88~100~2'}[damage]
        results[1].campaign_asset_result.resource_name = f'customers/{CID}/campaignAssets/{parts}'
    elif damage in {'content', 'tracking', 'mobile', 'date', 'schedule'}:
        saved = data['saved'][f'customers/{CID}/assets/100']
        if damage == 'content':
            saved['sitelink_asset']['link_text'] = 'Changed'
        elif damage == 'tracking':
            saved['tracking_url_template'] = 'https://unexpected.example/?url={lpurl}'
        elif damage == 'mobile':
            saved['final_mobile_urls'] = ['https://unexpected.example/mobile']
        elif damage == 'date':
            saved['sitelink_asset']['start_date'] = '2026-09-08'
        else:
            saved['sitelink_asset']['ad_schedule_targets'] = [{'day_of_week': 'MONDAY'}]
    else:
        rn = f'customers/{CID}/campaignAssets/88~100~13'
        data['saved'][rn]['status'] = 'ENABLED'
        assert client.created_resource_state(CID, 'campaign_asset', rn)['status'] == 'ENABLED'
        expected = dict(plan.post_checks[1]['expected'], asset=f'customers/{CID}/assets/100')
        assert not client._created_match(client.created_resource_state(CID, 'campaign_asset', rn), expected)
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified'] is False
    assert out['code'] == 'POST_WRITE_VERIFICATION_FAILED'
    assert d['draft_id'] not in rails._DRAFTS and len(fake.mutate_calls) == 1
    if damage in {'tracking', 'mobile', 'date', 'schedule'}:
        query = next(q for q in data['queries'] if "FROM asset WHERE asset.resource_name =" in q)
        assert 'asset.final_mobile_urls' in query and 'asset.tracking_url_template' in query
        assert 'asset.sitelink_asset.start_date' in query
        assert 'asset.sitelink_asset.ad_schedule_targets' in query


def test_validate_only_serializes_without_readback(sitelinks):
    data, fake = sitelinks
    plan = rails._DRAFTS[draft()['draft_id']].plan
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    out = client._dispatch_entity(plan, True)
    assert out['validate_only'] is True
    assert not any("resource_name = 'customers/" in q for q in data['queries'])


def test_real_v25_default_output_fields_normalize_without_expanding_scope():
    asset = make_type('Asset')
    asset.final_urls.append('https://example.com/book')
    asset.sitelink_asset.link_text = 'Book Service'
    output = type(asset).to_dict(asset)
    assert output['sitelink_asset']['description1'] == ''
    assert client.sitelink_content(output) == ('Book Service', 'https://example.com/book', None, None)
    output['sitelink_asset']['start_date'] = '2026-09-08'
    with pytest.raises(rails.RailViolation):
        client.sitelink_content(output)
    assert client.existing_sitelink_content(output)['plain'] is False
