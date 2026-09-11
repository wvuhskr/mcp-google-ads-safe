"""Offline proof for linking existing assets to one paused PMax asset group."""
import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import make_type
from tests.test_pmax_asset_group_creation import (
    _raw_reader_rows,
    image_state,
    parent_proof,
)
from tests.test_pmax_asset_group_update import _target_row

CID, GROUP_ID = '1234567890', '90'
GROUP_RN = f'customers/{CID}/assetGroups/{GROUP_ID}'
CAMPAIGN_RN = f'customers/{CID}/campaigns/88'
ASSETS = [
    {'asset_id': '1', 'field_type': 'HEADLINE'},
    {'asset_id': '2', 'field_type': 'HEADLINE'},
    {'asset_id': '3', 'field_type': 'HEADLINE'},
    {'asset_id': '4', 'field_type': 'LONG_HEADLINE'},
    {'asset_id': '5', 'field_type': 'DESCRIPTION'},
    {'asset_id': '6', 'field_type': 'DESCRIPTION'},
    {'asset_id': '7', 'field_type': 'MARKETING_IMAGE'},
    {'asset_id': '8', 'field_type': 'SQUARE_MARKETING_IMAGE'},
]
TEXT = {
    ('1', 'HEADLINE'): 'Cool Today', ('2', 'HEADLINE'): 'Local Cooling',
    ('3', 'HEADLINE'): 'Book Service',
    ('4', 'LONG_HEADLINE'): 'Cooling service for your home',
    ('5', 'DESCRIPTION'): 'Book cooling service today',
    ('6', 'DESCRIPTION'): 'Your local cooling specialists',
}


def _proof(item):
    asset_id, role = item['asset_id'], item['field_type']
    content = (image_state(asset_id, 600, 314) if role == 'MARKETING_IMAGE' else
               image_state(asset_id, 300, 300) if role == 'SQUARE_MARKETING_IMAGE' else
               TEXT[(asset_id, role)])
    return {'asset': f'customers/{CID}/assets/{asset_id}',
            'field_type': role, 'content': content}


def add_state(items=ASSETS, existing=()):
    proof = parent_proof()
    proof.pop('images')
    return {'parent_proof': proof,
            'target': {'resource_name': GROUP_RN, 'id': GROUP_ID, 'campaign': CAMPAIGN_RN,
                       'name': 'Existing Group', 'status': 'PAUSED',
                       'final_urls': ['https://example.com/old'], 'final_mobile_urls': [],
                       'path1': '', 'path2': ''},
            'existing_assets': list(existing),
            'requested_assets': sorted((_proof(item) for item in items),
                                       key=lambda item: (item['asset'], item['field_type']))}


@pytest.fixture
def asset_add(monkeypatch, fake_client):
    state = add_state()
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: copy.deepcopy(state), raising=False)
    return state, fake_client


def draft(**changes):
    values = {'asset_group_id': GROUP_ID, 'assets': copy.deepcopy(ASSETS),
              'customer_id': CID}
    values.update(changes)
    return tools.add_asset_group_assets(**values)


def plan_for(**changes):
    return rails._DRAFTS[draft(**changes)['draft_id']].plan


def test_public_inventory_exposes_asset_group_asset_add_contract():
    async def check():
        inventory = {tool.name: tool for tool in await app.mcp.list_tools()}
        tool = inventory['add_asset_group_assets']
        assert set(tool.input_schema['properties']) == {
            'asset_group_id', 'assets', 'customer_id'}
        assert tool.input_schema['required'] == ['asset_group_id', 'assets']
        for text in ('PAUSED', 'existing owned assets', 'HEADLINE', 'LONG_HEADLINE',
                     'DESCRIPTION', 'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE',
                     'atomic', 'link-only', 'offline'):
            assert text in tool.description

    asyncio.run(check())


def test_none_uses_default_account_but_literal_null_is_never_a_default(asset_add):
    assert draft(customer_id=None)['dry_run'] is True
    with pytest.raises(rails.RailViolation):
        draft(customer_id='null')


def test_actual_mcp_omitted_customer_uses_default_account(asset_add):
    from tests.test_protocol_errors import boundary

    result = boundary(app.mcp, 'add_asset_group_assets', {
        'asset_group_id': GROUP_ID, 'assets': copy.deepcopy(ASSETS)})
    assert result.model_dump(by_alias=True)['isError'] is False


