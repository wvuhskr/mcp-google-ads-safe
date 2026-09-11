"""Offline removal coverage using real installed Google Ads v25 messages."""
import copy
import json
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

CAMPAIGN = f'customers/{CID}/campaigns/88'
GROUP = f'customers/{CID}/adGroups/77'
FAMILIES = {'SITELINK': ('13', {'base': ('Link', '', '', ('https://example.com',)), 'plain': True}),
            'CALLOUT': ('11', {'base': 'Call us', 'plain': True}),
            'STRUCTURED_SNIPPET': ('12', {'base': ('Services', ('Repair', 'Install')), 'plain': True})}


@pytest.fixture
def removal(monkeypatch, fake_gads):
    data = {'status': 'PAUSED', 'post': [], 'reads': 0, 'post_reads': 0}

    def state(cid, target_type, target_id, family):
        assert cid == CID
        data['reads'] += 1
        tid = '88' if target_type == 'campaign' else '77'
        assert target_id == tid
        number, content = FAMILIES[family]
        parent = {'resource_name': CAMPAIGN if target_type == 'campaign' else GROUP,
                  'status': 'ENABLED'}
        if target_type == 'ad_group':
            parent.update(type_='SEARCH_STANDARD', campaign=CAMPAIGN)
        kind = 'campaignAssets' if target_type == 'campaign' else 'adGroupAssets'
        return {'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'},
                'parent': parent,
                'campaign': {'resource_name': CAMPAIGN},
                'links': [{'resource_name': f'customers/{CID}/{kind}/{tid}~9~{number}',
                           'status': data['status'], 'asset': f'customers/{CID}/assets/9',
                           'content': copy.deepcopy(content)}]}

    monkeypatch.setattr(client, 'sitelink_state', lambda c, t, i: state(c, t, i, 'SITELINK'))
    monkeypatch.setattr(client, 'callout_state', lambda c, t, i: state(c, t, i, 'CALLOUT'))
    monkeypatch.setattr(client, 'structured_snippet_state',
                        lambda c, t, i: state(c, t, i, 'STRUCTURED_SNIPPET'))
    def post_read(query, cid):
        data['post_reads'] += 1
        return copy.deepcopy(data['post'])

    monkeypatch.setattr(client, 'gaql_all', post_read)
    return data, fake_gads


def draft(family='SITELINK', target='campaign'):
    return tools.remove_extension(asset_id='9', extension_type=family,
                                  **{target + '_id': '88' if target == 'campaign' else '77'})


def result_for(fake, target, family):
    number = FAMILIES[family][0]
    prefix = 'campaign' if target == 'campaign' else 'ad_group'
    kind = 'campaignAssets' if target == 'campaign' else 'adGroupAssets'
    tid = '88' if target == 'campaign' else '77'
    rn = f'customers/{CID}/{kind}/{tid}~9~{number}'
    response = make_type('MutateGoogleAdsResponse')
    response.mutate_operation_responses.append(
        {prefix + '_asset_result': {'resource_name': rn}})
    fake.mutate_response = response
    return rn


@pytest.mark.parametrize('family', FAMILIES)
@pytest.mark.parametrize('target', ['campaign', 'ad_group'])
@pytest.mark.parametrize('status', ['ENABLED', 'PAUSED'])
def test_all_families_targets_and_statuses_serialize_one_remove(removal, family, target, status):
    data, fake = removal
    data['status'] = status
    d = draft(family, target)
    rn = result_for(fake, target, family)
    result = rails.apply_draft(d['draft_id'])
    assert result['verified'] is True
    call = fake.mutate_calls[0]
    assert len(call['operations']) == 1 and call['partial_failure'] is False
    operation = call['operations'][0]
    actual = (operation.campaign_asset_operation.remove if target == 'campaign'
              else operation.ad_group_asset_operation.remove)
    assert actual == rn
    assert d['preview']['association'] == rn
    assert 'underlying asset' in ' '.join(d['preview']['warnings'])


