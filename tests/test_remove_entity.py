import copy
import json

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import make_type

CID = '1234567890'
CAMPAIGN = f'customers/{CID}/campaigns/88'
GROUP = f'customers/{CID}/adGroups/77'
AD = f'customers/{CID}/adGroupAds/77~66'


def base_state(kind='campaign'):
    state = {
        'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'},
        'campaign': {'resource_name': CAMPAIGN, 'id': '88', 'name': 'Search',
                     'status': 'PAUSED', 'advertising_channel_type': 'SEARCH',
                     'advertising_channel_sub_type': 'UNSPECIFIED',
                     'campaign_budget': f'customers/{CID}/campaignBudgets/5'},
        'budget': {'resource_name': f'customers/{CID}/campaignBudgets/5',
                   'amount_micros': '50000000', 'explicitly_shared': False,
                   'reference_count': '1'},
        'ad_groups': [], 'ads': [], 'campaign_criteria': [], 'ad_group_criteria': [],
        'campaign_assets': [], 'ad_group_assets': [], 'asset_groups': [],
    }
    if kind in {'ad_group', 'ad'}:
        state['ad_group'] = {'resource_name': GROUP, 'id': '77', 'name': 'Group',
                             'status': 'PAUSED', 'type': 'SEARCH_STANDARD',
                             'campaign': CAMPAIGN}
    if kind == 'ad':
        state['ad'] = {'resource_name': AD, 'id': '66', 'ad_group': GROUP,
                       'status': 'PAUSED', 'type': 'RESPONSIVE_SEARCH_AD',
                       'final_urls': ['https://example.com'],
                       'responsive_search_ad': {'headlines': [{'text': 'One'}],
                                                'descriptions': [{'text': 'Two'}],
                                                'path1': '', 'path2': ''}}
    return state


@pytest.fixture
def removal(monkeypatch, fake_gads):
    data = {'kind': 'campaign', 'state': base_state(), 'reads': 0, 'post': 'REMOVED'}

    def read(cid, kind, entity_id, ad_group_id=None):
        assert cid == CID
        data['reads'] += 1
        return copy.deepcopy(data['state'])

    monkeypatch.setattr(client, 'removal_entity_state', read)
    monkeypatch.setattr(client, 'removed_entity_state', lambda check: data['post'])
    return data, fake_gads


def draft(kind='campaign'):
    return tools.remove_entity(kind, '66' if kind == 'ad' else ('77' if kind == 'ad_group' else '88'),
                               ad_group_id='77' if kind == 'ad' else None)


def result(fake, kind):
    response = make_type('MutateGoogleAdsResponse')
    rn = {'campaign': CAMPAIGN, 'ad_group': GROUP, 'ad': AD}[kind]
    result_type = 'ad_group_ad_result' if kind == 'ad' else kind + '_result'
    response.mutate_operation_responses.append({result_type: {'resource_name': rn}})
    fake.mutate_response = response


@pytest.mark.parametrize('kind,service,field', [
    ('campaign', 'CampaignService', 'campaign_operation'),
    ('ad_group', 'AdGroupService', 'ad_group_operation'),
    ('ad', 'AdGroupAdService', 'ad_group_ad_operation'),
])
def test_each_kind_builds_one_real_v25_remove(removal, kind, service, field):
    data, fake = removal
    data['kind'], data['state'] = kind, base_state(kind)
    d = draft(kind)
    result(fake, kind)
    applied = rails.apply_draft(d['draft_id'])
    assert applied['verified'] is True
    call = fake.mutate_calls[0]
    assert call['customer_id'] == CID and call['partial_failure'] is False
    assert len(call['operations']) == 1
    operation = call['operations'][0]
    assert operation._pb.WhichOneof('operation') == field
    assert getattr(getattr(operation, field), 'remove') == {'campaign': CAMPAIGN, 'ad_group': GROUP, 'ad': AD}[kind]
    assert not getattr(operation, field).update_mask.paths
    assert rails._DRAFTS.get(d['draft_id']) is None


