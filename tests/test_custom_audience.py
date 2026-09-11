"""Offline standalone audience creation checks with real installed v25 messages."""
import copy
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

REAL_GAQL_ALL = client.gaql_all
RN = f'customers/{CID}/userLists/123'


@pytest.fixture
def upload(monkeypatch, fake_gads):
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', 'true')
    data = {'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'},
            'saved': [], 'inventory': [], 'reads': []}
    monkeypatch.setattr(client, 'account_info', lambda cid: {
        'rows': [{'customer': copy.deepcopy(data['account'])}], 'pages_complete': True,
        'returned_count': 1, 'total_results_count': 1})
    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        if 'WHERE' not in query:
            return copy.deepcopy(data['inventory'])
        data['reads'].append(query)
        return copy.deepcopy(data['saved'])
    monkeypatch.setattr(client, 'gaql_all', read)
    return data, fake_gads


def draft(text=None, name='Cooling text', **kwargs):
    return tools.create_custom_audience(name, ['/service'] if text is None else text, **kwargs)


def land(data, fake, text=None):
    data['saved'] = [{'user_list': {'resource_name': RN, 'type_': 'RULE_BASED',
        'name': 'Cooling text', 'access_reason': 'OWNED', 'read_only': False,
        'membership_status': 'OPEN', 'rule_based_user_list': client.audience_rules(text or ['/service'])}}]
    fake.mutate_response = make_type('MutateGoogleAdsResponse')
    fake.mutate_response.mutate_operation_responses.append({'user_list_result': {'resource_name': RN}})


@pytest.mark.parametrize('count', [1, 10])
def test_real_request(upload, count):
    data, fake = upload
    urls = ['/service' + str(i) for i in range(count)]
    d = draft(urls)
    land(data, fake, urls)
    data['saved'][0]['user_list']['rule_based_user_list']['flexible_rule_user_list']['inclusive_operands'].reverse()
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified']
    req = make_type('MutateGoogleAdsRequest')
    req.customer_id = CID
    req.mutate_operations.extend(fake.mutate_calls[0]['operations'])
    decoded = type(req).deserialize(type(req).serialize(req))
    obj = decoded.mutate_operations[0].user_list_operation.create
    assert obj.membership_status.name == 'OPEN' and not obj.membership_life_span
    assert not obj.rule_based_user_list.prepopulation_status
    flex = obj.rule_based_user_list.flexible_rule_user_list
    assert flex.inclusive_rule_operator.name == 'OR' and not flex.exclusive_operands
    assert len(flex.inclusive_operands) == count
    for i, operand in enumerate(flex.inclusive_operands):
        assert operand.lookback_window_days == 30 and operand.rule.rule_type.name == 'AND_OF_ORS'
        item = operand.rule.rule_item_groups[0].rule_items[0]
        assert item.name == 'url__' and item.string_rule_item.operator.name == 'CONTAINS'
        assert item.string_rule_item.value == urls[i]
    assert not req.partial_failure


@pytest.mark.parametrize('value', [[], 'x', False, 1, {}, [''], [' '], [' x'], ['x '],
    ['x\n'], ['x\u200b'], ['{x}'], ['x*'], ['x?'], ['a'*257], ['a','A'], ['a']*11])
def test_rule_refusals_audited(upload, value):
    with pytest.raises(rails.RailViolation):
        draft(value)
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()
    assert not upload[1].mutate_calls


@pytest.mark.parametrize('name', [None, False, 1, [], '', ' ', 'a\n', 'x' * 129, '界' * 86])
def test_names(upload, name):
    with pytest.raises(rails.RailViolation):
        draft(name=name)


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
                                     ('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', 'false'),
                                     ('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', 'garbage'),
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


@pytest.mark.parametrize('damage', ['mixed', 'mask', 'resource_name', 'type_', 'status',
    'membership_life_span', 'hidden', 'update', 'remove', 'rule', 'lookback', 'exclusive', 'group'])
def test_closed_dispatch_before_provider(upload, monkeypatch, damage):
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    op = plan.operations[0]
    values = op.operation['create']
    flex = values['rule_based_user_list']['flexible_rule_user_list']
    if damage == 'mixed':
        plan.operations.append(copy.deepcopy(op))
    elif damage == 'mask':
        object.__setattr__(op, 'update_mask', ['name'])
    elif damage in {'update', 'remove'}:
        op.operation[damage] = op.operation.pop('create')
    elif damage == 'rule':
        flex['inclusive_operands'][0]['rule']['rule_type'] = 'OR_OF_ANDS'
    elif damage == 'lookback':
        flex['inclusive_operands'][0]['lookback_window_days'] = 31
    elif damage == 'exclusive':
        flex['exclusive_operands'] = flex['inclusive_operands']
    elif damage == 'group':
        flex['inclusive_operands'][0]['rule']['rule_item_groups'] *= 2
    else:
        values[damage] = 'unexpected'
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
        results.append({'user_list_result': {'resource_name': RN}})
    elif damage == 'type':
        results[0] = {'campaign_result': {'resource_name': RN}}
    else:
        results[0].user_list_result.resource_name = {
            'negative': f'customers/{CID}/userLists/-1', 'foreign': 'customers/2/userLists/123',
            'zero': f'customers/{CID}/userLists/0'}[damage]
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified'] and not data['reads']
    assert d['draft_id'] not in rails._DRAFTS


