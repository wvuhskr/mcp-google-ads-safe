import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import make_row

CID = '1234567890'


def account_row():
    return make_row(**{'customer.resource_name': f'customers/{CID}', 'customer.id': int(CID),
        'customer.descriptive_name': 'Test', 'customer.currency_code': 'USD',
        'customer.time_zone': 'America/New_York', 'customer.manager': False, 'customer.status': 'ENABLED'})


def test_minimum_empty_set(monkeypatch):
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'true')
    monkeypatch.setattr(client, '_search_one_page', lambda q, c, t:
                        ([account_row()], None, 1) if ' FROM customer' in q else ([], None, 0))
    draft = tools.create_shared_negative_set('Cooling exclusions', CID)
    plan = rails._DRAFTS[draft['draft_id']].plan
    assert plan.operations == [rails.safe_create_operation('SharedSetService', {
        'name': 'Cooling exclusions', 'type_': 'NEGATIVE_KEYWORDS'})]


def set_row(sid=11, name='Cooling exclusions'):
    return make_row(**{'shared_set.resource_name': f'customers/{CID}/sharedSets/{sid}',
        'shared_set.id': sid, 'shared_set.name': name, 'shared_set.type_': 'NEGATIVE_KEYWORDS',
        'shared_set.status': 'ENABLED', 'shared_set.member_count': 0, 'shared_set.reference_count': 0})


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'true')
    state = {'customer': [account_row()], 'shared_set': [], 'exact': [set_row()],
             'shared_criterion': [], 'campaign_shared_set': []}
    reads = []

    def page(query, cid, token):
        assert cid == CID and token is None
        assert 'SELECT *' not in query and 'SELECT shared_set FROM' not in query
        reads.append(query)
        kind = query.split(' FROM ')[1].split()[0]
        if 'WHERE shared_set.resource_name' in query:
            kind = 'exact'
        value = state[kind]
        if isinstance(value, Exception):
            raise value
        return value, None, len(value)

    monkeypatch.setattr(client, '_search_one_page', page)
    return state, reads


def plan_for():
    return rails.compile(rails.CreateSharedNegativeSetIntent(CID, 'Cooling exclusions')).plan


def result_for():
    return {'results': [{'type': 'shared_set_result',
                        'resource_name': f'customers/{CID}/sharedSets/11'}], 'request_id': 'offline'}


def save(shared):
    shared[0]['shared_set'] = [set_row()]


@pytest.mark.parametrize('value', [None, True, 7, 1.1, [], {}, 'null', '0', '01', '+1', '-1', ' 1', '1 ', '١', str(2**63), 'customers/1'])
def test_original_customer_ids(shared, value):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    out = boundary(app.mcp, 'create_shared_negative_set', {'name': 'Test', 'customer_id': value})
    assert out.is_error
    assert shared[1] == []


@pytest.mark.parametrize('value', [None, True, 7, 1.1, [], {}, '', ' ', 'x\n', 'x\u200d', 'x'*129, '界'*86])
def test_original_name_limits(shared, value):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    assert boundary(app.mcp, 'create_shared_negative_set', {'name': value}).is_error
    assert shared[1] == []


def test_omitted_customer_unknown_keys_and_name_preservation(shared):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    assert boundary(app.mcp, 'create_shared_negative_set', {'name': 'Test', 'status': 'ENABLED'}).is_error
    assert not boundary(app.mcp, 'create_shared_negative_set', {'name': ' Test '}).is_error
    plan = rails.compile(rails.CreateSharedNegativeSetIntent(CID, 'é'*127)).plan
    assert plan.operations[0].operation['create']['name'] == 'é'*127


@pytest.mark.parametrize('gate,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
    ('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'false'),
    ('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'invalid'),
    ('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999'), ('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')])
def test_gates_before_reads_and_rechecked(shared, monkeypatch, gate, value):
    draft = tools.create_shared_negative_set('Cooling exclusions', CID)
    shared[1].clear()
    monkeypatch.setenv(gate, value)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    with pytest.raises(rails.RailViolation):
        plan_for()
    assert shared[1] == [] and draft['draft_id'] in rails._DRAFTS


def test_default_off_and_content_before_reads(shared, monkeypatch):
    monkeypatch.delenv('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT')
    with pytest.raises(rails.RailViolation):
        plan_for()
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'true')
    monkeypatch.setattr(rails.settings, 'blocked_terms', lambda: ('cooling',))
    with pytest.raises(rails.RailViolation):
        plan_for()
    assert shared[1] == []


def test_collision_and_complete_name_pages(shared, monkeypatch):
    calls = []

    def page(q, cid, token):
        calls.append((q, token))
        if ' FROM customer' in q:
            return [account_row()], None, 1
        return ([], 'next', 1) if token is None else ([set_row()], None, 1)

    monkeypatch.setattr(client, '_search_one_page', page)
    with pytest.raises(rails.RailViolation, match='collision'):
        plan_for()
    assert calls[-1][1] == 'next' and all('LIMIT' not in q for q, _ in calls)
    monkeypatch.setattr(client, '_search_one_page', lambda *a: ([], None, 1))
    with pytest.raises(rails.RailViolation) as exc:
        plan_for()
    assert exc.value.code == 'SCAN_INCOMPLETE'


@pytest.mark.parametrize('field,value', [('resource_name', 'customers/999/sharedSets/12'),
    ('id', 0), ('name', ''), ('type_', 'NEGATIVE_PLACEMENTS'), ('status', 'REMOVED')])
def test_raw_name_inventory_validation(shared, field, value):
    row = set_row(12, 'Other')
    setattr(row.shared_set, field, value)
    shared[0]['shared_set'] = [row]
    with pytest.raises(rails.RailViolation):
        plan_for()


@pytest.mark.parametrize('field', ['id', 'name', 'resource_name'])
def test_raw_name_optional_presence(shared, field):
    row = set_row(12, 'Other')
    row.shared_set._pb.ClearField(field)
    shared[0]['shared_set'] = [row]
    with pytest.raises(rails.RailViolation):
        plan_for()


@pytest.mark.parametrize('population', [[{}], [set_row(12, 'Other'), set_row(12, 'Other')],
                                       [set_row(12, 'Other'), set_row(13, 'Other')]])
def test_sparse_and_duplicate_inventory_refuses(shared, population):
    shared[0]['shared_set'] = population
    with pytest.raises(rails.RailViolation):
        plan_for()


