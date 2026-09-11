"""Offline exact dismissal approval, request and positive saved-state verification."""
import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type
from tests.test_recommendation_apply import recommendation  # noqa: F401

RID = 'opaque-A_b'
RN = f'customers/{CID}/recommendations/{RID}'


@pytest.fixture
def dismissal(recommendation, monkeypatch):  # noqa: F811
    data, fake = recommendation
    data['recommendation'].update(type_='KEYWORD', campaign_budget='')
    data['recommendation'].pop('campaign_budget_recommendation')
    monkeypatch.delenv('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION')
    original = client._search_one_page
    data['post'] = lambda rec: rec.update(dismissed=True)

    def page(query, cid, token):
        assert 'dismissed = FALSE' not in query and 'LIMIT' not in query
        rows, next_token, total = original(query, cid, token)
        if fake.reco_calls:
            data['post'](rows[0]['recommendation'])
        return rows, next_token, total

    monkeypatch.setattr(client, '_search_one_page', page)
    fake.reco_response = make_type('DismissRecommendationResponse')
    fake.reco_response.results.append({'resource_name': RN})
    return data, fake


def draft(**kwargs):
    return tools.dismiss_recommendation(kwargs.pop('recommendation_id', RID), **kwargs)


def test_real_exact_parameter_free_request_and_positive_saved_flag(dismissal):
    data, fake = dismissal
    d = draft()
    assert d['preview']['recommendation']['dismissed'] is False
    assert d['preview']['expected']['dismissed'] is True
    assert d['preview']['confirmation_required'] is True
    out = rails.apply_draft(d['draft_id'])
    assert out['verified'] is True
    assert len(fake.reco_calls) == 1 and not fake.mutate_calls
    rpc, req = fake.reco_calls[0]
    assert rpc == 'dismiss' and req.customer_id == CID and req.partial_failure is False
    assert len(req.operations) == 1 and req.operations[0].resource_name == RN
    assert {f.name for f, _ in type(req.operations[0]).pb(req.operations[0]).ListFields()} == {'resource_name'}
    assert {f.name for f in type(req).pb(req).DESCRIPTOR.fields} == {'customer_id', 'operations', 'partial_failure'}
    assert not any('FROM campaign' in q for q in data['reads'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])


@pytest.mark.parametrize('field,value', [('recommendation_id', ''), ('recommendation_id', True),
    ('recommendation_id', 7), ('recommendation_id', 'a/b'), ('recommendation_id', "x'"),
    ('recommendation_id', ' x'), ('recommendation_id', RN), ('customer_id', ''),
    ('customer_id', True), ('customer_id', 123), ('customer_id', '0123'), ('customer_id', ' 123')])
def test_strict_inputs_before_reads(dismissal, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})
    assert not dismissal[0]['reads']


@pytest.mark.parametrize('env', ['GOOGLE_ADS_ENABLE_WRITES', 'GOOGLE_ADS_READ_CUSTOMER_IDS',
                                'GOOGLE_ADS_WRITE_CUSTOMER_IDS'])
def test_gates_before_reads_and_on_confirm(dismissal, monkeypatch, env):
    d = draft()
    dismissal[0]['reads'].clear()
    monkeypatch.setenv(env, 'false' if 'ENABLE' in env else '999')
    for call in [draft, lambda: rails.apply_draft(d['draft_id'])]:
        with pytest.raises(rails.RailViolation):
            call()
    assert not dismissal[0]['reads'] and d['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('field,value', [('type_', 'UNKNOWN'), ('type_', 'UNSPECIFIED'),
    ('type_', 'FUTURE'), ('type_', 999), ('dismissed', True), ('dismissed', 0), ('dismissed', None),
    ('campaign', None), ('campaign', 'customers/999/campaigns/1'),
    ('campaign_budget', f'customers/{CID}/campaigns/1'), ('ad_group', f'customers/{CID}/adGroups/01'),
    ('resource_name', f'customers/999/recommendations/{RID}')])