@pytest.mark.parametrize('kind', ['campaign', 'ad_group', 'ad'])
def test_each_kind_validate_only_uses_real_v25_remove_without_postcheck(removal, monkeypatch,
                                                                       kind):
    data, fake = removal
    data['state'] = base_state(kind)
    d = draft(kind)
    result(fake, kind)
    monkeypatch.setattr(client, 'removed_entity_state',
                        lambda check: (_ for _ in ()).throw(AssertionError('postcheck ran')))
    out = client._dispatch(rails._DRAFTS[d['draft_id']].plan, validate_only=True)
    assert out['validate_only'] is True
    assert fake.mutate_calls[0]['validate_only'] is True


@pytest.mark.parametrize('kind,entity_id,ad_group_id,reason', [
    ('keyword', '1', None, 'unsupported'), ('campaign', 88, None, 'string'),
    ('campaign', '0', None, 'invalid numeric'), ('campaign', '88', '77', 'forbidden'),
    ('ad', '66', None, 'required'), ('ad_group', '77', '88', 'forbidden'),
])
def test_strict_inputs_before_reads(removal, kind, entity_id, ad_group_id, reason):
    data, fake = removal
    with pytest.raises(rails.RailViolation, match=reason):
        tools.remove_entity(kind, entity_id, ad_group_id=ad_group_id)
    assert data['reads'] == 0 and not fake.mutate_calls


@pytest.mark.parametrize('change,reason', [
    (('campaign', 'status', 'ENABLED'), 'PAUSED'),
    (('campaign', 'advertising_channel_type', 'DISPLAY'), 'standard Search'),
    (('campaign', 'resource_name', 'customers/9999999999/campaigns/88'), 'standard Search'),
])
def test_target_and_parent_refusals(removal, change, reason):
    data, fake = removal
    section, key, value = change
    data['state'][section][key] = value
    with pytest.raises(rails.RailViolation, match=reason):
        draft()
    assert not fake.mutate_calls


@pytest.mark.parametrize('kind,section,key,value,reason', [
    ('ad_group', 'ad_group', 'campaign', f'customers/{CID}/campaigns/99', 'parent'),
    ('ad', 'ad', 'type', 'EXPANDED_TEXT_AD', 'responsive search ad'),
    ('ad', 'ad', 'ad_group', f'customers/{CID}/adGroups/99', 'identity'),
])
def test_full_parent_and_ad_type_refusals(removal, kind, section, key, value, reason):
    data, fake = removal
    data['state'] = base_state(kind)
    data['state'][section][key] = value
    with pytest.raises(rails.RailViolation, match=reason):
        draft(kind)
    assert not fake.mutate_calls


def test_complete_children_are_sorted_previewed_and_drift_refuses(removal):
    data, fake = removal
    data['state']['ad_groups'] = [
        {'resource_name': f'customers/{CID}/adGroups/2'},
        {'resource_name': f'customers/{CID}/adGroups/1'},
    ]
    data['state']['ads'] = [{'resource_name': f'customers/{CID}/adGroupAds/1~3'}]
    d = draft()
    affected = d['preview']['affected_children']
    assert affected['ad_groups']['count'] == 2
    assert affected['ad_groups']['resource_names'] == sorted(affected['ad_groups']['resource_names'])
    data['state']['ads'].append({'resource_name': f'customers/{CID}/adGroupAds/2~4'})
    with pytest.raises(rails.RailViolation, match='account changed'):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.mutate_calls


def test_validate_only_skips_saved_state_read(removal, monkeypatch):
    data, fake = removal
    d = draft()
    result(fake, 'campaign')
    monkeypatch.setattr(client, 'removed_entity_state',
                        lambda check: (_ for _ in ()).throw(AssertionError('postcheck ran')))
    out = client._dispatch(rails._DRAFTS[d['draft_id']].plan, validate_only=True)
    assert out['validate_only'] is True


@pytest.mark.parametrize('post,verified', [(None, True), ('REMOVED', True),
                                             ('PAUSED', False), ('UNKNOWN', False)])
def test_exact_postcheck_accepts_only_removed_or_absent(removal, post, verified):
    data, fake = removal
    d = draft()
    result(fake, 'campaign')
    data['post'] = post
    assert rails.apply_draft(d['draft_id'])['verified'] is verified


