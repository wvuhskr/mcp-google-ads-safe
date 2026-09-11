"""Offline standalone text upload checks with real installed v25 messages."""
import copy
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

REAL_GAQL_ALL = client.gaql_all
RN = f'customers/{CID}/assets/123'


@pytest.fixture
def upload(monkeypatch, fake_gads):
    data = {'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'},
            'saved': [], 'reads': []}
    monkeypatch.setattr(client, 'account_info', lambda cid: {
        'rows': [{'customer': copy.deepcopy(data['account'])}], 'pages_complete': True,
        'returned_count': 1, 'total_results_count': 1})

    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        assert query == ("SELECT asset.resource_name, asset.type, asset.text_asset.text "
                         f"FROM asset WHERE asset.resource_name = '{RN}'")
        data['reads'].append(query)
        return copy.deepcopy(data['saved'])

    monkeypatch.setattr(client, 'gaql_all', read)
    return data, fake_gads


def draft(text='Cooling service', name='Cooling text', **kwargs):
    return tools.upload_text_asset(text, name, **kwargs)


def land(data, fake, text='Cooling service'):
    data['saved'] = [{'asset': {'resource_name': RN, 'type_': 'TEXT', 'text_asset': {'text': text}}}]
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    fake.mutate_response.mutate_operation_responses.append({'asset_result': {'resource_name': RN}})


@pytest.mark.parametrize('text', ['Cooling  service', 'Réparation été', '冷房修理', '界' * 45, 'a' * 90])
@pytest.mark.parametrize('name', [None, '', 'Existing different name'])
def test_real_request_exact_text_and_optional_saved_name(upload, text, name):
    data, fake = upload
    d = draft(text)
    assert d['preview']['text'] == text
    land(data, fake, text)
    if name is not None:
        data['saved'][0]['asset']['name'] = name
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified'] and 'exact text' in out['verification_scope']
    assert len(fake.mutate_calls) == len(data['reads']) == 1
    request = fake.mutate_calls[0]
    assert request['partial_failure'] is False and len(request['operations']) == 1
    req = make_type('MutateGoogleAdsRequest')
    req.customer_id = CID
    req.mutate_operations.extend(request['operations'])
    decoded = type(req).deserialize(type(req).serialize(req))
    asset = decoded.mutate_operations[0].asset_operation.create
    assert asset.text_asset.text == text and asset.name == 'Cooling text'
    assert not asset.resource_name and asset._pb.WhichOneof('asset_data') == 'text_asset'
    assert 'upload_text_asset' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('value', [None, 12, False, [], {}, '', ' ', ' x', 'x ', '\nx', 'x\n',
                                  'x\t', 'x\x00', 'x\u200b', 'x\u0378', '{KeyWord:Cool}',
                                  'x}', 'a' * 91, '界' * 46, '界' * 45 + 'a'])
def test_text_refusals_audited(upload, value):
    with pytest.raises(rails.RailViolation):
        draft(value)
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()
    assert not upload[1].mutate_calls


@pytest.mark.parametrize('name', [None, False, 1, [], '', ' ', 'a\n', 'x' * 129, '界' * 86])
def test_names(upload, name):
    with pytest.raises(rails.RailViolation):
        draft(name=name)


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
                                     ('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '2'),
                                     ('GOOGLE_ADS_READ_CUSTOMER_IDS', '2')])