def test_sdk_roundtrip_and_atomic_validate_only(shared, fake_gads):
    from tests.conftest import make_type
    plan = plan_for()
    context = client.validate_mutation_plan(plan)
    message = client._build_mutate_operation(fake_gads, plan.operations[0], context)
    decoded = type(message).deserialize(type(message).serialize(message))
    assert decoded._pb.WhichOneof('operation') == 'shared_set_operation'
    operation = decoded.shared_set_operation
    assert operation._pb.WhichOneof('operation') == 'create'
    assert not operation.update_mask.paths
    assert {field.name for field, _ in operation.create._pb.ListFields()} == {'name', 'type_'}
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    out = client._dispatch_entity(plan, True)
    assert out['validate_only'] and len(fake_gads.mutate_calls) == 1
    req = fake_gads.mutate_calls[0]
    assert req['customer_id'] == CID and req['partial_failure'] is False and req['validate_only']


@pytest.mark.parametrize('field,value', [('status', 'ENABLED'), ('type_', 'NEGATIVE_PLACEMENTS'),
    ('id', 11), ('resource_name', f'customers/{CID}/sharedSets/11'),
    ('member_count', 0), ('reference_count', 0), ('name', 'Other')])
def test_forged_create_fields_before_factory(shared, field, value):
    plan = plan_for()
    plan.operations[0].operation['create'][field] = value
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


@pytest.mark.parametrize('mode', ['missing_checks', 'marker', 'extra_check', 'extra_operation',
    'mask', 'update', 'remove', 'future_member', 'future_link', 'extra_proof', 'extra_account',
    'manager', 'status', 'currency', 'account_id', 'collision', 'duplicate', 'foreign_set',
    'result_index_bool', 'validate_false', 'cid_int'])
def test_closed_admission_before_factory(shared, mode):
    import dataclasses
    plan = plan_for()
    check = plan.post_checks[0]
    if mode == 'missing_checks':
        plan.post_checks.clear()
    elif mode == 'marker':
        del check['shared_negative_create']
    elif mode == 'extra_check':
        check['extra'] = True
    elif mode == 'extra_operation':
        plan.operations.append(plan.operations[0])
    elif mode == 'mask':
        plan.operations[0] = dataclasses.replace(plan.operations[0], update_mask=['name'])
    elif mode in {'update', 'remove'}:
        plan.operations[0].operation.clear()
        plan.operations[0].operation[mode] = {} if mode == 'update' else f'customers/{CID}/sharedSets/11'
    elif mode in {'future_member', 'future_link'}:
        plan.operations[0] = dataclasses.replace(plan.operations[0], service=(
            'SharedCriterionService' if mode == 'future_member' else 'CampaignSharedSetService'))
    elif mode == 'extra_proof':
        check['proof']['extra'] = []
    elif mode == 'extra_account':
        check['proof']['account']['extra'] = 1
    elif mode in {'manager', 'status', 'currency', 'account_id'}:
        field, value = {'manager': ('manager', True), 'status': ('status', 'SUSPENDED'),
            'currency': ('currency_code', ''), 'account_id': ('id', '999')}[mode]
        check['proof']['account'][field] = value
    elif mode in {'collision', 'duplicate', 'foreign_set'}:
        item = {'resource_name': f'customers/{CID}/sharedSets/12', 'id': '12',
                'name': 'Cooling exclusions' if mode == 'collision' else 'Other',
                'type': 'NEGATIVE_KEYWORDS', 'status': 'ENABLED'}
        if mode == 'foreign_set':
            item['resource_name'] = 'customers/999/sharedSets/12'
        check['proof']['sets'] = [item] * (2 if mode == 'duplicate' else 1)
    elif mode == 'result_index_bool':
        check['result_index'] = False
    elif mode == 'validate_false':
        plan = dataclasses.replace(plan, validate_only_supported=False)
    elif mode == 'cid_int':
        plan = dataclasses.replace(plan, mutate_customer_id=int(CID))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


@pytest.mark.parametrize('service', ['SharedSetService', 'SharedCriterionService', 'CampaignSharedSetService'])
def test_unproved_shared_builder_refuses(shared, fake_gads, service):
    op = rails.safe_create_operation(service, {'name': 'x'})
    with pytest.raises(rails.RailViolation):
        client._build_mutate_operation(fake_gads, op)


@pytest.mark.parametrize('mode', ['missing', 'extra', 'wrong_type', 'foreign', 'zero', 'leading_zero',
                                 'overflow', 'validate_only'])
def test_bad_ordered_result_before_reads(shared, mode):
    plan = plan_for()
    shared[1].clear()
    result = result_for()
    if mode == 'missing':
        result['results'] = []
    elif mode == 'extra':
        result['results'] *= 2
    elif mode == 'wrong_type':
        result['results'][0]['type'] = 'campaign_result'
    elif mode == 'validate_only':
        result['validate_only'] = True
    else:
        result['results'][0]['resource_name'] = {'foreign': 'customers/999/sharedSets/11',
            'zero': f'customers/{CID}/sharedSets/0', 'leading_zero': f'customers/{CID}/sharedSets/01',
            'overflow': f'customers/{CID}/sharedSets/{2**63}'}[mode]
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)
    assert shared[1] == []


def test_saved_empty_exact_and_unchanged_inventory(shared):
    shared[0]['shared_set'] = [set_row(12, 'Existing')]
    plan = plan_for()
    shared[0]['shared_set'].insert(0, set_row())
    assert client.verify_created_results(plan.post_checks, result_for()) == [f'customers/{CID}/sharedSets/11']


@pytest.mark.parametrize('field,value', [('id', 12), ('resource_name', f'customers/{CID}/sharedSets/12'),
    ('name', 'Other'), ('type_', 'NEGATIVE_PLACEMENTS'), ('status', 'REMOVED'),
    ('member_count', 1), ('member_count', -1), ('reference_count', 1), ('reference_count', -1)])
def test_saved_metadata_mismatch(shared, field, value):
    plan = plan_for()
    save(shared)
    setattr(shared[0]['exact'][0].shared_set, field, value)
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result_for())


@pytest.mark.parametrize('field', ['member_count', 'reference_count', 'name', 'id'])
def test_saved_missing_optional_zero_is_not_proof(shared, field):
    plan = plan_for()
    save(shared)
    shared[0]['exact'][0].shared_set._pb.ClearField(field)
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result_for())


@pytest.mark.parametrize('mode', ['member', 'active_link', 'tombstone', 'extra_set', 'same_name',
    'missing_set', 'sparse_saved', 'duplicate_saved', 'account', 'query_error', 'incomplete'])
