"""Offline bounded recommendation request, refusal, drift, and saved-state checks."""
import copy
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

RN = f'customers/{CID}/recommendations/opaque-A_b'
BN = f'customers/{CID}/campaignBudgets/42'
CN = f'customers/{CID}/campaigns/123'


@pytest.fixture
def recommendation(monkeypatch, fake_gads):
    rails._DRAFTS.clear()
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION', 'true')
    data = {
        'recommendation': {'resource_name': RN, 'type_': 'CAMPAIGN_BUDGET',
            'dismissed': False, 'campaign_budget': BN, 'campaign': '', 'ad_group': '',
            'campaign_budget_recommendation': {'current_budget_amount_micros': '20000000',
                'recommended_budget_amount_micros': '25000000', 'budget_options': []}},
        'budget': {'resource_name': BN, 'amount_micros': '20000000', 'period': 'DAILY',
            'explicitly_shared': False, 'reference_count': '1', 'aligned_bidding_strategy_id': '0',
            'total_amount_micros': '0'},
        'campaigns': [{'id': '123', 'resource_name': CN, 'name': 'Search',
                      'status': 'PAUSED', 'campaign_budget': BN}],
        'currency': 'USD', 'reads': [], 'after': None,
    }

    def page(query, cid, token):
        assert cid == CID and token is None and 'LIMIT' not in query
        data['reads'].append(query)
        entity = query.split(' FROM ')[1].split()[0]
        if entity == 'recommendation':
            return [{'recommendation': copy.deepcopy(data['recommendation'])}], None, 1
        if entity == 'campaign_budget':
            budget = copy.deepcopy(data['budget'])
            if fake_gads.reco_calls:
                budget['amount_micros'] = '25000000'
                if data['after']:
                    data['after'](budget)
            return [{'campaign_budget': budget, 'customer': {'currency_code': data['currency']}}], None, 1
        return [{'campaign': copy.deepcopy(c)} for c in data['campaigns']], None, len(data['campaigns'])

    def campaign_budget(cid, campaign_id):
        b = data['budget']
        return dict(budget_resource_name=b['resource_name'], campaign_status=data['campaigns'][0]['status'],
            amount=client.from_micros(b['amount_micros']), explicitly_shared=b['explicitly_shared'],
            reference_count=int(b['reference_count']), period=b['period'], aligned_bidding_strategy_id=None)

    class Row(dict):
        @staticmethod
        def to_dict(row):
            return dict(row)

    def typed_page(query, cid, token):
        rows, next_token, total = page(query, cid, token)
        return [Row(row) for row in rows], next_token, total

    monkeypatch.setattr(client, '_search_one_page', typed_page)
    monkeypatch.setattr(client, 'campaign_budget', campaign_budget)
    fake_gads.reco_response = make_type('ApplyRecommendationResponse')
    fake_gads.reco_response.results.append({'resource_name': RN})
    return data, fake_gads


def draft(**kw):
    return tools.apply_recommendation(kw.pop('recommendation_id', 'opaque-A_b'), **kw)


def test_exact_real_request_and_saved_state(recommendation):
    data, fake = recommendation
    d = draft()
    assert d['preview']['currency'] == 'USD'
    assert d['preview']['affected_campaigns'][0]['status'] == 'PAUSED'
    assert d['preview']['new_daily_budget'] == '25'
    out = rails.apply_draft(d['draft_id'])
    assert out['verified'] is True
    assert not fake.mutate_calls and len(fake.reco_calls) == 1
    req = fake.reco_calls[0][1]
    assert req.customer_id == CID and req.partial_failure is False
    assert len(req.operations) == 1
    op = req.operations[0]
    assert op.resource_name == RN and op.campaign_budget.new_budget_amount_micros == 25000000
    pb = type(op).pb(op)
    assert pb.WhichOneof('apply_parameters') == 'campaign_budget'
    assert pb.campaign_budget.HasField('new_budget_amount_micros')
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.reco_calls) == 1