def test_malformed_or_noop_snapshot_refuses(dismissal, field, value):
    dismissal[0]['recommendation'][field] = value
    with pytest.raises(rails.RailViolation):
        draft()
    assert not dismissal[1].reco_calls


@pytest.mark.parametrize('field,value', [('type_', 'CAMPAIGN_BUDGET'), ('dismissed', True),
    ('campaign', f'customers/{CID}/campaigns/1'), ('ad_group', f'customers/{CID}/adGroups/2'),
    ('campaign_budget', f'customers/{CID}/campaignBudgets/3')])
def test_fresh_drift_refuses_even_noop_callback(dismissal, field, value):
    d = draft()
    rails._DRAFTS[d['draft_id']].validate_fn = lambda: None
    dismissal[0]['recommendation'][field] = value
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not dismissal[1].reco_calls


@pytest.mark.parametrize('change', [lambda d: setattr(d, 'validate_fn', None),
    lambda d: d.plan.post_checks.clear(), lambda d: d.preview.update(recommendation_id='other'),
    lambda d: d.fingerprint['intent'].update(recommendation_id='other'),
    lambda d: d.plan.post_checks[0]['expected'].update(dismissed=False),
    lambda d: d.plan.post_checks[0].update(extra=True)])
def test_structural_tampering_refuses_before_consumption(dismissal, change):
    d = draft()
    saved = rails._DRAFTS[d['draft_id']]
    saved.tool = 'misleading'
    change(saved)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] in rails._DRAFTS and not dismissal[1].reco_calls


@pytest.mark.parametrize('post', [lambda r: None, lambda r: r.pop('dismissed'),
    lambda r: r.update(dismissed=1), lambda r: r.update(dismissed=True, type_='CAMPAIGN_BUDGET'),
    lambda r: r.update(dismissed=True, campaign=f'customers/{CID}/campaigns/9'),
    lambda r: r.update(resource_name='other')])
def test_saved_mismatch_is_consumed_unverified(dismissal, post):
    dismissal[0]['post'] = post
    d = draft()
    out = rails.apply_draft(d['draft_id'])
    assert out['verified'] is False and d['draft_id'] not in rails._DRAFTS
    assert len(dismissal[1].reco_calls) == 1


@pytest.mark.parametrize('rows,total', [([], 0), ([{}, {}], 2), ([{}], 2)])
def test_complete_scan_missing_duplicates_count_refuse(dismissal, monkeypatch, rows, total):
    rec = dismissal[0]['recommendation']
    class Row(dict):
        @staticmethod
        def to_dict(row):
            return dict(row)
    monkeypatch.setattr(client, '_search_one_page', lambda *a: (
        [Row(recommendation=copy.deepcopy(rec)) for _ in rows], None, total))
    with pytest.raises(rails.RailViolation):
        draft()


def test_complete_scanner_follows_empty_first_page(dismissal, monkeypatch):
    original = client._search_one_page
    calls = []
    def page(query, cid, token):
        calls.append(token)
        return ([], 'next', 1) if token is None else original(query, cid, None)
    monkeypatch.setattr(client, '_search_one_page', page)
    draft()
    assert calls == [None, 'next', None, 'next']


@pytest.mark.parametrize('kind', ['missing', 'foreign', 'extra', 'error', 'transport'])
def test_uncertain_response_consumed_before_saved_reads(dismissal, kind, monkeypatch):
    data, fake = dismissal
    d = draft()
    if kind == 'missing':
        fake.reco_response.results.clear()
    elif kind == 'foreign':
        fake.reco_response.results[0].resource_name = 'customers/999/recommendations/x'
    elif kind == 'extra':
        fake.reco_response.results.append({'resource_name': RN})
    elif kind == 'error':
        fake.reco_response.partial_failure_error.message = 'failure'
    else:
        def broken(self, request):
            fake.reco_calls.append(('dismiss', request))
            raise RuntimeError('transport broke')
        monkeypatch.setattr(type(fake.get_service('RecommendationService')), 'dismiss_recommendation', broken)
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] not in rails._DRAFTS
    assert len(fake.reco_calls) == 1


