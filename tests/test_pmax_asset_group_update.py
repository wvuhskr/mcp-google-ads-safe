"""Offline proof for updating one paused PMax asset group's name and URL."""
import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import make_type
from tests.test_pmax_asset_group_creation import (
    _raw_reader_rows,
    parent_proof,
)

CID, CAMPAIGN_ID, GROUP_ID = '1234567890', '88', '90'
GROUP_RN = f'customers/{CID}/assetGroups/{GROUP_ID}'
CAMPAIGN_RN = f'customers/{CID}/campaigns/{CAMPAIGN_ID}'


def update_state():
    proof = parent_proof()
    proof.pop('images')
    target = {'resource_name': GROUP_RN, 'id': GROUP_ID, 'campaign': CAMPAIGN_RN,
              'name': 'Existing Group', 'status': 'PAUSED',
              'final_urls': ['https://example.com/old'], 'final_mobile_urls': [],
              'path1': '', 'path2': ''}
    inventory = [{key: target[key] for key in
                  ('resource_name', 'id', 'campaign', 'name', 'status')}]
    return {'parent_proof': proof, 'target': target, 'asset_groups': inventory}


@pytest.fixture
def asset_group_update(monkeypatch, fake_client):
    state = update_state()
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: copy.deepcopy(state), raising=False)
    return state, fake_client


def draft(**changes):
    values = {'asset_group_id': GROUP_ID, 'name': 'Renamed Group',
              'final_url': None, 'customer_id': CID}
    values.update(changes)
    return tools.update_asset_group(**values)


def plan_for(**changes):
    return rails._DRAFTS[draft(**changes)['draft_id']].plan


def test_rename_drafts_one_exact_paused_group_update(asset_group_update):
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    assert len(plan.operations) == 1
    assert plan.operations[0] == rails.MutationOp(
        'AssetGroupService',
        {'update': {'resource_name': GROUP_RN, 'name': 'Renamed Group'}},
        ['name'])
    assert pending['preview']['target_status'] == 'PAUSED'


def test_public_inventory_exposes_exact_optional_update_contract():
    async def check():
        inventory = {tool.name: tool for tool in await app.mcp.list_tools()}
        tool = inventory['update_asset_group']
        assert set(tool.input_schema['properties']) == {
            'asset_group_id', 'name', 'final_url', 'customer_id'}
        assert tool.input_schema['required'] == ['asset_group_id']
        for text in ('PAUSED', 'name', 'final URL', 'atomic', 'status', 'branding',
                     'assets', 'bidding', 'targeting', 'automation', 'offline'):
            assert text in tool.description

    asyncio.run(check())


@pytest.mark.parametrize(('changes', 'fields', 'mask'), [
    ({'name': 'Renamed Group'}, {'name': 'Renamed Group'}, ['name']),
    ({'name': None, 'final_url': 'https://example.com/new'},
     {'final_urls': ['https://example.com/new']}, ['final_urls']),
    ({'name': 'Renamed Group', 'final_url': 'https://example.com/new'},
     {'name': 'Renamed Group', 'final_urls': ['https://example.com/new']},
     ['name', 'final_urls']),
])
def test_name_url_and_combined_drafts_use_literal_masks(asset_group_update, changes, fields, mask):
    pending = draft(**changes)
    plan = rails._DRAFTS[pending['draft_id']].plan
    assert plan.operations[0].operation == {'update': {'resource_name': GROUP_RN, **fields}}
    assert plan.operations[0].update_mask == mask
    assert pending['preview']['update_mask'] == mask


@pytest.mark.parametrize('changes', [
    {'name': None, 'final_url': None},
    {'name': 'Existing Group'},
    {'name': None, 'final_url': 'https://example.com/old'},
    {'name': 'Renamed Group', 'final_url': 'https://example.com/old'},
    {'name': 'Existing Group', 'final_url': 'https://example.com/new'},
    {'name': ''}, {'name': ' '}, {'name': 'null'},
    {'name': None, 'final_url': ''}, {'name': None, 'final_url': 'null'},
    {'name': None, 'final_url': 'http://example.com/new'},
    {'name': None, 'final_url': 'https://other.example/new'},
])
def test_empty_unchanged_blank_null_and_off_domain_requests_refuse(asset_group_update, changes):
    with pytest.raises(rails.RailViolation):
        draft(**changes)


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 123), ('customer_id', True), ('customer_id', []),
    ('asset_group_id', 90), ('asset_group_id', True), ('asset_group_id', '090'),
    ('name', 4), ('name', False), ('name', []), ('name', {}),
    ('final_url', 4), ('final_url', False), ('final_url', []), ('final_url', {}),
])
def test_direct_required_and_optional_input_types_refuse_before_read(monkeypatch, field, value):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: pytest.fail('safety read reached'), raising=False)
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 123), ('customer_id', True), ('customer_id', []),
    ('customer_id', 'null'), ('asset_group_id', 90), ('asset_group_id', True),
    ('asset_group_id', []), ('name', 4), ('name', False), ('name', []),
    ('name', 'null'), ('final_url', 4), ('final_url', False),
    ('final_url', []), ('final_url', 'null'),
])
def test_actual_mcp_rejects_coerced_and_literal_null_originals(asset_group_update, field, value):
    from tests.test_protocol_errors import boundary, payload
    args = {'asset_group_id': GROUP_ID, 'name': 'Renamed Group',
            'final_url': None, 'customer_id': CID, field: value}
    assert payload(boundary(app.mcp, 'update_asset_group', args))['code'] == 'BAD_INPUT'


