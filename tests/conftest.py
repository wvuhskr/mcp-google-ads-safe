"""Shared offline test fixtures.

Default provider factories are denied; explicit fakes use local v25 messages.
Python-level socket/DNS denial is defense in depth, not an OS network sandbox:
C-backed gRPC can bypass Python socket hooks, so denying real client construction
is the primary barrier. Pure money helpers remain real and run through compile.
"""
import socket

import pytest

# google.ads types are imported here on purpose: conftest lives under tests/, which the AST
# import-guard test does NOT scan (it scans only the package). The B3 fake's get_type returns
# REAL v25 proto-plus messages via this credential-less stand-in, so client.py builds real
# requests and we parse real responses — no network anywhere.
from google.ads.googleads.client import GoogleAdsClient as _RealGoogleAdsClient

from mcp_google_ads_safe import audit, client, settings

# A test customer id used as the default account throughout; the digits match the customer
# segment of DEFAULT_BUDGET_INFO's resource name.
TEST_CUSTOMER_ID = "1234567890"
OFFLINE_GADS_FACTORY = client.gads
_NETWORK_GUARD = pytest.MonkeyPatch()


def _deny_network(*args, **kwargs):
    raise AssertionError("offline test process forbids network and DNS")


def pytest_sessionstart(session):
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'create_connection'):
        _NETWORK_GUARD.setattr(socket, name, _deny_network)
    for name in ('connect', 'connect_ex', 'sendto'):
        _NETWORK_GUARD.setattr(socket.socket, name, _deny_network)


def pytest_sessionfinish(session, exitstatus):
    _NETWORK_GUARD.undo()


@pytest.fixture(autouse=True)
def _deny_real_provider(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("real provider client forbidden; use an explicit offline fake")
    monkeypatch.setattr(client, 'gads', refuse)
    monkeypatch.setattr(client.GoogleAdsClient, 'load_from_dict', staticmethod(refuse))
    monkeypatch.setattr(client.GoogleAdsClient, 'load_from_storage', staticmethod(refuse))
    monkeypatch.setenv('GOOGLE_ADS_YAML', str(tmp_path / 'no-credentials.yaml'))
    client._CLIENT_CACHE.clear()
    yield
    client._CLIENT_CACHE.clear()



@pytest.fixture(autouse=True)
def _google_env(monkeypatch):
    """Scrub any real credentials / config / allowlists a developer's machine may export,
    then set the suite's baseline: writes ON, a single default account. Tests that exercise
    a gate/allowlist explicitly override these."""
    for var in [
        "GOOGLE_ADS_AUDIT_PATH", "GOOGLE_ADS_ADVERTISER_CONFIG",
        "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        "GOOGLE_ADS_READ_CUSTOMER_IDS", "GOOGLE_ADS_WRITE_CUSTOMER_IDS",
        "GOOGLE_ADS_MAX_DAILY_BUDGET", "GOOGLE_ADS_MAX_CPC", "GOOGLE_ADS_MAX_TARGET_CPA",
        "GOOGLE_ADS_DRAFT_TTL_SECONDS", "GOOGLE_ADS_ALLOW_REMOVE_ENTITY",
        "GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT", "GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT",
        "GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT",
        "GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "true")
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", TEST_CUSTOMER_ID)


@pytest.fixture(autouse=True)
def _sandbox_audit_path(monkeypatch, tmp_path):
    """Belt-and-suspenders on top of the env scrub: monkeypatch audit.AUDIT_PATH itself to a
    per-test tmp path, so no test can ever create a real ~/.mcp-google-ads-safe/audit.jsonl
    in a developer's home dir. Tests needing a specific path override it in their body."""
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))


@pytest.fixture(autouse=True)
def clean_advertiser_settings(monkeypatch, tmp_path):
    """Point every test at a settings file that does not exist (-> library defaults),
    regardless of any real advertiser.yaml on the machine. Tests needing non-default
    settings set their own env var/path and call settings._reset()."""
    monkeypatch.setenv(settings.ENV_VAR, str(tmp_path / "no-such-advertiser.yaml"))
    settings._reset()
    yield
    settings._reset()


DEFAULT_BUDGET_INFO = {
    "budget_resource_name": f"customers/{TEST_CUSTOMER_ID}/campaignBudgets/555",
    "amount": "50",
    "explicitly_shared": False,
    "reference_count": 1,
    "period": "DAILY",
    "total_amount_micros": None,
    "aligned_bidding_strategy_id": None,
}


