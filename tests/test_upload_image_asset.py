"""Offline image upload checks: real pixels and real v25 messages, no credentials."""
import base64
import copy
import io
from pathlib import Path

import pytest
from PIL import Image

from mcp_google_ads_safe import audit, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

RN = f'customers/{CID}/assets/123'


def encoded(fmt='PNG', color='red', **kwargs):
    output = io.BytesIO()
    Image.new('RGB', (12, 9), color).save(output, format=fmt, **kwargs)
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def upload(monkeypatch, fake_gads):
    state = {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'}
    data = {'account': state, 'reads': [], 'saved': None}
    monkeypatch.setattr(client, 'account_info', lambda cid: {
        'rows': [{'customer': copy.deepcopy(data['account'])}], 'pages_complete': True,
        'returned_count': 1, 'total_results_count': 1})

    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query and f"asset.resource_name = '{RN}'" in query
        assert 'image_asset.data' not in query
        data['reads'].append(query)
        return copy.deepcopy(data['saved']) if data['saved'] is not None else []

    monkeypatch.setattr(client, 'gaql_all', read)
    return data, fake_gads


def draft(image=None, **kwargs):
    return tools.upload_image_asset(encoded() if image is None else image, 'Cooling photo', **kwargs)


def land(data, fake, d, name='Existing duplicate name'):
    plan = rails._DRAFTS[d['draft_id']].plan
    data['saved'] = [{'asset': {'resource_name': RN, 'name': name, 'type_': 'IMAGE',
                               **copy.deepcopy(plan.post_checks[0]['expected'])}}]
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    fake.mutate_response.mutate_operation_responses.append({'asset_result': {'resource_name': RN}})


@pytest.mark.parametrize('fmt', ['JPEG', 'PNG'])
def test_real_bytes_atomic_request_metadata_only(upload, fmt):
    data, fake = upload
    image = encoded(fmt)
    d = draft(image)
    assert image not in str(d)
    land(data, fake, d)
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified'] and 'metadata only' in out['verification_scope']
    assert len(fake.mutate_calls) == 1 and len(data['reads']) == 1
    request = fake.mutate_calls[0]
    assert request['partial_failure'] is False and len(request['operations']) == 1
    asset = request['operations'][0].asset_operation.create
    assert asset.image_asset.data == base64.b64decode(image)
    assert asset.image_asset.mime_type.name == ('IMAGE_JPEG' if fmt == 'JPEG' else 'IMAGE_PNG')
    assert not asset.resource_name
    assert image not in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('value', ['', 'file.png', 'https://example.com/x', 'data:image/png;base64,eA==',
                                 ' eA==', 'eA==\n', 'eB==', '====', 'é', 'eA===', 'eA==', 12])
def test_strict_input_refused_audited(upload, value):
    with pytest.raises(rails.RailViolation):
        draft(value)
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()
    assert not upload[1].mutate_calls


@pytest.mark.parametrize('fmt', ['GIF', 'BMP', 'TIFF', 'WEBP'])
def test_unsupported(upload, fmt):
    with pytest.raises(rails.RailViolation):
        draft(encoded(fmt))


def test_animated_and_truncated(upload):
    first = Image.new('RGB', (12, 9), 'red')
    output = io.BytesIO()
    first.save(output, 'PNG', save_all=True, append_images=[Image.new('RGB', (12, 9), 'blue')])
    for raw in [output.getvalue(), base64.b64decode(encoded('JPEG'))[:-10],
                base64.b64decode(encoded())[:-12]]:
        with pytest.raises(rails.RailViolation):
            draft(base64.b64encode(raw).decode())


def test_caps_before_decode_and_load(upload, monkeypatch):
    monkeypatch.setattr(client, 'IMAGE_BYTE_CAP', 3)
    monkeypatch.setattr(client.base64, 'b64decode', lambda *a, **k: pytest.fail('decoded oversize'))
    with pytest.raises(rails.RailViolation):
        draft('x' * 8)


def test_pixel_cap_before_load(upload, monkeypatch):
    image = encoded()
    monkeypatch.setattr(client, 'IMAGE_PIXEL_CAP', 100)
    monkeypatch.setattr(Image.Image, 'load', lambda *a: pytest.fail('loaded over pixel cap'))
    with pytest.raises(rails.RailViolation):
        draft(image)


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
                                     ('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '2'),
                                     ('GOOGLE_ADS_READ_CUSTOMER_IDS', '2')])