def test_default_off_refuses_direct_and_actual_mcp_before_read_or_provider(monkeypatch):
    from tests.test_protocol_errors import boundary, payload
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: pytest.fail('safety read reached'), raising=False)
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider construction reached'))
    with pytest.raises(rails.RailViolation) as direct:
        draft()
    assert direct.value.code == 'WRITES_DISABLED'
    result = payload(boundary(app.mcp, 'update_asset_group', {
        'asset_group_id': GROUP_ID, 'name': 'Renamed Group', 'customer_id': CID}))
    assert result['code'] == 'WRITES_DISABLED'


@pytest.mark.parametrize('mode', ['read', 'write'])
def test_draft_requires_read_and_write_allowlists_before_state_read(monkeypatch, mode):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999' if mode == 'read' else f'{CID},999')
    monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: pytest.fail('allowlist must precede state read'), raising=False)
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'NOT_ALLOWLISTED'


@pytest.mark.parametrize('gate', ['writes', 'write', 'read'])
def test_all_gates_repeat_before_confirmation_read(asset_group_update, monkeypatch, gate):
    _, fake = asset_group_update
    pending = draft()
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'write':
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: pytest.fail('gate must precede read'))
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize('part', ['account', 'parent', 'branding', 'target', 'siblings'])
def test_account_parent_brand_target_and_sibling_drift_refuse_unconsumed(
        asset_group_update, part):
    state, fake = asset_group_update
    pending = draft()
    if part == 'account':
        state['parent_proof']['account']['currency_code'] = 'EUR'
    elif part == 'parent':
        state['parent_proof']['parent']['name'] = 'Changed Parent'
    elif part == 'branding':
        state['parent_proof']['branding'][0]['content'] = 'Changed Brand'
    elif part == 'target':
        state['target']['final_urls'] = ['https://example.com/drift']
    else:
        sibling = dict(state['asset_groups'][0])
        sibling.update(resource_name=f'customers/{CID}/assetGroups/91', id='91', name='Sibling')
        state['asset_groups'].append(sibling)
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(pending['draft_id'])
    assert exc.value.code == 'STATE_DRIFT'
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


def test_real_v25_serialization_is_one_update_without_status_parent_or_assets(
        asset_group_update, fake_gads):
    plan = plan_for(name='Renamed Group', final_url='https://example.com/new')
    context = client.validate_mutation_plan(plan)
    built = client._build_mutate_operation(fake_gads, plan.operations[0], context)
    update = built.asset_group_operation.update
    assert {field.name for field, _ in update._pb.ListFields()} == {
        'resource_name', 'name', 'final_urls'}
    assert update.resource_name == GROUP_RN and update.name == 'Renamed Group'
    assert list(update.final_urls) == ['https://example.com/new']
    assert list(built.asset_group_operation.update_mask.paths) == ['name', 'final_urls']
    assert update.status.name == 'UNSPECIFIED' and update.campaign == ''