def test_link_only_plan_preview_and_real_v25_serialization(asset_add, fake_gads):
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    context = client.validate_mutation_plan(plan)
    built = [client._build_mutate_operation(fake_gads, op, context)
             for op in plan.operations]
    assert len(plan.operations) == len(ASSETS)
    assert {op.service for op in plan.operations} == {'AssetGroupAssetService'}
    assert [op.operation['create']['field_type'] for op in plan.operations] == [
        item['field_type'] for item in ASSETS]
    assert all(op.update_mask is None and op.operation['create']['status'] == 'PAUSED'
               for op in plan.operations)
    assert all(item.asset_group_asset_operation.create.status.name == 'PAUSED'
               for item in built)
    assert pending['preview']['current_counts'] == {
        role: 0 for role in client.PMAX_ASSET_GROUP_ROLES}
    assert pending['preview']['resulting_counts'] == {
        'HEADLINE': 3, 'LONG_HEADLINE': 1, 'DESCRIPTION': 2,
        'MARKETING_IMAGE': 1, 'SQUARE_MARKETING_IMAGE': 1}


def test_actual_atomic_validate_only_request(asset_add, fake_gads):
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan_for(), True)
    assert result['validate_only'] is True and len(fake_gads.mutate_calls) == 1
    sent = fake_gads.mutate_calls[0]
    assert sent['customer_id'] == CID and sent['partial_failure'] is False
    assert sent['validate_only'] is True and len(sent['operations']) == len(ASSETS)


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 123), ('customer_id', True), ('asset_group_id', 90),
    ('asset_group_id', '090'), ('assets', None), ('assets', '[]'), ('assets', []),
    ('assets', [{}]), ('assets', [{'asset_id': '1'}]),
    ('assets', [{'asset_id': '1', 'field_type': 'HEADLINE', 'extra': 'x'}]),
    ('assets', [{'asset_id': 1, 'field_type': 'HEADLINE'}]),
    ('assets', [{'asset_id': '1', 'field_type': 2}]),
    ('assets', [{'asset_id': '01', 'field_type': 'HEADLINE'}]),
    ('assets', [{'asset_id': '1', 'field_type': 'LOGO'}]),
    ('assets', [{'asset_id': '1', 'field_type': 'HEADLINE'}] * 66),
])
def test_direct_input_strictness_precedes_reads(monkeypatch, field, value):
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'), raising=False)
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


def test_duplicate_pair_refuses_but_same_asset_different_roles_is_allowed(
        asset_add, monkeypatch):
    with pytest.raises(rails.RailViolation):
        draft(assets=[ASSETS[0], ASSETS[0]])
    same_asset = copy.deepcopy(ASSETS)
    same_asset[3]['asset_id'] = '1'
    state = add_state()
    long = next(item for item in state['requested_assets']
                if item['field_type'] == 'LONG_HEADLINE')
    long.update(asset=f'customers/{CID}/assets/1', content='Cool Today')
    state['requested_assets'].sort(key=lambda item: (item['asset'], item['field_type']))
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: copy.deepcopy(state))
    plan = rails._DRAFTS[draft(assets=same_asset)['draft_id']].plan
    assert plan.operations[0].operation['create']['asset'] == plan.operations[3].operation[
        'create']['asset']


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', None), ('customer_id', 123), ('customer_id', 'null'),
    ('asset_group_id', 90),
    ('asset_group_id', 'null'), ('assets', '[]'),
    ('assets', [{'asset_id': 1, 'field_type': 'HEADLINE'}]),
    ('assets', [{'asset_id': '1', 'field_type': 2}]),
    ('assets', [{'asset_id': '1', 'field_type': 'HEADLINE', 'extra': 'x'}]),
])
def test_actual_mcp_rejects_original_container_and_value_coercion(asset_add, field, value):
    from tests.test_protocol_errors import boundary, payload
    args = {'asset_group_id': GROUP_ID, 'assets': copy.deepcopy(ASSETS),
            'customer_id': CID, field: value}
    assert payload(boundary(app.mcp, 'add_asset_group_assets', args))['code'] == 'BAD_INPUT'


