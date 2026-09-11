"""server.py entrypoint + the 4 minimal slice tools (health_check, run_gaql,
update_campaign budget, confirm_and_apply). Fully offline -- fake_client fixture."""
import pytest

from mcp_google_ads_safe import client, rails, server, tools
from tests.conftest import TEST_CUSTOMER_ID

# --- 1: registration ---------------------------------------------------------------------

def test_all_four_tools_are_registered_and_callable():
    for fn in (tools.health_check, tools.run_gaql, tools.update_campaign, tools.confirm_and_apply):
        assert callable(fn)


# --- 2/3: update_campaign draft + cap ------------------------------------------------------

def test_update_campaign_in_cap_returns_dry_run_draft(fake_client):
    result = tools.update_campaign(campaign_id="1", daily_budget=75)
    assert result["dry_run"] is True
    assert "draft_id" in result


def test_update_campaign_above_cap_refuses(monkeypatch, fake_client):
    monkeypatch.setenv("GOOGLE_ADS_MAX_DAILY_BUDGET", "100")
    tools.update_campaign(campaign_id="1", daily_budget=90)  # in-cap: must NOT raise
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign(campaign_id="1", daily_budget=500)
    assert exc.value.code == "CAP_EXCEEDED"


# --- 4: end to end draft -> confirm_and_apply ----------------------------------------------

def test_update_campaign_then_confirm_and_apply(fake_client):
    draft = tools.update_campaign(campaign_id="1", daily_budget=75)
    result = tools.confirm_and_apply(draft["draft_id"])
    assert result["applied"] is True
    assert len(fake_client.dispatch_calls) == 1


# --- 5: run_gaql read allowlist gate --------------------------------------------------------

def test_run_gaql_refuses_non_allowlisted_customer(monkeypatch, fake_client):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "9999999999")
    with pytest.raises(rails.RailViolation) as exc:
        tools.run_gaql("SELECT campaign.id FROM campaign", customer_id=TEST_CUSTOMER_ID)
    assert exc.value.code == "NOT_ALLOWLISTED"


def test_run_gaql_returns_fake_envelope_for_allowlisted_customer(fake_client):
    result = tools.run_gaql("SELECT campaign.id FROM campaign", customer_id=TEST_CUSTOMER_ID)
    assert result == {"results": [], "next_page_token": None}


# --- 6: run_gaql with no customer_id anywhere -----------------------------------------------

def test_run_gaql_no_customer_id_and_no_default_env(monkeypatch, fake_client):
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    with pytest.raises(rails.RailViolation):
        tools.run_gaql("SELECT campaign.id FROM campaign")


# --- 7: main() fails closed ------------------------------------------------------------------

def test_main_fails_closed_before_serving(monkeypatch):
    run_calls = []
    monkeypatch.setattr(server.settings, "load", lambda: None)

    def boom():
        raise rails.RailViolation("boom", code="ANCESTRY_NOT_DESCENDANT")

    monkeypatch.setattr(server.client, "preflight", boom)
    monkeypatch.setattr(server.mcp, "run", lambda: run_calls.append("run"))

    with pytest.raises(rails.RailViolation, match="boom"):
        server.main()
    assert run_calls == []  # mcp.run must NEVER be reached


# --- 8: main() happy path + ordering ----------------------------------------------------------

def test_main_happy_path_calls_preflight_before_run(monkeypatch):
    order = []
    monkeypatch.setattr(server.settings, "load", lambda: order.append("load"))
    monkeypatch.setattr(server.client, "preflight", lambda: order.append("preflight"))
    monkeypatch.setattr(server.mcp, "run", lambda: order.append("run"))

    server.main()

    assert order == ["load", "preflight", "run"]


# --- 9: importing server.py is network-free -----------------------------------------------

def test_importing_server_module_does_not_touch_the_network(monkeypatch):
    # If import-time code called preflight/gads, this monkeypatch-then-reload would raise.
    import importlib

    def boom():
        raise AssertionError("server import must not call client.preflight")

    monkeypatch.setattr(client, "preflight", boom)
    importlib.reload(server)
    assert callable(server.main)