@pytest.mark.parametrize('damage', [
    'service', 'action', 'mask', 'extra_mask', 'hidden_field', 'target',
    'parent', 'descriptor', 'proof', 'before', 'expected',
])
def test_operation_and_proof_tampering_refuse_before_provider(
        asset_group_update, fake_gads, damage):
    plan = copy.deepcopy(plan_for())
    op, check = plan.operations[0], plan.post_checks[0]
    if damage == 'service':
        object.__setattr__(op, 'service', 'CampaignService')
    elif damage == 'action':
        op.operation['create'] = op.operation.pop('update')
    elif damage == 'mask':
        op.update_mask[:] = ['final_urls']
    elif damage == 'extra_mask':
        op.update_mask.append('status')
    elif damage == 'hidden_field':
        op.operation['update']['status'] = 'PAUSED'
    elif damage == 'target':
        op.operation['update']['resource_name'] = f'customers/{CID}/assetGroups/91'
    elif damage == 'parent':
        check['campaign'] = f'customers/{CID}/campaigns/99'
    elif damage == 'descriptor':
        check['extra'] = True
    elif damage == 'proof':
        check['parent_proof']['parent']['brand_guidelines_enabled'] = False
    elif damage == 'before':
        check['before']['status'] = 'ENABLED'
    else:
        check['expected']['campaign'] = f'customers/{CID}/campaigns/99'
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert fake_gads.mutate_calls == []


def test_stored_draft_tampering_is_consumed_before_dispatch(asset_group_update):
    _, fake = asset_group_update
    pending = draft()
    rails._DRAFTS[pending['draft_id']].plan.operations[0].operation['update']['status'] = 'PAUSED'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(pending['draft_id'])
    assert exc.value.code == 'PLAN_TAMPERED'
    assert pending['draft_id'] not in rails._DRAFTS and fake.dispatch_calls == []


def test_asset_group_update_has_no_generic_plan_bypass(fake_gads):
    plan = rails.EntityMutationPlan(CID, [rails.MutationOp(
        'AssetGroupService', {'update': {'resource_name': GROUP_RN,
                                         'name': 'Bypass'}}, ['name'])], True)
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert fake_gads.mutate_calls == []


def _target_row(**changes):
    row = make_type('GoogleAdsRow')
    values = {'resource_name': GROUP_RN, 'id': GROUP_ID, 'campaign': CAMPAIGN_RN,
              'name': 'Existing Group', 'status': 'PAUSED',
              'final_urls': ['https://example.com/old'], 'final_mobile_urls': [],
              'path1': '', 'path2': ''}
    values.update(changes)
    row.asset_group = values
    return row


def _reader(monkeypatch, *, targets=None, groups=None, campaign=None, brands=None,
            proposed_name='Renamed Group'):
    default_campaign, default_brands = _raw_reader_rows()
    targets = [_target_row()] if targets is None else targets
    groups = copy.deepcopy(targets) if groups is None else groups
    campaign = default_campaign if campaign is None else campaign
    brands = default_brands if brands is None else brands
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    proof = parent_proof()
    monkeypatch.setattr(client, '_creation_account',
                        lambda *a, **kw: copy.deepcopy(proof['account']))
    monkeypatch.setattr(client, 'pmax_image_state', lambda *a: pytest.fail(
        'update reader must not scan marketing images'))

    def scan(query, cid):
        if 'FROM campaign_asset' in query:
            return brands
        if 'FROM campaign WHERE' in query:
            return [campaign]
        if 'WHERE asset_group.id =' in query:
            return targets
        if 'FROM asset_group' in query:
            return groups
        pytest.fail(f'unexpected reader query: {query}')

    monkeypatch.setattr(client, '_scan_rows', scan)
    return lambda: client.pmax_asset_group_update_state(CID, GROUP_ID, proposed_name)


def test_populated_target_defaults_parent_brand_and_inventory_are_read_without_images(monkeypatch):
    state = _reader(monkeypatch)()
    assert state['target'] == update_state()['target']
    assert state['parent_proof'] == update_state()['parent_proof']
    assert state['asset_groups'] == update_state()['asset_groups']


@pytest.mark.parametrize('damage', [
    'missing', 'duplicate', 'sparse', 'foreign_resource', 'wrong_id', 'foreign_parent',
    'enabled', 'unknown_status', 'missing_status', 'no_url', 'two_urls', 'off_domain',
    'mobile_url', 'path1', 'path2',
])
def test_target_reader_refuses_missing_foreign_malformed_or_alternate_destination(
        monkeypatch, damage):
    row = _target_row()
    targets = [row]
    if damage == 'missing':
        targets = []
    elif damage == 'duplicate':
        targets.append(copy.deepcopy(row))
    elif damage == 'sparse':
        targets = [type(row).to_dict(row)]
    elif damage == 'foreign_resource':
        row.asset_group.resource_name = 'customers/999/assetGroups/90'
    elif damage == 'wrong_id':
        row.asset_group.id = 91
    elif damage == 'foreign_parent':
        row.asset_group.campaign = 'customers/999/campaigns/88'
    elif damage == 'enabled':
        row.asset_group.status = 'ENABLED'
    elif damage == 'unknown_status':
        row.asset_group.status = 'UNKNOWN'
    elif damage == 'missing_status':
        row.asset_group._pb.ClearField('status')
    elif damage == 'no_url':
        row.asset_group.final_urls = []
    elif damage == 'two_urls':
        row.asset_group.final_urls = ['https://example.com/one', 'https://example.com/two']
    elif damage == 'off_domain':
        row.asset_group.final_urls = ['https://other.example/old']
    elif damage == 'mobile_url':
        row.asset_group.final_mobile_urls = ['https://example.com/mobile']
    elif damage == 'path1':
        row.asset_group.path1 = 'old'
    else:
        row.asset_group.path2 = 'old'
    with pytest.raises(rails.RailViolation):
        _reader(monkeypatch, targets=targets, groups=[])()