def test_default_off_refuses_direct_and_actual_mcp_before_reads_or_provider(monkeypatch):
    from tests.test_protocol_errors import boundary, payload
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'), raising=False)
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'WRITES_DISABLED'
    result = payload(boundary(app.mcp, 'add_asset_group_assets', {
        'asset_group_id': GROUP_ID, 'assets': ASSETS, 'customer_id': CID}))
    assert result['code'] == 'WRITES_DISABLED'


@pytest.mark.parametrize('gate', ['read', 'write'])
def test_draft_allowlists_precede_reader(monkeypatch, gate):
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS',
                       '999' if gate == 'read' else f'{CID},999')
    monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'), raising=False)
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'NOT_ALLOWLISTED'


@pytest.mark.parametrize('gate', ['writes', 'read', 'write'])
def test_confirmation_repeats_gates_before_reader(asset_add, monkeypatch, gate):
    _, fake = asset_add
    pending = draft()
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'read':
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('reader reached'))
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


def _canonical(asset_id, role, content=None):
    if content is None:
        content = (image_state(asset_id, 600, 314) if role == 'MARKETING_IMAGE' else
                   image_state(asset_id, 300, 300) if role == 'SQUARE_MARKETING_IMAGE' else
                   f'{role.title()} {asset_id}')
    return {'asset': f'customers/{CID}/assets/{asset_id}',
            'field_type': role, 'content': content}


def _complete_proofs():
    return [
        _canonical('1', 'HEADLINE', 'Short one'),
        _canonical('2', 'HEADLINE', 'Second headline'),
        _canonical('3', 'HEADLINE', 'Third headline'),
        _canonical('4', 'LONG_HEADLINE', 'A complete long headline'),
        _canonical('5', 'DESCRIPTION', 'Short description'),
        _canonical('6', 'DESCRIPTION', 'A second complete description'),
        _canonical('7', 'MARKETING_IMAGE'),
        _canonical('8', 'SQUARE_MARKETING_IMAGE'),
    ]


@pytest.mark.parametrize(('role', 'maximum'), [
    ('HEADLINE', 15), ('LONG_HEADLINE', 5), ('DESCRIPTION', 5),
    ('MARKETING_IMAGE', 20), ('SQUARE_MARKETING_IMAGE', 20),
])
def test_combined_role_upper_bounds_accept_exactly_and_reject_one_more(role, maximum):
    base = [item for item in _complete_proofs() if item['field_type'] != role]
    values = []
    for number in range(100, 100 + maximum):
        content = None
        if role == 'HEADLINE':
            content = 'Short' if number == 100 else f'Headline {number}'
        elif role == 'LONG_HEADLINE':
            content = f'Long headline {number}'
        elif role == 'DESCRIPTION':
            content = 'Short description' if number == 100 else f'Description {number}'
        values.append(_canonical(str(number), role, content))
    exact = sorted([*base, *values], key=lambda item: (item['asset'], item['field_type']))
    assert client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], exact) is True
    extra = _canonical('999', role, 'Extra') if role in {
        'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION'} else _canonical('999', role)
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(
            CID, GROUP_RN, [], sorted([*exact, extra],
                                      key=lambda item: (item['asset'], item['field_type'])))


@pytest.mark.parametrize('role', client.PMAX_ASSET_GROUP_ROLES)
def test_combined_role_minimums_are_required(role):
    proofs = _complete_proofs()
    first = next(index for index, item in enumerate(proofs) if item['field_type'] == role)
    proofs.pop(first)
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], proofs)


@pytest.mark.parametrize(('role', 'contents'), [
    ('HEADLINE', ['Sixteen chars ok?', 'Another long one', 'Third long value']),
    ('DESCRIPTION', ['This description is deliberately longer than sixty weighted characters total',
                     'This other description is also deliberately longer than sixty characters']),
])
def test_combined_short_text_rules_are_required(role, contents):
    proofs = [item for item in _complete_proofs() if item['field_type'] != role]
    proofs.extend(_canonical(str(200 + index), role, content)
                  for index, content in enumerate(contents))
    proofs.sort(key=lambda item: (item['asset'], item['field_type']))
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], proofs)