class FakeClient:
    """In-memory stand-in for the client API surface. Mutate `.budget_info` /
    `.dispatch_result` / `.dispatch_error` to drive a test; inspect `.dispatch_calls`."""

    def __init__(self):
        self.budget_info = dict(DEFAULT_BUDGET_INFO)
        self.strategy = {"type": "MANUAL_CPC", "portfolio_resource_name": None,
                         "owner_customer_id": None}
        # Newly invented repeated-42 campaign sentinel for offline tests in this phase.
        self.status_info = {"exists": True,
                            "resource_name": f"customers/{TEST_CUSTOMER_ID}/campaigns/424242424",
                            "status": "ENABLED"}
        self.dispatch_calls = []
        # request_id is None on the SUCCESS path to match production: real _mutate_result
        # returns request_id=None (the provider request_id lives in trailing metadata, captured
        # only via a logging interceptor — a documented deferral). The fake must not assert a
        # guarantee production does not provide.
        self.dispatch_result = {
            "results": [{"resource_name": DEFAULT_BUDGET_INFO["budget_resource_name"]}],
            "request_id": None,
        }
        self.dispatch_error = None

    def campaign_budget(self, customer_id, campaign_id):
        return dict(self.budget_info)

    def effective_strategy(self, customer_id, campaign_id):
        return dict(self.strategy)

    def entity_status(self, customer_id, entity_type, entity_id):
        return dict(self.status_info)

    def _dispatch(self, plan):
        self.dispatch_calls.append(plan)
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return dict(self.dispatch_result)


@pytest.fixture
def fake_client(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(client, "campaign_budget", fc.campaign_budget)
    monkeypatch.setattr(client, "effective_strategy", fc.effective_strategy)
    monkeypatch.setattr(client, "entity_status", fc.entity_status)
    monkeypatch.setattr(client, "_dispatch", fc._dispatch)
    monkeypatch.setattr(client, "gaql", lambda q, cid, page_token=None: {"results": [], "next_page_token": None})
    monkeypatch.setattr(client, "gaql_all", lambda q, cid: [])
    return fc


# --- B3: offline fake GoogleAdsClient (exercises client.py's REAL request-building path) --

def _type_factory():
    tf = _RealGoogleAdsClient.__new__(_RealGoogleAdsClient)  # bypass __init__ (no creds)
    tf.version = "v25"
    tf.use_proto_plus = True
    return tf


_TF = _type_factory()


def make_type(name):
    return _TF.get_type(name)


def make_row(**paths):
    """Build a GoogleAdsRow. Keys are dotted proto paths, e.g.
    make_row(**{"campaign_budget.amount_micros": 50_000_000})."""
    row = make_type("GoogleAdsRow")
    for path, value in paths.items():
        obj = row
        parts = path.split(".")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], value)
    return row


def make_search_response(rows, next_token="", total=None):
    """A SearchGoogleAdsResponse. total defaults to len(rows) so a well-formed page
    reconciles by default; pass an explicit total to simulate a missing page."""
    resp = make_type("SearchGoogleAdsResponse")
    for r in rows:
        resp.results.append(r)
    resp.next_page_token = next_token
    resp.total_results_count = len(rows) if total is None else total
    return resp


class _FakeSearchPager:
    def __init__(self, response):
        self._response = response

    @property
    def pages(self):
        yield self._response


class _FakeService:
    """Stands in for BOTH GoogleAdsService and RecommendationService — records every call."""

    def __init__(self, owner):
        self.owner = owner

    def search(self, request):
        self.owner.search_requests.append(request)
        key = (request.customer_id, request.page_token or "")
        if key not in self.owner.search_responses:
            raise AssertionError(
                f"fake: no search response for {key}; known={list(self.owner.search_responses)}")
        return _FakeSearchPager(self.owner.search_responses[key])

    def mutate(self, request=None):
        # Match the REAL v25 GoogleAdsService.mutate() convenience signature: it takes a
        # `request` (MutateGoogleAdsRequest) — partial_failure / validate_only are FIELDS on
        # that message, NOT top-level kwargs. The old fake accepted them as kwargs, which is
        # exactly how the kwargs-form bug in _dispatch_entity slipped past the suite.
        self.owner.mutate_calls.append({
            "customer_id": request.customer_id,
            "operations": list(request.mutate_operations),
            "partial_failure": request.partial_failure,
            "validate_only": request.validate_only,
        })
        if self.owner.mutate_error is not None:
            raise self.owner.mutate_error
        return self.owner.mutate_response

    def apply_recommendation(self, request=None):
        self.owner.reco_calls.append(("apply", request))
        return self.owner.reco_response

    def dismiss_recommendation(self, request=None):
        self.owner.reco_calls.append(("dismiss", request))
        return self.owner.reco_response


class FakeGoogleAdsClient:
    """Drives client.py offline. Populate `.search_responses[(customer_id, page_token)]`
    with make_search_response(...); set `.mutate_response`/`.mutate_error`/`.reco_response`
    to steer _dispatch."""

    def __init__(self, login_customer_id="1234567890"):
        self.login_customer_id = login_customer_id
        self.search_responses = {}
        self.search_requests = []
        self.mutate_calls = []
        self.mutate_response = None
        self.mutate_error = None
        self.reco_calls = []
        self.reco_response = None
        self._svc = _FakeService(self)

    def get_service(self, name):
        return self._svc

    def get_type(self, name):
        return make_type(name)


@pytest.fixture
def fake_gads(monkeypatch):
    fc = FakeGoogleAdsClient()
    monkeypatch.setattr(client, "gads", lambda profile=None: fc)
    return fc
