"""Creation safety contracts, entirely synthetic and credential free."""
import copy

import pytest

from mcp_google_ads_safe import client, rails, tools

CID = '1234567890'
REAL_GAQL_ALL = client.gaql_all


@pytest.fixture
def creation(monkeypatch, fake_client):
    state = {'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'},
             'collisions': [], 'locations': [{'resource_name': 'geoTargetConstants/100', 'name': 'Example', 'status': 'ENABLED'}],
             'languages': [{'resource_name': 'languageConstants/1000', 'name': 'English', 'targetable': True}]}
    monkeypatch.setattr(client, 'creation_state', lambda *args, **kwargs: copy.deepcopy(state))
    return state, fake_client


def campaign(**changes):
    args = dict(campaign_name='Synthetic Search', daily_budget='10', bidding_strategy='MANUAL_CPC',
                geo_target_ids=['100'], language_ids=['1000'], contains_eu_political_advertising=False)
    args.update(changes)
    return tools.draft_campaign(**args)


@pytest.mark.parametrize('strategy,message', [('MANUAL_CPC', 'manual_cpc'), ('MAXIMIZE_CONVERSIONS', 'maximize_conversions'), ('MAXIMIZE_CONVERSION_VALUE', 'maximize_conversion_value')])
def test_atomic_paused_campaign(creation, strategy, message):
    result = campaign(bidding_strategy=strategy)
    plan = rails._DRAFTS[result['draft_id']].plan
    assert [op.service for op in plan.operations] == ['CampaignBudgetService', 'CampaignService', 'CampaignCriterionService', 'CampaignCriterionService']
    assert plan.operations[1].operation['create']['status'] == 'PAUSED'
    assert plan.operations[1].operation['create'][message] == {}
    assert plan.operations[0].operation['create']['explicitly_shared'] is False
    client.validate_mutation_plan(plan)


@pytest.mark.parametrize('changes', [dict(daily_budget=True), dict(daily_budget='0'), dict(daily_budget='NaN'), dict(daily_budget='0.0000001'), dict(daily_budget='1001'), dict(campaign_name=''), dict(campaign_name='x\n'), dict(campaign_name='x'*129), dict(geo_target_ids=[]), dict(language_ids=['01']), dict(geo_target_ids=[True]), dict(geo_target_ids=['100','100']), dict(contains_eu_political_advertising=0), dict(bidding_strategy='TARGET_CPA'), dict(target_cpa='1'), dict(target_roas='2')])
def test_campaign_refusals(creation, changes):
    with pytest.raises(rails.RailViolation):
        campaign(**changes)


def test_creation_real_messages(creation, fake_gads):
    plan = rails._DRAFTS[campaign()['draft_id']].plan
    context = client.validate_mutation_plan(plan)
    built = [client._build_mutate_operation(fake_gads, op, context) for op in plan.operations]
    assert built[1].campaign_operation.create._pb.WhichOneof('campaign_bidding_strategy') == 'manual_cpc'
    assert built[1].campaign_operation.create.campaign_budget.endswith('/-1')
    assert built[2].campaign_criterion_operation.create.campaign.endswith('/-2')


@pytest.fixture
def group_creation(creation):
    state, fc = creation
    state.update(parent={'resource_name': f'customers/{CID}/campaigns/77', 'name': 'Parent',
                         'status': 'ENABLED', 'advertising_channel_type': 'SEARCH',
                         'advertising_channel_sub_type': 'UNSPECIFIED',
                         'campaign_budget': f'customers/{CID}/campaignBudgets/78'},
                 strategy={'type': 'MANUAL_CPC', 'portfolio_resource_name': None})
    return state, fc


@pytest.mark.parametrize('cpc', [None, '2.125'])
def test_group_paused_inherits_or_explicit(group_creation, cpc):
    result = tools.create_ad_group('77', 'New Group', cpc_bid=cpc)
    op = rails._DRAFTS[result['draft_id']].plan.operations[0]
    assert op.operation['create']['status'] == 'PAUSED'
    assert op.operation['create']['type_'] == 'SEARCH_STANDARD'
    assert ('cpc_bid_micros' in op.operation['create']) == (cpc is not None)


@pytest.mark.parametrize('strategy', ['MAXIMIZE_CONVERSIONS', 'MAXIMIZE_CONVERSION_VALUE', 'TARGET_CPA', 'TARGET_ROAS'])
def test_group_smart_inherit_only(group_creation, strategy):
    group_creation[0]['strategy']['type'] = strategy
    assert tools.create_ad_group('77', 'New')['dry_run']
    with pytest.raises(rails.RailViolation):
        tools.create_ad_group('77', 'New', cpc_bid='1')