def test_saved_population_or_account_races(shared, monkeypatch, mode):
    plan = plan_for()
    save(shared)
    if mode == 'member':
        shared[0]['shared_criterion'] = [make_row()]
    elif mode in {'active_link', 'tombstone'}:
        shared[0]['campaign_shared_set'] = [make_row(**{'campaign_shared_set.status':
            'ENABLED' if mode == 'active_link' else 'REMOVED'})]
    elif mode in {'extra_set', 'same_name'}:
        shared[0]['shared_set'].append(set_row(12, 'Other' if mode == 'extra_set' else 'Cooling exclusions'))
    elif mode == 'missing_set':
        shared[0]['shared_set'] = []
    elif mode == 'sparse_saved':
        shared[0]['exact'] = [{}]
    elif mode == 'duplicate_saved':
        shared[0]['exact'] *= 2
    elif mode == 'account':
        shared[0]['customer'][0].customer.currency_code = 'EUR'
    elif mode == 'query_error':
        shared[0]['shared_criterion'] = RuntimeError('offline query failure')
    elif mode == 'incomplete':
        monkeypatch.setattr(client, '_search_one_page', lambda *a: ([], None, 1))
    with pytest.raises((rails.RailViolation, RuntimeError)):
        client.verify_created_results(plan.post_checks, result_for())


@pytest.mark.parametrize('field,value', [('resource_name', 'customers/999'), ('id', 999),
    ('descriptive_name', 'Changed'), ('currency_code', 'EUR'), ('time_zone', 'UTC'),
    ('manager', True), ('status', 'SUSPENDED')])
def test_account_fingerprint_drift_before_dispatch(shared, field, value):
    draft = tools.create_shared_negative_set('Cooling exclusions', CID)
    setattr(shared[0]['customer'][0].customer, field, value)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert draft['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('field,value', [('resource_name', f'customers/{CID}/sharedSets/13'),
    ('id', 13), ('name', 'Changed'), ('type_', 'NEGATIVE_PLACEMENTS'), ('status', 'REMOVED')])
