"""Readable, bounded report output with a provenance block. Offline: a fake page function
answers the account lookup and the report query with real v25 rows."""
import datetime

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_row

ROW = {"campaign.id": 5, "campaign.name": "Search", "campaign.status": "PAUSED",
       "campaign.advertising_channel_type": "SEARCH", "metrics.impressions": 1000,
       "metrics.clicks": 7, "metrics.cost_micros": 12340000, "metrics.conversions": 1.5,
       "metrics.average_cpc": 1762857.0, "metrics.ctr": 0.007}


@pytest.fixture
def api(monkeypatch):
    state = {"queries": [], "rows": [ROW], "total": None,
             "customer": {"customer.id": int(CID), "customer.descriptive_name": "Test Account",
                          "customer.currency_code": "EUR", "customer.time_zone": "Europe/Paris"}}

    def page(query, cid, token):
        state["queries"].append(query)
        if " FROM customer " in query + " " and "customer_client" not in query:
            return [make_row(**state["customer"])], None, 1
        rows = [make_row(**r) for r in state["rows"]]
        return rows, None, len(rows) if state["total"] is None else state["total"]

    monkeypatch.setattr(client, "gads", lambda profile=None: type("C", (), {})())
    monkeypatch.setattr(client, "_search_one_page", page)
    def today(tz):
        assert tz, "no time zone: the real function fails here too"
        return datetime.date(2026, 3, 10)

    monkeypatch.setattr(client, "_today", today)
    return state


def report_queries(api):
    return [q for q in api["queries"] if " FROM customer" not in q]


# --- readable rows -----------------------------------------------------------------------

def test_report_rows_use_names_numbers_and_account_currency(api):
    row = client.campaign_performance(CID)["rows"][0]
    assert row["campaign"]["status"] == "PAUSED"                      # name, not 3
    assert row["campaign"]["advertising_channel_type"] == "SEARCH"
    assert row["campaign"]["id"] == "5"                               # ids stay text
    assert row["metrics"]["impressions"] == 1000 and row["metrics"]["clicks"] == 7
    assert row["metrics"]["cost_micros"] == 12340000                  # original kept
    assert row["metrics"]["cost"] == 12.34                            # account currency
    assert row["metrics"]["average_cpc_amount"] == 1.76               # micros-valued metric
    assert row["metrics"]["conversions"] == 1.5


def test_readable_rows_drop_unset_noise_but_keep_real_zeroes(api):
    api["rows"] = [dict(ROW, **{"metrics.clicks": 0})]
    row = client.campaign_performance(CID)["rows"][0]
    assert "primary_status" not in row["campaign"]        # never selected, UNSPECIFIED
    assert "labels" not in row["campaign"]
    assert row["metrics"]["clicks"] == 0                  # selected, genuinely zero


def test_run_gaql_is_readable_by_default_and_raw_on_request(api):
    readable = tools.run_gaql("SELECT campaign.status FROM campaign", customer_id=CID)
    assert readable["rows"][0]["campaign"]["status"] == "PAUSED"
    assert readable["provenance"]["query"] == "SELECT campaign.status FROM campaign"
    raw = tools.run_gaql("SELECT campaign.status FROM campaign", customer_id=CID, raw=True)
    expected = client.gaql("SELECT campaign.status FROM campaign", CID)
    assert raw["rows"] == expected["rows"] and raw["rows"][0]["campaign"]["status"] == 3
    assert raw["rows"][0]["metrics"]["cost_micros"] == "12340000"     # today's exact shape
    assert "provenance" not in raw


# --- limit -------------------------------------------------------------------------------

REPORTS = [client.campaign_performance, client.ad_performance, client.keyword_performance,
           client.search_terms, client.geo_performance]


@pytest.mark.parametrize("report", REPORTS, ids=lambda f: f.__name__)
def test_every_report_is_limited_to_100_rows_by_default(api, report):
    report(CID)
    (query,) = report_queries(api)
    assert query.endswith(" LIMIT 100") and query.count("LIMIT") == 1


@pytest.mark.parametrize("report", REPORTS, ids=lambda f: f.__name__)
def test_limit_can_be_raised_to_1000(api, report):
    report(CID, limit=1000)
    assert report_queries(api)[0].endswith(" LIMIT 1000")


