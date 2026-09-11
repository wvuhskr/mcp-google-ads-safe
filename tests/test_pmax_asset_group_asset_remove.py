"""Offline proof for removing one paused PMax asset-group connection."""
import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import make_type
from tests.test_pmax_asset_group_assets import (
    CID,
    GROUP_ID,
    GROUP_RN,
    _complete_proofs,
    add_state,
)

_REAL_REMOVED_STATE = client.pmax_asset_group_asset_removed_state


def _links():
    proofs = _complete_proofs()
    proofs.append({'asset': f'customers/{CID}/assets/9', 'field_type': 'HEADLINE',
                   'content': 'Fourth headline'})
    return sorted([{
        'resource_name': (f'{GROUP_RN.replace("assetGroups", "assetGroupAssets")}~'
                          f'{item["asset"].rsplit("/", 1)[1]}~'
                          f'{client.PMAX_FIELD_NUMBERS[item["field_type"]]}'),
        'asset_group': GROUP_RN, 'status': 'PAUSED', **copy.deepcopy(item),
    } for item in proofs], key=lambda item: item['resource_name'])


@pytest.fixture
def asset_remove(monkeypatch, fake_client):
    state = add_state([], _links())
    selected = {(item['asset'], item['field_type']): {
        'asset': item['asset'], 'field_type': item['field_type'],
        'content': copy.deepcopy(item['content'])} for item in state['existing_assets']}
    tombstone = []
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: copy.deepcopy(state))

    def proofs(cid, items):
        pair = (items[0]['asset'], items[0]['field_type'])
        return [copy.deepcopy(selected[pair])]

    monkeypatch.setattr(client, '_pmax_requested_asset_proofs', proofs)
    monkeypatch.setattr(client, 'pmax_asset_group_asset_removed_state',
                        lambda check: None if not tombstone else tombstone[0])
    return state, selected, tombstone, fake_client


def draft(**changes):
    values = {'asset_group_id': GROUP_ID, 'asset_id': '1',
              'field_type': 'HEADLINE', 'customer_id': CID}
    values.update(changes)
    return tools.remove_asset_group_asset(**values)


def _make_role_removable(state, selected, role):
    if role == 'HEADLINE':
        return
    source = next(item for item in state['existing_assets'] if item['field_type'] == role)
    extra = copy.deepcopy(source)
    suffix = client.PMAX_FIELD_NUMBERS[role]
    asset_id = source['asset'].rsplit('/', 1)[1]
    extra.update(resource_name=extra['resource_name'].replace(
                     f'~{asset_id}~{suffix}', f'~20~{suffix}'),
                 asset=f'customers/{CID}/assets/20')
    if role == 'LONG_HEADLINE':
        extra['content'] = 'Another complete long headline'
    elif role == 'DESCRIPTION':
        extra['content'] = 'Another complete description'
    else:
        extra['content']['resource_name'] = extra['asset']
    state['existing_assets'].append(extra)
    state['existing_assets'].sort(key=lambda item: item['resource_name'])
    selected[(extra['asset'], role)] = {
        key: copy.deepcopy(extra[key]) for key in ('asset', 'field_type', 'content')}


def test_public_inventory_exposes_exact_remove_contract(asset_remove):
    async def check():
        inventory = {tool.name: tool for tool in await app.mcp.list_tools()}
        tool = inventory['remove_asset_group_asset']
        assert set(tool.input_schema['properties']) == {
            'asset_group_id', 'asset_id', 'field_type', 'customer_id'}
        assert tool.input_schema['required'] == ['asset_group_id', 'asset_id', 'field_type']
        for text in ('PAUSED', 'connection', 'bare asset', 'one atomic', 'offline'):
            assert text in tool.description
    asyncio.run(check())