def test_set_fingerprint_drift_before_dispatch(shared, field, value):
    shared[0]['shared_set'] = [set_row(12, 'Other')]
    draft = tools.create_shared_negative_set('Cooling exclusions', CID)
    setattr(shared[0]['shared_set'][0].shared_set, field, value)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert draft['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('mode', ['success', 'saved_mismatch', 'query_error', 'ambiguous', 'validate_only'])
def test_apply_lifecycle_consumption_and_audit(shared, monkeypatch, mode):
    import json
    from pathlib import Path

    from mcp_google_ads_safe import audit
    draft = tools.create_shared_negative_set('Cooling exclusions', CID)
    calls = []

    def dispatch(plan, **kwargs):
        calls.append(plan)
        if mode == 'ambiguous':
            raise rails.UnknownWriteOutcome('offline ambiguous transport', request_id='offline', failure={'code': 'UNAVAILABLE'})
        if mode == 'validate_only':
            return dict(result_for(), validate_only=True)
        save(shared)
        if mode == 'saved_mismatch':
            shared[0]['exact'][0].shared_set.member_count = 1
        elif mode == 'query_error':
            shared[0]['exact'] = RuntimeError('offline saved query failure')
        return result_for()

    monkeypatch.setattr(client, '_dispatch', dispatch)
    if mode == 'ambiguous':
        with pytest.raises(rails.UnknownWriteOutcome):
            rails.apply_draft(draft['draft_id'])
    else:
        out = rails.apply_draft(draft['draft_id'])
        assert out['verified'] is (mode == 'success')
        if mode != 'validate_only':
            assert out['applied']
    assert len(calls) == 1
    if mode != 'validate_only':
        with pytest.raises(rails.RailViolation):
            rails.apply_draft(draft['draft_id'])
        assert len(calls) == 1
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    assert events[0]['phase'] == 'draft'
    assert events[-1]['phase'] == ('unknown' if mode == 'ambiguous' else 'apply' if mode == 'success' else 'error')


def test_actual_fake_dispatch_and_saved_path(shared, fake_gads, monkeypatch):
    from tests.conftest import make_type
    response = make_type('MutateGoogleAdsResponse')
    response.mutate_operation_responses.append({'shared_set_result': {
        'resource_name': f'customers/{CID}/sharedSets/11'}})
    fake_gads.mutate_response = response
    draft = tools.create_shared_negative_set('Cooling exclusions', CID)
    original = client._dispatch_entity

    def dispatch(plan, validate_only):
        result = original(plan, validate_only)
        save(shared)
        return result

    monkeypatch.setattr(client, '_dispatch_entity', dispatch)
    assert rails.apply_draft(draft['draft_id'])['verified'] is True
    assert len(fake_gads.mutate_calls) == 1
    assert fake_gads.mutate_calls[0]['partial_failure'] is False
    assert fake_gads.mutate_calls[0]['validate_only'] is False


def test_complete_empty_saved_scan_cannot_hide_later_member(shared, monkeypatch):
    plan = plan_for()
    save(shared)
    page = client._search_one_page

    def paged(query, cid, token):
        if ' FROM shared_criterion ' in query:
            return ([], 'next', 1) if token is None else ([make_row()], None, 1)
        return page(query, cid, token)

    monkeypatch.setattr(client, '_search_one_page', paged)
    with pytest.raises(rails.RailViolation, match='unexpected members'):
        client.verify_created_results(plan.post_checks, result_for())


def test_copied_intent_and_canonical_row_order(shared):
    shared[0]['shared_set'] = [set_row(13, 'Other B'), set_row(12, 'Other A')]
    intent = rails.CreateSharedNegativeSetIntent(CID, 'Cooling exclusions')
    draft = rails.shared_negative_creation_draft(intent)
    object.__setattr__(intent, 'name', 'Changed')
    shared[0]['shared_set'].reverse()
    rails._DRAFTS[draft['draft_id']].validate_fn()
    assert rails._DRAFTS[draft['draft_id']].plan.operations[0].operation['create']['name'] == 'Cooling exclusions'


def test_refused_audit_and_failed_draft_audit_discard(shared, monkeypatch):
    import json
    from pathlib import Path

    from mcp_google_ads_safe import audit
    with pytest.raises(rails.RailViolation):
        tools.create_shared_negative_set('', CID)
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    assert events[-1]['phase'] == 'refused'
    before = set(rails._DRAFTS)

    def fail(*args):
        raise OSError('offline audit failure')

    monkeypatch.setattr(audit, 'log_event', fail)
    with pytest.raises(OSError):
        tools.create_shared_negative_set('Cooling exclusions', CID)
    assert set(rails._DRAFTS) == before


@pytest.mark.parametrize('field,value', [('kind', 'other'), ('extra', True)])
def test_unexpected_plan_fields_refuse(shared, field, value):
    plan = plan_for()
    object.__setattr__(plan, field, value)
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


def test_minimum_add_existing_set(shared):
    draft = tools.add_to_shared_set('11', [{'text': ' jobs ', 'match_type': 'EXACT'}], CID)
    plan = rails._DRAFTS[draft['draft_id']].plan
    assert plan.operations == [rails.safe_create_operation('SharedCriterionService', {
        'shared_set': f'customers/{CID}/sharedSets/11', 'negative': True,
        'keyword': {'text': 'jobs', 'match_type': 'EXACT'}})]


ADDITIONS = [{'text': 'jobs', 'match_type': 'EXACT'}, {'text': 'training', 'match_type': 'PHRASE'}]


def member_row(mid=21, text='Existing', match='BROAD', sid=11):
    return make_row(**{'shared_criterion.resource_name': f'customers/{CID}/sharedCriteria/{sid}~{mid}',
        'shared_criterion.shared_set': f'customers/{CID}/sharedSets/{sid}',
        'shared_criterion.criterion_id': mid, 'shared_criterion.type_': 'KEYWORD',
        'shared_criterion.negative': True, 'shared_criterion.keyword.text': text,
        'shared_criterion.keyword.match_type': match})


def link_row(campaign=31, status='ENABLED'):
    return make_row(**{'campaign_shared_set.resource_name': f'customers/{CID}/campaignSharedSets/{campaign}~11',
        'campaign_shared_set.campaign': f'customers/{CID}/campaigns/{campaign}',
        'campaign_shared_set.shared_set': f'customers/{CID}/sharedSets/11',
        'campaign_shared_set.status': status})


def campaign_row(campaign=31):
    return make_row(**{'campaign.resource_name': f'customers/{CID}/campaigns/{campaign}',
        'campaign.id': campaign, 'campaign.name': 'Paused Search', 'campaign.status': 'PAUSED',
        'campaign.advertising_channel_type': 'SEARCH', 'campaign.advertising_channel_sub_type': 'UNSPECIFIED'})


@pytest.fixture
def populated(shared, monkeypatch):
    state = shared[0]
    state['exact'][0].shared_set.member_count = 1
    state['exact'][0].shared_set.reference_count = 2
    state['shared_criterion'] = [member_row()]
    state['campaign_shared_set'] = [link_row(), link_row(32), link_row(33, 'REMOVED')]
    state['campaign'] = [campaign_row(), campaign_row(32)]
    original = client._search_one_page

    def page(query, cid, token):
        if ' FROM campaign ' in query:
            shared[1].append(query)
            rows = [row for row in state['campaign'] if row.campaign.resource_name in query]
            return rows, None, len(rows)
        return original(query, cid, token)

    monkeypatch.setattr(client, '_search_one_page', page)
    return shared


def add_plan():
    return rails.compile(rails.AddToSharedSetIntent(CID, '11', ADDITIONS)).plan


def add_result():
    return {'results': [{'type': 'shared_criterion_result',
        'resource_name': f'customers/{CID}/sharedCriteria/11~{mid}'} for mid in (22, 23)]}


def save_add(shared):
    shared[0]['shared_criterion'] += [member_row(22, 'jobs', 'EXACT'), member_row(23, 'training', 'PHRASE')]
    shared[0]['exact'][0].shared_set.member_count += 2


@pytest.mark.parametrize('keywords', [None, {}, (), [], [None], [1], [{'text': 'x'}],
    [{'text': 'x', 'match_type': 'EXACT', 'extra': True}], [{'text': 2, 'match_type': 'EXACT'}],
    [{'text': 'x', 'match_type': None}], [{'text': 'x', 'match_type': 1}],
    [{'text': ' ', 'match_type': 'EXACT'}], [{'text': 'x', 'match_type': 'exact'}],
    [{'text': ' jobs ', 'match_type': 'EXACT'}, {'text': 'JOBS', 'match_type': 'EXACT'}]])
def test_add_original_keywords_and_compiler(shared, keywords):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    assert boundary(app.mcp, 'add_to_shared_set', {'shared_set_id': '11', 'keywords': keywords}).is_error
    with pytest.raises(rails.RailViolation):
        rails.compile(rails.AddToSharedSetIntent(CID, '11', keywords))
    assert shared[1] == []


@pytest.mark.parametrize('value', [None, True, 11, 1.1, [], {}, 'null', '0', '01', '+1', '-1', ' 1', '1 ', '١', str(2**63)])
@pytest.mark.parametrize('field', ['shared_set_id', 'customer_id'])
def test_add_original_ids(shared, field, value):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    args = {'shared_set_id': '11', 'customer_id': CID, 'keywords': ADDITIONS, field: value}
    assert boundary(app.mcp, 'add_to_shared_set', args).is_error
    with pytest.raises(rails.RailViolation):
        rails.compile(rails.AddToSharedSetIntent(args['customer_id'], args['shared_set_id'], ADDITIONS))
    assert shared[1] == []


def test_add_preview_copy_and_reordering(populated):
    import copy
    keywords = copy.deepcopy(ADDITIONS)
    draft = tools.add_to_shared_set('11', keywords, CID)
    keywords[0]['text'] = 'Changed'
    stored = rails._DRAFTS[draft['draft_id']]
    assert stored.plan.post_checks[0]['keywords'] == ADDITIONS
    compiled = rails.compile(rails.AddToSharedSetIntent(CID, '11', ADDITIONS))
    assert len(compiled.preview['attached_campaigns']) == 2
    assert compiled.preview['provider_member_count'] == 1
    assert compiled.preview['provider_reference_count'] == 2
    populated[0]['campaign_shared_set'].reverse()
    populated[0]['campaign'].reverse()
    compiled.validate_fn()


@pytest.mark.parametrize('field,value', [('negative', False), ('type_', 'PLACEMENT'), ('type_', 'UNKNOWN'),
    ('shared_set', f'customers/{CID}/sharedSets/12'), ('criterion_id', 22),
    ('resource_name', 'customers/999/sharedCriteria/11~21')])
def test_add_bad_member_population(populated, field, value):
    setattr(populated[0]['shared_criterion'][0].shared_criterion, field, value)
    with pytest.raises(rails.RailViolation):
        add_plan()


@pytest.mark.parametrize('field', ['negative', 'shared_set', 'criterion_id'])
def test_add_member_optional_presence(populated, field):
    populated[0]['shared_criterion'][0].shared_criterion._pb.ClearField(field)
    with pytest.raises(rails.RailViolation):
        add_plan()


@pytest.mark.parametrize('kind,field', [('shared_set', 'id'), ('shared_set', 'name'),
    ('shared_set', 'member_count'), ('shared_set', 'reference_count'),
    ('campaign_shared_set', 'campaign'), ('campaign_shared_set', 'shared_set'),
    ('campaign', 'id'), ('campaign', 'name')])
def test_add_optional_proof_presence(populated, kind, field):
    rows = populated[0]['exact' if kind == 'shared_set' else kind]
    getattr(rows[0], kind)._pb.ClearField(field)
    with pytest.raises(rails.RailViolation):
        add_plan()


@pytest.mark.parametrize('field,value', [('status', 'ENABLED'), ('status', 'REMOVED'),
    ('advertising_channel_type', 'DISPLAY'), ('advertising_channel_type', 'UNKNOWN'),
    ('advertising_channel_sub_type', 'SEARCH_MOBILE_APP'), ('advertising_channel_sub_type', 'UNKNOWN'),
    ('id', 99), ('name', '')])
def test_add_bad_sibling_campaign(populated, field, value):
    setattr(populated[0]['campaign'][1].campaign, field, value)
    with pytest.raises(rails.RailViolation):
        add_plan()


@pytest.mark.parametrize('mode', ['member_count', 'reference_count', 'missing_campaign', 'duplicate_member',
    'duplicate_keyword', 'collision', 'foreign_link', 'unknown_link', 'wrong_link', 'positive_mixed',
    'missing_oneof', 'sparse', 'missing_set', 'wrong_set'])
def test_add_population_refusals(populated, mode):
    state = populated[0]
    if mode in ('member_count', 'reference_count'):
        setattr(state['exact'][0].shared_set, mode, 7)
    elif mode == 'missing_campaign':
        state['campaign'].pop()
    elif mode in ('duplicate_member', 'duplicate_keyword', 'positive_mixed'):
        state['shared_criterion'].append(member_row(21 if mode == 'duplicate_member' else 24))
        state['exact'][0].shared_set.member_count += 1
        if mode == 'positive_mixed':
            state['shared_criterion'][-1].shared_criterion.negative = False
    elif mode == 'collision':
        state['shared_criterion'][0].shared_criterion.keyword.text = ' JOBS '
        state['shared_criterion'][0].shared_criterion.keyword.match_type = 'EXACT'
    elif mode == 'foreign_link':
        state['campaign_shared_set'][0].campaign_shared_set.campaign = 'customers/999/campaigns/31'
    elif mode == 'unknown_link':
        state['campaign_shared_set'][0].campaign_shared_set.status = 'UNKNOWN'
    elif mode == 'wrong_link':
        state['campaign_shared_set'][0].campaign_shared_set.resource_name = f'customers/{CID}/campaignSharedSets/31~12'
    elif mode == 'missing_oneof':
        state['shared_criterion'][0].shared_criterion._pb.ClearField('keyword')
    elif mode == 'sparse':
        state['shared_criterion'] = [{'negative': True}]
    elif mode == 'missing_set':
        state['exact'] = []
    elif mode == 'wrong_set':
        state['exact'] = [set_row(12)]
    with pytest.raises(rails.RailViolation):
        add_plan()


@pytest.mark.parametrize('kind,field,value', [('shared_set', 'name', 'Changed'),
    ('shared_set', 'member_count', 2), ('shared_set', 'reference_count', 3),
    ('shared_criterion', 'criterion_id', 99), ('shared_criterion', 'negative', False),
    ('campaign_shared_set', 'status', 'REMOVED'), ('campaign', 'name', 'Changed'),
    ('campaign', 'status', 'ENABLED'), ('customer', 'time_zone', 'UTC')])
def test_add_fingerprint_fields(populated, kind, field, value):
    draft = tools.add_to_shared_set('11', ADDITIONS, CID)
    row = populated[0]['exact' if kind == 'shared_set' else kind][0]
    setattr(getattr(row, kind), field, value)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert draft['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('mode', ['keyword', 'attachment', 'tombstone'])
def test_add_content_population_drift(populated, mode):
    draft = tools.add_to_shared_set('11', ADDITIONS, CID)
    if mode == 'keyword':
        populated[0]['shared_criterion'][0].shared_criterion.keyword.text = 'Other'
    elif mode == 'attachment':
        populated[0]['campaign_shared_set'].pop(0)
        populated[0]['exact'][0].shared_set.reference_count -= 1
    else:
        populated[0]['campaign_shared_set'].pop()
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert draft['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('mode', ['success', 'swapped', 'wrong_set', 'duplicate_result', 'missing_result',
    'wrong_type', 'extra_result', 'omit_old', 'changed_old', 'extra_member', 'changed_new', 'extra_link',
    'missing_tombstone', 'changed_campaign', 'changed_account', 'changed_set', 'query_error'])
def test_add_exact_saved_union(populated, mode):
    plan = add_plan()
    save_add(populated)
    result = add_result()
    state = populated[0]
    if mode == 'swapped':
        result['results'].reverse()
    elif mode == 'wrong_set':
        result['results'][0]['resource_name'] = f'customers/{CID}/sharedCriteria/12~22'
    elif mode == 'duplicate_result':
        result['results'][1] = result['results'][0]
    elif mode == 'missing_result':
        result['results'].pop()
    elif mode == 'wrong_type':
        result['results'][0]['type'] = 'shared_set_result'
    elif mode == 'extra_result':
        result['results'].append(result['results'][0])
    elif mode == 'omit_old':
        state['shared_criterion'].pop(0)
        state['exact'][0].shared_set.member_count -= 1
    elif mode in ('changed_old', 'changed_new'):
        state['shared_criterion'][0 if mode == 'changed_old' else 1].shared_criterion.keyword.text = 'Other'
    elif mode == 'extra_member':
        state['shared_criterion'].append(member_row(25, 'extra'))
        state['exact'][0].shared_set.member_count += 1
    elif mode == 'extra_link':
        state['campaign_shared_set'].append(link_row(34, 'REMOVED'))
    elif mode == 'missing_tombstone':
        state['campaign_shared_set'].pop()
    elif mode == 'changed_campaign':
        state['campaign'][0].campaign.name = 'Changed'
    elif mode == 'changed_account':
        state['customer'][0].customer.time_zone = 'UTC'
    elif mode == 'changed_set':
        state['exact'][0].shared_set.name = 'Changed'
    elif mode == 'query_error':
        state['shared_criterion'] = RuntimeError('offline saved query failure')
    if mode == 'success':
        assert client.verify_created_results(plan.post_checks, result) == [item['resource_name'] for item in result['results']]
    else:
        with pytest.raises((rails.RailViolation, RuntimeError)):
            client.verify_created_results(plan.post_checks, result)


@pytest.mark.parametrize('mode', ['marker', 'proof', 'extra_check', 'extra_proof', 'extra_member_key',
    'mask', 'status', 'positive', 'subtype', 'service', 'missing_operations', 'extra_operations',
    'forged_campaign', 'forged_counts', 'forged_account', 'foreign_set'])
def test_add_closed_admission_before_factory(populated, mode):
    import dataclasses
    plan = add_plan()
    check = plan.post_checks[0]
    if mode in ('marker', 'proof'):
        del check['shared_negative_add' if mode == 'marker' else 'proof']
    elif mode in ('extra_check', 'extra_proof', 'extra_member_key'):
        target = check if mode == 'extra_check' else check['proof'] if mode == 'extra_proof' else check['proof']['members'][0]
        target['extra'] = True
    elif mode == 'mask':
        plan.operations[0] = dataclasses.replace(plan.operations[0], update_mask=['keyword.text'])
    elif mode == 'status':
        plan.operations[0].operation['create']['status'] = 'ENABLED'
    elif mode == 'positive':
        plan.operations[0].operation['create']['negative'] = False
    elif mode == 'subtype':
        plan.operations[0].operation['create']['placement'] = {'url': 'example.com'}
    elif mode == 'service':
        plan.operations[0] = dataclasses.replace(plan.operations[0], service='CampaignCriterionService')
    elif mode == 'missing_operations':
        plan.operations.clear()
    elif mode == 'extra_operations':
        plan.operations.append(plan.operations[0])
    elif mode == 'forged_campaign':
        check['proof']['campaigns'][1]['status'] = 'ENABLED'
    elif mode == 'forged_counts':
        check['proof']['set']['reference_count'] = 0
    elif mode == 'forged_account':
        check['proof']['account']['manager'] = True
    elif mode == 'foreign_set':
        check['shared_set_id'] = '12'
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


def test_add_sdk_atomic_and_saved_path(populated, fake_gads, monkeypatch):
    from tests.conftest import make_type
    plan = add_plan()
    context = client.validate_mutation_plan(plan)
    for op in plan.operations:
        message = client._build_mutate_operation(fake_gads, op, context)
        decoded = type(message).deserialize(type(message).serialize(message))
        assert decoded._pb.WhichOneof('operation') == 'shared_criterion_operation'
        operation = decoded.shared_criterion_operation
        assert operation._pb.WhichOneof('operation') == 'create'
        assert 'update_mask' not in operation._pb.DESCRIPTOR.fields_by_name
        assert {field.name for field, _ in operation.create._pb.ListFields()} == {'shared_set', 'negative', 'keyword'}
    response = make_type('MutateGoogleAdsResponse')
    for result in add_result()['results']:
        response.mutate_operation_responses.append({'shared_criterion_result': {'resource_name': result['resource_name']}})
    fake_gads.mutate_response = response
    draft = tools.add_to_shared_set('11', ADDITIONS, CID)
    original = client._dispatch_entity

    def dispatch(plan, validate_only):
        result = original(plan, validate_only)
        save_add(populated)
        return result

    monkeypatch.setattr(client, '_dispatch_entity', dispatch)
    out = rails.apply_draft(draft['draft_id'])
    assert out['verified'] is True, str(out)
    assert len(fake_gads.mutate_calls) == 1
    assert len(fake_gads.mutate_calls[0]['operations']) == 2
    assert fake_gads.mutate_calls[0]['partial_failure'] is False


@pytest.mark.parametrize('mode', ['saved_mismatch', 'query_error', 'ambiguous', 'validate_only'])
def test_add_lifecycle(populated, monkeypatch, mode):
    draft = tools.add_to_shared_set('11', ADDITIONS, CID)
    calls = []

    def dispatch(plan, **kwargs):
        calls.append(plan)
        if mode == 'ambiguous':
            raise rails.UnknownWriteOutcome('offline ambiguous transport', request_id='offline', failure={'code': 'UNAVAILABLE'})
        if mode == 'validate_only':
            return dict(add_result(), validate_only=True)
        save_add(populated)
        if mode == 'saved_mismatch':
            populated[0]['shared_criterion'][0].shared_criterion.keyword.text = 'Changed'
        else:
            populated[0]['exact'] = RuntimeError('offline query error')
        return add_result()

    monkeypatch.setattr(client, '_dispatch', dispatch)
    if mode == 'ambiguous':
        with pytest.raises(rails.UnknownWriteOutcome):
            rails.apply_draft(draft['draft_id'])
    else:
        out = rails.apply_draft(draft['draft_id'])
        assert out['verified'] is False
        if mode != 'validate_only':
            assert out['applied'] is True
    assert len(calls) == 1
    if mode != 'validate_only':
        with pytest.raises(rails.RailViolation):
            rails.apply_draft(draft['draft_id'])
        assert len(calls) == 1


@pytest.mark.parametrize('value', ['UNSPECIFIED', 'UNKNOWN'])
def test_add_vertical_type_absence(populated, value):
    populated[0]['exact'][0].shared_set.vertical_ads_item_vertical_type = value
    with pytest.raises(rails.RailViolation):
        add_plan()


@pytest.mark.parametrize('gate,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
    ('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'false'),
    ('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'invalid'),
    ('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999'), ('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')])
def test_add_gates_before_reads_and_rechecked(shared, monkeypatch, gate, value):
    draft = tools.add_to_shared_set('11', ADDITIONS, CID)
    shared[1].clear()
    monkeypatch.setenv(gate, value)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    with pytest.raises(rails.RailViolation):
        add_plan()
    assert shared[1] == [] and draft['draft_id'] in rails._DRAFTS


def test_add_default_off_content_unknown_top_key(shared, monkeypatch):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    assert boundary(app.mcp, 'add_to_shared_set', {'shared_set_id': '11', 'keywords': ADDITIONS, 'extra': True}).is_error
    monkeypatch.delenv('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT')
    with pytest.raises(rails.RailViolation):
        add_plan()
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'true')
    monkeypatch.setattr(rails.settings, 'blocked_terms', lambda: ('jobs',))
    with pytest.raises(rails.RailViolation):
        add_plan()
    assert shared[1] == []


@pytest.mark.parametrize('kind', ['shared_set', 'shared_criterion', 'campaign_shared_set', 'campaign'])
def test_add_incomplete_scan(populated, monkeypatch, kind):
    page = client._search_one_page

    def incomplete(query, cid, token):
        rows, next_token, total = page(query, cid, token)
        return (rows, next_token, total + 1) if f' FROM {kind} ' in query else (rows, next_token, total)

    monkeypatch.setattr(client, '_search_one_page', incomplete)
    with pytest.raises(rails.RailViolation) as exc:
        add_plan()
    assert exc.value.code == 'SCAN_INCOMPLETE'


def test_add_validate_only_no_saved_reads(populated, fake_gads):
    from tests.conftest import make_type
    plan = add_plan()
    populated[1].clear()
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan, True)
    assert result['validate_only'] is True
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)
    assert populated[1] == []
    assert fake_gads.mutate_calls[0]['validate_only'] is True