@pytest.mark.parametrize('damage', [
    'collision', 'self_exclusion', 'missing_target', 'duplicate', 'foreign_parent',
    'blank_name', 'unknown_status', 'target_disagreement',
])
def test_complete_sibling_inventory_collision_exclusion_and_integrity(monkeypatch, damage):
    target = _target_row()
    sibling = _target_row(resource_name=f'customers/{CID}/assetGroups/91', id='91',
                          name='Sibling')
    groups, proposed, accepted = [target, sibling], 'Renamed Group', False
    if damage == 'collision':
        sibling.asset_group.name = proposed
    elif damage == 'self_exclusion':
        proposed, accepted = 'Existing Group', True
    elif damage == 'missing_target':
        groups = [sibling]
    elif damage == 'duplicate':
        groups.append(copy.deepcopy(sibling))
    elif damage == 'foreign_parent':
        sibling.asset_group.campaign = f'customers/{CID}/campaigns/99'
    elif damage == 'blank_name':
        sibling.asset_group.name = ''
    elif damage == 'unknown_status':
        sibling.asset_group.status = 'UNKNOWN'
    else:
        target.asset_group.name = 'Inventory Disagrees'
    call = _reader(monkeypatch, targets=[_target_row()], groups=groups,
                   proposed_name=proposed)
    if accepted:
        assert len(call()['asset_groups']) == 2
    else:
        with pytest.raises(rails.RailViolation):
            call()


@pytest.mark.parametrize('damage', [
    'enabled_parent', 'search_parent', 'retail_parent', 'missing_brand', 'unknown_brand_status',
])
def test_update_reader_reuses_strict_parent_and_brand_failures(monkeypatch, damage):
    campaign, brands = _raw_reader_rows('shopping' if damage == 'retail_parent' else None)
    if damage == 'enabled_parent':
        campaign.campaign.status = 'ENABLED'
    elif damage == 'search_parent':
        campaign.campaign.advertising_channel_type = 'SEARCH'
    elif damage == 'missing_brand':
        brands.pop()
    elif damage == 'unknown_brand_status':
        brands[0].campaign_asset.status = 'UNKNOWN'
    with pytest.raises(rails.RailViolation):
        _reader(monkeypatch, campaign=campaign, brands=brands)()


@pytest.mark.parametrize('damage', ['missing_image', 'image_disagreement'])
def test_creation_refactor_keeps_two_equal_image_proofs(monkeypatch, damage):
    from tests.test_pmax_asset_group_creation import ARGS as CREATE_ARGS
    proof = parent_proof()
    state = {'parent_proof': proof, 'asset_groups': []}
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, 'pmax_asset_group_state', lambda *a: copy.deepcopy(state))
    plan = rails._DRAFTS[tools.create_asset_group(**CREATE_ARGS)['draft_id']].plan
    if damage == 'missing_image':
        plan.post_checks[0]['parent_proof']['images'].pop('MARKETING_IMAGE')
    else:
        plan.post_checks[0]['parent_proof']['images']['MARKETING_IMAGE']['image_asset']['file_size'] = 1
    with pytest.raises(rails.RailViolation):
        client.validate_mutation_plan(plan)


