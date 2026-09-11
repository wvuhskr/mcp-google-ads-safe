"""Credentialless standalone portfolio creation safety checks."""
import copy
import json

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import make_type

CID = '1234567890'


@pytest.fixture
def portfolio_creation(monkeypatch, fake_client):
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT', 'true')
    state = {'account': {'id': CID, 'currency_code': 'USD',
                         'time_zone': 'America/New_York', 'manager': False},
             'strategies': []}
    monkeypatch.setattr(client, 'portfolio_creation_state', lambda *a: copy.deepcopy(state))
    return state, fake_client


@pytest.mark.parametrize(('strategy', 'target', 'scheme', 'field', 'expected'), [
    ('TARGET_CPA', {'target_cpa': '0.000001'}, 'target_cpa', 'target_cpa_micros', 1),
    ('TARGET_CPA', {'target_cpa': '50'}, 'target_cpa', 'target_cpa_micros', 50_000_000),
    ('TARGET_ROAS', {'target_roas': '0.01'}, 'target_roas', 'target_roas', 0.01),
    ('TARGET_ROAS', {'target_roas': '1000'}, 'target_roas', 'target_roas', 1000.0),
])
def test_exact_valid_target_boundaries(portfolio_creation, strategy, target, scheme, field, expected):
    draft = tools.create_portfolio_bidding_strategy('Boundary', strategy, **target)
    values = rails._DRAFTS[draft['draft_id']].plan.operations[0].operation['create']
    assert values[scheme][field] == expected


@pytest.mark.parametrize(('strategy', 'target', 'scheme', 'field', 'value'), [
    ('TARGET_CPA', {'target_cpa': '12.345678'}, 'target_cpa', 'target_cpa_micros', 12345678),
    ('TARGET_ROAS', {'target_roas': '2'}, 'target_roas', 'target_roas', 2.0),
])
def test_exact_standalone_request(portfolio_creation, fake_gads, strategy, target, scheme, field, value):
    result = tools.create_portfolio_bidding_strategy('Portfolio One', strategy, **target)
    plan = rails._DRAFTS[result['draft_id']].plan
    assert len(plan.operations) == 1
    assert plan.operations[0].operation == {'create': {'name': 'Portfolio One', scheme: {field: value}}}
    assert plan.operations[0].update_mask is None
    built = client._build_mutate_operation(fake_gads, plan.operations[0])
    entity = built.bidding_strategy_operation.create
    assert entity._pb.WhichOneof('scheme') == scheme
    assert not entity.resource_name and not entity.currency_code
    assert entity.type_.name == 'UNSPECIFIED' and entity.status.name == 'UNSPECIFIED'


@pytest.mark.parametrize(('strategy', 'kwargs'), [
    ('TARGET_CPA', {}), ('TARGET_CPA', {'target_cpa': True}),
    ('TARGET_CPA', {'target_cpa': '50.000001'}), ('TARGET_CPA', {'target_cpa': '1', 'target_roas': '2'}),
    ('TARGET_ROAS', {}), ('TARGET_ROAS', {'target_roas': True}),
    ('TARGET_ROAS', {'target_roas': '0.009'}), ('TARGET_ROAS', {'target_roas': '1000.01'}),
    ('MAXIMIZE_CONVERSIONS', {'target_cpa': '1'}),
])
def test_invalid_combinations_refuse(portfolio_creation, strategy, kwargs):
    with pytest.raises(rails.RailViolation):
        tools.create_portfolio_bidding_strategy('Portfolio One', strategy, **kwargs)


@pytest.mark.parametrize(('field', 'value'), [
    ('name', 7), ('strategy_type', 7), ('customer_id', 1234567890),
    ('customer_id', '0123456789'), ('customer_id', '１２３'),
])
def test_original_string_and_canonical_account_inputs(portfolio_creation, field, value):
    kwargs = {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA',
              'target_cpa': '10', 'customer_id': CID}
    kwargs[field] = value
    with pytest.raises((rails.RailViolation, TypeError)):
        tools.create_portfolio_bidding_strategy(**kwargs)


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 'null'), ('customer_id', '  null\t'),
    ('target_cpa', 'null'), ('target_cpa', ' null '),
    ('target_roas', 'null'), ('target_roas', '\nnull '),
])
def test_actual_mcp_literal_null_refuses_before_portfolio_read(monkeypatch, field, value):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary, payload

    monkeypatch.setattr(client, 'portfolio_creation_state',
                        lambda *a: pytest.fail('literal null reached portfolio reads'))
    arguments = {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA',
                 'target_cpa': '10', field: value}
    out = payload(boundary(app.mcp, 'create_portfolio_bidding_strategy', arguments))
    assert out['code'] == 'BAD_INPUT'