@pytest.mark.parametrize('changes,reason', [
    ({'asset_id': 9}, 'positive'), ({'asset_id': '0'}, 'invalid numeric'),
    ({'asset_id': 'customers/1/assets/9'}, 'invalid numeric'),
    ({'extension_type': 1}, 'must be a string'),
    ({'extension_type': 'sitelink'}, 'must be SITELINK'),
    ({'campaign_id': None}, 'exactly one'),
    ({'ad_group_id': '77'}, 'exactly one'),
])
def test_exact_input_refusals(removal, changes, reason):
    args = {'asset_id': '9', 'extension_type': 'SITELINK', 'campaign_id': '88'}
    args.update(changes)
    with pytest.raises(rails.RailViolation, match=reason):
        tools.remove_extension(**args)


def test_missing_ambiguous_and_unknown_status_refuse_without_mutation(removal, monkeypatch):
    data, fake = removal
    original = client.sitelink_state
    monkeypatch.setattr(client, 'sitelink_state', lambda *args: {**original(*args), 'links': []})
    with pytest.raises(rails.RailViolation, match='missing, removed, or ambiguous'):
        draft()
    state = original(CID, 'campaign', '88')
    monkeypatch.setattr(client, 'sitelink_state', lambda *args: {**state, 'links': state['links'] * 2})
    with pytest.raises(rails.RailViolation, match='ambiguous'):
        draft()
    data['status'] = 'UNKNOWN'
    monkeypatch.setattr(client, 'sitelink_state', original)
    with pytest.raises(rails.RailViolation, match='identity or status'):
        draft()
    assert not fake.mutate_calls


def test_existing_broader_content_and_blocked_term_remain_removable(removal, monkeypatch):
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ('call us',))
    assert draft('CALLOUT')['dry_run'] is True


def test_write_and_read_allowlists_are_checked_before_state_reads(removal, monkeypatch):
    data, fake = removal
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},9999999999')
    monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '9999999999')
    with pytest.raises(rails.RailViolation, match='not allowlisted for write'):
        draft()
    assert data['reads'] == 0 and not fake.mutate_calls


@pytest.mark.parametrize('post_status,verified', [(None, True), ('REMOVED', True),
                                                   ('PAUSED', False), ('UNKNOWN', False)])
def test_postcheck_absent_removed_or_failure_consumes_draft(removal, post_status, verified):
    data, fake = removal
    d = draft()
    rn = result_for(fake, 'campaign', 'SITELINK')
    if post_status is not None:
        data['post'] = [{'campaign_asset': {'resource_name': rn, 'campaign': CAMPAIGN,
                         'asset': f'customers/{CID}/assets/9', 'field_type': 'SITELINK',
                         'status': post_status}}]
    result = rails.apply_draft(d['draft_id'])
    assert result['verified'] is verified
    assert d['draft_id'] not in rails._DRAFTS


@pytest.mark.parametrize('entry', [[], [{'campaign_asset_result': {'resource_name': 'bad'}}],
                                        [{'asset_result': {'resource_name': 'bad'}}],
                                        [{'campaign_asset_result': {'resource_name':
                                          f'customers/{CID}/campaignAssets/88~9~13'}}] * 2])
def test_bad_result_refuses_before_readback_and_consumes(removal, entry):
    data, fake = removal
    d = draft()
    response = make_type('MutateGoogleAdsResponse')
    response.mutate_operation_responses.extend(entry)
    fake.mutate_response = response
    reads_before_apply = data['reads']
    result = rails.apply_draft(d['draft_id'])
    assert result['verified'] is False
    assert data['reads'] == reads_before_apply + 1  # apply-time drift read only
    assert data['post_reads'] == 0
    assert d['draft_id'] not in rails._DRAFTS