@pytest.mark.parametrize('strategy', ['UNKNOWN', None, 'ENHANCED_CPC', 'MANUAL_CPM'])
def test_group_strategy_refuses(group_creation, strategy):
    group_creation[0]['strategy']['type'] = strategy
    with pytest.raises(rails.RailViolation):
        tools.create_ad_group('77', 'New')


def test_portfolio_cpc_refuses(group_creation):
    group_creation[0]['strategy']['portfolio_resource_name'] = f'customers/{CID}/biddingStrategies/8'
    with pytest.raises(rails.RailViolation):
        tools.create_ad_group('77', 'New', cpc_bid='1')


@pytest.mark.parametrize('declaration', [True, False])
@pytest.mark.parametrize('strategy', ['MANUAL_CPC', 'MAXIMIZE_CONVERSIONS', 'MAXIMIZE_CONVERSION_VALUE'])
def test_v25_oneofs_political_and_atomic_dispatch(creation, fake_gads, strategy, declaration):
    from tests.conftest import make_type
    plan = rails._DRAFTS[campaign(bidding_strategy=strategy, contains_eu_political_advertising=declaration)['draft_id']].plan
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    client._dispatch_entity(plan, False)
    assert len(fake_gads.mutate_calls) == 1
    request = fake_gads.mutate_calls[0]
    assert request['partial_failure'] is False
    assert request['validate_only'] is False
    entity = request['operations'][1].campaign_operation.create
    assert entity._pb.WhichOneof('campaign_bidding_strategy') == strategy.lower()
    assert entity.contains_eu_political_advertising.name == client.POLITICAL_DECLARATIONS[declaration]
    assert entity.status.name == 'PAUSED'
    assert request['operations'][0].campaign_budget_operation.create.name == ''
    assert request['operations'][3].campaign_criterion_operation.create.language.language_constant == 'languageConstants/1000'


@pytest.mark.parametrize('damage', ['forward', 'orphan', 'cross_kind', 'wrong_customer', 'duplicate', 'negative_update', 'dangling', 'extra_field', 'active', 'strategy_pair', 'empty_unknown'])
def test_later_bad_op_refuses_before_client(creation, monkeypatch, damage):
    plan = copy.deepcopy(rails._DRAFTS[campaign()['draft_id']].plan)
    ops = plan.operations
    if damage == 'forward':
        ops[0], ops[1] = ops[1], ops[0]
    elif damage == 'orphan':
        del ops[1:]
    elif damage == 'cross_kind':
        ops[2].operation['create']['campaign'] = ops[0].operation['create']['resource_name']
    elif damage == 'wrong_customer':
        ops[2].operation['create']['campaign'] = 'customers/99/campaigns/-2'
    elif damage == 'duplicate':
        ops.append(copy.deepcopy(ops[0]))
    elif damage == 'negative_update':
        ops.append(rails.MutationOp('CampaignService', {'update': {'resource_name': f'customers/{CID}/campaigns/-2', 'status': 'PAUSED'}}, ['status']))
    elif damage == 'dangling':
        ops[3].operation['create']['campaign'] = f'customers/{CID}/campaigns/-3'
    elif damage == 'extra_field':
        ops[1].operation['create']['bidding_strategy_type'] = 'MANUAL_CPC'
    elif damage == 'active':
        ops[1].operation['create']['status'] = 'ENABLED'
    elif damage == 'strategy_pair':
        ops[1].operation['create']['maximize_conversions'] = {}
    else:
        ops[1].operation['create']['unknown_message'] = {}
    calls = []
    monkeypatch.setattr(client, 'gads', lambda *a: calls.append(a))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert calls == []


def test_direct_builder_cannot_resolve_negatives(creation, fake_gads):
    plan = rails._DRAFTS[campaign()['draft_id']].plan
    for op in plan.operations:
        with pytest.raises(rails.RailViolation):
            client._build_mutate_operation(fake_gads, op)


@pytest.mark.parametrize('field,value', [('period', 'CUSTOM_PERIOD'), ('explicitly_shared', True), ('amount_micros', True), ('amount_micros', 0), ('amount_micros', 2**63)])
def test_budget_structural_payload(creation, field, value):
    plan = copy.deepcopy(rails._DRAFTS[campaign()['draft_id']].plan)
    plan.operations[0].operation['create'][field] = value
    with pytest.raises(rails.RailViolation):
        client.validate_mutation_plan(plan)