@pytest.mark.parametrize('include_none', [False, True])
def test_actual_mcp_omitted_or_none_customer_default_remains_valid(portfolio_creation,
                                                                  include_none):
    from mcp_google_ads_safe import app
    from tests.test_protocol_errors import boundary

    arguments = {'name': 'Portfolio One', 'strategy_type': 'TARGET_CPA', 'target_cpa': '10'}
    if include_none:
        arguments['customer_id'] = None
        arguments['target_roas'] = None
    result = boundary(app.mcp, 'create_portfolio_bidding_strategy', arguments)
    assert result.model_dump(by_alias=True)['isError'] is False
    assert json.loads(result.content[0].text)['dry_run'] is True


def test_gates_and_state_rechecked(portfolio_creation, monkeypatch):
    state, fake = portfolio_creation
    result = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_CPA', target_cpa='10')
    state['account']['currency_code'] = 'EUR'
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(result['draft_id'])
    assert exc.value.code == 'STATE_DRIFT'
    assert fake.dispatch_calls == []


@pytest.mark.parametrize('gate', ['writes', 'portfolio', 'write_allowlist', 'read_allowlist'])
def test_apply_refuses_revoked_gate_without_dispatch(portfolio_creation, monkeypatch, gate):
    _, fake = portfolio_creation
    draft = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_CPA', target_cpa='10')
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'portfolio':
        monkeypatch.setenv('GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT', 'false')
    elif gate == 'write_allowlist':
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    with pytest.raises(rails.RailViolation) as exc:
        rails.apply_draft(draft['draft_id'])
    if gate == 'read_allowlist':
        assert 'WRITE_CUSTOMER_IDS' in str(exc.value) and 'must also be a read id' in str(exc.value)
    assert draft['draft_id'] in rails._DRAFTS
    assert fake.dispatch_calls == []


def test_apply_refuses_collision_inventory_drift(portfolio_creation, monkeypatch):
    _, fake = portfolio_creation
    draft = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_ROAS', target_roas='2')
    monkeypatch.setattr(client, 'portfolio_creation_state',
                        lambda *a: (_ for _ in ()).throw(rails.RailViolation(
                            'owned portfolio strategy name already exists')))
    with pytest.raises(rails.RailViolation, match='already exists'):
        rails.apply_draft(draft['draft_id'])
    assert draft['draft_id'] in rails._DRAFTS
    assert fake.dispatch_calls == []


def test_portfolio_gate_precedes_reads(monkeypatch):
    calls = []
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT', 'false')
    monkeypatch.setattr(client, 'portfolio_creation_state', lambda *a: calls.append(a))
    with pytest.raises(rails.RailViolation):
        tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_CPA', target_cpa='10')
    assert calls == []


def test_validate_only_one_atomic_mutation(portfolio_creation, fake_gads):
    result = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_ROAS', target_roas='2')
    plan = rails._DRAFTS[result['draft_id']].plan
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    out = client._dispatch_entity(plan, True)
    assert out['validate_only'] is True
    assert len(fake_gads.mutate_calls) == 1
    assert fake_gads.mutate_calls[0]['validate_only'] is True
    assert len(fake_gads.mutate_calls[0]['operations']) == 1


@pytest.mark.parametrize('damage', ['extra', 'nested', 'descriptor'])
def test_tampered_plan_or_descriptor_refuses(portfolio_creation, damage):
    result = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_CPA', target_cpa='10')
    plan = copy.deepcopy(rails._DRAFTS[result['draft_id']].plan)
    if damage == 'extra':
        plan.operations[0].operation['create']['status'] = 'PAUSED'
    elif damage == 'nested':
        plan.operations[0].operation['create']['target_cpa']['cpc_bid_ceiling_micros'] = 1
    else:
        plan.post_checks[0]['expected']['name'] = 'Other'
    with pytest.raises(rails.RailViolation):
        client.validate_mutation_plan(plan)