@pytest.mark.parametrize(('role', 'content'), [
    ('HEADLINE', '界' * 16), ('LONG_HEADLINE', '界' * 46), ('DESCRIPTION', '界' * 46),
])
def test_weighted_text_limits_refuse(role, content):
    proofs = _complete_proofs()
    target = next(item for item in proofs if item['field_type'] == role)
    target['content'] = content
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], proofs)


def test_duplicate_text_different_ids_refuses_within_role():
    proofs = _complete_proofs()
    headlines = [item for item in proofs if item['field_type'] == 'HEADLINE']
    headlines[1]['content'] = headlines[0]['content']
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], proofs)


def test_same_asset_can_fill_different_valid_roles():
    proofs = _complete_proofs()
    long = next(item for item in proofs if item['field_type'] == 'LONG_HEADLINE')
    long.update(asset=proofs[0]['asset'], content=proofs[0]['content'])
    proofs.sort(key=lambda item: (item['asset'], item['field_type']))
    assert client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], proofs)


def test_underfilled_existing_inventory_is_valid_only_when_additions_complete_it():
    all_items = _complete_proofs()
    existing = []
    for item in all_items[:2]:
        existing.append({'resource_name':
                         f'customers/{CID}/assetGroupAssets/{GROUP_ID}~'
                         f'{item["asset"].rsplit("/", 1)[1]}~'
                         f'{client.PMAX_FIELD_NUMBERS[item["field_type"]]}',
                         'asset_group': GROUP_RN, 'status': 'PAUSED', **item})
    assert client._validate_pmax_asset_group_creatives(
        CID, GROUP_RN, existing, [], require_complete=False)
    assert client._validate_pmax_asset_group_creatives(
        CID, GROUP_RN, existing, all_items[2:])


def test_already_linked_pair_and_bad_image_proof_refuse():
    proofs = _complete_proofs()
    first = proofs[0]
    existing = [{'resource_name':
                 f'customers/{CID}/assetGroupAssets/{GROUP_ID}~1~2',
                 'asset_group': GROUP_RN, 'status': 'PAUSED', **first}]
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(CID, GROUP_RN, existing, proofs)
    image = next(item for item in proofs if item['field_type'] == 'MARKETING_IMAGE')
    image['content']['image_asset']['full_size']['width_pixels'] = 601
    with pytest.raises(rails.RailViolation):
        client._validate_pmax_asset_group_creatives(CID, GROUP_RN, [], proofs)


def _asset_data(item):
    return (copy.deepcopy(item['content']) if isinstance(item['content'], dict) else
            {'resource_name': item['asset'], 'type_': 'TEXT',
             'text_asset': {'text': item['content']}})


def _link_row(item):
    row = make_type('GoogleAdsRow')
    asset_id = item['asset'].rsplit('/', 1)[1]
    row.asset_group_asset = {
        'resource_name': (f'customers/{CID}/assetGroupAssets/{GROUP_ID}~{asset_id}~'
                          f'{client.PMAX_FIELD_NUMBERS[item["field_type"]]}'),
        'asset_group': GROUP_RN, 'asset': item['asset'],
        'field_type': item['field_type'], 'status': 'PAUSED'}
    row.asset = _asset_data(item)
    return row


def _requested_row(item):
    row = make_type('GoogleAdsRow')
    row.asset = _asset_data(item)
    return row


def _reader_call(monkeypatch, *, links, requested=(), target=None, campaign=None,
                 brands=None, additions=None):
    default_campaign, default_brands = _raw_reader_rows()
    proof = parent_proof()
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, '_creation_account',
                        lambda *a, **kw: copy.deepcopy(proof['account']))
    target = _target_row() if target is None else target
    campaign = default_campaign if campaign is None else campaign
    brands = default_brands if brands is None else brands

    def scan(query, cid):
        if 'FROM campaign_asset' in query:
            return brands
        if 'FROM campaign WHERE' in query:
            return [campaign]
        if 'FROM asset_group WHERE' in query:
            return [target]
        if 'FROM asset_group_asset' in query:
            return links
        if 'FROM asset WHERE' in query:
            return list(requested)
        pytest.fail(f'unexpected query: {query}')

    monkeypatch.setattr(client, '_scan_rows', scan)
    return lambda: client.pmax_asset_group_asset_state(CID, GROUP_ID, additions)