def test_direct_none_defaults_but_mcp_explicit_null_refuses(asset_remove):
    from tests.test_protocol_errors import boundary, payload
    assert draft(customer_id=None)['dry_run'] is True
    result = boundary(app.mcp, 'remove_asset_group_asset', {
        'asset_group_id': GROUP_ID, 'asset_id': '1', 'field_type': 'HEADLINE'})
    assert result.model_dump(by_alias=True)['isError'] is False
    bad = payload(boundary(app.mcp, 'remove_asset_group_asset', {
        'asset_group_id': GROUP_ID, 'asset_id': '1', 'field_type': 'HEADLINE',
        'customer_id': None}))
    assert bad['code'] == 'BAD_INPUT'


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 123), ('customer_id', 'null'), ('asset_group_id', 90),
    ('asset_group_id', '090'), ('asset_id', 1), ('asset_id', '01'),
    ('field_type', 2), ('field_type', 'LOGO'), ('field_type', 'headline'),
])
def test_original_input_and_role_guards_precede_reads(monkeypatch, field, value):
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'))
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


def test_default_off_refuses_direct_and_mcp_before_reads_or_provider(monkeypatch):
    from tests.test_protocol_errors import boundary, payload
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'))
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'WRITES_DISABLED'
    bad = payload(boundary(app.mcp, 'remove_asset_group_asset', {
        'asset_group_id': GROUP_ID, 'asset_id': '1', 'field_type': 'HEADLINE'}))
    assert bad['code'] == 'WRITES_DISABLED'


@pytest.mark.parametrize('gate', ['read', 'write'])
def test_draft_allowlists_refuse_before_reader(monkeypatch, gate):
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS',
                       '999' if gate == 'read' else f'{CID},999')
    monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'NOT_ALLOWLISTED'


@pytest.mark.parametrize('gate', ['writes', 'read', 'write'])
def test_confirmation_repeats_gates_before_reader(asset_remove, monkeypatch, gate):
    _, _, _, fake = asset_remove
    pending = draft()
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'read':
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'))
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


def test_one_exact_numeric_suffix_remove_and_deep_copied_proofs(asset_remove, fake_gads):
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    check = plan.post_checks[0]
    target = _links()[0]
    assert len(plan.operations) == 1
    assert plan.operations[0] == rails.MutationOp(
        'AssetGroupAssetService', {'remove': target['resource_name']}, None)
    context = client.validate_mutation_plan(plan)
    built = client._build_mutate_operation(fake_gads, plan.operations[0], context)
    assert built.asset_group_asset_operation.remove == target['resource_name']
    assert check['selected_asset'] is not check['target_link']
    check['selected_asset']['content'] = 'tampered copy'
    assert check['target_link']['content'] == 'Short one'


def test_actual_atomic_validation_only_has_no_postreads(asset_remove, fake_gads, monkeypatch):
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    plan = rails._DRAFTS[draft()['draft_id']].plan
    monkeypatch.setattr(client, 'pmax_asset_group_asset_removed_state',
                        lambda *a: pytest.fail('postread reached'))
    result = client._dispatch_entity(plan, True)
    sent = fake_gads.mutate_calls[0]
    assert result['validate_only'] is True and len(sent['operations']) == 1
    assert sent['partial_failure'] is False and sent['validate_only'] is True


