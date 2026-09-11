"""Offline policy read contract using real v25 rows and the existing page engine."""
from unittest.mock import Mock

import pytest

from mcp_google_ads_safe import app, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_row, make_search_response
from tests.test_protocol_errors import boundary, error_text


@pytest.mark.parametrize("total", [None, "missing", 19])
@pytest.mark.parametrize("explicit", [False, True])
def test_query_and_envelope(monkeypatch, total, explicit):
    envelope = dict(rows=[{'raw': 'unchanged'}], returned_count=1, total_results_count=None,
                    pages_complete=False, query_limited=False, next_page_token='next')
    cid = '9876543210' if explicit else CID
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', CID + ',9876543210')
    if total == 'missing':
        envelope.pop('total_results_count')
    else:
        envelope['total_results_count'] = total
    gaql = Mock(return_value=envelope)
    monkeypatch.setattr(client, 'gaql', gaql)
    result = tools.get_policy_issues(customer_id=cid if explicit else None)
    assert {k: result[k] for k in envelope} == envelope
    assert result['rows'] is envelope['rows']
    assert result['source']['customer_id'] == cid
    assert result['source']['date_range'] == 'current snapshot'
    query, actual_cid, token = gaql.call_args.args
    assert actual_cid == cid and token is None
    selected, rest = query.removeprefix('SELECT ').split(' FROM ')
    assert set(selected.split(', ')) == set('campaign.id campaign.name campaign.status ad_group.id ad_group.name ad_group.status ad_group_ad.resource_name ad_group_ad.status ad_group_ad.ad.id ad_group_ad.ad.name ad_group_ad.policy_summary.approval_status ad_group_ad.policy_summary.review_status ad_group_ad.policy_summary.policy_topic_entries'.split())
    assert rest == "ad_group_ad WHERE ad_group_ad.policy_summary.approval_status != 'APPROVED' AND ad_group_ad.status != 'REMOVED' AND campaign.status != 'REMOVED' AND ad_group.status != 'REMOVED' ORDER BY campaign.id, ad_group.id, ad_group_ad.ad.id"
    assert 'LIMIT' not in query and 'metrics.' not in query and 'segments.date' not in query
    gaql.assert_called_once()


@pytest.mark.parametrize('cid', ['', '0', '0123', ' 123', '123 ', '123-456', '１２３', 123, True])
def test_bad_customer_before_client(monkeypatch, cid):
    provider = Mock()
    monkeypatch.setattr(client, 'get_policy_issues', provider)
    with pytest.raises(rails.RailViolation):
        tools.get_policy_issues(customer_id=cid)
    provider.assert_not_called()


def test_read_gate_and_missing_default(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(client, 'get_policy_issues', provider)
    with pytest.raises(rails.RailViolation):
        tools.get_policy_issues(customer_id='9999999999')
    monkeypatch.delenv('GOOGLE_ADS_CUSTOMER_ID')
    with pytest.raises(rails.RailViolation):
        tools.get_policy_issues()
    provider.assert_not_called()


@pytest.mark.parametrize('token', ['', 'raw', 'null', 1, True, 'a'*64 + ':', 'a'*63 + ':raw', 'A'*64 + ':raw', 'a'*64 + ':raw'])
def test_invalid_or_mismatched_token_before_provider(monkeypatch, token):
    provider = Mock()
    monkeypatch.setattr(client, '_search_one_page', provider)
    with pytest.raises(rails.RailViolation):
        tools.get_policy_issues(page_token=token)
    provider.assert_not_called()


@pytest.mark.parametrize('field', ['customer_id', 'page_token'])
@pytest.mark.parametrize('value', ['null', ' null ', 123, False, [], {}])
def test_actual_mcp_raw_input(monkeypatch, field, value):
    provider = Mock()
    monkeypatch.setattr(client, '_search_one_page', provider)
    assert error_text(boundary(app.mcp, 'get_policy_issues', {field: value}))
    provider.assert_not_called()


def test_empty_short_page_continuation_write_disabled_and_account_binding(fake_gads, monkeypatch):
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    monkeypatch.setenv('GOOGLE_ADS_MAX_PAGES', '1')
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', CID + ',9876543210')
    fake_gads.search_responses[(CID, '')] = make_search_response([], next_token='next:raw', total=1)
    result = tools.get_policy_issues()
    assert result['rows'] == [] and result['returned_count'] == 0
    assert result['total_results_count'] == 1 and result['pages_complete'] is False
    token = result['next_page_token']
    with pytest.raises(rails.RailViolation):
        tools.get_policy_issues(customer_id='9876543210', page_token=token)
    assert len(fake_gads.search_requests) == 1
    row = make_row(**{'campaign.id': 7, 'campaign.status': 'PAUSED', 'ad_group.id': 8,
                     'ad_group.status': 'ENABLED', 'ad_group_ad.ad.id': 9,
                     'ad_group_ad.policy_summary.approval_status': 'APPROVED_LIMITED',
                     'ad_group_ad.policy_summary.policy_topic_entries': [
                         {'topic': 'SYNTHETIC', 'type_': 'LIMITED',
                          'evidences': [{'text_list': {'texts': ['synthetic detail']}}]}]})
    fake_gads.search_responses[(CID, 'next:raw')] = make_search_response([row], total=1)
    final = tools.get_policy_issues(page_token=token)
    assert final['rows'] == [type(row).to_dict(row)]
    assert final['pages_complete'] is True and final['next_page_token'] is None
    assert final['returned_count'] == final['total_results_count'] == 1
    assert not final['query_limited']
    assert not fake_gads.mutate_calls and not fake_gads.reco_calls


def test_provider_failure_propagates(fake_gads, monkeypatch):
    error = RuntimeError('synthetic provider error')
    monkeypatch.setattr(fake_gads._svc, 'search', Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        tools.get_policy_issues()
    assert caught.value is error