@pytest.mark.parametrize('field,value', [('recommendation_id', ''), ('recommendation_id', 'x/y'),
 ('recommendation_id', ' x'), ('recommendation_id', 'x~y'), ('recommendation_id', "x'"),
 ('recommendation_id', 123), ('recommendation_id', True), ('recommendation_id', 'é'),
 ('customer_id', ''), ('customer_id', '0123'), ('customer_id', False), ('customer_id', '１２３')])
def test_strict_inputs(recommendation, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})
    assert not recommendation[0]['reads']
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION','false'),
 ('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION', 'yesplease'), ('GOOGLE_ADS_ENABLE_WRITES','false'),
 ('GOOGLE_ADS_READ_CUSTOMER_IDS','456'), ('GOOGLE_ADS_WRITE_CUSTOMER_IDS','456')])
def test_early_gate(recommendation, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(rails.RailViolation):
        draft()
    assert not recommendation[0]['reads']


@pytest.mark.parametrize('field,value', [('type_', 'KEYWORD'), ('dismissed', True), ('dismissed', 0),
 ('campaign_budget', 'customers/456/campaignBudgets/42'), ('campaign', 'customers/456/campaigns/123'),
 ('ad_group', 'customers/123/adGroups/1'), ('campaign_budget_recommendation', None)])
def test_recommendation_refusals(recommendation, field, value):
    recommendation[0]['recommendation'][field] = value
    with pytest.raises(rails.RailViolation):
        draft()
    assert not rails._DRAFTS


@pytest.mark.parametrize('value', [None, True, 0, -1, 1.5, '1.5', '01', 9223372036854775808])
def test_invalid_amount(recommendation, value):
    recommendation[0]['recommendation']['campaign_budget_recommendation']['recommended_budget_amount_micros'] = value
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('field,value', [('period','CUSTOM_PERIOD'), ('explicitly_shared', 'false'),
 ('reference_count', '2'), ('aligned_bidding_strategy_id', '1'), ('amount_micros', '21000000')])
def test_budget_refusals(recommendation, field, value):
    recommendation[0]['budget'][field] = value
    with pytest.raises(rails.RailViolation):
        draft()


def test_shared_requires_opt_in(recommendation, monkeypatch):
    data, _ = recommendation
    data['budget']['explicitly_shared'] = True
    with pytest.raises(rails.RailViolation):
        draft()
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT', 'true')
    assert draft()['preview']['affected_campaigns']


@pytest.mark.parametrize('mutation', [lambda d: d.update(currency='EUR'),
 lambda d: d['campaigns'][0].update(status='ENABLED'),
 lambda d: d['campaigns'][0].update(campaign_budget='customers/123/campaignBudgets/9'),
 lambda d: d['recommendation']['campaign_budget_recommendation'].update(recommended_budget_amount_micros='26000000'),
 lambda d: d['budget'].update(explicitly_shared=True)])
def test_drift_before_consumption(recommendation, mutation):
    data, fake = recommendation
    d = draft()
    mutation(data)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not fake.reco_calls


def test_structural_gate_recheck(recommendation, monkeypatch):
    d = draft()
    stored = rails._DRAFTS[d['draft_id']]
    stored.tool = 'misleading'
    stored.validate_fn = None
    monkeypatch.delenv('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION')
    with pytest.raises(rails.RailViolation, match='GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION'):
        rails.apply_draft(d['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.create_draft('misleading', stored.preview, stored.plan, {})
    assert d['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('field,value', [('amount_micros','24000000'), ('period','CUSTOM_PERIOD'),
 ('explicitly_shared',True), ('reference_count','2'), ('resource_name', 'customers/456/campaignBudgets/42')])
def test_postcheck_consumed_unverified(recommendation, field, value):
    data, fake = recommendation
    data['after'] = lambda b: b.update({field:value})
    d = draft()
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified'] is False
    assert d['draft_id'] not in rails._DRAFTS and len(fake.reco_calls) == 1


@pytest.mark.parametrize('response', ['empty', 'foreign', 'extra', 'error', 'malformed'])
def test_response_unknown_consumed(recommendation, response):
    _, fake = recommendation
    resp = make_type('ApplyRecommendationResponse')
    if response != 'empty':
        resp.results.append({'resource_name': RN if response != 'foreign' else 'customers/456/recommendations/77'})
    if response == 'extra':
        resp.results.append({'resource_name': RN})
    if response == 'error':
        resp.partial_failure_error.code = 3
        resp.partial_failure_error.message = 'bad request'
    fake.reco_response = object() if response == 'malformed' else resp
    d = draft()
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] not in rails._DRAFTS and len(fake.reco_calls) == 1
    assert 'unknown' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('change', [{'rpc':'other'}, {'mutate_customer_id':'0123'},
 {'new_budget_amount_micros':None}, {'new_budget_amount_micros':True},
 {'new_budget_amount_micros':9223372036854775808}, {'new_budget_amount_micros':'25000000'},
 {'recommendation_resource_name':'customers/123/campaigns/1'}, {'rpc':'dismiss'}])
def test_invalid_plan_before_provider(monkeypatch, change):
    values = dict(mutate_customer_id=CID, rpc='apply', recommendation_resource_name=RN,
                  new_budget_amount_micros=25000000)
    values.update(change)
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.RecommendationActionPlan(**values))