def test_bad_result_is_unverified_consumed_and_never_postchecked(removal, monkeypatch):
    _, fake = removal
    d = draft()
    response = make_type('MutateGoogleAdsResponse')
    response.mutate_operation_responses.append(
        {'campaign_result': {'resource_name': f'customers/{CID}/campaigns/99'}})
    fake.mutate_response = response
    monkeypatch.setattr(client, 'removed_entity_state',
                        lambda check: (_ for _ in ()).throw(AssertionError('postcheck ran')))
    out = rails.apply_draft(d['draft_id'])
    assert out['verified'] is False and d['draft_id'] not in rails._DRAFTS
    assert len(fake.mutate_calls) == 1


def test_transport_ambiguity_consumes_and_does_not_retry(removal):
    _, fake = removal
    d = draft()
    fake.mutate_error = rails.UnknownWriteOutcome('may have landed')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] not in rails._DRAFTS and len(fake.mutate_calls) == 1


def test_tampered_descriptor_and_action_refuse_before_provider(removal):
    _, fake = removal
    d = draft()
    saved = rails._DRAFTS[d['draft_id']]
    saved.plan.post_checks[0]['resource_name'] = f'customers/{CID}/campaigns/99'
    with pytest.raises(rails.RailViolation, match='descriptor|digest'):
        rails.apply_draft(d['draft_id'])
    assert not fake.mutate_calls


@pytest.mark.parametrize('gate,reason', [('read', 'read allowlist'),
                                         ('writes', 'mutating tools are disabled')])
def test_revoked_gates_at_apply_refuse_nonconsumingly(removal, monkeypatch, gate, reason):
    _, fake = removal
    d = draft()
    if gate == 'read':
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '9999999999')
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', CID)
    else:
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    with pytest.raises(rails.RailViolation, match=reason):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.mutate_calls


def test_direct_dispatch_rejects_incoherent_descriptor(removal):
    _, fake = removal
    op = rails.MutationOp('CampaignService', {'remove': CAMPAIGN}, None)
    plan = rails.EntityMutationPlan(CID, [op], True, post_checks=[])
    with pytest.raises(rails.RailViolation, match='descriptor'):
        client._dispatch(plan)
    assert not fake.mutate_calls


@pytest.mark.parametrize('rows,reason', [
    ([{'ad_group': {'resource_name': GROUP, 'id': '77', 'campaign': CAMPAIGN,
                    'status': 'PAUSED'}},
      {'ad_group': {'resource_name': GROUP, 'id': '77', 'campaign': CAMPAIGN,
                    'status': 'PAUSED'}}],
     'duplicate'),
    ([{'ad_group': {'resource_name': 'bad', 'campaign': CAMPAIGN, 'status': 'PAUSED'}}],
     'malformed'),
    ([{'ad_group': {'resource_name': 'customers/9999999999/adGroups/77',
                    'id': '77', 'campaign': CAMPAIGN, 'status': 'PAUSED'}}],
     'another customer'),
])
def test_population_duplicate_and_malformed_fail_closed(monkeypatch, rows, reason):
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: rows)
    with pytest.raises(rails.RailViolation, match=reason):
        client._removal_inventory(CID, 'SELECT x FROM ad_group', 'ad_group', 'adGroups',
                                  'campaign', CAMPAIGN, {'status': 'AdGroupStatusEnum'})


def test_population_read_error_propagates(monkeypatch):
    def fail(query, cid):
        raise rails.RailViolation('internal scan incomplete', code='SCAN_INCOMPLETE')
    monkeypatch.setattr(client, 'gaql_all', fail)
    with pytest.raises(rails.RailViolation, match='scan incomplete'):
        client._removal_inventory(CID, 'SELECT x FROM ad_group', 'ad_group', 'adGroups')


@pytest.mark.parametrize('entity,rn,parent,row_key', [
    ('campaign', CAMPAIGN, None, 'campaign'),
    ('ad_group', GROUP, CAMPAIGN, 'ad_group'),
    ('ad_group_ad', AD, GROUP, 'ad_group_ad'),
])
def test_real_exact_removed_state_reader_accepts_removed(monkeypatch, entity, rn, parent,
                                                         row_key):
    row = {'resource_name': rn, 'status': 'REMOVED'}
    if entity == 'ad_group':
        row['campaign'] = parent
    elif entity == 'ad_group_ad':
        row['ad_group'] = parent
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: [{row_key: row}])
    check = {'entity_type': entity, 'customer_id': CID, 'resource_name': rn,
             'parent_resource_name': parent}
    assert client.removed_entity_state(check) == 'REMOVED'