@pytest.mark.parametrize(('asset_id', 'role'), [
    ('1', 'HEADLINE'), ('4', 'LONG_HEADLINE'), ('5', 'DESCRIPTION'),
    ('7', 'MARKETING_IMAGE'), ('8', 'SQUARE_MARKETING_IMAGE'),
])
def test_all_five_roles_use_installed_generated_numeric_compound_path(
        asset_remove, asset_id, role):
    state, selected, _, _ = asset_remove
    _make_role_removable(state, selected, role)
    plan = rails._DRAFTS[draft(asset_id=asset_id, field_type=role)['draft_id']].plan
    expected = client.pmax_asset_group_asset_path(
        CID, GROUP_ID, asset_id, client.PMAX_FIELD_NUMBERS[role])
    assert plan.operations[0].operation == {'remove': expected}


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'enabled', 'foreign', 'malformed'])
def test_exact_target_association_required(asset_remove, damage):
    state, _, _, _ = asset_remove
    target = next(item for item in state['existing_assets']
                  if item['asset'].endswith('/1') and item['field_type'] == 'HEADLINE')
    if damage == 'missing':
        state['existing_assets'].remove(target)
    elif damage == 'duplicate':
        state['existing_assets'].append(copy.deepcopy(target))
    elif damage == 'enabled':
        target['status'] = 'ENABLED'
    elif damage == 'foreign':
        target['asset_group'] = f'customers/{CID}/assetGroups/99'
    else:
        target['resource_name'] += '~2'
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize(('asset_id', 'role'), [
    ('1', 'HEADLINE'), ('4', 'LONG_HEADLINE'), ('5', 'DESCRIPTION'),
    ('7', 'MARKETING_IMAGE'), ('8', 'SQUARE_MARKETING_IMAGE'),
])
def test_removal_refuses_every_role_floor(asset_remove, asset_id, role):
    state, _, _, _ = asset_remove
    if role == 'HEADLINE':
        state['existing_assets'] = [item for item in state['existing_assets']
                                    if not item['asset'].endswith('/9')]
    with pytest.raises(rails.RailViolation):
        draft(asset_id=asset_id, field_type=role)


@pytest.mark.parametrize('role', ['HEADLINE', 'DESCRIPTION'])
def test_removal_refuses_last_required_short_copy_even_above_count_floor(asset_remove, role):
    state, selected, _, fake = asset_remove
    if role == 'DESCRIPTION':
        source = next(item for item in state['existing_assets']
                      if item['field_type'] == role)
        extra = copy.deepcopy(source)
        suffix = client.PMAX_FIELD_NUMBERS[role]
        extra.update(resource_name=extra['resource_name'].replace(f'~5~{suffix}', f'~20~{suffix}'),
                     asset=f'customers/{CID}/assets/20')
        state['existing_assets'].append(extra)
        state['existing_assets'].sort(key=lambda item: item['resource_name'])
    items = [item for item in state['existing_assets'] if item['field_type'] == role]
    target_id = '1' if role == 'HEADLINE' else '5'
    target = next(item for item in items if item['asset'].endswith('/' + target_id))
    remaining_items = [item for item in items if item is not target]
    for index, item in enumerate(remaining_items, 1):
        item['content'] = (f'Long valid headline number {index}' if role == 'HEADLINE'
                           else f'This valid description remains above sixty characters for test number {index}')
    assert client._validate_pmax_asset_group_creatives(
        CID, GROUP_RN, copy.deepcopy(state['existing_assets']), [], require_complete=True)
    assert selected[(target['asset'], role)] == {
        key: target[key] for key in ('asset', 'field_type', 'content')}
    with pytest.raises(
            rails.RailViolation,
            match=rf'{role} requires at least one text within [0-9]+ weighted characters'):
        draft(asset_id=target['asset'].rsplit('/', 1)[1], field_type=role)
    assert fake.dispatch_calls == []
    remaining_items[0]['content'] = 'Still short'
    assert draft(asset_id=target['asset'].rsplit('/', 1)[1], field_type=role)['dry_run'] is True


def test_same_asset_other_role_is_retained(asset_remove):
    state, _, _, _ = asset_remove
    extra = copy.deepcopy(next(item for item in state['existing_assets']
                               if item['field_type'] == 'LONG_HEADLINE'))
    suffix = client.PMAX_FIELD_NUMBERS['LONG_HEADLINE']
    extra.update(resource_name=extra['resource_name'].replace(f'~4~{suffix}', f'~1~{suffix}'),
                 asset=f'customers/{CID}/assets/1', content='Short one')
    state['existing_assets'].append(extra)
    state['existing_assets'].sort(key=lambda item: item['resource_name'])
    pending = draft()
    remaining = rails._DRAFTS[pending['draft_id']].plan.post_checks[0]['remaining_assets']
    assert any(item['asset'].endswith('/1') and item['field_type'] == 'LONG_HEADLINE'
               for item in remaining)