def test_gates_before_reads(upload, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    monkeypatch.setattr(client, 'account_info', lambda *a: pytest.fail('read'))
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('cid', ['', 'abc', False, '123-456-7890', '0', '2'])
def test_bad_explicit_account_audited(upload, cid):
    with pytest.raises(rails.RailViolation):
        draft(customer_id=cid)
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('term', ['service', 'text'])
def test_blocked_at_draft_and_confirmation(upload, monkeypatch, term):
    d = draft()
    monkeypatch.setattr(settings, 'blocked_terms', lambda: (term,))
    with pytest.raises(rails.RailViolation):
        draft()
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not upload[1].mutate_calls


@pytest.mark.parametrize('damage', ['mixed', 'image', 'link', 'mask', 'resource', 'type', 'status',
                                     'url', 'nested', 'blank', 'name', 'update', 'remove', 'hidden'])
def test_closed_dispatch_before_provider(upload, monkeypatch, damage):
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    op = plan.operations[0]
    values = op.operation['create']
    if damage in {'mixed', 'image'}:
        plan.operations.append(copy.deepcopy(op))
        if damage == 'image':
            plan.operations[-1].operation['create'] = {'name': 'image', 'image_asset': {}}
    elif damage == 'link':
        plan.operations.append(rails.safe_create_operation('CampaignAssetService', {
            'campaign': f'customers/{CID}/campaigns/1', 'asset': RN, 'field_type': 'TEXT'}))
    elif damage == 'mask':
        object.__setattr__(op, 'update_mask', ['name'])
    elif damage in {'resource', 'type', 'status', 'url', 'hidden'}:
        values[{'resource': 'resource_name', 'type': 'type_', 'status': 'status',
                'url': 'final_urls', 'hidden': 'image_asset'}[damage]] = 'unexpected'
    elif damage == 'nested':
        values['text_asset']['unknown'] = 'x'
    elif damage == 'blank':
        values['text_asset']['text'] = ' '
    elif damage == 'name':
        values['name'] = ''
    elif damage == 'update':
        op.operation['update'] = op.operation.pop('create')
    else:
        op.operation.clear()
        op.operation['remove'] = RN
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider constructed'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


@pytest.mark.parametrize('damage', ['count', 'extra', 'type', 'negative', 'foreign', 'zero'])
def test_result_identity_before_read(upload, damage):
    data, fake = upload
    d = draft()
    land(data, fake)
    results = fake.mutate_response.mutate_operation_responses
    if damage == 'count':
        results.pop()
    elif damage == 'extra':
        results.append({'asset_result': {'resource_name': RN}})
    elif damage == 'type':
        results[0] = {'campaign_result': {'resource_name': RN}}
    else:
        results[0].asset_result.resource_name = {
            'negative': f'customers/{CID}/assets/-1', 'foreign': 'customers/2/assets/123',
            'zero': f'customers/{CID}/assets/0'}[damage]
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified'] and not data['reads']
    assert d['draft_id'] not in rails._DRAFTS


@pytest.mark.parametrize('damage', ['missing', 'extra', 'identity', 'image', 'unknown_type', 'text',
                                     'case', 'spacing', 'unicode', 'incomplete', 'error', 'unreadable'])
def test_content_failures_consumed(upload, monkeypatch, damage):
    data, fake = upload
    d = draft('Réparation été')
    land(data, fake, 'Réparation été')
    saved = data['saved'][0]['asset']
    if damage == 'missing':
        data['saved'] = []
    elif damage == 'extra':
        data['saved'] *= 2
    elif damage == 'identity':
        saved['resource_name'] = ''
    elif damage in {'image', 'unknown_type'}:
        saved['type_'] = 'IMAGE' if damage == 'image' else 'UNSPECIFIED'
    elif damage == 'incomplete':
        saved.pop('text_asset')
    elif damage == 'unreadable':
        data['saved'] = [None]
    elif damage == 'error':
        monkeypatch.setattr(client, 'gaql_all', lambda *a: (_ for _ in ()).throw(ValueError('read failed')))
    else:
        saved['text_asset']['text'] = {'text': None, 'case': 'réparation été',
                                     'spacing': 'Réparation  été', 'unicode': 'Réparation été'}[damage]
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified']
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls) == 1