def test_unchecked_internal_plan_and_validate_only_refuse(dismissal):
    plan = rails.RecommendationActionPlan(CID, 'dismiss', RN)
    with pytest.raises(rails.RailViolation):
        client._dispatch(plan)
    with pytest.raises(rails.RailViolation):
        rails.create_draft('unrelated', {}, plan, {}, lambda: None)
    compiled = rails.compile(rails.DismissRecommendationIntent(CID, RID))
    with pytest.raises(rails.RailViolation, match='validate_only'):
        client._dispatch(compiled.plan, validate_only=True)
    assert not dismissal[1].reco_calls


def test_expiry_avoids_reads(dismissal):
    d = draft()
    rails._DRAFTS[d['draft_id']].created_at -= rails.draft_ttl_seconds() + 1
    dismissal[0]['reads'].clear()
    with pytest.raises(rails.RailViolation, match='expired'):
        rails.apply_draft(d['draft_id'])
    assert not dismissal[0]['reads'] and d['draft_id'] not in rails._DRAFTS


def test_mcp_rejects_number_without_reads(dismissal):
    async def call():
        return await app.mcp.call_tool('dismiss_recommendation', {'recommendation_id': 7})
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError, match='valid string'):
        asyncio.run(call())
    assert not dismissal[0]['reads']


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'read_failure'])
def test_postcheck_disappearance_and_read_failure_unverified(dismissal, monkeypatch, kind):
    original = client._search_one_page
    def page(query, cid, token):
        if dismissal[1].reco_calls:
            if kind == 'missing':
                return [], None, 0
            if kind == 'read_failure':
                raise RuntimeError('read failed')
            rows, _, _ = original(query, cid, token)
            return rows + rows, None, 2
        return original(query, cid, token)
    monkeypatch.setattr(client, '_search_one_page', page)
    d = draft()
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] is True and out['verified'] is False
    assert d['draft_id'] not in rails._DRAFTS and len(dismissal[1].reco_calls) == 1


@pytest.mark.parametrize('change', [lambda p: p.post_checks[0].update(resource_name='wrong'),
    lambda p: p.post_checks[0]['expected'].update(extra=True),
    lambda p: p.post_checks[0]['expected'].update(type_='UNKNOWN'),
    lambda p: p.post_checks.append(copy.deepcopy(p.post_checks[0])),
    lambda p: object.__setattr__(p, 'new_budget_amount_micros', 1),
    lambda p: object.__setattr__(p, 'operations', [])])
def test_invalid_descriptors_refuse_without_provider(dismissal, monkeypatch, change):
    compiled = rails.compile(rails.DismissRecommendationIntent(CID, RID))
    change(compiled.plan)
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider reached'))
    with pytest.raises(rails.RailViolation):
        client._dispatch(compiled.plan)


def test_actual_v25_row_and_known_nonbudget_types(dismissal, monkeypatch):
    from tests.conftest import make_search_response
    row = make_type('GoogleAdsRow')
    row.recommendation.resource_name = RN
    row.recommendation.type_ = 'KEYWORD'
    row.recommendation.dismissed = False
    response = make_search_response([row], total=1)
    monkeypatch.setattr(client, '_search_one_page', lambda *a: (list(response.results), None, 1))
    d = draft()
    assert d['preview']['recommendation']['type_'] == 'KEYWORD'
    assert d['preview']['recommendation']['campaign'] == ''


def test_audited_refusals_and_digest(dismissal, monkeypatch):
    from mcp_google_ads_safe import audit
    events = []
    monkeypatch.setattr(audit, 'log_event', lambda *args: events.append(args))
    d = draft()
    saved = rails._DRAFTS[d['draft_id']]
    assert saved.digest == rails.plan_digest(saved.plan)
    assert events[-1][1] == 'draft' and events[-1][2]['digest'] == saved.digest
    dismissal[0]['recommendation']['dismissed'] = True
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert events[-1][1] == 'refused'
    with pytest.raises(rails.RailViolation):
        draft(recommendation_id=True)
    assert events[-1][1] == 'refused'