def test_gates_before_decode_reads(upload, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    monkeypatch.setattr(client, 'image_metadata', lambda *a: pytest.fail('decoded'))
    monkeypatch.setattr(client, 'account_info', lambda *a: pytest.fail('read'))
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('cid', ['', 'abc', False, '123-456-7890', '0', '2'])
def test_explicit_bad_account_not_defaulted(upload, cid):
    with pytest.raises(rails.RailViolation):
        draft(customer_id=cid)


@pytest.mark.parametrize('name', ['', ' ', 'a\n', 'x' * 129, '界' * 100])
def test_names(upload, name):
    with pytest.raises(rails.RailViolation):
        tools.upload_image_asset(encoded(), name)


def test_blocked_name(upload, monkeypatch):
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ('cooling',))
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('damage', ['mixed', 'link', 'mask', 'resource', 'type', 'status', 'url',
                                     'extra_image', 'mime', 'bad_data', 'update', 'remove'])
def test_dispatch_closed_before_provider(upload, monkeypatch, damage):
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    op = plan.operations[0]
    values = op.operation['create']
    if damage == 'mixed':
        plan.operations.append(copy.deepcopy(op))
    elif damage == 'link':
        plan.operations.append(rails.safe_create_operation('CampaignAssetService', {
            'campaign': f'customers/{CID}/campaigns/1', 'asset': RN, 'field_type': 'IMAGE'}))
    elif damage == 'mask':
        object.__setattr__(op, 'update_mask', ['name'])
    elif damage == 'resource':
        values['resource_name'] = f'customers/{CID}/assets/-1'
    elif damage == 'type':
        values['type_'] = 'IMAGE'
    elif damage == 'status':
        values['status'] = 'PAUSED'
    elif damage == 'url':
        values['final_urls'] = ['https://example.com']
    elif damage == 'extra_image':
        values['image_asset']['full_size'] = {'width_pixels': 12}
    elif damage == 'mime':
        values['image_asset']['mime_type'] = 'IMAGE_JPEG'
    elif damage == 'bad_data':
        values['image_asset']['data'] = 'eA=='
    elif damage == 'update':
        op.operation['update'] = op.operation.pop('create')
    else:
        op.operation.clear()
        op.operation['remove'] = RN
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider constructed'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


@pytest.mark.parametrize('damage', ['count', 'extra', 'type', 'negative', 'foreign', 'zero'])
def test_result_refusal_before_reads(upload, damage):
    data, fake = upload
    d = draft()
    land(data, fake, d)
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


@pytest.mark.parametrize('damage', ['missing', 'extra', 'identity', 'name', 'type', 'mime', 'size', 'width', 'height', 'incomplete', 'error'])
def test_metadata_failures_consumed(upload, monkeypatch, damage):
    data, fake = upload
    d = draft()
    land(data, fake, d)
    saved = data['saved'][0]['asset']
    if damage == 'missing':
        data['saved'] = []
    elif damage == 'extra':
        data['saved'] *= 2
    elif damage in {'identity', 'name', 'type'}:
        saved[{'identity': 'resource_name', 'name': 'name', 'type': 'type_'}[damage]] = ''
    elif damage == 'mime':
        saved['image_asset']['mime_type'] = 'IMAGE_JPEG'
    elif damage == 'size':
        saved['image_asset']['file_size'] += 1
    elif damage in {'width', 'height'}:
        saved['image_asset']['full_size'][damage + '_pixels'] += 1
    elif damage == 'incomplete':
        saved['image_asset'].pop('full_size')
    else:
        monkeypatch.setattr(client, 'gaql_all', lambda *a: (_ for _ in ()).throw(ValueError(encoded())))
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified'] and encoded() not in str(out)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls) == 1


def test_drift_and_tampering(upload):
    data, fake = upload
    d = draft()
    data['account']['time_zone'] = 'UTC'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'STATE_DRIFT' and d['draft_id'] in rails._DRAFTS
    data['account']['time_zone'] = 'America/New_York'
    rails._DRAFTS[d['draft_id']].plan.operations[0].operation['create']['image_asset']['data'] = encoded(color='blue')
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'PLAN_TAMPERED' and d['draft_id'] not in rails._DRAFTS
    assert not fake.mutate_calls