def test_real_raw_readers_accept_underfilled_inventory_completed_by_batch(monkeypatch):
    proofs = _complete_proofs()
    existing, additions = proofs[:2], proofs[2:]
    state = _reader_call(
        monkeypatch, links=[_link_row(item) for item in existing],
        requested=[_requested_row(item) for item in additions],
        additions=[{key: item[key] for key in ('asset', 'field_type')}
                   for item in additions])()
    assert len(state['existing_assets']) == 2
    assert state['requested_assets'] == sorted(
        additions, key=lambda item: (item['asset'], item['field_type']))
    assert state['target']['resource_name'] == GROUP_RN
    assert set(state['parent_proof']) == {'account', 'parent', 'branding'}


@pytest.mark.parametrize('damage', [
    'duplicate', 'dict', 'foreign_rn', 'wrong_compound', 'foreign_group', 'foreign_asset',
    'unsupported_role', 'enabled', 'unknown_status', 'wrong_type', 'blank_text',
    'bad_image',
])
def test_complete_link_reader_refuses_malformed_or_unsupported_rows(monkeypatch, damage):
    rows = [_link_row(item) for item in _complete_proofs()]
    row = rows[0]
    if damage == 'duplicate':
        rows.append(copy.deepcopy(row))
    elif damage == 'dict':
        rows[0] = type(row).to_dict(row)
    elif damage == 'foreign_rn':
        row.asset_group_asset.resource_name = row.asset_group_asset.resource_name.replace(
            f'customers/{CID}', 'customers/999')
    elif damage == 'wrong_compound':
        row.asset_group_asset.resource_name = row.asset_group_asset.resource_name.replace('~2', '~3')
    elif damage == 'foreign_group':
        row.asset_group_asset.asset_group = f'customers/{CID}/assetGroups/99'
    elif damage == 'foreign_asset':
        row.asset_group_asset.asset = 'customers/999/assets/1'
    elif damage == 'unsupported_role':
        row.asset_group_asset.field_type = 'LOGO'
    elif damage == 'enabled':
        row.asset_group_asset.status = 'ENABLED'
    elif damage == 'unknown_status':
        row.asset_group_asset.status = 'UNKNOWN'
    elif damage == 'wrong_type':
        row.asset.type_ = 'IMAGE'
    elif damage == 'blank_text':
        row.asset.text_asset.text = ''
    else:
        image = next(item for item in rows
                     if item.asset_group_asset.field_type.name == 'MARKETING_IMAGE')
        image.asset.image_asset.full_size.width_pixels = 601
    with pytest.raises(rails.RailViolation):
        _reader_call(monkeypatch, links=rows)()


@pytest.mark.parametrize('damage', [
    'missing', 'duplicate', 'dict', 'foreign', 'wrong_type', 'missing_text', 'bad_image',
])
def test_requested_asset_batch_reader_reconciles_exact_complete_rows(monkeypatch, damage):
    proofs = _complete_proofs()
    additions = [{key: item[key] for key in ('asset', 'field_type')} for item in proofs]
    rows = [_requested_row(item) for item in proofs]
    if damage == 'missing':
        rows.pop()
    elif damage == 'duplicate':
        rows.append(copy.deepcopy(rows[0]))
    elif damage == 'dict':
        rows[0] = type(rows[0]).to_dict(rows[0])
    elif damage == 'foreign':
        rows[0].asset.resource_name = 'customers/999/assets/1'
    elif damage == 'wrong_type':
        rows[0].asset.type_ = 'IMAGE'
    elif damage == 'missing_text':
        rows[0].asset.text_asset.text = ''
    else:
        image = next(item for item in rows if item.asset.type_.name == 'IMAGE')
        image.asset.image_asset.full_size.width_pixels = 601
    with pytest.raises(rails.RailViolation):
        _reader_call(monkeypatch, links=[], requested=rows, additions=additions)()