@pytest.mark.parametrize('value', [1, 1.0])
def test_add_numeric_negative_refuses_before_factory(populated, value):
    plan = add_plan()
    plan.operations[0].operation['create']['negative'] = value
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


def test_minimum_attach_shared_set(populated):
    populated[0]['campaign'].append(campaign_row(34))
    draft = tools.attach_shared_set('11', '34', CID)
    plan = rails._DRAFTS[draft['draft_id']].plan
    assert plan.operations == [rails.safe_create_operation('CampaignSharedSetService', {
        'campaign': f'customers/{CID}/campaigns/34', 'shared_set': f'customers/{CID}/sharedSets/11'})]


@pytest.fixture
def attachable(populated):
    populated[0]['campaign'].append(campaign_row(34))
    return populated


def attach_plan():
    return rails.compile(rails.AttachSharedSetIntent(CID, '11', '34')).plan


def attach_result():
    return {'results': [{'type': 'campaign_shared_set_result',
                         'resource_name': f'customers/{CID}/campaignSharedSets/34~11'}]}


def save_attach(shared):
    shared[0]['campaign_shared_set'].append(link_row(34))
    shared[0]['exact'][0].shared_set.reference_count += 1


@pytest.mark.parametrize('field', ['shared_set_id', 'campaign_id', 'customer_id'])
@pytest.mark.parametrize('value', [None, True, 1, 1.0, [], {}, 'null', '0', '01', '-1', ' 11',
                                   '１２', '9223372036854775808', 'customers/999/campaigns/34'])