@pytest.mark.parametrize("bad", [0, 1001, -1, True, "50", 2.5, None])
def test_bad_limit_is_refused_before_any_request(api, bad):
    with pytest.raises(rails.RailViolation) as exc:
        client.campaign_performance(CID, limit=bad)
    assert exc.value.code == "BAD_LIMIT" and api["queries"] == []


def test_tools_pass_limit_through(api, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", CID)
    for tool in (tools.get_campaign_performance, tools.get_ad_performance,
                 tools.get_keyword_performance, tools.get_search_terms,
                 tools.get_geo_performance):
        api["queries"].clear()
        out = tool(customer_id=CID, limit=7)
        assert report_queries(api)[0].endswith(" LIMIT 7")
        assert out["account"]["customer_id"] == CID and out["provenance"]["rows_returned"] == 1


# --- provenance --------------------------------------------------------------------------

def test_provenance_names_the_account_and_resolves_the_default_period(api):
    out = client.campaign_performance(CID)
    prov = out["provenance"]
    assert prov["account"] == {"customer_id": CID, "descriptive_name": "Test Account",
                               "currency_code": "EUR", "time_zone": "Europe/Paris"}
    # "last 30 days" = the 30 days ending yesterday, in the account's time zone
    assert prov["period"] == {"start_date": "2026-02-08", "end_date": "2026-03-09",
                              "basis": "default: last 30 days, not including today, "
                                       "in the account time zone"}
    (query,) = report_queries(api)
    assert "segments.date BETWEEN '2026-02-08' AND '2026-03-09'" in query
    assert "DURING" not in query
    assert prov["query"] == query


def test_today_is_taken_in_the_account_time_zone(api, monkeypatch):
    seen = []
    monkeypatch.setattr(client, "_today", lambda tz: seen.append(tz) or datetime.date(2026, 3, 10))
    client.campaign_performance(CID)
    assert seen == ["Europe/Paris"]


def test_explicit_dates_are_reported_as_given(api):
    prov = client.campaign_performance(CID, "2026-01-01", "2026-01-31")["provenance"]
    assert prov["period"] == {"start_date": "2026-01-01", "end_date": "2026-01-31",
                              "basis": "dates given"}


def test_provenance_says_when_rows_were_cut_and_how_to_narrow(api):
    api["total"] = 4321
    prov = client.campaign_performance(CID, limit=1)["provenance"]
    assert (prov["rows_returned"], prov["rows_total"], prov["truncated"]) == (1, 4321, True)
    assert "limit" in prov["how_to_get_more"] and "1,000" in prov["how_to_get_more"]
    api["total"] = None
    prov = client.campaign_performance(CID)["provenance"]
    assert prov["truncated"] is False and "how_to_get_more" not in prov


def test_provenance_explains_conversions_and_money(api):
    prov = client.campaign_performance(CID)["provenance"]
    assert "included in the Conversions column" in prov["conversions_note"]
    assert "conversions_by_conversion_date" in prov["conversions_note"]
    assert "EUR" in prov["money_note"] and "millionths" in prov["money_note"]
    lookup = tools.get_negative_keywords(customer_id=CID)
    assert "conversions_note" not in lookup["provenance"]  # no conversions selected


def test_account_details_are_looked_up_once_per_account(api):
    client.campaign_performance(CID)
    client.keyword_performance(CID)
    assert len([q for q in api["queries"] if " FROM customer" in q]) == 1


def test_a_report_still_answers_when_the_account_lookup_fails(api, monkeypatch):
    real = client._search_one_page

    def page(query, cid, token):
        if " FROM customer" in query:
            raise RuntimeError("lookup down")
        return real(query, cid, token)

    monkeypatch.setattr(client, "_search_one_page", page)
    out = client.campaign_performance(CID, "2026-01-01", "2026-01-31")
    assert out["rows"] and out["provenance"]["account"]["currency_code"] is None
    # Default period with no time zone: Google resolves the window, and the answer says so.
    out = client.campaign_performance(CID)
    assert "DURING LAST_30_DAYS" in out["provenance"]["query"]
    assert out["provenance"]["period"]["start_date"] is None
    assert "resolved by Google" in out["provenance"]["period"]["basis"]