def test_group_parent_and_complete_scan_failures_propagate(monkeypatch):
    target = _target_row(status='ENABLED')
    with pytest.raises(rails.RailViolation):
        _reader_call(monkeypatch, links=[], target=target)()
    campaign, brands = _raw_reader_rows()
    campaign.campaign.status = 'ENABLED'
    with pytest.raises(rails.RailViolation):
        _reader_call(monkeypatch, links=[], campaign=campaign, brands=brands)()
    monkeypatch.setattr(client, '_scan_rows', lambda *a: (_ for _ in ()).throw(
        rails.RailViolation('scan incomplete', code='SCAN_INCOMPLETE')))
    with pytest.raises(rails.RailViolation) as exc:
        client.pmax_asset_group_asset_state(CID, GROUP_ID)
    assert exc.value.code == 'SCAN_INCOMPLETE'


@pytest.mark.parametrize('damage', [
    'service', 'action', 'field', 'status', 'mask', 'owner', 'target', 'duplicate',
    'marker', 'parent', 'existing', 'requested', 'addition_content', 'descriptor_extra',
])
def test_closed_plan_and_all_proof_tampering_refuse_before_provider(
        asset_add, fake_gads, damage):
    plan = copy.deepcopy(plan_for())
    op, check = plan.operations[0], plan.post_checks[0]
    if damage == 'service':
        object.__setattr__(op, 'service', 'AssetService')
    elif damage == 'action':
        op.operation['update'] = op.operation.pop('create')
    elif damage == 'field':
        op.operation['create']['campaign'] = CAMPAIGN_RN
    elif damage == 'status':
        op.operation['create']['status'] = 'ENABLED'
    elif damage == 'mask':
        object.__setattr__(op, 'update_mask', ['status'])
    elif damage == 'owner':
        op.operation['create']['asset'] = 'customers/999/assets/1'
    elif damage == 'target':
        op.operation['create']['asset_group'] = f'customers/{CID}/assetGroups/99'
    elif damage == 'duplicate':
        plan.operations[1] = copy.deepcopy(plan.operations[0])
        check['additions'][1] = copy.deepcopy(check['additions'][0])
    elif damage == 'marker':
        check['pmax_asset_group_assets'] = False
    elif damage == 'parent':
        check['parent_proof']['parent']['brand_guidelines_enabled'] = False
    elif damage == 'existing':
        check['existing_assets'] = [{'hidden': True}]
    elif damage == 'requested':
        check['requested_assets'][0]['content'] = 'Changed'
    elif damage == 'addition_content':
        check['additions'][0]['content'] = 'Changed'
    else:
        check['extra'] = True
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert fake_gads.mutate_calls == []


def test_asset_group_link_create_has_no_generic_plan_bypass(fake_gads):
    plan = rails.EntityMutationPlan(CID, [rails.safe_create_operation(
        'AssetGroupAssetService', {'asset_group': GROUP_RN,
                                   'asset': f'customers/{CID}/assets/1',
                                   'field_type': 'HEADLINE'})], True)
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert fake_gads.mutate_calls == []


def test_create_and_update_keep_separate_contexts_and_creation_image_equality_guard(
        asset_add, monkeypatch):
    from tests.test_pmax_asset_group_creation import ARGS as CREATE_ARGS
    from tests.test_pmax_asset_group_update import update_state

    creation_proof = parent_proof()
    monkeypatch.setattr(client, 'pmax_asset_group_state', lambda *a: {
        'parent_proof': copy.deepcopy(creation_proof), 'asset_groups': []})
    create_plan = rails._DRAFTS[
        tools.create_asset_group(**CREATE_ARGS)['draft_id']].plan
    assert type(client.validate_mutation_plan(create_plan)) is client._PMaxAssetGroupContext
    create_plan.post_checks[0]['images']['MARKETING_IMAGE']['image_asset'][
        'file_size'] = 1
    with pytest.raises(rails.RailViolation):
        client.validate_mutation_plan(create_plan)

    monkeypatch.setattr(client, 'pmax_asset_group_update_state',
                        lambda *a: copy.deepcopy(update_state()))
    update_plan = rails._DRAFTS[tools.update_asset_group(
        GROUP_ID, name='Renamed Group', customer_id=CID)['draft_id']].plan
    assert type(client.validate_mutation_plan(update_plan)) is client._PMaxAssetGroupUpdateContext
    assert type(client.validate_mutation_plan(plan_for())) is client._PMaxAssetGroupAssetAddContext