@pytest.mark.parametrize('change,reason', [('parent', 'parent'), ('resource', 'identity'),
                                            ('duplicate', 'ambiguous')])
def test_real_exact_removed_state_reader_refuses_wrong_rows(monkeypatch, change, reason):
    row = {'resource_name': GROUP, 'status': 'REMOVED', 'campaign': CAMPAIGN}
    if change == 'parent':
        row['campaign'] = f'customers/{CID}/campaigns/99'
    elif change == 'resource':
        row['resource_name'] = f'customers/{CID}/adGroups/99'
    rows = [{'ad_group': row}] * (2 if change == 'duplicate' else 1)
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: rows)
    with pytest.raises(rails.RailViolation, match=reason):
        client.removed_entity_state({
            'entity_type': 'ad_group', 'customer_id': CID, 'resource_name': GROUP,
            'parent_resource_name': CAMPAIGN})


def test_actual_mcp_boundary_rejects_coerced_id_before_reads(removal):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary, payload

    data, fake = removal
    out = payload(boundary(app.mcp, 'remove_entity', {
        'entity_type': 'campaign', 'entity_id': 88, 'customer_id': None,
        'ad_group_id': None}))
    assert out['code'] == 'BAD_INPUT'
    assert data['reads'] == 0 and not fake.mutate_calls


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 'null'), ('customer_id', ' \tnull '),
    ('ad_group_id', 'null'), ('ad_group_id', '\nnull '),
])
def test_actual_mcp_literal_null_refuses_before_reads(removal, field, value):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary, payload

    data, fake = removal
    arguments = {'entity_type': 'campaign', 'entity_id': '88', field: value}
    out = payload(boundary(app.mcp, 'remove_entity', arguments))
    assert out['code'] == 'BAD_INPUT'
    assert data['reads'] == 0 and not fake.mutate_calls


@pytest.mark.parametrize('arguments,reason', [
    ({'entity_type': 'keyword', 'entity_id': '1'}, 'unsupported'),
    ({'entity_type': 'campaign', 'entity_id': '0'}, 'invalid numeric'),
    ({'entity_type': 'ad', 'entity_id': '66'}, 'required'),
    ({'entity_type': 'campaign', 'entity_id': '88', 'ad_group_id': '77'}, 'forbidden'),
])
def test_actual_mcp_boundary_invalid_contract_inputs(removal, arguments, reason):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary, payload

    data, fake = removal
    out = payload(boundary(app.mcp, 'remove_entity', arguments))
    assert reason in out['reason']
    assert data['reads'] == 0 and not fake.mutate_calls


@pytest.mark.parametrize('include_none', [False, True])
def test_actual_mcp_boundary_accepts_strict_inputs_and_returns_draft(removal, include_none):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary

    arguments = {'entity_type': 'campaign', 'entity_id': '88'}
    if include_none:
        arguments.update(customer_id=None, ad_group_id=None)
    result = boundary(app.mcp, 'remove_entity', arguments)
    assert result.model_dump(by_alias=True)['isError'] is False
    out = json.loads(result.content[0].text)
    assert out['dry_run'] is True and out['preview']['confirmation_required'] is True


@pytest.fixture
def real_removal_population(monkeypatch):
    """Supply only complete scan rows; keep the actual reader and compiler in use."""
    state = base_state('ad_group')
    rows = {
        'campaign': [{'campaign': state['campaign']}],
        'ad_group': [{'ad_group': state['ad_group']}],
        'campaign_budget': [{'campaign_budget': state['budget']}],
        'ad_group_ad': [{'ad_group_ad': {
            'resource_name': AD, 'ad_group': GROUP, 'status': 'PAUSED',
            'ad': {'id': '66', 'type_': 'EXPANDED_TEXT_AD'}}}],
        'campaign_asset': [{'campaign_asset': {
            'resource_name': f'customers/{CID}/campaignAssets/88~4~13',
            'campaign': CAMPAIGN, 'asset': f'customers/{CID}/assets/4',
            'field_type': 'SITELINK', 'status': 'ENABLED'}}],
        'ad_group_asset': [{'ad_group_asset': {
            'resource_name': f'customers/{CID}/adGroupAssets/77~4~13',
            'ad_group': GROUP, 'asset': f'customers/{CID}/assets/4',
            'field_type': 'SITELINK', 'status': 'ENABLED'}}],
    }

    def scan(query, cid):
        assert cid == CID and 'LIMIT' not in query
        return copy.deepcopy(rows.get(query.split(' FROM ')[1].split()[0], []))

    def forbidden(*args, **kwargs):
        pytest.fail('provider construction or dispatch must not run')

    monkeypatch.setattr(client, '_creation_account', lambda cid: copy.deepcopy(state['account']))
    monkeypatch.setattr(client, 'gaql_all', scan)
    monkeypatch.setattr(client, 'gads', forbidden)
    monkeypatch.setattr(client, '_dispatch', forbidden)
    return rows