def test_dispatch_rejects_mixed_or_malformed_remove_before_provider(removal):
    _, fake = removal
    rn = f'customers/{CID}/campaignAssets/88~9~13'
    remove = rails.MutationOp('CampaignAssetService', {'remove': rn}, None)
    create = rails.safe_create_operation('AssetService', {
        'resource_name': f'customers/{CID}/assets/-1', 'callout_asset': {'callout_text': 'x'}})
    with pytest.raises(rails.RailViolation, match='standalone'):
        client._dispatch(rails.EntityMutationPlan(CID, [remove, create], True))
    bad = rails.MutationOp('CampaignAssetService', {'remove': rn + '~1'}, None)
    with pytest.raises(rails.RailViolation, match='invalid supported'):
        client._dispatch(rails.EntityMutationPlan(CID, [bad], True))
    assert not fake.mutate_calls


def test_validation_only_serializes_without_saved_state_read(removal):
    data, fake = removal
    d = draft()
    result_for(fake, 'campaign', 'SITELINK')
    before = data['reads']
    result = client._dispatch(rails._DRAFTS[d['draft_id']].plan, validate_only=True)
    assert result['validate_only'] is True and data['reads'] == before
    assert data['post_reads'] == 0


def test_postcheck_read_error_is_unverified_and_consumes(removal, monkeypatch):
    _, fake = removal
    d = draft()
    result_for(fake, 'campaign', 'SITELINK')
    monkeypatch.setattr(client, 'removed_association_state',
                        lambda check: (_ for _ in ()).throw(RuntimeError('read failed')))
    result = rails.apply_draft(d['draft_id'])
    assert result['verified'] is False and 'read failed' in result['error']
    assert d['draft_id'] not in rails._DRAFTS


@pytest.fixture
def raw_removal(monkeypatch, fake_gads):
    data = {
        'campaign': {'resource_name': CAMPAIGN, 'name': 'Search', 'status': 'ENABLED',
                     'advertising_channel_type': 'SEARCH',
                     'advertising_channel_sub_type': 'UNSPECIFIED',
                     'campaign_budget': f'customers/{CID}/campaignBudgets/5'},
        'ad_group': {'resource_name': GROUP, 'status': 'PAUSED', 'type_': 'SEARCH_STANDARD',
                     'campaign': CAMPAIGN},
        'links': [], 'queries': [],
    }

    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        data['queries'].append(query)
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


def raw_link(family, target='campaign', asset_id='9', status='PAUSED', broader=True):
    number, _ = FAMILIES[family]
    tid = '88' if target == 'campaign' else '77'
    kind = 'campaignAssets' if target == 'campaign' else 'adGroupAssets'
    entity = target + '_asset'
    asset = {'resource_name': f'customers/{CID}/assets/{asset_id}', 'type_': family}
    if family == 'SITELINK':
        asset.update(final_urls=['https://example.com', 'https://example.org'] if broader
                     else ['https://example.com'],
                     sitelink_asset={'link_text': 'Localized link',
                                     'start_date': '2026-01-01' if broader else ''})
    elif family == 'CALLOUT':
        asset['callout_asset'] = {'callout_text': 'Localized callout',
                                  'ad_schedule_targets': [{'day_of_week': 'MONDAY'}]
                                  if broader else []}
    else:
        asset.update(tracking_url_template='{lpurl}' if broader else '',
                     structured_snippet_asset={
                         'header': 'Dienstleistungen', 'values': ['Reparatur', 'Wartung']})
    return {entity: {'resource_name': f'customers/{CID}/{kind}/{tid}~{asset_id}~{number}',
                     target: CAMPAIGN if target == 'campaign' else GROUP,
                     'asset': asset['resource_name'], 'field_type': family, 'status': status},
            'asset': asset}


@pytest.mark.parametrize('family', FAMILIES)
@pytest.mark.parametrize('target', ['campaign', 'ad_group'])
def test_real_family_readers_accept_broader_localized_selected_content(raw_removal, family, target):
    data, _ = raw_removal
    data['links'] = [raw_link(family, target)]
    d = draft(family, target)
    fingerprint = rails._DRAFTS[d['draft_id']].fingerprint
    assert fingerprint['links'][0]['content']['plain'] is False
    assert d['preview']['content'] == fingerprint['links'][0]['content']
    if target == 'ad_group':
        assert fingerprint['campaign']['resource_name'] == CAMPAIGN
    assert any('status != \'REMOVED\'' in query for query in data['queries'])


