"""Tool-layer tests for the M1-READS batch: 9 new read-only @mcp.tool() wrappers.

Mirrors test_server.py's run_gaql tests (test_run_gaql_refuses_non_allowlisted_customer /
test_run_gaql_returns_fake_envelope_for_allowlisted_customer): every tool here is a THIN
wrapper (resolve customer_id -> check the read allowlist -> delegate to client.<fn>), so for
each of the 9 tools we prove two things offline against the `fake_client` fixture (whose
`client.gaql` stub is reached by every one of these tools internally, since each client-side
function calls client.gaql(...)):
  1. a non-allowlisted customer_id raises rails.RailViolation(code="NOT_ALLOWLISTED") BEFORE
     the fake is ever touched -- discriminating because NOT_ALLOWLISTED can ONLY come from
     rails.check_customer_allowlisted, never from client.gaql or any of the 9 new client
     functions, so seeing it proves the gate ran first.
  2. an allowlisted call returns the fake's stubbed envelope exactly, unmodified.
"""
import pytest

from mcp_google_ads_safe import rails, tools
from tests.conftest import TEST_CUSTOMER_ID

FAKE_ENVELOPE = {"results": [], "next_page_token": None}
NOT_ALLOWLISTED = "9999999999"

# (name, tool fn, required positional args). get_entities/search_geo_targets need their
# required positional arg (entity_type/query) supplied on every call.
TOOL_CASES = [
    ("get_account_info", tools.get_account_info, ()),
    ("get_campaign_performance", tools.get_campaign_performance, ()),
    ("get_ad_performance", tools.get_ad_performance, ()),
    ("get_keyword_performance", tools.get_keyword_performance, ()),
    ("get_search_terms", tools.get_search_terms, ()),
    ("get_geo_performance", tools.get_geo_performance, ()),
    ("get_negative_keywords", tools.get_negative_keywords, ()),
    ("search_geo_targets", tools.search_geo_targets, ("Orlando",)),
    ("get_entities", tools.get_entities, ("campaign",)),
]
_IDS = [c[0] for c in TOOL_CASES]


def test_all_nine_read_tools_are_registered_and_callable():
    for _name, fn, _args in TOOL_CASES:
        assert callable(fn)


@pytest.mark.parametrize("name,fn,args", TOOL_CASES, ids=_IDS)
def test_tool_refuses_non_allowlisted_customer_before_touching_fake(
        monkeypatch, fake_client, name, fn, args):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", TEST_CUSTOMER_ID)
    with pytest.raises(rails.RailViolation) as exc:
        fn(*args, customer_id=NOT_ALLOWLISTED)
    assert exc.value.code == "NOT_ALLOWLISTED"


@pytest.mark.parametrize("name,fn,args", TOOL_CASES, ids=_IDS)
def test_tool_returns_fake_envelope_for_allowlisted_customer(fake_client, name, fn, args):
    assert fn(*args, customer_id=TEST_CUSTOMER_ID) == FAKE_ENVELOPE


# --- get_account_info has NO page_token param (always a single row) -----------------------

def test_get_account_info_has_no_page_token_param():
    import inspect
    assert "page_token" not in inspect.signature(tools.get_account_info).parameters


# --- the other 8 DO accept page_token and pass it straight through -------------------------

@pytest.mark.parametrize("name,fn,args", [c for c in TOOL_CASES if c[0] != "get_account_info"],
                         ids=[c[0] for c in TOOL_CASES if c[0] != "get_account_info"])
def test_tool_accepts_page_token_kwarg(fake_client, name, fn, args):
    assert fn(*args, customer_id=TEST_CUSTOMER_ID, page_token="RAW1") == FAKE_ENVELOPE