def test_attach_original_and_compiler_ids(attachable, field, value):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    args = {'shared_set_id': '11', 'campaign_id': '34', 'customer_id': CID}
    args[field] = value
    attachable[1].clear()
    assert boundary(app.mcp, 'attach_shared_set', args).is_error
    with pytest.raises(rails.RailViolation):
        rails.compile(rails.AttachSharedSetIntent(**args))
    assert attachable[1] == []


@pytest.mark.parametrize('status', ['PAUSED', 'ENABLED', None])
def test_attach_no_status_input(attachable, status):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary
    assert boundary(app.mcp, 'attach_shared_set', {'shared_set_id': '11', 'campaign_id': '34',
                                                  'status': status}).is_error


def test_attach_sdk_and_full_saved_union(attachable, fake_gads):
    from tests.conftest import make_type
    plan = attach_plan()
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    fake_gads.mutate_response.mutate_operation_responses.append(
        {'campaign_shared_set_result': {'resource_name': attach_result()['results'][0]['resource_name']}})
    result = client._dispatch_entity(plan, False)
    wire = fake_gads.mutate_calls[0]['operations'][0]
    restored = type(wire).deserialize(type(wire).serialize(wire))
    assert restored._pb.WhichOneof('operation') == 'campaign_shared_set_operation'
    create = restored.campaign_shared_set_operation.create
    assert {field.name for field, _ in create._pb.ListFields()} == {'campaign', 'shared_set'}
    assert fake_gads.mutate_calls[0]['partial_failure'] is False
    save_attach(attachable)
    assert client.verify_created_results(plan.post_checks, result) == [attach_result()['results'][0]['resource_name']]