@pytest.mark.parametrize('change', ['parent', 'campaign', 'selected_content',
                                     'selected_status', 'inventory'])
def test_real_reader_state_drift_refuses_before_dispatch_and_keeps_draft(raw_removal, change):
    data, fake = raw_removal
    data['links'] = [raw_link('SITELINK', 'ad_group')]
    d = draft('SITELINK', 'ad_group')
    if change == 'parent':
        data['ad_group']['status'] = 'ENABLED'
    elif change == 'campaign':
        data['campaign']['name'] = 'Changed'
    elif change == 'selected_content':
        data['links'][0]['asset']['sitelink_asset']['link_text'] = 'Changed'
    elif change == 'selected_status':
        data['links'][0]['ad_group_asset']['status'] = 'ENABLED'
    else:
        data['links'].append(raw_link('SITELINK', 'ad_group', asset_id='10'))
    with pytest.raises(rails.RailViolation, match='account changed'):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.mutate_calls


@pytest.mark.parametrize('gate', ['writes', 'read'])
def test_all_pre_read_gates_refuse_without_draft(raw_removal, monkeypatch, gate):
    data, fake = raw_removal
    data['links'] = [raw_link('SITELINK')]
    before = set(rails._DRAFTS)
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
        reason = 'mutating tools are disabled'
    else:
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '9999999999')
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', CID)
        reason = 'not in the read allowlist'
    with pytest.raises(rails.RailViolation, match=reason):
        draft()
    assert not data['queries'] and set(rails._DRAFTS) == before and not fake.mutate_calls


@pytest.mark.parametrize('customer_id,reason', [('', 'invalid numeric ID'),
                                                ('abc', 'invalid numeric ID'),
                                                (False, 'positive numeric string'),
                                                (0, 'positive numeric string')])
def test_explicit_invalid_customer_is_audited_without_fallback_or_reads(
        removal, customer_id, reason):
    data, fake = removal
    before = set(rails._DRAFTS)
    with pytest.raises(rails.RailViolation, match=reason) as exc:
        tools.remove_extension('9', 'SITELINK', campaign_id='88', customer_id=customer_id)
    assert data['reads'] == 0 and set(rails._DRAFTS) == before and not fake.mutate_calls
    event = json.loads(Path(audit.AUDIT_PATH).read_text().splitlines()[-1])
    assert event['tool'] == 'remove_extension' and event['phase'] == 'refused'
    assert event['reason'] == str(exc.value)


def test_empty_customer_refuses_through_public_mcp_boundary(removal):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary, payload

    data, fake = removal
    before = set(rails._DRAFTS)
    result = payload(boundary(app.mcp, 'remove_extension', {
        'asset_id': '9', 'extension_type': 'SITELINK', 'campaign_id': '88',
        'customer_id': ''}))
    assert result['code'] == 'BAD_ID'
    assert "invalid numeric ID ''" in result['reason']
    assert data['reads'] == 0 and set(rails._DRAFTS) == before and not fake.mutate_calls
    event = json.loads(Path(audit.AUDIT_PATH).read_text().splitlines()[-1])
    assert event['tool'] == 'remove_extension' and event['phase'] == 'refused'
    assert event['reason'] == result['reason']


def removal_check():
    rn = f'customers/{CID}/campaignAssets/88~9~13'
    return {'removal': True, 'entity_type': 'campaign_asset', 'customer_id': CID,
            'resource_name': rn, 'target_id': '88', 'target_resource_name': CAMPAIGN,
            'asset_id': '9', 'field_type': 'SITELINK'}