@pytest.mark.parametrize('damage', ['kind', 'owner', 'resource_name', 'count', 'validate_only'])
def test_result_identity_refuses_before_saved_reads(asset_group_update, monkeypatch, damage):
    plan = plan_for()
    result = {'results': [{'type': 'asset_group_result', 'resource_name': GROUP_RN}]}
    if damage == 'kind':
        result['results'][0]['type'] = 'campaign_result'
    elif damage == 'owner':
        result['results'][0]['resource_name'] = 'customers/999/assetGroups/90'
    elif damage == 'resource_name':
        result['results'][0]['resource_name'] = f'customers/{CID}/assetGroups/91'
    elif damage == 'count':
        result['results'].append(copy.deepcopy(result['results'][0]))
    else:
        result['validate_only'] = True
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: pytest.fail('saved read reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


def _saved_apply(asset_group_update, monkeypatch, damage=None):
    state, fake = asset_group_update
    pending = draft(name='Renamed Group', final_url='https://example.com/new')
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result = {'results': [
        {'type': 'asset_group_result', 'resource_name': GROUP_RN}], 'request_id': None}

    def dispatch(sent):
        fake.dispatch_calls.append(sent)
        state['target'] = copy.deepcopy(plan.post_checks[0]['expected'])
        state['asset_groups'][0].update(name=state['target']['name'])
        if damage == 'changed_name':
            state['target']['name'] = 'Wrong Saved Name'
        elif damage == 'changed_url':
            state['target']['final_urls'] = ['https://example.com/wrong']
        elif damage == 'status':
            state['target']['status'] = 'ENABLED'
        elif damage == 'parent':
            state['target']['campaign'] = f'customers/{CID}/campaigns/99'
        elif damage == 'mobile':
            state['target']['final_mobile_urls'] = ['https://example.com/mobile']
        elif damage == 'path':
            state['target']['path1'] = 'hidden'
        elif damage == 'parent_proof':
            state['parent_proof']['parent']['name'] = 'Changed Parent'
        elif damage == 'brand':
            state['parent_proof']['branding'][0]['content'] = 'Changed Brand'
        elif damage == 'collision':
            sibling = dict(state['asset_groups'][0])
            sibling.update(resource_name=f'customers/{CID}/assetGroups/91', id='91')
            state['asset_groups'].append(sibling)
        return copy.deepcopy(fake.dispatch_result)

    monkeypatch.setattr(client, '_dispatch', dispatch)
    return pending, fake


def test_exact_saved_update_and_unchanged_fields_verify(asset_group_update, monkeypatch):
    pending, fake = _saved_apply(asset_group_update, monkeypatch)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is True
    assert 'saved target fields, parent branding and sibling-name uniqueness' in result[
        'verification_scope']
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', [
    'changed_name', 'changed_url', 'status', 'parent', 'mobile', 'path', 'parent_proof',
    'brand', 'collision', 'scan_failure',
])
def test_saved_mismatch_parent_drift_collision_and_scan_failure_consume_without_retry(
        asset_group_update, monkeypatch, damage):
    state, _ = asset_group_update
    if damage == 'scan_failure':
        original = client.pmax_asset_group_update_state
        landed = {'value': False}

        def reader(*args):
            if landed['value']:
                raise rails.RailViolation('post-write sibling scan failed')
            return original(*args)

        monkeypatch.setattr(client, 'pmax_asset_group_update_state', reader)
        pending = draft(name='Renamed Group', final_url='https://example.com/new')
        fake = asset_group_update[1]
        fake.dispatch_result = {'results': [
            {'type': 'asset_group_result', 'resource_name': GROUP_RN}], 'request_id': None}

        def dispatch(sent):
            fake.dispatch_calls.append(sent)
            landed['value'] = True
            return copy.deepcopy(fake.dispatch_result)

        monkeypatch.setattr(client, '_dispatch', dispatch)
    else:
        pending, fake = _saved_apply(asset_group_update, monkeypatch, damage)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_verification_and_audit_failure_still_consumes_without_retry(
        asset_group_update, monkeypatch):
    pending, fake = _saved_apply(asset_group_update, monkeypatch, 'changed_name')
    original = rails.audit.log_event

    def fail_apply_audit(tool, phase, data):
        if phase == 'error' and data.get('applied') is True:
            raise OSError('synthetic apply audit failure')
        return original(tool, phase, data)

    monkeypatch.setattr(rails.audit, 'log_event', fail_apply_audit)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    assert result['audit_error'] == 'synthetic apply audit failure'
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_validation_only_dispatch_never_reads_or_claims_saved_update(
        asset_group_update, fake_gads, monkeypatch):
    response = make_type('MutateGoogleAdsResponse')
    fake_gads.mutate_response = response
    plan = plan_for()
    result = client._dispatch_entity(plan, True)
    assert result['validate_only'] is True and len(fake_gads.mutate_calls) == 1
    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: pytest.fail('saved read reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


def test_unknown_transport_consumes_without_retry(asset_group_update):
    from tests.test_final_fixes import mapped_error
    _, fake = asset_group_update
    pending = draft()
    fake.dispatch_error = mapped_error('remapped')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(pending['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1