@pytest.mark.parametrize('damage', [
    'service', 'action', 'extra_action', 'mask', 'resource', 'field_type',
    'target', 'selected', 'remaining', 'parent',
])
def test_closed_plan_and_proof_tampering_refuses_before_provider(
        asset_remove, monkeypatch, damage):
    plan = rails._DRAFTS[draft()['draft_id']].plan
    op, check = plan.operations[0], plan.post_checks[0]
    if damage == 'service':
        object.__setattr__(op, 'service', 'AssetService')
    elif damage == 'action':
        op.operation.clear()
        op.operation['create'] = {}
    elif damage == 'extra_action':
        op.operation['hidden'] = True
    elif damage == 'mask':
        object.__setattr__(op, 'update_mask', ['status'])
    elif damage == 'resource':
        op.operation['remove'] = op.operation['remove'].replace('~1~2', '~9~2')
    elif damage == 'field_type':
        check['field_type'] = 'DESCRIPTION'
    elif damage == 'target':
        check['target_link']['asset_group'] = f'customers/{CID}/assetGroups/99'
    elif damage == 'selected':
        check['selected_asset']['content'] = 'changed'
    elif damage == 'remaining':
        check['remaining_assets'].pop()
    else:
        check['parent_proof']['parent']['status'] = 'ENABLED'
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


@pytest.mark.parametrize('part', ['parent', 'branding', 'group', 'links', 'selected'])
def test_confirmation_drift_refuses_predispatch_without_consuming(
        asset_remove, part):
    state, selected, _, fake = asset_remove
    pending = draft()
    if part == 'parent':
        state['parent_proof']['parent']['name'] = 'Changed Parent'
    elif part == 'branding':
        state['parent_proof']['branding'][0]['content'] = 'Changed Brand'
    elif part == 'group':
        state['target']['name'] = 'Changed Group'
    elif part == 'links':
        state['existing_assets'][-1]['content'] = 'Changed link content'
    else:
        selected[(f'customers/{CID}/assets/1', 'HEADLINE')]['content'] = 'Changed asset'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(pending['draft_id'])
    assert exc.value.code == 'STATE_DRIFT' or part == 'selected'
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


def test_apply_verifies_result_tombstone_bare_asset_and_exact_remaining(
        asset_remove, monkeypatch):
    state, _, tombstone, fake = asset_remove
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    check = plan.post_checks[0]
    response = make_type('MutateGoogleAdsResponse')
    response.mutate_operation_responses.append(
        {'asset_group_asset_result': {'resource_name': check['resource_name']}})
    fake.dispatch_result = {'results': [{'type': 'asset_group_asset_result',
                                        'resource_name': check['resource_name']}],
                            'request_id': None}

    def dispatch(sent):
        fake.dispatch_calls.append(sent)
        state['existing_assets'] = copy.deepcopy(check['remaining_assets'])
        return copy.deepcopy(fake.dispatch_result)
    monkeypatch.setattr(client, '_dispatch', dispatch)
    result = rails.apply_draft(pending['draft_id'])
    assert result['verified'] is True, result.get('error')
    assert 'connection' in result['verification_scope']
    assert tombstone == []