@pytest.mark.parametrize('change,reason', [
    ('resource', 'identity'), ('target', 'identity'), ('asset', 'identity'),
    ('type', 'identity'), ('duplicate', 'ambiguous'), ('active', 'remains active'),
])
def test_postread_wrong_identity_duplicate_and_active_refuse(monkeypatch, change, reason):
    check = removal_check()
    row = {'resource_name': check['resource_name'], 'campaign': CAMPAIGN,
           'asset': f'customers/{CID}/assets/9', 'field_type': 'SITELINK', 'status': 'REMOVED'}
    rows = [{'campaign_asset': row}]
    if change == 'resource':
        row['resource_name'] = f'customers/{CID}/campaignAssets/88~10~13'
    elif change == 'target':
        row['campaign'] = f'customers/{CID}/campaigns/99'
    elif change == 'asset':
        row['asset'] = f'customers/{CID}/assets/10'
    elif change == 'type':
        row['field_type'] = 'CALLOUT'
    elif change == 'duplicate':
        rows *= 2
    else:
        row['status'] = 'ENABLED'
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid: rows)
    with pytest.raises(rails.RailViolation, match=reason):
        client.verify_removed_result([check], {'results': [{
            'type': 'campaign_asset_result', 'resource_name': check['resource_name']}]})


def test_postread_incomplete_scan_refuses(monkeypatch):
    def incomplete(query, cid):
        raise rails.RailViolation('internal scan incomplete', code='SCAN_INCOMPLETE')
    monkeypatch.setattr(client, 'gaql_all', incomplete)
    check = removal_check()
    with pytest.raises(rails.RailViolation, match='scan incomplete'):
        client.verify_removed_result([check], {'results': [{
            'type': 'campaign_asset_result', 'resource_name': check['resource_name']}]})


def test_multiple_results_refuse_before_postread(removal):
    data, _ = removal
    check = removal_check()
    entry = {'type': 'campaign_asset_result', 'resource_name': check['resource_name']}
    with pytest.raises(rails.RailViolation, match='count does not reconcile'):
        client.verify_removed_result([check], {'results': [entry, entry]})
    assert data['post_reads'] == 0


@pytest.mark.parametrize('operation,reason', [
    ({'remove': f'customers/{CID}/campaignAssets/88~-9~13'}, 'invalid supported'),
    ({'remove': 'customers/9999999999/campaignAssets/88~9~13'}, 'invalid supported'),
    ({'remove': f'customers/{CID}/campaignAssets/88~9~10'}, 'invalid supported'),
    ({'remove': f'customers/{CID}/campaignAssets/88~9~13', 'hidden': True}, 'exactly one'),
])
def test_dispatch_closed_remove_shapes_fail_before_provider(removal, monkeypatch, operation, reason):
    _, fake = removal
    monkeypatch.setattr(client, 'gads', lambda: (_ for _ in ()).throw(AssertionError('provider built')))
    op = rails.MutationOp('CampaignAssetService', operation, None)
    with pytest.raises(rails.RailViolation, match=reason):
        client._dispatch(rails.EntityMutationPlan(CID, [op], True))
    assert not fake.mutate_calls


def test_dispatch_remove_rejects_mask_and_batch_before_provider(removal, monkeypatch):
    _, fake = removal
    monkeypatch.setattr(client, 'gads', lambda: (_ for _ in ()).throw(AssertionError('provider built')))
    rn = f'customers/{CID}/campaignAssets/88~9~13'
    masked = rails.MutationOp('CampaignAssetService', {'remove': rn}, ['status'])
    with pytest.raises(rails.RailViolation, match='only updates accept a mask'):
        client._dispatch(rails.EntityMutationPlan(CID, [masked], True))
    batch = [rails.MutationOp('CampaignAssetService', {'remove': rn}, None)] * 2
    with pytest.raises(rails.RailViolation, match='standalone'):
        client._dispatch(rails.EntityMutationPlan(CID, batch, True))
    assert not fake.mutate_calls