@pytest.mark.parametrize('field', ['text', 'name'])
def test_intent_and_plan_drift(upload, field):
    intent = rails.UploadTextAssetIntent(CID, 'Cooling service', 'Cooling text')
    d = rails.upload_text_draft(intent)
    object.__setattr__(intent, field, 'Changed')
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'STATE_DRIFT'
    d = draft()
    values = rails._DRAFTS[d['draft_id']].plan.operations[0].operation['create']
    (values if field == 'name' else values['text_asset'])[field] = 'Changed'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'PLAN_TAMPERED' and d['draft_id'] not in rails._DRAFTS
    assert not upload[1].mutate_calls


def test_account_and_expiry(upload, monkeypatch):
    data, fake = upload
    d = draft()
    data['account']['time_zone'] = 'UTC'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'STATE_DRIFT'
    monkeypatch.setattr(client, 'account_info', lambda *a: {'rows': [], 'pages_complete': False})
    with pytest.raises(rails.RailViolation):
        draft()
    rails._DRAFTS[d['draft_id']].created_at -= 4000
    with pytest.raises(rails.RailViolation, match='expired'):
        rails.apply_draft(d['draft_id'])
    assert not fake.mutate_calls


def test_validate_only_no_saved_read(upload):
    data, fake = upload
    d = draft()
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    out = client._dispatch_entity(rails._DRAFTS[d['draft_id']].plan, True)
    assert out['validate_only'] and not data['reads']
    with pytest.raises(rails.RailViolation, match='validation-only'):
        client.verify_created_results(rails._DRAFTS[d['draft_id']].plan.post_checks, out)


def test_unknown_consumed_with_structured_details(upload, monkeypatch):
    d = draft()
    error = rails.UnknownWriteOutcome('unknown', request_id='request', failure={'code': 'X'})
    monkeypatch.setattr(client, '_dispatch', lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(rails.UnknownWriteOutcome) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.request_id == 'request' and exc.value.failure == {'code': 'X'}
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert 'unknown' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('text', ['Cooling service', '冷房  Réparation'])
@pytest.mark.parametrize('total', [1, 2])
def test_real_complete_readback(upload, monkeypatch, text, total):
    from tests.conftest import make_row, make_search_response
    data, fake = upload
    d = draft(text)
    land(data, fake, text)
    monkeypatch.setattr(client, 'gaql_all', REAL_GAQL_ALL)
    fake.search_responses[(CID, '')] = make_search_response([
        make_row(**{'asset.resource_name': RN, 'asset.type_': 'TEXT', 'asset.text_asset.text': text})
    ], total=total)
    out = rails.apply_draft(d['draft_id'])
    assert out['verified'] is (total == 1)
    assert len(fake.search_requests) == 1 and len(fake.mutate_calls) == 1
    request = fake.search_requests[0]
    assert request.search_settings.return_total_results_count
    assert f"asset.resource_name = '{RN}'" in request.query
    assert 'asset.text_asset.text' in request.query and 'LIMIT' not in request.query
    assert d['draft_id'] not in rails._DRAFTS


def test_schema():
    import asyncio

    from mcp_google_ads_safe.app import mcp

    async def check():
        tool = next(t for t in await mcp.list_tools() if t.name == 'upload_text_asset')
        assert set(tool.input_schema['properties']) == {'text', 'name', 'customer_id'}
        assert set(tool.input_schema['required']) == {'text', 'name'}
        for phrase in ['Offline', '90', 'twice', 'blocked terms', 'confirmation', 'exact text']:
            assert phrase in tool.description
    asyncio.run(check())


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
                                     ('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '2'),
                                     ('GOOGLE_ADS_READ_CUSTOMER_IDS', '2')])
def test_confirmation_gates_before_reads(upload, monkeypatch, env, value):
    d = draft()
    monkeypatch.setenv(env, value)
    monkeypatch.setattr(client, 'account_info', lambda *a: pytest.fail('read'))
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not upload[1].mutate_calls


@pytest.mark.parametrize('term', ['text', 'service'])
def test_dispatch_content_rechecked_before_provider(upload, monkeypatch, term):
    plan = rails._DRAFTS[draft()['draft_id']].plan
    monkeypatch.setattr(settings, 'blocked_terms', lambda: (term,))
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider constructed'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
