"""Offline structured-snippet coverage using real installed Google Ads v25 messages."""
import copy

import pytest

from mcp_google_ads_safe import client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

CAMPAIGN = f'customers/{CID}/campaigns/88'
GROUP = f'customers/{CID}/adGroups/77'


def item(header='Services', values=None):
    return {'header': header, 'values': values or ['Repair', 'Maintenance', 'Installation']}


@pytest.fixture
def snippets(monkeypatch, fake_gads):
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
        'rows': [{'customer': {'id': CID, 'currency_code': 'USD',
                               'time_zone': 'America/New_York'}}],
        'pages_complete': True, 'returned_count': 1, 'total_results_count': 1})
    return data, fake_gads


def draft(target='campaign', values=None, **changes):
    args = {'snippets': values or [item()],
            target + '_id': '88' if target == 'campaign' else '77'}
    args.update(changes)
    return tools.create_structured_snippets(**args)


def land(data, fake, plan):
    response = make_type('MutateGoogleAdsResponse')
    for index, op in enumerate(plan.operations):
        entity = op.service[:-7]
        result_name = ''.join(['_' + c.lower() if c.isupper() else c
                               for c in entity]).lstrip('_') + '_result'
        if entity == 'Asset':
            rn = f'customers/{CID}/assets/{100 + index}'
            saved = copy.deepcopy(op.operation['create'])
            saved['resource_name'] = rn
            saved['type_'] = 'STRUCTURED_SNIPPET'
        else:
            asset_rn = f'customers/{CID}/assets/{99 + index}'
            parent = '88' if entity == 'CampaignAsset' else '77'
            prefix = 'campaignAssets' if entity == 'CampaignAsset' else 'adGroupAssets'
            rn = f'customers/{CID}/{prefix}/{parent}~{99 + index}~12'
            saved = copy.deepcopy(op.operation['create'])
            saved['resource_name'] = rn
            saved['asset'] = asset_rn
        data['saved'][rn] = saved
        response.mutate_operation_responses.append({result_name: {'resource_name': rn}})
    fake.mutate_response = response


@pytest.mark.parametrize('target', ['campaign', 'ad_group'])
def test_real_v25_atomic_paused_batch_and_verified_readback(snippets, target):
    data, fake = snippets
    requested = [item(), item('Brands', ['Carrier', 'Trane', 'Lennox'])]
    d = draft(target, requested)
    assert d['preview']['snippets'] == requested
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
        assert linked.status.name == 'PAUSED'
        assert linked.field_type.name == 'STRUCTURED_SNIPPET'


@pytest.mark.parametrize('values,reason', [
    ([], 'nonempty list'), ([item()] * 11, 'at most 10'), ('bad', 'nonempty list'),
    ([{'header': 'Services'}], 'exactly header and values'),
    ([{'header': 'Services', 'values': ['a', 'b', 'c'], 'extra': 1}],
     'exactly header and values'),
    ([item(['Services'])], 'exact supported English header'),
    ([item({'header': 'Services'})], 'exact supported English header'),
    ([item('services')], 'exact supported English header'),
    ([item('Services', ['a', 'b'])], '3..10'),
    ([item('Services', ['a'] * 11)], '3..10'),
    ([item('Services', ['a', 'b', 3])], 'plain text'),
    ([item('Services', ['a', ' b', 'c'])], 'plain text'),
    ([item('Services', ['a', 'b', '界' * 13])], 'character limit'),
    ([item('Services', ['Same', 'same', 'Third'])], 'duplicate structured snippet values'),
    ([item(), item(values=['installation', 'REPAIR', 'maintenance'])],
     'duplicate structured snippets'),
])
def test_input_refusal_reaches_exact_rule_with_valid_target(snippets, values, reason):
    _, fake = snippets
    with pytest.raises(rails.RailViolation, match=reason):
        tools.create_structured_snippets(snippets=values, campaign_id='88')
    assert not fake.mutate_calls


@pytest.mark.parametrize('changes', [
    {'campaign_id': '88', 'ad_group_id': '77'},
    {'campaign_id': None, 'ad_group_id': None},
])
def test_target_exclusivity_refusal(snippets, changes):
    _, fake = snippets
    with pytest.raises(rails.RailViolation, match='exactly one campaign_id or ad_group_id'):
        tools.create_structured_snippets(snippets=[item()], **changes)
    assert not fake.mutate_calls


@pytest.mark.parametrize('header', sorted(client.STRUCTURED_SNIPPET_HEADERS))
def test_every_exact_english_header_and_true_text_boundary_pass(snippets, header):
    result = draft(values=[item(header, ['x', '界' * 12 + 'x', 'third'])])
    assert result['dry_run']


def test_maximum_batch_and_value_counts_pass(snippets):
    requested = [item(header, [f'value {n}' for n in range(10)])
                 for header in sorted(client.STRUCTURED_SNIPPET_HEADERS)[:10]]
    result = draft(values=requested)
    plan = rails._DRAFTS[result['draft_id']].plan
    assert result['preview']['snippets'] == requested
    assert len(plan.operations) == 20


def test_existing_equivalent_refused_but_broader_localized_inventory_preserved(snippets):
    data, fake = snippets
    data['links'] = [{'campaign_asset': {
        'resource_name': f'customers/{CID}/campaignAssets/88~9~12', 'campaign': CAMPAIGN,
        'asset': f'customers/{CID}/assets/9', 'field_type': 'STRUCTURED_SNIPPET',
        'status': 'PAUSED'},
        'asset': {'resource_name': f'customers/{CID}/assets/9',
                  'type_': 'STRUCTURED_SNIPPET',
                  'structured_snippet_asset': item(values=['Installation', 'repair', 'Maintenance'])}}]
    with pytest.raises(rails.RailViolation, match='already associated'):
        draft()
    data['links'][0]['asset'] = {
        'resource_name': f'customers/{CID}/assets/9', 'type_': 'STRUCTURED_SNIPPET',
        'tracking_url_template': '{lpurl}',
        'structured_snippet_asset': item('Dienstleistungen', ['Repair', 'Maintenance', 'Installation'])}
    d = draft()
    content = rails._DRAFTS[d['draft_id']].fingerprint['links'][0]['content']
    assert content['base'][0] == 'Dienstleistungen' and content['plain'] is False
    assert not fake.mutate_calls