def test_validate_only_before_provider(monkeypatch):
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.RecommendationActionPlan(CID, 'apply', RN, new_budget_amount_micros=25000000), True)


def test_repeated_read_disagreement(recommendation, monkeypatch):
    original = client.campaign_budget
    def changed(*args):
        return dict(original(*args), campaign_status='ENABLED')
    monkeypatch.setattr(client, 'campaign_budget', changed)
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('amount', ['20000000', '999999999000000'])
def test_noop_and_budget_cap(recommendation, amount):
    recommendation[0]['recommendation']['campaign_budget_recommendation']['recommended_budget_amount_micros'] = amount
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('rows', [[], [{'recommendation': {'resource_name':'foreign'}}],
 [{'recommendation': {'resource_name':RN}}, {'recommendation': {'resource_name':RN}}]])
def test_missing_duplicate_foreign_identity(recommendation, monkeypatch, rows):
    monkeypatch.setattr(client, 'gaql_all', lambda *args: rows)
    with pytest.raises(rails.RailViolation):
        draft()


def test_postcheck_read_failure(recommendation, monkeypatch):
    d = draft()
    original = client.recommendation_budget_state
    def read(*args):
        if recommendation[1].reco_calls:
            raise RuntimeError('read unavailable')
        return original(*args)
    monkeypatch.setattr(client, 'recommendation_budget_state', read)
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified']
    assert len(recommendation[1].reco_calls) == 1


def test_postcheck_campaign_status(recommendation, monkeypatch):
    d = draft()
    original = client.shared_budget_attachments
    def read(*args):
        result = original(*args)
        if recommendation[1].reco_calls:
            result[0]['status'] = 'ENABLED'
        return result
    monkeypatch.setattr(client, 'shared_budget_attachments', read)
    assert not rails.apply_draft(d['draft_id'])['verified']


def test_real_v25_read_shape(recommendation, monkeypatch):
    original = client._search_one_page
    def page(*args):
        rows, token, total = original(*args)
        return [type(make_type('GoogleAdsRow'))(row) for row in rows], token, total
    monkeypatch.setattr(client, '_search_one_page', page)
    assert draft()['preview']['recommendation']['type_'] == 'CAMPAIGN_BUDGET'


def test_expiry_and_tamper(recommendation):
    d = draft()
    rails._DRAFTS[d['draft_id']].created_at -= rails.draft_ttl_seconds() + 1
    with pytest.raises(rails.RailViolation, match='expired'):
        rails.apply_draft(d['draft_id'])
    assert not recommendation[1].reco_calls
    d = draft()
    rails._DRAFTS[d['draft_id']].plan.post_checks[0]['expected']['budget']['amount_micros'] = 26000000
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not recommendation[1].reco_calls


def test_extra_plan_fields(recommendation, monkeypatch):
    plan = rails.RecommendationActionPlan(CID, 'apply', RN, new_budget_amount_micros=25000000)
    object.__setattr__(plan, 'operations', [{'keyword': 'unsafe'}])
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(plan)