@pytest.mark.parametrize('kind', ['campaign', 'ad_group'])
def test_real_populated_removal_reader_and_draft(real_removal_population, kind):
    state = client.removal_entity_state(CID, kind, '88' if kind == 'campaign' else '77')
    assert state['ads'][0]['ad']['type_'] == 'EXPANDED_TEXT_AD'
    assert state['ad_group_assets'][0]['asset'] == f'customers/{CID}/assets/4'
    if kind == 'campaign':
        assert state['ad_groups'][0]['resource_name'] == GROUP
        assert state['campaign_assets'][0]['asset'] == f'customers/{CID}/assets/4'
    assert draft(kind)['preview']['affected_children']['ads']['count'] == 1


@pytest.mark.parametrize('kind', ['campaign', 'ad_group'])
@pytest.mark.parametrize('field,value', [
    ('status', 'NONSENSE'), ('status', 'UNKNOWN'), ('status', 'UNSPECIFIED'),
    ('status', 'REMOVED'), ('status', None),
    ('ad_type', 'NOT_AN_AD'), ('ad_type', 'UNKNOWN'), ('ad_type', 'UNSPECIFIED'),
    ('ad_type', None), ('ad_type', 999),
])
def test_real_removal_refuses_unreadable_ad_before_draft(real_removal_population, kind,
                                                        field, value):
    ad = real_removal_population['ad_group_ad'][0]['ad_group_ad']
    if field == 'status':
        ad['status'] = value
    else:
        ad['ad']['type_'] = value
    with pytest.raises(rails.RailViolation, match='unreadable'):
        draft(kind)


@pytest.mark.parametrize('entity,field', [
    ('ad_group', 'status'), ('ad_group', 'type'),
    ('campaign_asset', 'status'), ('campaign_asset', 'field_type'),
    ('ad_group_asset', 'status'), ('ad_group_asset', 'field_type'),
])
def test_real_campaign_refuses_other_invalid_enum_strings(real_removal_population, entity, field):
    real_removal_population[entity][0][entity][field] = 'NONSENSE'
    with pytest.raises(rails.RailViolation, match='unreadable'):
        draft()


@pytest.mark.parametrize('kind,entity', [
    ('campaign', 'campaign_asset'), ('campaign', 'ad_group_asset'),
    ('ad_group', 'ad_group_asset'),
])
@pytest.mark.parametrize('asset', [
    'customers/9999999999/assets/4', 'malformed/4', f'customers/{CID}/campaigns/4',
    f'customers/{CID}/assets/5',
])
def test_real_removal_refuses_unreconciled_asset_before_draft(real_removal_population,
                                                             kind, entity, asset):
    real_removal_population[entity][0][entity]['asset'] = asset
    with pytest.raises(rails.RailViolation):
        draft(kind)


@pytest.mark.parametrize('arguments', [
    {'entity_type': 'campaign', 'entity_id': '088'},
    {'entity_type': 'campaign', 'entity_id': '88', 'customer_id': '01234567890'},
    {'entity_type': 'ad', 'entity_id': '66', 'ad_group_id': '077'},
])
@pytest.mark.parametrize('mcp', [False, True])
def test_leading_zero_inputs_refuse_before_reads_and_dispatch(removal, arguments, mcp):
    data, fake = removal
    if mcp:
        from mcp_google_ads_safe import app
        from tests.test_protocol_errors import boundary, payload
        out = payload(boundary(app.mcp, 'remove_entity', arguments))
        assert out['code'] == 'BAD_ID'
    else:
        with pytest.raises(rails.RailViolation, match='canonical'):
            tools.remove_entity(**arguments)
    assert data['reads'] == 0 and not fake.mutate_calls