def test_blocked_term_and_incomplete_scan_refuse(snippets, monkeypatch):
    _, fake = snippets
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ('repair',))
    with pytest.raises(rails.RailViolation, match='blocked term'):
        draft()
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ())
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: (_ for _ in ()).throw(
        rails.RailViolation('incomplete', code='SCAN_INCOMPLETE')))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'SCAN_INCOMPLETE' and not fake.mutate_calls


@pytest.mark.parametrize('damage', ['asset_extra', 'asset_oneof', 'bad_header_list',
                                     'bad_header_dict', 'bad_header_string', 'duplicate_value',
                                     'link_enabled', 'forward',
                                     'dangling', 'reuse', 'mixed_target', 'mixed_family',
                                     'kind_mismatch', 'unrelated'])
def test_closed_payload_and_temporary_graph_refusal(snippets, damage):
    _, fake = snippets
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    asset, link = plan.operations
    if damage == 'asset_extra':
        asset.operation['create']['tracking_url_template'] = 'x'
    elif damage == 'asset_oneof':
        asset.operation['create']['callout_asset'] = {'callout_text': 'x'}
    elif damage == 'bad_header_list':
        asset.operation['create']['structured_snippet_asset']['header'] = ['Services']
    elif damage == 'bad_header_dict':
        asset.operation['create']['structured_snippet_asset']['header'] = {'name': 'Services'}
    elif damage == 'bad_header_string':
        asset.operation['create']['structured_snippet_asset']['header'] = 'services'
    elif damage == 'duplicate_value':
        asset.operation['create']['structured_snippet_asset']['values'][1] = 'repair'
    elif damage == 'link_enabled':
        link.operation['create']['status'] = 'ENABLED'
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
        pair, _ = rails._closed_asset_pair(
            CID, 'campaign', CAMPAIGN, 2, 'CALLOUT', 'Free Estimates')
        plan.operations.extend(pair)
    elif damage == 'kind_mismatch':
        link.operation['create']['field_type'] = 'CALLOUT'
    else:
        plan.operations.append(rails.safe_create_operation('AdGroupService', {
            'name': 'x', 'campaign': CAMPAIGN, 'type_': 'SEARCH_STANDARD'}))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert not fake.mutate_calls


@pytest.mark.parametrize('field,value', [
    ('status', 'REMOVED'), ('name', 'Renamed Search'),
    ('campaign_budget', f'customers/{CID}/campaignBudgets/6'),
])
def test_confirmation_parent_campaign_drift_refuses_without_consuming(snippets, field, value):
    data, fake = snippets
    d = draft('ad_group')
    data['campaign'][field] = value
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.mutate_calls


@pytest.mark.parametrize('damage', ['count', 'kind', 'foreign_asset', 'negative_asset',
                                     'wrong_target', 'wrong_asset', 'wrong_field', 'content',
                                     'order', 'status', 'tracking', 'mobile', 'url', 'suffix',
                                     'custom', 'alternate'])
def test_result_or_readback_mismatch_is_consumed(snippets, damage):
    data, fake = snippets
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
        parts = {'wrong_target': '89~100~12', 'wrong_asset': '88~999~12',
                 'wrong_field': '88~100~11'}[damage]
        results[1].campaign_asset_result.resource_name = f'customers/{CID}/campaignAssets/{parts}'
    elif damage in {'content', 'order', 'tracking', 'mobile', 'url', 'suffix', 'custom', 'alternate'}:
        saved = data['saved'][f'customers/{CID}/assets/100']
        if damage == 'content':
            saved['structured_snippet_asset']['header'] = 'Brands'
        elif damage == 'order':
            saved['structured_snippet_asset']['values'].reverse()
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
        else:
            saved['callout_asset'] = {'callout_text': 'Unexpected'}
    else:
        data['saved'][f'customers/{CID}/campaignAssets/88~100~12']['status'] = 'ENABLED'
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified'] is False
    assert out['code'] == 'POST_WRITE_VERIFICATION_FAILED'
    assert d['draft_id'] not in rails._DRAFTS and len(fake.mutate_calls) == 1
    if damage in {'count', 'kind', 'foreign_asset', 'negative_asset',
                  'wrong_target', 'wrong_asset', 'wrong_field'}:
        assert not any("resource_name = 'customers/" in query for query in data['queries'])


def test_validate_only_serializes_without_readback(snippets):
    data, fake = snippets
    plan = rails._DRAFTS[draft()['draft_id']].plan
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    out = client._dispatch_entity(plan, True)
    assert out['validate_only'] is True
    assert not any("resource_name = 'customers/" in q for q in data['queries'])


def test_real_v25_default_output_normalization_and_order():
    asset = make_type('Asset')
    asset.structured_snippet_asset.header = 'Services'
    asset.structured_snippet_asset.values.extend(['Repair', 'Maintenance', 'Installation'])
    output = type(asset).to_dict(asset)
    assert client.structured_snippet_content(output) == (
        'Services', ('Repair', 'Maintenance', 'Installation'))
    output['tracking_url_template'] = '{lpurl}'
    with pytest.raises(rails.RailViolation):
        client.structured_snippet_content(output)
    assert client.existing_structured_snippet_content(output)['plain'] is False