def test_stored_plan_tampering_is_consumed_before_provider(asset_add):
    _, fake = asset_add
    pending = draft()
    rails._DRAFTS[pending['draft_id']].plan.operations[0].operation['create'][
        'status'] = 'ENABLED'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(pending['draft_id'])
    assert exc.value.code == 'PLAN_TAMPERED'
    assert pending['draft_id'] not in rails._DRAFTS and fake.dispatch_calls == []


def _result_for(plan):
    check = plan.post_checks[0]
    results = []
    for item in check['additions']:
        asset_id = item['asset'].rsplit('/', 1)[1]
        role_number = client.PMAX_FIELD_NUMBERS[item['field_type']]
        results.append({'type': 'asset_group_asset_result',
                        'resource_name': (f'customers/{CID}/assetGroupAssets/'
                                          f'{GROUP_ID}~{asset_id}~{role_number}')})
    return {'results': results, 'request_id': None}


def _saved_union(check):
    saved = copy.deepcopy(check['existing_assets'])
    for item in check['additions']:
        asset_id = item['asset'].rsplit('/', 1)[1]
        saved.append({'resource_name':
                      f'customers/{CID}/assetGroupAssets/{GROUP_ID}~{asset_id}~'
                      f'{client.PMAX_FIELD_NUMBERS[item["field_type"]]}',
                      'asset_group': GROUP_RN, 'asset': item['asset'],
                      'field_type': item['field_type'], 'status': 'PAUSED',
                      'content': copy.deepcopy(item['content'])})
    return sorted(saved, key=lambda item: item['resource_name'])


