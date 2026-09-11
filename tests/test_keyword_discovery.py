"""Offline discovery contract using real v25 requests/results, never credentials."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from google.ads.googleads.v25.services.types.keyword_plan_idea_service import (
    GenerateKeywordIdeaResponse,
    GenerateKeywordIdeaResult,
    GenerateKeywordIdeasRequest,
)

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(calls=[], pages=0, constructed=0, response=GenerateKeywordIdeaResponse(
        results=[GenerateKeywordIdeaResult(text="air conditioning")], total_size=1))

    class Pager:
        @property
        def pages(self):
            state.pages += 1
            yield state.response
            raise AssertionError("must not request another page")

        def __iter__(self):
            raise AssertionError("must not iterate the auto-pager")

    class Fake:
        def get_type(self, name):
            assert name == 'GenerateKeywordIdeasRequest'
            return GenerateKeywordIdeasRequest()

        def get_service(self, name):
            assert name == 'KeywordPlanIdeaService'
            return self

        def generate_keyword_ideas(self, request):
            state.calls.append(request)
            if isinstance(state.response, Exception):
                raise state.response
            return Pager()

    def construct():
        state.constructed += 1
        return Fake()

    monkeypatch.setattr(client, 'gads', construct)
    monkeypatch.delenv('GOOGLE_ADS_ENABLE_WRITES', raising=False)
    monkeypatch.setattr(rails, 'compile', lambda *a, **k: pytest.fail('write compilation'))
    monkeypatch.setattr(client, '_dispatch', lambda *a, **k: pytest.fail('write dispatch'))
    return state


def call(**kwargs):
    return tools.discover_keywords(kwargs.pop('seed_keywords', ['AC', 'café', 'AC']), **kwargs)


def test_default_request_raw_estimates(provider, monkeypatch):
    monkeypatch.setenv('GOOGLE_ADS_MAX_PAGES', '100')
    result = call()
    req = provider.calls[0]
    assert req.customer_id == CID and list(req.keyword_seed.keywords) == ['AC', 'café', 'AC']
    assert req.page_size == 50 and req.keyword_plan_network.name == 'GOOGLE_SEARCH'
    assert not req.include_adult_keywords and not req.geo_target_constants
    assert req._pb.WhichOneof('seed') == 'keyword_seed'
    assert not req._pb.HasField('language') and not req._pb.HasField('historical_metrics_options')
    assert not req.keyword_annotation and not req._pb.HasField('aggregate_metrics')
    assert provider.pages == 1 and len(provider.calls) == 1
    assert result['request']['all_geographies'] and result['request']['all_languages']
    assert result['returned_count'] == result['total_results_count'] == 1
    assert result['pages_complete'] and result['next_page_token'] is None
    assert 'keyword_idea_metrics' not in result['keyword_ideas'][0]
    assert 'USD' not in str(result) and 'currency code not fetched' in str(result)


def test_configured_and_no_mutation(provider, monkeypatch):
    config = dict(geo_target_constant_ids=[2840, '2124'], language_constant_id='1000',
                  keyword_plan_network='GOOGLE_SEARCH_AND_PARTNERS', include_adult_keywords=True)
    before = copy.deepcopy(config)
    monkeypatch.setattr(settings, 'keyword_research', lambda: config)
    call()
    req = provider.calls[0]
    assert list(req.geo_target_constants) == ['geoTargetConstants/2840', 'geoTargetConstants/2124']
    assert req.language == 'languageConstants/1000'
    assert req.keyword_plan_network.name == 'GOOGLE_SEARCH_AND_PARTNERS' and req.include_adult_keywords
    assert config == before


@pytest.mark.parametrize('seeds', [None, '', '["AC"]', (), {}, [], [''] , [' AC'], ['AC '], ['a\nb'], [1], [True], ['x'] * 21])
def test_invalid_seeds(provider, seeds):
    with pytest.raises(rails.RailViolation):
        call(seed_keywords=seeds)
    assert provider.constructed == 0


@pytest.mark.parametrize('cid', ['', '0', '01', 123, True, '123-456-7890', '１２３', 'null'])
def test_invalid_customer(provider, cid):
    with pytest.raises(rails.RailViolation):
        call(customer_id=cid)
    assert provider.constructed == 0


def test_read_denial(provider, monkeypatch):
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    with pytest.raises(rails.RailViolation):
        call()
    assert provider.constructed == 0


@pytest.mark.parametrize('key,value', [
    ('geo_target_constant_ids', [True]), ('geo_target_constant_ids', [1, '1']),
    ('geo_target_constant_ids', ['01']), ('geo_target_constant_ids', [0]),
    ('geo_target_constant_ids', list(range(1, 12))), ('geo_target_constant_ids', (1,)),
    ('language_constant_id', True), ('language_constant_id', 'languageConstants/1'),
    ('language_constant_id', '01'), ('language_constant_id', 0),
    ('keyword_plan_network', 'UNSPECIFIED'), ('keyword_plan_network', 2),
    ('include_adult_keywords', 'false'), ('include_adult_keywords', 0),
])
def test_invalid_effective_settings(provider, monkeypatch, key, value):
    config = settings.keyword_research()
    config[key] = value
    monkeypatch.setattr(settings, 'keyword_research', lambda: config)
    with pytest.raises(rails.RailViolation):
        call()
    assert provider.constructed == 0


def test_pages_and_zero_metrics(provider):
    provider.response = GenerateKeywordIdeaResponse(results=[GenerateKeywordIdeaResult(
        text='AC', keyword_idea_metrics={'avg_monthly_searches': 0, 'low_top_of_page_bid_micros': 1230000,
                                       'competition': 'LOW'})], total_size=75, next_page_token='raw:next')
    first = call()
    metrics = first['keyword_ideas'][0]['keyword_idea_metrics']
    assert metrics['avg_monthly_searches'] == '0'
    assert metrics['low_top_of_page_bid_micros'] == '1230000' and metrics['competition'] == 2
    assert 'average_cpc_micros' not in metrics
    assert first['returned_count'] == 1 and first['total_results_count'] == 75
    assert not first['pages_complete'] and first['next_page_token'] != 'raw:next'
    provider.response = GenerateKeywordIdeaResponse(results=[GenerateKeywordIdeaResult(text='AC2')], total_size=75)
    last = call(page_token=first['next_page_token'])
    assert provider.calls[-1].page_token == 'raw:next' and last['pages_complete']
    assert provider.pages == 2


def test_empty(provider):
    provider.response = GenerateKeywordIdeaResponse()
    result = call()
    assert result['keyword_ideas'] == [] and result['total_results_count'] == 0
    assert result['pages_complete'] and result['next_page_token'] is None


@pytest.mark.parametrize('token', ['', 'raw', 'x:raw', 'a'*64+':', 7, True])
def test_malformed_token_before_provider(provider, token):
    with pytest.raises(rails.RailViolation, match='token'):
        call(page_token=token)
    assert provider.constructed == 0


@pytest.mark.parametrize('change', ['customer', 'seeds', 'order', 'geos', 'language', 'network', 'adult', 'service'])
def test_bound_token(provider, monkeypatch, change):
    provider.response.next_page_token = 'next'
    provider.response.total_size = 99
    token = call()['next_page_token']
    count = provider.constructed
    kwargs = {}
    config = settings.keyword_research()
    if change == 'customer':
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', CID + ',999')
        kwargs['customer_id'] = '999'
    elif change == 'seeds':
        kwargs['seed_keywords'] = ['different']
    elif change == 'order':
        kwargs['seed_keywords'] = ['AC', 'AC', 'café']
    elif change == 'service':
        token = client._wrap_token('next', 'other service', CID)
    else:
        key, value = {'geos': ('geo_target_constant_ids', [2840]),
                      'language': ('language_constant_id', 1000),
                      'network': ('keyword_plan_network', 'GOOGLE_SEARCH_AND_PARTNERS'),
                      'adult': ('include_adult_keywords', True)}[change]
        config[key] = value
        monkeypatch.setattr(settings, 'keyword_research', lambda: config)
    with pytest.raises(rails.RailViolation) as error:
        call(page_token=token, **kwargs)
    assert error.value.code == 'TOKEN_MISMATCH' and provider.constructed == count


def test_repeated_token(provider):
    provider.response.next_page_token = 'next'
    provider.response.total_size = 99
    token = call()['next_page_token']
    with pytest.raises(rails.RailViolation, match='repeated'):
        call(page_token=token)


@pytest.mark.parametrize('response', [RuntimeError('provider unavailable'),
    SimpleNamespace(results=[], total_size=-1, next_page_token=''),
    SimpleNamespace(results=[], total_size=1, next_page_token=''),
    SimpleNamespace(results=[], total_size=0, next_page_token=7),
    SimpleNamespace(results=[], total_size=True, next_page_token=''),
    GenerateKeywordIdeaResponse(results=[GenerateKeywordIdeaResult(text='x')]*51, total_size=51),
    SimpleNamespace(results=[object()], total_size=1, next_page_token=''),
])
def test_bad_response_never_success(provider, response):
    provider.response = response
    with pytest.raises(Exception):
        call()


@pytest.mark.parametrize('arguments', [{'seed_keywords': '["AC"]'}, {'seed_keywords': [1]},
    {'seed_keywords': ['AC'], 'customer_id': 123}, {'seed_keywords': ['AC'], 'customer_id': 'null'},
    {'seed_keywords': ['AC'], 'page_token': 'null'},
    {'seed_keywords': ['AC'], 'customer_id': ' null '}, {'seed_keywords': ['AC'], 'page_token': 1}])
def test_mcp_rejects_coercion(provider, arguments):
    with pytest.raises(Exception):
        asyncio.run(app.mcp.call_tool('discover_keywords', arguments))
    assert provider.constructed == 0


def test_mcp_success(provider):
    result = asyncio.run(app.mcp.call_tool('discover_keywords', {'seed_keywords': ['AC']}))
    assert 'air conditioning' in str(result) and provider.pages == 1


def test_inconsistent_first_page_total(provider):
    provider.response.total_size = 2
    with pytest.raises(rails.RailViolation, match="malformed"):
        call()


def test_research_does_not_apply_write_content_rules(provider, monkeypatch):
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ['AC'])
    call(seed_keywords=['AC'])
    assert provider.calls


@pytest.mark.parametrize("seeds", ['["AC"]', [1], [" AC"], []])
def test_real_protocol_refuses_invalid_seeds(provider, seeds):
    from tests.test_protocol_errors import boundary, error_text
    assert error_text(boundary(app.mcp, "discover_keywords", {"seed_keywords": seeds}))
    assert provider.constructed == 0


def test_real_protocol_success(provider):
    from tests.test_protocol_errors import boundary
    result = boundary(app.mcp, "discover_keywords", {"seed_keywords": ["AC"]})
    assert not result.is_error and "air conditioning" in str(result)
    assert provider.pages == 1
