"""Consolidated review regressions, entirely offline at both client layers."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from google.api_core import exceptions, grpc_helpers

from mcp_google_ads_safe import audit, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import _FakeSearchPager, make_row, make_search_response, make_type


@pytest.fixture(params=['fake_client', 'fake_gads'])
def shared(request, monkeypatch):
    fake = request.getfixturevalue(request.param)
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT', 'true')
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT', 'true')
    budget = f'customers/{CID}/campaignBudgets/555'
    def campaign(i):
        return {'id': str(i), 'resource_name': f'customers/{CID}/campaigns/{i}',
                'name': f'Campaign {i}', 'status': 'PAUSED', 'campaign_budget': budget}
    s = SimpleNamespace(count=2, attachments=[campaign(42), campaign(43)], incomplete=False,
                        fake=fake, layer=request.param)
    if request.param == 'fake_client':
        fake.budget_info.update(explicitly_shared=True, reference_count=2)
        monkeypatch.setattr(client, 'campaign_budget', lambda *a: {
            **fake.budget_info, 'reference_count': s.count})
        monkeypatch.setattr(client, 'update_state', lambda *a: campaign(42))
        fake.strategy.update(type='TARGET_CPA', portfolio_resource_name=f'customers/{CID}/biddingStrategies/77',
                             owner_customer_id=CID, strategy_id='77')
        monkeypatch.setattr(client, 'portfolio_state', lambda *a: {
            'strategy': {'resource_name': f'customers/{CID}/biddingStrategies/77',
                         'type': 'TARGET_CPA', 'non_removed_campaign_count': '1'},
            'attachments': [{'customer_id': CID, **campaign(42)}]})
        def read(query, cid):
            assert 'campaign_budget' in query and "campaign.status != 'REMOVED'" in query
            if s.incomplete:
                raise rails.RailViolation('incomplete', code='SCAN_INCOMPLETE')
            return [{'campaign': c} for c in copy.deepcopy(s.attachments)]
        monkeypatch.setattr(client, 'gaql_all', read)
    else:
        fake.mutate_response = make_type('MutateGoogleAdsResponse')
        def row(c):
            fields = {f'campaign.{key}': (3 if value == 'PAUSED' else value)
                      for key, value in c.items()}
            fields['campaign.id'] = int(c['id'])
            return make_row(**fields)
        def search(request):
            req = request
            fake.search_requests.append(req)
            if 'FROM bidding_strategy' in req.query:
                rows = [make_row(**{'bidding_strategy.resource_name': f'customers/{CID}/biddingStrategies/77',
                                    'bidding_strategy.type': 'TARGET_CPA',
                                    'bidding_strategy.non_removed_campaign_count': 1})]
            elif 'WHERE campaign.status' in req.query and 'campaign_budget' in req.query:
                rows = [row(c) for c in s.attachments]
                return _FakeSearchPager(make_search_response(rows, total=len(rows) + int(s.incomplete)))
            else:
                r = row(campaign(42))
                r.campaign.bidding_strategy_type = 'TARGET_CPA'
                r.campaign.bidding_strategy = f'customers/{CID}/biddingStrategies/77'
                r.accessible_bidding_strategy.id = 77
                r.accessible_bidding_strategy.owner_customer_id = int(CID)
                r.campaign_budget.resource_name = budget
                r.campaign_budget.amount_micros = 50000000
                r.campaign_budget.explicitly_shared = True
                r.campaign_budget.reference_count = s.count
                r.campaign_budget.period = 2
                rows = [r]
            return _FakeSearchPager(make_search_response(rows))
        monkeypatch.setattr(fake._svc, 'search', search)
    return s


@pytest.mark.parametrize('extra', [{}, {'name': 'After'}, {'name': 'After', 'target_cpa': 20}])
def test_shared_budget_preview_and_atomic_apply(shared, extra):
    out = tools.update_campaign('42', 75, **extra)
    key = 'budget_affected_campaigns' if extra else 'affected_campaigns'
    assert {c['id'] for c in out['preview'][key]} == {'42', '43'}
    if 'target_cpa' in extra:
        assert [c['id'] for c in out['preview']['affected_campaigns']] == ['42']
    assert rails.apply_draft(out['draft_id'])['applied'] is True
    calls = shared.fake.dispatch_calls if shared.layer == 'fake_client' else shared.fake.mutate_calls
    assert len(calls) == 1
    if shared.layer == 'fake_gads':
        assert calls[0]['partial_failure'] is False
        assert len(calls[0]['operations']) == 1 + bool(extra) + ('target_cpa' in extra)
        assert all(r.search_settings.return_total_results_count for r in shared.fake.search_requests)


@pytest.mark.parametrize('extra', [{}, {'name': 'After', 'target_cpa': 20}])
@pytest.mark.parametrize('change', ['count', 'added', 'replaced', 'incomplete', 'unknown_status', 'duplicate', 'missing_requested'])
def test_shared_budget_scope_drift_blocks_dispatch(shared, change, extra):
    out = tools.update_campaign('42', 75, **extra)
    if change == 'count':
        shared.count = 3
    elif change == 'added':
        shared.count = 3
        shared.attachments.append({**shared.attachments[1], 'id': '44',
                                   'resource_name': f'customers/{CID}/campaigns/44'})
    elif change == 'replaced':
        shared.attachments[1].update(id='44', resource_name=f'customers/{CID}/campaigns/44')
    elif change == 'incomplete':
        shared.incomplete = True
    elif change == 'unknown_status':
        shared.attachments[1]['status'] = 0 if shared.layer == 'fake_gads' else 'UNKNOWN'
    elif change == 'duplicate':
        shared.attachments[1] = copy.deepcopy(shared.attachments[0])
    else:
        shared.attachments[0].update(id='44', resource_name=f'customers/{CID}/campaigns/44')
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(out['draft_id'])
    assert out['draft_id'] in rails._DRAFTS
    assert not (shared.fake.dispatch_calls if shared.layer == 'fake_client' else shared.fake.mutate_calls)


@pytest.mark.parametrize('incomplete', [False, True])
def test_shared_budget_incomplete_draft_refused(shared, incomplete):
    shared.incomplete = incomplete
    shared.count = 3
    with pytest.raises(rails.RailViolation):
        tools.update_campaign('42', 75, name='After')


class RemappedFailure(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return 'offline connection lost after request'

    def trailing_metadata(self):
        return ()


def mapped_error(kind):
    if kind != 'remapped':
        return kind('offline ambiguous mutation', errors=[{'reason': 'test-detail'}])
    def fail():
        raise RemappedFailure()
    try:
        grpc_helpers.wrap_errors(fail)()
    except exceptions.ServiceUnavailable as exc:
        assert isinstance(exc.__cause__, RemappedFailure)
        return exc
    raise AssertionError('expected real grpc remapping')


@pytest.mark.parametrize('layer', ['fake_client', 'fake_gads'])
@pytest.mark.parametrize('kind', [exceptions.ServiceUnavailable, exceptions.DeadlineExceeded,
                                  exceptions.InternalServerError, 'remapped'])
def test_real_mapped_transport_is_unknown_consumed_once(request, layer, kind):
    fake = request.getfixturevalue(layer)
    error = mapped_error(kind)
    if layer == 'fake_client':
        fake.dispatch_error = error
    else:
        fake.mutate_error = error
    plan = rails.EntityMutationPlan(CID, [rails.MutationOp('CampaignBudgetService', {
        'update': {'resource_name': f'customers/{CID}/campaignBudgets/555', 'amount_micros': 75000000}},
        ['amount_micros'])], True)
    draft = rails.create_draft('update_campaign', {}, plan, {})
    with pytest.raises(rails.UnknownWriteOutcome) as caught:
        rails.apply_draft(draft['draft_id'])
    assert caught.value.__cause__ is error
    assert caught.value.cause is error
    assert caught.value.failure['errors'] == list(error.errors)
    assert caught.value.failure['code'] == error.code
    assert 'verify account state' in str(caught.value)
    assert draft['draft_id'] not in rails._DRAFTS
    assert json.loads(Path(audit.AUDIT_PATH).read_text().splitlines()[-1])['phase'] == 'unknown'
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(draft['draft_id'])
    assert len(fake.dispatch_calls if layer == 'fake_client' else fake.mutate_calls) == 1


def test_shared_budget_complete_pagination_and_count_mismatch(fake_gads):
    budget = f'customers/{CID}/campaignBudgets/555'
    def row(i):
        return make_row(**{'campaign.id': i, 'campaign.resource_name': f'customers/{CID}/campaigns/{i}',
                           'campaign.name': f'Campaign {i}', 'campaign.status': 'PAUSED',
                           'campaign.campaign_budget': budget})
    fake_gads.search_responses[(CID, '')] = make_search_response([row(43)], next_token='page2', total=2)
    fake_gads.search_responses[(CID, 'page2')] = make_search_response([row(42)], total=2)
    scope = client.shared_budget_attachments(CID, '42', budget, 2)
    assert [c['id'] for c in scope] == ['42', '43']
    assert [r.page_token for r in fake_gads.search_requests] == ['', 'page2']
    assert all(r.search_settings.return_total_results_count for r in fake_gads.search_requests)
    fake_gads.search_responses[(CID, '')] = make_search_response([row(43)], next_token='page2', total=3)
    with pytest.raises(rails.RailViolation) as caught:
        client.shared_budget_attachments(CID, '42', budget, 2)
    assert caught.value.code == 'SCAN_INCOMPLETE'