def test_dispatch_exception_never_retries(recommendation, monkeypatch):
    _, fake = recommendation
    service = fake.get_service('RecommendationService')
    calls = []
    def uncertain(request):
        calls.append(request)
        raise RuntimeError('failure after send')
    monkeypatch.setattr(service, 'apply_recommendation', uncertain)
    original = fake.get_service
    monkeypatch.setattr(fake, 'get_service', lambda name: service if name == 'RecommendationService' else original(name))
    d = draft()
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(d['draft_id'])
    assert len(calls) == 1 and d['draft_id'] not in rails._DRAFTS


def test_enabled_omitted_callback_refuses(recommendation):
    compiled = rails._compile_apply_recommendation(CID, 'opaque-A_b')
    with pytest.raises(rails.RailViolation, match='callback'):
        rails.create_draft('misleading', compiled.preview, compiled.plan, compiled.fingerprint)
    assert not recommendation[1].reco_calls


def test_enabled_removed_callback_refuses(recommendation):
    d = draft()
    stored = rails._DRAFTS[d['draft_id']]
    stored.tool = 'misleading'
    stored.validate_fn = None
    with pytest.raises(rails.RailViolation, match='callback'):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not recommendation[1].reco_calls


@pytest.mark.parametrize('change', ['cap', 'drift'])
def test_replaced_callback_cannot_skip_bounded_compile(recommendation, monkeypatch, change):
    d = draft()
    stored = rails._DRAFTS[d['draft_id']]
    stored.tool = 'misleading'
    stored.validate_fn = lambda: None
    if change == 'cap':
        monkeypatch.setenv('GOOGLE_ADS_MAX_DAILY_BUDGET', '1')
    else:
        recommendation[0]['campaigns'][0]['status'] = 'ENABLED'
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not recommendation[1].reco_calls


@pytest.mark.parametrize('checks', [[], None, [{}], [{'recommendation_budget': True}]])
def test_incomplete_checks_refuse_all_boundaries(recommendation, monkeypatch, checks):
    compiled = rails._compile_apply_recommendation(CID, 'opaque-A_b')
    plan = compiled.plan
    object.__setattr__(plan, 'post_checks', checks)
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(plan)
    with pytest.raises(rails.RailViolation):
        rails.create_draft('misleading', compiled.preview, plan, compiled.fingerprint, lambda: None)
    assert not recommendation[1].reco_calls


def test_removed_postchecks_refuse_before_consumption(recommendation):
    d = draft()
    rails._DRAFTS[d['draft_id']].plan.post_checks.clear()
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not recommendation[1].reco_calls


def test_fake_recommendation_variant_refuses(recommendation, monkeypatch):
    from types import SimpleNamespace
    compiled = rails._compile_apply_recommendation(CID, 'opaque-A_b')
    fake_plan = SimpleNamespace(**vars(compiled.plan))
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(fake_plan)
    with pytest.raises(rails.RailViolation):
        rails.create_draft('misleading', compiled.preview, fake_plan, compiled.fingerprint, lambda: None)


@pytest.mark.parametrize('edit', [lambda c: c['expected']['budget'].pop('currency'),
 lambda c: c['expected']['budget'].update(amount_micros=26000000),
 lambda c: c['expected']['attachments'].clear(),
 lambda c: c['expected']['attachments'][0].update(status='REMOVED'),
 lambda c: c.update(customer_id='456')])
def test_malformed_complete_postcheck_refuses(recommendation, monkeypatch, edit):
    compiled = rails._compile_apply_recommendation(CID, 'opaque-A_b')
    edit(compiled.plan.post_checks[0])
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(compiled.plan)


def test_internal_dismiss_callback_refuses_before_consumption(recommendation, monkeypatch):
    def refuse():
        raise rails.RailViolation('dismiss policy changed')

    monkeypatch.setattr(client, '_dispatch', lambda *args: pytest.fail('dispatch reached'))
    compiled = rails.compile(rails.DismissRecommendationIntent(CID, 'opaque-A_b'))
    d = rails.create_draft('internal_dismiss', compiled.preview, compiled.plan, compiled.fingerprint, refuse)
    with pytest.raises(rails.RailViolation, match='dismiss policy changed'):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS
    assert not recommendation[1].reco_calls