@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('field,value', [('status', 'ENABLED'), ('status', 'REMOVED'),
    ('advertising_channel_type', 'DISPLAY'), ('advertising_channel_sub_type', 'SEARCH_MOBILE_APP'),
    ('id', 99), ('resource_name', 'customers/999/campaigns/34'), ('name', '')])
def test_attach_all_campaigns_independently(attachable, index, field, value):
    setattr(attachable[0]['campaign'][index].campaign, field, value)
    with pytest.raises(rails.RailViolation):
        attach_plan()


@pytest.mark.parametrize('field', ['id', 'name'])
def test_attach_target_missing_optional(attachable, field):
    attachable[0]['campaign'][-1].campaign._pb.ClearField(field)
    with pytest.raises(rails.RailViolation):
        attach_plan()


@pytest.mark.parametrize('target', ['31', '33', '35'])
def test_attach_duplicate_tombstone_missing_target(attachable, target):
    if target == '33':
        attachable[0]['campaign'].append(campaign_row(33))
    with pytest.raises(rails.RailViolation):
        rails.compile(rails.AttachSharedSetIntent(CID, '11', target))


def test_attach_empty_list_preview(attachable):
    state = attachable[0]
    state['shared_criterion'] = []
    state['exact'][0].shared_set.member_count = 0
    compiled = rails.compile(rails.AttachSharedSetIntent(CID, '11', '34'))
    assert len(compiled.preview['attached_campaigns']) == 2
    assert len(compiled.preview['proposed_attached_campaigns']) == 3
    assert compiled.preview['keywords'] == [] and compiled.preview['account_currency'] == 'USD'
    assert 'currently contains no exclusions' in ' '.join(compiled.preview['warnings'])
    assert 'no pause control' in ' '.join(compiled.preview['warnings'])
    save_attach(attachable)
    client.verify_created_results(compiled.plan.post_checks, attach_result())


@pytest.mark.parametrize('mode', ['marker', 'generic', 'proof', 'status', 'resource_name', 'mask',
    'remove', 'extra', 'reference', 'target', 'target_enabled', 'sibling_enabled', 'bool_marker', 'negative'])
def test_attach_forgery_before_factory(attachable, mode):
    import dataclasses
    plan = attach_plan()
    check = plan.post_checks[0]
    if mode in ('marker', 'proof'):
        del check['shared_negative_attach' if mode == 'marker' else 'proof']
    elif mode == 'generic':
        plan.post_checks[:] = [{'entity_type': 'campaign_shared_set', 'result_index': 0}]
    elif mode in ('status', 'resource_name'):
        plan.operations[0].operation['create'][mode] = 'PAUSED' if mode == 'status' else 'output-only'
    elif mode == 'mask':
        plan.operations[0] = dataclasses.replace(plan.operations[0], update_mask=['status'])
    elif mode == 'remove':
        plan.operations[0].operation['remove'] = attach_result()['results'][0]['resource_name']
    elif mode == 'extra':
        check['extra'] = True
    elif mode == 'reference':
        check['shared_set_id'] = '12'
        plan.operations[0].operation['create']['shared_set'] = f'customers/{CID}/sharedSets/12'
    elif mode == 'target':
        check['campaign_id'] = '35'
        plan.operations[0].operation['create']['campaign'] = f'customers/{CID}/campaigns/35'
    elif mode == 'target_enabled':
        check['target']['status'] = 'ENABLED'
    elif mode == 'sibling_enabled':
        check['proof']['campaigns'][0]['status'] = 'ENABLED'
    elif mode == 'bool_marker':
        check['shared_negative_attach'] = 1
    elif mode == 'negative':
        check['proof']['members'][0]['negative'] = 1
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