@pytest.mark.parametrize('damage', ['missing', 'extra', 'name', 'type_', 'membership_status',
    'access_reason', 'read_only', 'rules', 'exclusion', 'extra_field'])
def test_content_failures_consumed(upload, damage):
    data, fake = upload
    d = draft()
    land(data, fake)
    saved = data['saved'][0]['user_list']
    if damage == 'missing':
        data['saved'] = []
    elif damage == 'extra':
        data['saved'] *= 2
    elif damage == 'rules':
        saved['rule_based_user_list'] = client.audience_rules(['/Service'])
    elif damage == 'exclusion':
        flex = saved['rule_based_user_list']['flexible_rule_user_list']
        flex['exclusive_operands'] = flex['inclusive_operands']
    elif damage == 'extra_field':
        saved['rule_based_user_list']['unknown'] = 'x'
    else:
        saved[damage] = True if damage == 'read_only' else 'OTHER'
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified']
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls) == 1


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


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
                                     ('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', 'false'),
                                     ('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', 'garbage'),
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


@pytest.mark.parametrize('ownership', ['OWNED', 'SHARED', 'UNKNOWN'])
def test_inventory_collision(upload, ownership):
    data, fake = upload
    data['inventory'] = [{'user_list': {'resource_name': RN, 'id': '123', 'name': 'COOLING TEXT',
        'access_reason': ownership, 'read_only': False, 'type_': 'RULE_BASED', 'rule_based_user_list': {}}}]
    if ownership == 'SHARED':
        draft()
    else:
        with pytest.raises(rails.RailViolation):
            draft()


def test_inventory_and_input_drift(upload):
    data, fake = upload
    d = draft()
    data['inventory'] = [{'user_list': {'resource_name': RN, 'id': '123', 'name': 'Different',
        'access_reason': 'OWNED', 'read_only': False, 'type_': 'RULE_BASED', 'rule_based_user_list': {}}}]
    with pytest.raises(rails.RailViolation, match='changed'):
        rails.apply_draft(d['draft_id'])
    intent = rails.CreateCustomAudienceIntent(CID, 'Cooling text', ['/service'])
    d = rails.custom_audience_draft(intent)
    intent.url_contains.append('/new')
    with pytest.raises(rails.RailViolation, match='changed'):
        rails.apply_draft(d['draft_id'])
    d = draft()
    rails._DRAFTS[d['draft_id']].plan.operations[0].operation['create']['name'] = 'tampered'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(d['draft_id'])
    assert exc.value.code == 'PLAN_TAMPERED'


@pytest.mark.parametrize('total', [1, 2])
def test_real_complete_readback(upload, monkeypatch, total):
    from tests.conftest import make_search_response
    data, fake = upload
    d = draft()
    land(data, fake)
    row = make_type('GoogleAdsRow')
    row.user_list = data['saved'][0]['user_list']
    fake.search_responses[(CID, '')] = make_search_response([row], total=total)
    mocked = client.gaql_all
    monkeypatch.setattr(client, 'gaql_all', lambda query, cid:
                        REAL_GAQL_ALL(query, cid) if 'WHERE' in query else mocked(query, cid))
    out = rails.apply_draft(d['draft_id'])
    assert out['verified'] is (total == 1)
    assert len(fake.search_requests) == 1
    assert fake.search_requests[0].search_settings.return_total_results_count


def test_default_flag_off(upload, monkeypatch):
    monkeypatch.delenv('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT')
    monkeypatch.setattr(client, 'account_info', lambda *a: pytest.fail('read'))
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('field', ['access_reason', 'read_only', 'id', 'name', 'rule_based_user_list'])
def test_unreadable_inventory(upload, field):
    data, fake = upload
    row = {'resource_name': RN, 'id': '123', 'name': 'Other', 'access_reason': 'OWNED',
           'read_only': False, 'type_': 'RULE_BASED', 'rule_based_user_list': {}}
    row.pop(field)
    data['inventory'] = [{'user_list': row}]
    with pytest.raises(rails.RailViolation):
        draft()


def test_schema():
    import asyncio

    from mcp_google_ads_safe.app import mcp
    async def check():
        tool = next(t for t in await mcp.list_tools() if t.name == 'create_custom_audience')
        assert set(tool.input_schema['properties']) == {'name', 'url_contains', 'customer_id'}
        assert set(tool.input_schema['required']) == {'name', 'url_contains'}
    asyncio.run(check())