def test_successful_image_connection_removal_keeps_bare_image_and_verifies(
        asset_remove, monkeypatch):
    state, selected, _, fake = asset_remove
    _make_role_removable(state, selected, 'MARKETING_IMAGE')
    pending = draft(asset_id='7', field_type='MARKETING_IMAGE')
    check = rails._DRAFTS[pending['draft_id']].plan.post_checks[0]
    fake.dispatch_result = {'results': [{'type': 'asset_group_asset_result',
                                        'resource_name': check['resource_name']}],
                            'request_id': None}

    def dispatch(sent):
        fake.dispatch_calls.append(sent)
        state['existing_assets'] = copy.deepcopy(check['remaining_assets'])
        return copy.deepcopy(fake.dispatch_result)

    monkeypatch.setattr(client, '_dispatch', dispatch)
    result = rails.apply_draft(pending['draft_id'])
    assert result['verified'] is True
    assert selected[(f'customers/{CID}/assets/7', 'MARKETING_IMAGE')] == check['selected_asset']
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', ['empty', 'duplicate', 'kind', 'resource', 'group', 'role'])
def test_bad_mutate_result_refuses_before_every_saved_reader(
        asset_remove, monkeypatch, damage):
    check = rails._DRAFTS[draft()['draft_id']].plan.post_checks[0]
    entry = {'type': 'asset_group_asset_result', 'resource_name': check['resource_name']}
    results = [entry]
    if damage == 'empty':
        results = []
    elif damage == 'duplicate':
        results *= 2
    elif damage == 'kind':
        entry['type'] = 'asset_result'
    elif damage == 'resource':
        entry['resource_name'] = entry['resource_name'].replace('~1~2', '~99~2')
    elif damage == 'group':
        entry['resource_name'] = entry['resource_name'].replace('/90~', '/99~')
    else:
        entry['resource_name'] = entry['resource_name'].rsplit('~', 1)[0] + '~3'
    for name in ('pmax_asset_group_asset_removed_state', '_pmax_requested_asset_proofs',
                 'pmax_asset_group_asset_state'):
        monkeypatch.setattr(client, name, lambda *a, **k: pytest.fail('saved reader reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_pmax_asset_group_asset_remove_result(
            [check], {'results': results, 'request_id': None})


@pytest.mark.parametrize('mode', ['absent', 'removed'])
def test_exact_complete_removed_connection_reader_accepts_only_tombstone(
        asset_remove, monkeypatch, mode):
    check = rails._DRAFTS[draft()['draft_id']].plan.post_checks[0]
    rows = []
    if mode == 'removed':
        row = make_type('GoogleAdsRow')
        row.asset_group_asset = {
            'resource_name': check['resource_name'], 'asset_group': check['asset_group'],
            'asset': check['target_link']['asset'], 'field_type': check['field_type'],
            'status': 'REMOVED'}
        rows = [row]
    seen = []
    def scan(query, cid):
        seen.append(query)
        return rows
    monkeypatch.setattr(client, '_scan_rows', scan)
    assert _REAL_REMOVED_STATE(check) in {None, 'REMOVED'}
    assert 'resource_name =' in seen[0] and 'status !=' not in seen[0]


@pytest.mark.parametrize('damage', [
    'duplicate', 'active', 'resource', 'group', 'foreign', 'role', 'malformed',
])
def test_removed_connection_reader_rejects_ambiguous_or_wrong_tombstone(
        asset_remove, monkeypatch, damage):
    check = rails._DRAFTS[draft()['draft_id']].plan.post_checks[0]
    row = make_type('GoogleAdsRow')
    row.asset_group_asset = {
        'resource_name': check['resource_name'], 'asset_group': check['asset_group'],
        'asset': check['target_link']['asset'], 'field_type': check['field_type'],
        'status': 'REMOVED'}
    rows = [row]
    if damage == 'duplicate':
        rows *= 2
    elif damage == 'active':
        row.asset_group_asset.status = 'PAUSED'
    elif damage == 'resource':
        row.asset_group_asset.resource_name = check['resource_name'].replace('~1~2', '~99~2')
    elif damage == 'group':
        row.asset_group_asset.asset_group = f'customers/{CID}/assetGroups/99'
    elif damage == 'foreign':
        row.asset_group_asset.asset = f'customers/{CID}/assets/99'
    elif damage == 'role':
        row.asset_group_asset.field_type = 'DESCRIPTION'
    else:
        rows = [{'asset_group_asset': {'resource_name': check['resource_name']}}]
    monkeypatch.setattr(client, '_scan_rows', lambda *a: rows)
    with pytest.raises(rails.RailViolation):
        _REAL_REMOVED_STATE(check)


@pytest.mark.parametrize('damage', [
    'result_empty', 'result_duplicate', 'result_kind', 'result_resource',
    'result_group', 'result_role', 'tombstone_duplicate', 'tombstone_active',
    'tombstone_resource', 'tombstone_group', 'tombstone_role', 'bare_deleted',
    'bare_changed', 'remaining_missing', 'remaining_changed', 'remaining_extra',
    'parent', 'branding', 'group', 'postscan_failure',
])
def test_postwrite_mismatch_is_consumed_without_retry(asset_remove, monkeypatch, damage):
    state, selected, tombstone, fake = asset_remove
    pending = draft()
    check = rails._DRAFTS[pending['draft_id']].plan.post_checks[0]
    fake.dispatch_result = {'results': [{'type': 'asset_group_asset_result',
                                        'resource_name': check['resource_name']}],
                            'request_id': None}
    entry = fake.dispatch_result['results'][0]
    if damage == 'result_empty':
        fake.dispatch_result['results'] = []
    elif damage == 'result_duplicate':
        fake.dispatch_result['results'] *= 2
    elif damage == 'result_kind':
        fake.dispatch_result['results'][0]['type'] = 'asset_result'
    elif damage == 'result_resource':
        entry['resource_name'] = entry['resource_name'].replace('~1~2', '~99~2')
    elif damage == 'result_group':
        entry['resource_name'] = entry['resource_name'].replace('/90~', '/99~')
    elif damage == 'result_role':
        entry['resource_name'] = entry['resource_name'].rsplit('~', 1)[0] + '~3'

    if damage.startswith('tombstone_'):
        row = make_type('GoogleAdsRow')
        row.asset_group_asset = {
            'resource_name': check['resource_name'], 'asset_group': check['asset_group'],
            'asset': check['target_link']['asset'], 'field_type': check['field_type'],
            'status': 'REMOVED'}
        rows = [row]
        if damage == 'tombstone_duplicate':
            rows *= 2
        elif damage == 'tombstone_active':
            row.asset_group_asset.status = 'PAUSED'
        elif damage == 'tombstone_resource':
            row.asset_group_asset.resource_name = check['resource_name'].replace('~1~2', '~99~2')
        elif damage == 'tombstone_group':
            row.asset_group_asset.asset_group = f'customers/{CID}/assetGroups/99'
        else:
            row.asset_group_asset.field_type = 'DESCRIPTION'
        monkeypatch.setattr(client, '_scan_rows', lambda *a: rows)
        monkeypatch.setattr(client, 'pmax_asset_group_asset_removed_state',
                            _REAL_REMOVED_STATE)

    def dispatch(sent):
        fake.dispatch_calls.append(sent)
        state['existing_assets'] = copy.deepcopy(check['remaining_assets'])
        if damage == 'remaining_missing':
            state['existing_assets'].pop()
        elif damage == 'remaining_changed':
            state['existing_assets'][0]['content'] = 'Changed remaining content'
        elif damage == 'remaining_extra':
            extra = copy.deepcopy(state['existing_assets'][0])
            extra.update(resource_name=extra['resource_name'].replace('~2~2', '~99~2'),
                         asset=f'customers/{CID}/assets/99', content='Extra headline')
            state['existing_assets'].append(extra)
            state['existing_assets'].sort(key=lambda item: item['resource_name'])
        elif damage == 'parent':
            state['parent_proof']['parent']['name'] = 'Changed Parent'
        elif damage == 'branding':
            state['parent_proof']['branding'][0]['content'] = 'Changed Brand'
        elif damage == 'group':
            state['target']['name'] = 'Changed Group'
        if damage == 'bare_deleted':
            selected.pop((check['selected_asset']['asset'], check['field_type']))
        elif damage == 'bare_changed':
            selected[(check['selected_asset']['asset'], check['field_type'])]['content'] = 'changed'
        if damage == 'postscan_failure':
            monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                                lambda *a: (_ for _ in ()).throw(
                                    rails.RailViolation('postscan failed')))
        return copy.deepcopy(fake.dispatch_result)
    monkeypatch.setattr(client, '_dispatch', dispatch)
    result = rails.apply_draft(pending['draft_id'])
    assert result['verified'] is False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1