def test_saved_portfolio_verification(portfolio_creation, monkeypatch):
    result = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_CPA', target_cpa='10')
    plan = rails._DRAFTS[result['draft_id']].plan
    rn = f'customers/{CID}/biddingStrategies/7'
    monkeypatch.setattr(client, 'created_resource_state', lambda *a, **k: {
        'name': 'Portfolio One', 'type_': 'TARGET_CPA', 'status': 'ENABLED',
        'currency_code': 'USD', 'effective_currency_code': 'USD',
        'non_removed_campaign_count': '0', 'target_cpa': {'target_cpa_micros': '10000000'}})
    client.verify_created_results(plan.post_checks, {'results': [
        {'type': 'bidding_strategy_result', 'resource_name': rn}]})
    monkeypatch.setattr(client, 'created_resource_state', lambda *a, **k: {
        'name': 'Portfolio One', 'type_': 'TARGET_CPA', 'currency_code': 'USD',
        'non_removed_campaign_count': '1', 'target_cpa': {'target_cpa_micros': '10000000'}})
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, {'results': [
            {'type': 'bidding_strategy_result', 'resource_name': rn}]})


@pytest.mark.parametrize('status', [
    'NONSENSE', 999, True, None, 'UNKNOWN', 'REMOVED', 'UNSPECIFIED', 'INVALID',
])
def test_populated_saved_status_failure_is_unverified_consumed_once(portfolio_creation,
                                                                    monkeypatch, status):
    _, fake = portfolio_creation
    draft = tools.create_portfolio_bidding_strategy(
        'Portfolio One', 'TARGET_CPA', target_cpa='10')
    rn = f'customers/{CID}/biddingStrategies/7'
    fake.dispatch_result = {'results': [
        {'type': 'bidding_strategy_result', 'resource_name': rn}], 'request_id': None}
    saved = {'resource_name': rn, 'name': 'Portfolio One', 'type_': 'TARGET_CPA',
             'currency_code': 'USD', 'effective_currency_code': 'USD',
             'non_removed_campaign_count': '0',
             'target_cpa': {'target_cpa_micros': '10000000'}}
    if status is not None:
        saved['status'] = status
    monkeypatch.setattr(client, 'gaql_all', lambda *a: [{'bidding_strategy': copy.deepcopy(saved)}])
    out = rails.apply_draft(draft['draft_id'])
    assert out['verified'] is False
    assert draft['draft_id'] not in rails._DRAFTS
    with pytest.raises(rails.RailViolation, match='already-applied'):
        rails.apply_draft(draft['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_populated_saved_enabled_status_verifies(portfolio_creation, monkeypatch):
    _, fake = portfolio_creation
    draft = tools.create_portfolio_bidding_strategy(
        'Portfolio One', 'TARGET_CPA', target_cpa='10')
    rn = f'customers/{CID}/biddingStrategies/7'
    fake.dispatch_result = {'results': [
        {'type': 'bidding_strategy_result', 'resource_name': rn}], 'request_id': None}
    saved = {'resource_name': rn, 'name': 'Portfolio One', 'type_': 'TARGET_CPA',
             'status': 'ENABLED', 'currency_code': 'USD', 'effective_currency_code': 'USD',
             'non_removed_campaign_count': '0',
             'target_cpa': {'target_cpa_micros': '10000000'}}
    monkeypatch.setattr(client, 'gaql_all', lambda *a: [{'bidding_strategy': copy.deepcopy(saved)}])
    assert rails.apply_draft(draft['draft_id'])['verified'] is True
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', ['missing', 'owner', 'type', 'target'])
def test_saved_portfolio_wrong_observation_refuses(portfolio_creation, monkeypatch, damage):
    draft = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_ROAS', target_roas='2')
    plan = rails._DRAFTS[draft['draft_id']].plan
    rn = f'customers/{CID}/biddingStrategies/7'
    state = {'name': 'Portfolio One', 'type_': 'TARGET_ROAS', 'currency_code': 'USD',
             'non_removed_campaign_count': '0', 'target_roas': {'target_roas': 2.0}}
    monkeypatch.setattr(client, 'created_resource_state', lambda *a, **k: copy.deepcopy(state))
    result = {'results': [{'type': 'bidding_strategy_result', 'resource_name': rn}]}
    if damage == 'missing':
        result['results'] = []
    elif damage == 'owner':
        result['results'][0]['resource_name'] = 'customers/88/biddingStrategies/7'
    elif damage == 'type':
        state['type_'] = 'TARGET_CPA'
    else:
        state['target_roas']['target_roas'] = 3.0
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


@pytest.mark.parametrize('damage', ['manager', 'manager_missing', 'account_partial', 'inventory_partial',
                                    'foreign', 'duplicate', 'collision', 'status', 'type', 'name'])
def test_portfolio_state_refuses_unsafe_inventory(monkeypatch, damage):
    account = {'pages_complete': True, 'returned_count': 1, 'total_results_count': 1,
               'rows': [{'customer': {'id': CID, 'currency_code': 'USD', 'time_zone': 'UTC',
                                      'manager': damage == 'manager'}}]}
    if damage == 'manager_missing':
        account['rows'][0]['customer'].pop('manager')
    elif damage == 'account_partial':
        account['pages_complete'] = False
    row = {'bidding_strategy': {'resource_name': f'customers/{CID}/biddingStrategies/7',
                                'id': '7', 'name': 'Existing', 'status': 'ENABLED',
                                'type_': 'TARGET_CPA'}}
    if damage == 'foreign':
        row['bidding_strategy']['resource_name'] = 'customers/88/biddingStrategies/7'
    if damage == 'collision':
        row['bidding_strategy']['name'] = 'Portfolio One'
    elif damage == 'status':
        row['bidding_strategy']['status'] = 'UNKNOWN'
    elif damage == 'type':
        row['bidding_strategy']['type_'] = 'UNSPECIFIED'
    elif damage == 'name':
        row['bidding_strategy']['name'] = ''
    monkeypatch.setattr(client, 'account_info', lambda *a: copy.deepcopy(account))
    if damage == 'inventory_partial':
        monkeypatch.setattr(client, 'gaql_all', lambda *a: (_ for _ in ()).throw(
            rails.RailViolation('complete scan did not reconcile', code='SCAN_INCOMPLETE')))
    else:
        monkeypatch.setattr(client, 'gaql_all', lambda *a: [copy.deepcopy(row)] *
                            (2 if damage == 'duplicate' else 1))
    with pytest.raises(rails.RailViolation):
        client.portfolio_creation_state(CID, 'Portfolio One')


def test_portfolio_unknown_transport_consumes_without_retry(portfolio_creation):
    _, fake = portfolio_creation
    draft = tools.create_portfolio_bidding_strategy('Portfolio One', 'TARGET_CPA', target_cpa='10')
    fake.dispatch_error = rails.UnknownWriteOutcome('unknown', request_id='req',
                                                     failure={'code': 'UNAVAILABLE'})
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(draft['draft_id'])
    assert draft['draft_id'] not in rails._DRAFTS
    with pytest.raises(rails.RailViolation, match='already-applied'):
        rails.apply_draft(draft['draft_id'])
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('strategy_type', ['NOT_A_STRATEGY', 'UNKNOWN', 'UNSPECIFIED', 'INVALID', None, 999, 9.0,
                                          'TARGET_CPA', 9])
def test_real_portfolio_reader_validates_installed_enum(monkeypatch, strategy_type):
    def forbidden(*args, **kwargs):
        pytest.fail('provider construction or dispatch must not run')

    monkeypatch.setattr(client, 'gads', forbidden)
    monkeypatch.setattr(client, '_dispatch', forbidden)
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT', 'true')
    monkeypatch.setattr(client, '_creation_account', lambda *a, **kw: {
        'id': CID, 'currency_code': 'USD', 'time_zone': 'UTC', 'manager': False})
    monkeypatch.setattr(client, 'gaql_all', lambda *a: [{'bidding_strategy': {
        'resource_name': f'customers/{CID}/biddingStrategies/7', 'id': '7',
        'name': 'Existing', 'status': 'ENABLED', 'type_': strategy_type}}])
    if strategy_type == 'TARGET_CPA' or type(strategy_type) is int and strategy_type == 9:
        expected = 'TARGET_CPA' if strategy_type == 'TARGET_CPA' else 'TARGET_SPEND'
        assert client.portfolio_creation_state(CID, 'New')['strategies'][0]['type'] == expected
    else:
        with pytest.raises(rails.RailViolation, match='unreadable'):
            tools.create_portfolio_bidding_strategy('New', 'TARGET_CPA', target_cpa='10')