@pytest.mark.parametrize('damage', [
    'count', 'kind', 'order', 'resource_name', 'field_type', 'duplicate', 'validate_only',
])
def test_ordered_result_identity_refuses_before_any_saved_read(
        asset_add, monkeypatch, damage):
    plan = plan_for()
    result = _result_for(plan)
    if damage == 'count':
        result['results'].pop()
    elif damage == 'kind':
        result['results'][0]['type'] = 'asset_result'
    elif damage == 'order':
        result['results'][0], result['results'][1] = (
            result['results'][1], result['results'][0])
    elif damage == 'resource_name':
        result['results'][0]['resource_name'] = result['results'][0][
            'resource_name'].replace(f'customers/{CID}', 'customers/999')
    elif damage == 'field_type':
        result['results'][0]['resource_name'] = result['results'][0][
            'resource_name'].rsplit('~', 1)[0] + '~3'
    elif damage == 'duplicate':
        result['results'][1] = copy.deepcopy(result['results'][0])
    else:
        result['validate_only'] = True
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('saved read reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


def _landed_apply(asset_add, monkeypatch, damage=None):
    state, fake = asset_add
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    check = plan.post_checks[0]
    fake.dispatch_result = _result_for(plan)

    def dispatch(sent):
        fake.dispatch_calls.append(sent)
        state['requested_assets'] = []
        state['existing_assets'] = _saved_union(check)
        if damage == 'changed_old':
            state['existing_assets'][0]['content'] = 'Changed old content'
        elif damage == 'missing':
            state['existing_assets'].pop()
        elif damage == 'extra':
            extra = copy.deepcopy(state['existing_assets'][0])
            extra.update(resource_name=f'customers/{CID}/assetGroupAssets/{GROUP_ID}~99~2',
                         asset=f'customers/{CID}/assets/99', content='Extra headline')
            state['existing_assets'].append(extra)
            state['existing_assets'].sort(key=lambda item: item['resource_name'])
        elif damage == 'foreign':
            state['existing_assets'][0]['asset_group'] = f'customers/{CID}/assetGroups/99'
        elif damage == 'status':
            state['existing_assets'][0]['status'] = 'ENABLED'
        elif damage == 'content':
            state['existing_assets'][0]['content'] = 'Changed saved text'
        elif damage == 'image':
            image = next(item for item in state['existing_assets']
                         if item['field_type'] == 'MARKETING_IMAGE')
            image['content']['image_asset']['file_size'] = 1
        elif damage == 'parent':
            state['parent_proof']['parent']['name'] = 'Changed Parent'
        elif damage == 'group':
            state['target']['name'] = 'Changed Group'
        return copy.deepcopy(fake.dispatch_result)

    monkeypatch.setattr(client, '_dispatch', dispatch)
    return pending, fake


def test_exact_saved_union_and_unchanged_parent_group_verify(asset_add, monkeypatch):
    pending, fake = _landed_apply(asset_add, monkeypatch)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is True
    assert 'exact saved asset-link union' in result['verification_scope']
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', [None, 'changed', 'dropped'])
def test_saved_verifier_preserves_preexisting_links(asset_add, monkeypatch, damage):
    state, fake = asset_add
    first_two = [_proof(item) for item in ASSETS[:2]]
    existing = []
    for item in first_two:
        asset_id = item['asset'].rsplit('/', 1)[1]
        existing.append({'resource_name':
                         f'customers/{CID}/assetGroupAssets/{GROUP_ID}~{asset_id}~2',
                         'asset_group': GROUP_RN, 'status': 'PAUSED', **item})
    additions = ASSETS[2:]
    state.clear()
    state.update(add_state(additions, existing))
    pending = draft(assets=additions)
    plan = rails._DRAFTS[pending['draft_id']].plan
    check = plan.post_checks[0]
    fake.dispatch_result = _result_for(plan)

    def dispatch(sent):
        fake.dispatch_calls.append(sent)
        state['requested_assets'] = []
        state['existing_assets'] = _saved_union(check)
        if damage == 'changed':
            state['existing_assets'][0]['content'] = 'Changed old link content'
        elif damage == 'dropped':
            state['existing_assets'] = [item for item in state['existing_assets']
                                        if item['resource_name'] != existing[0]['resource_name']]
        return copy.deepcopy(fake.dispatch_result)

    monkeypatch.setattr(client, '_dispatch', dispatch)
    result = rails.apply_draft(pending['draft_id'])
    assert result['verified'] is (damage is None)
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', [
    'changed_old', 'missing', 'extra', 'foreign', 'status', 'content', 'image',
    'parent', 'group', 'scan_failure',
])
def test_postwrite_mismatch_is_consumed_and_never_retried(asset_add, monkeypatch, damage):
    if damage == 'scan_failure':
        state, fake = asset_add
        pending = draft()
        plan = rails._DRAFTS[pending['draft_id']].plan
        fake.dispatch_result = _result_for(plan)

        def dispatch(sent):
            fake.dispatch_calls.append(sent)
            monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                                lambda *a: (_ for _ in ()).throw(
                                    rails.RailViolation('post-read failed')))
            return copy.deepcopy(fake.dispatch_result)

        monkeypatch.setattr(client, '_dispatch', dispatch)
    else:
        pending, fake = _landed_apply(asset_add, monkeypatch, damage)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_validation_only_never_reads_or_claims_saved_links(asset_add, fake_gads, monkeypatch):
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    plan = plan_for()
    result = client._dispatch_entity(plan, True)
    monkeypatch.setattr(client, 'pmax_asset_group_asset_state',
                        lambda *a: pytest.fail('saved read reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


def test_unknown_transport_consumes_without_retry(asset_add):
    from tests.test_final_fixes import mapped_error
    _, fake = asset_add
    pending = draft()
    fake.dispatch_error = mapped_error('remapped')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(pending['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('part', ['account', 'parent', 'branding', 'target', 'links', 'asset'])
def test_fresh_snapshot_drift_refuses_without_consuming(asset_add, part):
    state, fake = asset_add
    pending = draft()
    if part == 'account':
        state['parent_proof']['account']['currency_code'] = 'EUR'
    elif part == 'parent':
        state['parent_proof']['parent']['name'] = 'Changed Parent'
    elif part == 'branding':
        state['parent_proof']['branding'][0]['content'] = 'Changed Brand'
    elif part == 'target':
        state['target']['name'] = 'Changed Group'
    elif part == 'links':
        item = _canonical('99', 'HEADLINE', 'New external headline')
        state['existing_assets'].append({
            'resource_name': f'customers/{CID}/assetGroupAssets/{GROUP_ID}~99~2',
            'asset_group': GROUP_RN, 'status': 'PAUSED', **item})
    else:
        state['requested_assets'][0]['content'] = 'Changed Asset'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(pending['draft_id'])
    assert exc.value.code == 'STATE_DRIFT'
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []
