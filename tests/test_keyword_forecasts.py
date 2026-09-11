"""Synthetic generated v25 messages and fake services only; no credentials or transport."""
import copy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from google.ads.googleads.v25.services.types.keyword_plan_idea_service import (
    GenerateKeywordForecastMetricsRequest,
    GenerateKeywordForecastMetricsResponse,
)

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.test_protocol_errors import boundary, error_text

ARGS = dict(keyword_texts=['AC', 'café'], match_type='EXACT', forecast_start_date='2028-03-01',
            forecast_end_date='2028-03-07', max_cpc_bid_micros=1230000)


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(calls=[], reads=[], constructed=0,
        rows=[{'customer': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'}}],
        config=dict(geo_target_constant_ids=[2840], language_constant_id='1000',
                    keyword_plan_network='GOOGLE_SEARCH_AND_PARTNERS', include_adult_keywords=True),
        response=GenerateKeywordForecastMetricsResponse(campaign_forecast_metrics={'clicks': 0}))

    class Fake:
        def get_type(self, name):
            assert name == 'GenerateKeywordForecastMetricsRequest'
            return GenerateKeywordForecastMetricsRequest()

        def get_service(self, name):
            assert name == 'KeywordPlanIdeaService'
            return self

        def generate_keyword_forecast_metrics(self, request, retry):
            assert retry is None
            state.calls.append(request)
            if isinstance(state.response, Exception):
                raise state.response
            return state.response

    def construct():
        state.constructed += 1
        return Fake()

    def read(query, cid):
        state.reads.append((query, cid))
        return state.rows

    monkeypatch.setattr(client, 'gads', construct)
    monkeypatch.setattr(client, 'gaql_all', read)
    monkeypatch.setattr(client, '_forecast_today', lambda zone: date(2028, 2, 29))
    monkeypatch.setattr(settings, 'keyword_research', lambda: state.config)
    monkeypatch.setattr(rails, 'compile', lambda *a, **k: pytest.fail('write compilation'))
    monkeypatch.setattr(client, '_dispatch', lambda *a, **k: pytest.fail('mutation'))
    monkeypatch.delenv('GOOGLE_ADS_ENABLE_WRITES', raising=False)
    return state


def call(**kwargs):
    return tools.get_keyword_forecasts(**dict(ARGS, **kwargs))


def test_request_and_optional_metrics(provider):
    before = copy.deepcopy(provider.config)
    result = call()
    req = provider.calls[0]
    assert len(provider.calls) == len(provider.reads) == 1
    assert provider.reads == [('SELECT customer.id, customer.currency_code, customer.time_zone FROM customer', CID)]
    assert req.customer_id == CID and not req._pb.HasField('currency_code')
    assert req.forecast_period.start_date == ARGS['forecast_start_date']
    assert req.forecast_period.end_date == ARGS['forecast_end_date']
    assert list(req.campaign.language_constants) == ['languageConstants/1000']
    assert list(req.campaign.geo_target_constants) == ['geoTargetConstants/2840']
    assert len(req.campaign.ad_groups) == 1
    assert [(k.text, k.match_type.name) for k in req.campaign.ad_groups[0].keywords] == [('AC', 'EXACT'), ('café', 'EXACT')]
    bid = req.campaign.bidding_strategy
    assert bid._pb.WhichOneof('bidding_strategy') == 'manual_cpc_bidding_strategy'
    assert bid.manual_cpc_bidding_strategy.max_cpc_bid_micros == 1230000
    assert not bid.manual_cpc_bidding_strategy._pb.HasField('daily_budget_micros')
    assert result['campaign_forecast_metrics'] == {'clicks': 0.0}
    assert result['metric_availability']['clicks'] == 'available'
    assert result['metric_availability']['cost_micros'] == 'unavailable'
    assert result['request']['currency_code'] == 'USD'
    assert result['request']['time_zone'] == 'America/New_York'
    assert result['request']['discovery_settings_not_applied'] == {
        'keyword_plan_network': 'GOOGLE_SEARCH_AND_PARTNERS', 'include_adult_keywords': True}
    assert provider.config == before


@pytest.mark.parametrize('match_type', ['EXACT', 'PHRASE', 'BROAD'])
def test_budget_and_match(provider, match_type):
    call(match_type=match_type, daily_budget_micros=2**63-1)
    req = provider.calls[0]
    assert req.campaign.bidding_strategy.manual_cpc_bidding_strategy.daily_budget_micros == 2**63-1
    assert req.campaign.ad_groups[0].keywords[0].match_type.name == match_type


@pytest.mark.parametrize('metrics', [None, {}, {'cost_micros': 0, 'average_cpc_micros': 0}])
def test_absence_and_zero(provider, metrics):
    provider.response = GenerateKeywordForecastMetricsResponse(
        **({} if metrics is None else {'campaign_forecast_metrics': metrics}))
    result = call()
    if metrics is None:
        assert 'campaign_forecast_metrics' not in result
        assert result['metric_availability'] == 'unavailable'
    else:
        assert result['campaign_forecast_metrics'] == {k: str(v) for k, v in metrics.items()}


@pytest.mark.parametrize('key,value', [
    ('keyword_texts', v) for v in (None, (), '["AC"]', [], [1], [True], [''], [' AC'], ['AC '],
                                   ['a\nb'], ['x'*81], [' '.join(['a']*11)], ['AC','AC'], ['a']*21)
] + [('match_type', v) for v in ('exact', '', True, 1)]
  + [(key, v) for key in ('max_cpc_bid_micros','daily_budget_micros') for v in (True, 1.1, '1', 0, -1, 2**63)]
  + [('customer_id', v) for v in ('', '01', '0', 1, True, 'null', '１２３', '123-456')]
  + [(key, v) for key in ('forecast_start_date','forecast_end_date') for v in (None, 1, '20280301', '2028-3-01', '2028-02-30')]
  + [('forecast_end_date', '2028-02-28')])
def test_invalid_input_before_read(provider, key, value):
    with pytest.raises(rails.RailViolation):
        call(**{key:value})
    assert not provider.calls and not provider.reads and provider.constructed == 0


@pytest.mark.parametrize('key,value', [('geo_target_constant_ids', v) for v in
    ([], [True], [0], ['01'], [1,'1'], list(range(1,12)), (1,))]
    + [('language_constant_id', v) for v in (None, True, '', '01', 0, 'languageConstants/1')])
def test_invalid_settings(provider, key, value):
    provider.config[key] = value
    with pytest.raises(rails.RailViolation):
        call()
    assert not provider.reads and not provider.calls


@pytest.mark.parametrize('rows', [[], [{}, {}], [{}], [{'customer': {}}],
    [{'customer': {'id': '9', 'currency_code': 'USD', 'time_zone': 'UTC'}}],
    [{'customer': {'id': CID, 'currency_code': 'usd', 'time_zone': 'UTC'}}],
    [{'customer': {'id': CID, 'currency_code': 'USD', 'time_zone': 'Mars/Unknown'}}]])
def test_invalid_account(provider, rows):
    provider.rows = rows
    with pytest.raises(rails.RailViolation):
        call()
    assert not provider.calls and provider.constructed == 0


def test_allowlist_and_selected_account(provider, monkeypatch):
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '9')
    monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '9')
    with pytest.raises(rails.RailViolation):
        call()
    assert not provider.reads
    provider.rows[0]['customer']['id'] = '9'
    assert call(customer_id='9')['request']['customer_id'] == '9'
    assert provider.reads[0][1] == '9' and provider.calls[0].customer_id == '9'


@pytest.mark.parametrize('start,end,ok', [('2028-02-29','2028-03-01',False),
    ('2028-03-01','2029-02-28',True), ('2028-03-01','2029-03-01',False)])
def test_leap_boundary(provider, start, end, ok):
    if ok:
        call(forecast_start_date=start, forecast_end_date=end)
    else:
        with pytest.raises(rails.RailViolation):
            call(forecast_start_date=start, forecast_end_date=end)
    assert bool(provider.calls) == ok


def test_account_timezone(provider, monkeypatch):
    instant = datetime(2028, 3, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(client, '_forecast_today', lambda zone: instant.astimezone(zone).date())
    assert call()['request']['account_local_today'] == '2028-02-29'
    provider.rows[0]['customer']['time_zone'] = 'Asia/Tokyo'
    with pytest.raises(rails.RailViolation):
        call()
    assert len(provider.calls) == 1


@pytest.mark.parametrize('response', [None, {}, SimpleNamespace(campaign_forecast_metrics={}), RuntimeError('fake provider failure')])
def test_failure(provider, response):
    provider.response = response
    with pytest.raises((rails.RailViolation, RuntimeError)):
        call()
    assert len(provider.calls) == 1


@pytest.mark.parametrize('key,value', [('keyword_texts','["AC"]'),('keyword_texts',[1]),
    ('max_cpc_bid_micros','123'),('max_cpc_bid_micros',True),('daily_budget_micros','null'),
    ('customer_id','null'),('customer_id',123),('match_type',1),('forecast_start_date',20280301)])
def test_protocol_raw_types(provider, key, value):
    assert error_text(boundary(app.mcp, 'get_keyword_forecasts', dict(ARGS, **{key:value})))
    assert not provider.reads and not provider.calls


def test_protocol_valid_and_blocklist_irrelevant(provider, monkeypatch):
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ['AC'])
    result = boundary(app.mcp, 'get_keyword_forecasts', ARGS)
    assert not result.is_error and 'campaign_forecast_metrics' in str(result)
    assert len(provider.calls) == 1