@pytest.mark.parametrize('change', ['currency', 'timezone', 'constant', 'collision', 'caps', 'permission', 'parent', 'strategy'])
def test_apply_rechecks(creation, group_creation, monkeypatch, change):
    state, fc = creation
    group = change in {'parent', 'strategy'}
    draft = tools.create_ad_group('77', 'New') if group else campaign()
    if change == 'currency':
        state['account']['currency_code'] = 'EUR'
    elif change == 'timezone':
        state['account']['time_zone'] = 'UTC'
    elif change == 'constant':
        state['locations'][0]['name'] = 'Changed'
    elif change == 'collision':
        state['collisions'].append({'name': 'New'})
    elif change == 'caps':
        monkeypatch.setenv('GOOGLE_ADS_MAX_DAILY_BUDGET', '1')
    elif change == 'permission':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif change == 'parent':
        state['parent']['name'] = 'Changed'
    else:
        state['strategy']['type'] = 'MAXIMIZE_CONVERSIONS'
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert fc.dispatch_calls == []


def _landed_fixture(plan):
    names = {'campaign_budget': f'customers/{CID}/campaignBudgets/901',
             'campaign': f'customers/{CID}/campaigns/902',
             'ad_group': f'customers/{CID}/adGroups/903'}
    results, states = [], {}
    for check in plan.post_checks:
        entity, index = check['entity_type'], check['result_index']
        rn = names.get(entity, f'customers/{CID}/campaignCriteria/902~{910+index}')
        results.append({'type': entity+'_result', 'resource_name': rn})
        expected = copy.deepcopy(check['expected'])
        if entity == 'campaign':
            expected.update(campaign_budget=names['campaign_budget'], bidding_strategy_type=check['strategy'])
            if check['strategy_parameters']:
                expected[check['strategy'].lower()] = check['strategy_parameters']
        if entity == 'campaign_criterion':
            expected['campaign'] = names['campaign']
        states[rn] = dict(expected, resource_name=rn)
    return {'results': results}, states


@pytest.mark.parametrize('damage', [None, 'missing', 'wrong_kind', 'wrong_customer', 'negative', 'duplicate', 'wrong_parent', 'setting'])
def test_landed_postchecks_consume_and_audit_once(creation, monkeypatch, damage):
    import json
    from pathlib import Path

    from mcp_google_ads_safe import audit
    state, fc = creation
    draft = campaign()
    plan = rails._DRAFTS[draft['draft_id']].plan
    fc.dispatch_result, states = _landed_fixture(plan)
    reads = []
    def read(cid, entity, rn):
        reads.append(rn)
        return states[rn]
    monkeypatch.setattr(client, 'created_resource_state', read)
    results = fc.dispatch_result['results']
    if damage == 'missing':
        results.pop()
    elif damage == 'wrong_kind':
        results[-1]['type'] = 'ad_group_result'
    elif damage == 'wrong_customer':
        results[-1]['resource_name'] = 'customers/88/campaignCriteria/902~913'
    elif damage == 'negative':
        results[0]['resource_name'] = f'customers/{CID}/campaignBudgets/-1'
    elif damage == 'duplicate':
        results[-1] = dict(results[-2])
    elif damage == 'wrong_parent':
        results[-1]['resource_name'] = f'customers/{CID}/campaignCriteria/999~913'
    elif damage == 'setting':
        states[results[1]['resource_name']]['status'] = 'ENABLED'
    out = rails.apply_draft(draft['draft_id'])
    assert out['applied'] is True
    assert out['verified'] is (damage is None)
    assert len(fc.dispatch_calls) == 1
    if damage:
        assert 'do not retry' in out['next']
    if damage not in {None, 'setting'}:
        assert reads == []
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    assert sum(event['phase'] in {'apply', 'apply_unverified'} for event in events) == 1


def test_deep_input_snapshot_and_postcheck_tamper(creation):
    geos = ['100']
    draft = campaign(geo_target_ids=geos)
    geos.append('999')
    saved = rails._DRAFTS[draft['draft_id']]
    saved.validate_fn()
    saved.plan.post_checks[0]['expected']['amount_micros'] = 1
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert creation[1].dispatch_calls == []


@pytest.mark.parametrize('strategy,kwargs,key,value', [
    ('MAXIMIZE_CONVERSIONS', {'target_cpa': '3.125'}, 'target_cpa_micros', 3125000),
    ('MAXIMIZE_CONVERSION_VALUE', {'target_roas': '2.75'}, 'target_roas', 2.75)])
def test_strategy_targets_real_messages(creation, fake_gads, strategy, kwargs, key, value):
    plan = rails._DRAFTS[campaign(bidding_strategy=strategy, **kwargs)['draft_id']].plan
    op = client._build_mutate_operation(fake_gads, plan.operations[1], client.validate_mutation_plan(plan))
    assert getattr(getattr(op.campaign_operation.create, strategy.lower()), key) == value