def test_validate_only_and_expiry(upload):
    data, fake = upload
    d = draft()
    plan = rails._DRAFTS[d['draft_id']].plan
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    assert client._dispatch_entity(plan, True)['validate_only']
    assert not data['reads']
    rails._DRAFTS[d['draft_id']].created_at -= 4000
    with pytest.raises(rails.RailViolation, match='expired'):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls) == 1


@pytest.mark.parametrize('transport', [True, False])
def test_error_payload_not_exposed(upload, monkeypatch, transport):
    d = draft()
    image = encoded()
    error = rails.UnknownWriteOutcome(image, request_id='safe-request', failure={'trigger': image}) if transport else ValueError(image)
    monkeypatch.setattr(client, '_dispatch', lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises((rails.UnknownWriteOutcome, rails.RailViolation)) as exc:
        rails.apply_draft(d['draft_id'])
    assert image not in str(exc.value) and image not in Path(audit.AUDIT_PATH).read_text()
    assert d['draft_id'] not in rails._DRAFTS


def test_intent_payload_drift_is_refused(upload):
    intent = rails.UploadImageAssetIntent(CID, encoded(), 'Photo')
    d = rails.upload_image_draft(intent)
    object.__setattr__(intent, 'image_base64', encoded(color='blue'))
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'STATE_DRIFT' and not upload[1].mutate_calls


def test_bomb_warning_and_relaxed_decoder_refused(upload, monkeypatch):
    from PIL import ImageFile
    image = encoded()
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)
    with pytest.raises(rails.RailViolation):
        draft(image)
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89_478_485)
    monkeypatch.setattr(ImageFile, 'LOAD_TRUNCATED_IMAGES', True)
    with pytest.raises(rails.RailViolation):
        draft(image)


def test_account_envelope_incomplete_refuses(upload, monkeypatch):
    monkeypatch.setattr(client, 'account_info', lambda *a: {
        'rows': [{'customer': upload[0]['account']}], 'pages_complete': False,
        'returned_count': 1, 'total_results_count': 2})
    with pytest.raises(rails.RailViolation):
        draft()


def test_bad_provider_identity_does_not_leak(upload):
    data, fake = upload
    d = draft()
    land(data, fake, d)
    fake.mutate_response.mutate_operation_responses[0].asset_result.resource_name = encoded()
    out = rails.apply_draft(d['draft_id'])
    assert not out['verified'] and out['result']['resource_names'] == []
    assert encoded() not in str(out) and encoded() not in Path(audit.AUDIT_PATH).read_text()


def test_image_tool_schema():
    import asyncio

    from mcp_google_ads_safe.app import mcp

    async def check():
        tool = next(t for t in await mcp.list_tools() if t.name == 'upload_image_asset')
        assert set(tool.input_schema['properties']) == {'image_base64', 'name', 'customer_id'}
        assert set(tool.input_schema['required']) == {'image_base64', 'name'}
        for text in ('5,120,000', '25,000,000', 'confirmation', 'base64', 'paused', 'deleted'):
            assert text in tool.description
    asyncio.run(check())


def test_structured_provider_codes_preserved_without_echo(upload, monkeypatch):
    failure = make_type('GoogleAdsFailure')
    failure.errors.append({'error_code': {'image_error': 'INVALID_IMAGE'}, 'message': encoded(),
                           'trigger': {'string_value': encoded()}, 'location': {
                               'field_path_elements': [{'field_name': 'mutate_operations', 'index': 0}]}})
    error = rails.UnknownWriteOutcome(encoded(), request_id='safe-request', failure=failure)
    d = draft()
    monkeypatch.setattr(client, '_dispatch', lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(rails.UnknownWriteOutcome) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.request_id == 'safe-request'
    assert exc.value.failure['errors'][0]['error_code']['image_error'] > 0
    assert exc.value.failure['errors'][0]['operation_indices'] == [0]
    assert encoded() not in str(exc.value.failure) and encoded() not in Path(audit.AUDIT_PATH).read_text()