@pytest.mark.parametrize('mode', ['empty', 'extra', 'wrong_type', 'foreign', 'wrong_set', 'wrong_target'])
def test_attach_result_identity_before_reads(attachable, mode):
    plan = attach_plan()
    result = attach_result()
    if mode == 'empty':
        result['results'] = []
    elif mode == 'extra':
        result['results'] *= 2
    elif mode == 'wrong_type':
        result['results'][0]['type'] = 'shared_set_result'
    else:
        result['results'][0]['resource_name'] = {'foreign': 'customers/999/campaignSharedSets/34~11',
            'wrong_set': f'customers/{CID}/campaignSharedSets/34~12',
            'wrong_target': f'customers/{CID}/campaignSharedSets/35~11'}[mode]
    attachable[1].clear()
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)
    assert attachable[1] == []


@pytest.mark.parametrize('mode', ['link_status', 'missing_sibling', 'missing_tombstone', 'member',
    'reference_count', 'member_count', 'set_status', 'set_name', 'account', 'target', 'sibling'])
def test_attach_saved_exact_union_races(attachable, mode):
    plan = attach_plan()
    save_attach(attachable)
    state = attachable[0]
    if mode == 'link_status':
        state['campaign_shared_set'][-1].campaign_shared_set.status = 'REMOVED'
    elif mode == 'missing_sibling':
        state['campaign_shared_set'].pop(0)
        state['exact'][0].shared_set.reference_count -= 1
    elif mode == 'missing_tombstone':
        state['campaign_shared_set'].pop(2)
    elif mode == 'member':
        state['shared_criterion'][0].shared_criterion.keyword.text = 'Changed'
    elif mode in ('reference_count', 'member_count'):
        setattr(state['exact'][0].shared_set, mode, 99)
    elif mode in ('set_status', 'set_name'):
        setattr(state['exact'][0].shared_set, mode[4:], 'REMOVED' if mode == 'set_status' else 'Changed')
    elif mode == 'account':
        state['customer'][0].customer.currency_code = 'EUR'
    else:
        state['campaign'][-1 if mode == 'target' else 0].campaign.status = 'ENABLED'
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, attach_result())


@pytest.mark.parametrize('gate,value', [('GOOGLE_ADS_ENABLE_WRITES', 'false'),
    ('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'false'),
    ('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', 'invalid'),
    ('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999'), ('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')])
def test_attach_gates_rechecked_before_reads(attachable, monkeypatch, gate, value):
    draft = tools.attach_shared_set('11', '34', CID)
    attachable[1].clear()
    monkeypatch.setenv(gate, value)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert not attachable[1] and draft['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('mode', ['success', 'saved_mismatch', 'query_error', 'ambiguous', 'validate_only'])
def test_attach_lifecycle(attachable, monkeypatch, mode):
    draft = tools.attach_shared_set('11', '34', CID)
    calls = []

    def dispatch(plan, **kwargs):
        calls.append(plan)
        if mode == 'ambiguous':
            raise rails.UnknownWriteOutcome('offline ambiguous transport', request_id='offline', failure={'code': 'UNAVAILABLE'})
        if mode == 'validate_only':
            return dict(attach_result(), validate_only=True)
        save_attach(attachable)
        if mode == 'saved_mismatch':
            attachable[0]['campaign'][0].campaign.status = 'ENABLED'
        elif mode == 'query_error':
            attachable[0]['exact'] = RuntimeError('offline query error')
        return attach_result()

    monkeypatch.setattr(client, '_dispatch', dispatch)
    if mode == 'ambiguous':
        with pytest.raises(rails.UnknownWriteOutcome):
            rails.apply_draft(draft['draft_id'])
    else:
        out = rails.apply_draft(draft['draft_id'])
        assert out['verified'] is (mode == 'success')
        if mode != 'validate_only':
            assert out['applied'] is True
    assert len(calls) == 1
    if mode != 'validate_only':
        with pytest.raises(rails.RailViolation):
            rails.apply_draft(draft['draft_id'])
        assert len(calls) == 1


def test_attach_target_fingerprint_drift(attachable):
    draft = tools.attach_shared_set('11', '34', CID)
    attachable[0]['campaign'][-1].campaign.name = 'Changed'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(draft['draft_id'])
    assert exc.value.code == 'STATE_DRIFT' and draft['draft_id'] in rails._DRAFTS


def test_attach_partial_scan(attachable, monkeypatch):
    original = client._search_one_page

    def page(query, cid, token):
        rows, next_token, count = original(query, cid, token)
        return rows, next_token, count + 1 if ' FROM campaign_shared_set ' in query else count

    monkeypatch.setattr(client, '_search_one_page', page)
    with pytest.raises(rails.RailViolation) as exc:
        attach_plan()
    assert exc.value.code == 'SCAN_INCOMPLETE'


def test_attach_validate_only_no_saved_reads(attachable, fake_gads):
    from tests.conftest import make_type
    plan = attach_plan()
    attachable[1].clear()
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan, True)
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)
    assert not attachable[1] and fake_gads.mutate_calls[0]['validate_only'] is True


@pytest.mark.parametrize('kind,field,value', [('shared_set', 'name', 'Changed'),
    ('shared_set', 'status', 'REMOVED'), ('shared_set', 'reference_count', 9),
    ('shared_set', 'member_count', 2), ('shared_criterion', 'negative', False),
    ('shared_criterion', 'criterion_id', 99), ('campaign_shared_set', 'status', 'REMOVED'),
    ('campaign', 'name', 'Changed'), ('customer', 'time_zone', 'UTC')])
def test_attach_existing_proof_fingerprint(attachable, kind, field, value):
    compiled = rails.compile(rails.AttachSharedSetIntent(CID, '11', '34'))
    rows = attachable[0]['exact' if kind == 'shared_set' else kind]
    setattr(getattr(rows[0], kind), field, value)
    with pytest.raises(rails.RailViolation):
        compiled.validate_fn()


def test_attach_content_fingerprint_and_order(attachable):
    compiled = rails.compile(rails.AttachSharedSetIntent(CID, '11', '34'))
    attachable[0]['campaign_shared_set'].reverse()
    attachable[0]['campaign'].reverse()
    compiled.validate_fn()
    attachable[0]['shared_criterion'][0].shared_criterion.keyword.text = 'Changed'
    with pytest.raises(rails.RailViolation) as exc:
        compiled.validate_fn()
    assert exc.value.code == 'STATE_DRIFT'


def test_attach_empty_attachment_population(attachable):
    attachable[0]['campaign_shared_set'] = []
    attachable[0]['exact'][0].shared_set.reference_count = 0
    plan = attach_plan()
    save_attach(attachable)
    assert client.verify_created_results(plan.post_checks, attach_result()) == [attach_result()['results'][0]['resource_name']]