@pytest.mark.parametrize('amount', [True, '0', '-1', 'NaN', 'Infinity', '0.0000001', '51'])
def test_group_cpc_bad_money(group_creation, amount):
    with pytest.raises(rails.RailViolation):
        tools.create_ad_group('77', 'New', cpc_bid=amount)


@pytest.mark.parametrize('field', ['geo_target_ids', 'language_ids'])
def test_target_bounds(creation, field):
    with pytest.raises(rails.RailViolation):
        campaign(**{field: [str(i) for i in range(1,102)]})


@pytest.mark.parametrize('name', ['é'*128, 'abc\x00', 'abc\t'])
def test_byte_limit_and_controls(creation, name):
    with pytest.raises(rails.RailViolation):
        campaign(campaign_name=name)


def test_blocked_names(creation, monkeypatch):
    from mcp_google_ads_safe import settings
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ['synthetic'])
    with pytest.raises(rails.RailViolation, match='blocked'):
        campaign()


def test_unknown_outcome_not_retried(creation):
    fc = creation[1]
    fc.dispatch_error = rails.UnknownWriteOutcome('uncertain')
    draft = campaign()
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(draft['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert len(fc.dispatch_calls) == 1


@pytest.mark.parametrize('service', ['CampaignService', 'AdGroupService', 'AdGroupCriterionService'])
def test_all_serving_creates_share_pause_helper(service):
    fields = {'name': 'New', 'status': 'ENABLED'}
    if service == 'AdGroupCriterionService':
        fields['keyword'] = {'text': 'synthetic', 'match_type': 'EXACT'}
    assert rails.safe_create_operation(service, fields).operation['create']['status'] == 'PAUSED'
    assert fields['status'] == 'ENABLED'


def test_group_effective_cpc_postcheck(group_creation, monkeypatch):
    fc = group_creation[1]
    draft = tools.create_ad_group('77', 'New', cpc_bid='2')
    plan = rails._DRAFTS[draft['draft_id']].plan
    fc.dispatch_result, states = _landed_fixture(plan)
    states[next(iter(states))]['effective_cpc_bid_micros'] = 1000000
    monkeypatch.setattr(client, 'created_resource_state', lambda cid, entity, rn: states[rn])
    result = rails.apply_draft(draft['draft_id'])
    assert result['applied'] and not result['verified']


def test_campaign_batch_rejects_unrelated_target_and_missing_targets(creation):
    original = rails._DRAFTS[campaign()['draft_id']].plan
    for variant in ['unrelated', 'no_targets', 'wrong_type']:
        plan = copy.deepcopy(original)
        if variant == 'unrelated':
            plan.operations[2].operation['create']['campaign'] = f'customers/{CID}/campaigns/88'
        elif variant == 'no_targets':
            del plan.operations[2:]
        else:
            plan.operations[2].operation['create'].pop('location')
            plan.operations[2].operation['create']['keyword'] = {'text':'example', 'match_type':'EXACT'}
            plan.operations[2].operation['create']['negative'] = True
        with pytest.raises(rails.RailViolation):
            client.validate_mutation_plan(plan)


@pytest.mark.parametrize('group', [False, True])
def test_v25_dispatch_response_and_readback_end_to_end(creation, group_creation, fake_gads, monkeypatch, group):
    from tests.conftest import _FakeSearchPager, make_search_response, make_type
    draft = tools.create_ad_group('77', 'New', cpc_bid='2') if group else campaign()
    plan = rails._DRAFTS[draft['draft_id']].plan
    result, states = _landed_fixture(plan)
    response = make_type('MutateGoogleAdsResponse')
    for entry in result['results']:
        response.mutate_operation_responses.append({entry['type']: {'resource_name': entry['resource_name']}})
    fake_gads.mutate_response = response
    monkeypatch.setattr(client, '_dispatch', lambda p: client._dispatch_entity(p, False))
    monkeypatch.setattr(client, 'gaql_all', REAL_GAQL_ALL)
    def search(request):
        assert len(fake_gads.mutate_calls) == 1
        fake_gads.search_requests.append(request)
        entity = request.query.split(' FROM ')[1].split()[0]
        rn = request.query.rsplit("'", 2)[1]
        row = make_type('GoogleAdsRow')
        setattr(row, entity, states[rn])
        return _FakeSearchPager(make_search_response([row]))
    monkeypatch.setattr(fake_gads._svc, 'search', search)
    out = rails.apply_draft(draft['draft_id'])
    assert out['applied'] and out['verified'], out.get('error')
    assert len(fake_gads.mutate_calls) == 1
    assert len(fake_gads.search_requests) == len(plan.operations)


@pytest.mark.parametrize('status', [None, 'ENABLED'])
def test_keyword_direct_create_must_be_paused(status):
    fields = {'ad_group': f'customers/{CID}/adGroups/3', 'keyword': {'text': 'sample', 'match_type': 'EXACT'}}
    if status is not None:
        fields['status'] = status
    op = rails.MutationOp('AdGroupCriterionService', {'create': fields}, None)
    with pytest.raises(rails.RailViolation):
        client.validate_mutation_operation(op, CID)


def test_surrogate_name_refusal(creation):
    with pytest.raises(rails.RailViolation):
        campaign(campaign_name='bad\ud800')


@pytest.mark.parametrize('status', ['PAUSED', 'REMOVED'])
def test_created_target_must_be_enabled(creation, monkeypatch, status):
    draft = campaign()
    plan = rails._DRAFTS[draft['draft_id']].plan
    fc = creation[1]
    fc.dispatch_result, states = _landed_fixture(plan)
    states[fc.dispatch_result['results'][2]['resource_name']]['status'] = status
    monkeypatch.setattr(client, 'created_resource_state', lambda cid, entity, rn: states[rn])
    out = rails.apply_draft(draft['draft_id'])
    assert out['applied'] and not out['verified']
    assert len(fc.dispatch_calls) == 1


@pytest.mark.parametrize('strategy,field,target', [
    ('MAXIMIZE_CONVERSIONS', 'target_cpa_micros', 3000000),
    ('MAXIMIZE_CONVERSION_VALUE', 'target_roas', 3.0)])
@pytest.mark.parametrize('representation', ['absent', 'empty', 'zero', 'unexpected', 'explicit', 'explicit_mismatch'])
@pytest.mark.parametrize('real_provider', [False, True])
def test_optional_creation_target_readback(creation, fake_gads, monkeypatch,
                                          strategy, field, target, representation, real_provider):
    import json
    from pathlib import Path

    from mcp_google_ads_safe import audit
    from tests.conftest import _FakeSearchPager, make_search_response, make_type

    kwargs = {}
    if representation.startswith('explicit'):
        kwargs['target_cpa' if field == 'target_cpa_micros' else 'target_roas'] = '3'
    draft = campaign(bidding_strategy=strategy, **kwargs)
    plan = rails._DRAFTS[draft['draft_id']].plan
    result, states = _landed_fixture(plan)
    campaign_state = states[result['results'][1]['resource_name']]
    if representation != 'absent':
        campaign_state[strategy.lower()] = {} if representation == 'empty' else {
            field: 0 if representation == 'zero' else target * (2 if representation == 'explicit_mismatch' else 1)}
    if real_provider:
        response = make_type('MutateGoogleAdsResponse')
        for entry in result['results']:
            response.mutate_operation_responses.append({entry['type']: {'resource_name': entry['resource_name']}})
        fake_gads.mutate_response = response
        monkeypatch.setattr(client, '_dispatch', lambda p: client._dispatch_entity(p, False))
        monkeypatch.setattr(client, 'gaql_all', REAL_GAQL_ALL)
        def search(request):
            assert len(fake_gads.mutate_calls) == 1
            entity = request.query.split(' FROM ')[1].split()[0]
            rn = request.query.rsplit("'", 2)[1]
            row = make_type('GoogleAdsRow')
            setattr(row, entity, states[rn])
            return _FakeSearchPager(make_search_response([row]))
        monkeypatch.setattr(fake_gads._svc, 'search', search)
    else:
        creation[1].dispatch_result = result
        monkeypatch.setattr(client, 'created_resource_state', lambda cid, entity, rn: states[rn])
    out = rails.apply_draft(draft['draft_id'])
    mismatch = representation in {'unexpected', 'explicit_mismatch'}
    assert out['applied'] is True
    assert out['verified'] is not mismatch, out
    if mismatch:
        assert out['code'] == 'POST_WRITE_VERIFICATION_FAILED'
        assert 'do not retry' in out['next']
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert len(fake_gads.mutate_calls if real_provider else creation[1].dispatch_calls) == 1
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    landed = [event for event in events if event['phase'] in {'apply', 'apply_unverified'}]
    assert len(landed) == 1
    assert landed[0]['phase'] == ('apply_unverified' if mismatch else 'apply')
    if mismatch:
        assert landed[0]['applied'] is True
        assert landed[0]['code'] == 'POST_WRITE_VERIFICATION_FAILED'
        assert landed[0]['verification_error']
