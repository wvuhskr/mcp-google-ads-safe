"""Account discovery, manager-wide read scope, the one-query startup check and the
read-only account pick. All offline: a synthetic manager tree answers customer_client
queries the way the live API does (every level in one query, the manager itself at level 0).
"""
import re

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import make_row

LOGIN = "9000000001"


def acct(cid, name, *, manager=False, level=1, status="ENABLED", hidden=False,
         currency="USD", tz="America/New_York", test=False):
    return {"id": int(cid), "descriptive_name": name, "currency_code": currency,
            "time_zone": tz, "status": status, "manager": manager, "level": level,
            "test_account": test, "hidden": hidden}


TREE = [
    acct(LOGIN, "Agency Manager", manager=True, level=0),
    acct("1000000001", "Smith Dental"),
    acct("1000000002", "Sub Manager", manager=True),
    acct("1000000003", "Smithfield Roofing", level=2, currency="CAD"),
    acct("1000000004", "Old Client", status="CANCELED"),
    acct("1000000005", "Hidden Client", hidden=True),
]


@pytest.fixture
def tree(monkeypatch):
    """Install a fake manager tree; returns the list of (customer_id, query) calls made."""
    state = {"accounts": list(TREE), "calls": []}

    def page(query, cid, token):
        state["calls"].append((cid, query))
        assert cid == LOGIN, "account lookups must be asked of the login manager"
        assert "FROM customer_client" in query and token is None
        rows = state["accounts"]
        many = re.search(r"customer_client\.id IN \(([\d, ]+)\)", query)
        if many:
            wanted = {int(x) for x in many.group(1).split(",")}
            rows = [r for r in rows if r["id"] in wanted]
        one = re.search(r"customer_client\.id = (\d+)", query)
        if one:
            rows = [r for r in rows if r["id"] == int(one.group(1))]
        if "customer_client.status = 'ENABLED'" in query:
            rows = [r for r in rows
                    if r["status"] == "ENABLED" and not r["hidden"] and not r["manager"]]
        return ([make_row(**{f"customer_client.{k}": v for k, v in r.items()}) for r in rows],
                None, len(rows))

    monkeypatch.setattr(client, "gads",
                        lambda profile=None: type("C", (), {"login_customer_id": LOGIN})())
    monkeypatch.setattr(client, "_search_one_page", page)
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    return state


# --- find_accounts -----------------------------------------------------------------------

def test_find_accounts_uses_one_query_and_finds_by_name_any_case(tree):
    out = client.find_accounts(name_contains="smith")
    assert len(tree["calls"]) == 1
    assert [a["descriptive_name"] for a in out["accounts"]] == ["Smith Dental", "Smithfield Roofing"]
    first = out["accounts"][0]
    assert first["customer_id"] == "1000000001"
    assert first["customer_id_formatted"] == "100-000-0001"
    assert first["currency_code"] == "USD" and first["time_zone"] == "America/New_York"
    assert first["status"] == "ENABLED" and first["manager"] is False
    assert first["level"] == 1 and first["test_account"] is False
    assert out["accounts"][1]["level"] == 2  # indirect descendant, same single query
    assert out["login_manager"] == LOGIN and out["total_matching"] == 2


def test_find_accounts_hides_managers_and_hidden_unless_asked(tree):
    names = lambda **kw: {a["descriptive_name"] for a in client.find_accounts(**kw)["accounts"]}  # noqa: E731
    assert names() == {"Smith Dental", "Smithfield Roofing", "Old Client"}
    assert "Sub Manager" in names(include_managers=True)
    assert "Hidden Client" in names(include_hidden=True)


def test_find_accounts_marks_read_and_write_lists(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000001,1000000003")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "1000000001")
    by_id = {a["customer_id"]: a for a in client.find_accounts()["accounts"]}
    assert (by_id["1000000001"]["can_read"], by_id["1000000001"]["can_write"]) == (True, True)
    assert (by_id["1000000003"]["can_read"], by_id["1000000003"]["can_write"]) == (True, False)
    assert by_id["1000000004"]["can_read"] is False


def test_find_accounts_manager_scope_reads_cover_enabled_visible_only(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    by_id = {a["customer_id"]: a
             for a in client.find_accounts(include_hidden=True)["accounts"]}
    assert by_id["1000000003"]["can_read"] is True and by_id["1000000003"]["can_write"] is False
    assert by_id["1000000004"]["can_read"] is False  # canceled
    assert by_id["1000000005"]["can_read"] is False  # hidden


def test_find_accounts_pages_with_limit(tree):
    first = client.find_accounts(limit=2)
    assert first["returned_count"] == 2 and first["total_matching"] == 3
    rest = client.find_accounts(limit=2, page_token=first["next_page_token"])
    assert rest["returned_count"] == 1 and rest["next_page_token"] is None
    seen = [a["customer_id"] for a in first["accounts"] + rest["accounts"]]
    assert len(set(seen)) == 3


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 201}, {"limit": True},
                                    {"page_token": "abc"}, {"page_token": "-1"},
                                    {"name_contains": 5}])
def test_find_accounts_refuses_bad_arguments_before_any_query(tree, kwargs):
    with pytest.raises(rails.RailViolation):
        client.find_accounts(**kwargs)
    assert tree["calls"] == []


# --- startup check -----------------------------------------------------------------------

def test_preflight_is_one_query_and_accepts_indirect_descendants(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000001,1000000003")
    client.preflight()
    assert len(tree["calls"]) == 1
    assert "customer_client.id IN (" in tree["calls"][0][1]


def test_preflight_refuses_an_id_outside_the_manager(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000001,7777777777")
    with pytest.raises(rails.RailViolation) as exc:
        client.preflight()
    assert exc.value.code == "ANCESTRY_NOT_DESCENDANT"
    assert "7777777777" in str(exc.value) and LOGIN in str(exc.value)


def test_preflight_handles_a_large_agency_tree_in_one_query(tree, monkeypatch):
    # 600 clients, several below sub-managers: the old walk refused this (ANCESTRY_TRUNCATED).
    tree["accounts"] += [acct(str(2000000000 + i), f"Client {i}", level=1 + i % 4)
                         for i in range(600)]
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "2000000599")
    client.preflight()
    assert len(tree["calls"]) == 1


def test_preflight_without_login_manager_refuses(tree, monkeypatch):
    monkeypatch.setattr(client, "gads",
                        lambda profile=None: type("C", (), {"login_customer_id": None})())
    with pytest.raises(rails.RailViolation) as exc:
        client.preflight()
    assert exc.value.code == "NO_LOGIN_MANAGER" and tree["calls"] == []


def test_preflight_manager_scope_needs_no_read_list_but_checks_write_ids(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    client.preflight()  # read-only agency setup: nothing listed at all
    assert len(tree["calls"]) == 1
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "7777777777")
    with pytest.raises(rails.RailViolation) as exc:
        client.preflight()
    assert exc.value.code == "ANCESTRY_NOT_DESCENDANT"


# --- read scope --------------------------------------------------------------------------

def test_list_scope_is_the_default_and_unchanged(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000001")
    rails.check_customer_allowlisted("1000000001", "read")
    with pytest.raises(rails.RailViolation) as exc:
        rails.check_customer_allowlisted("1000000003", "read")
    assert exc.value.code == "NOT_ALLOWLISTED"
    assert tree["calls"] == []  # list scope never asks Google


def test_manager_scope_reads_any_enabled_descendant_with_one_cached_lookup(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    rails.check_customer_allowlisted("100-000-0003", "read")
    rails.check_customer_allowlisted("1000000003", "read")
    assert len(tree["calls"]) == 1
    assert "customer_client.id = 1000000003" in tree["calls"][0][1]


@pytest.mark.parametrize("cid,code", [("7777777777", "NOT_UNDER_MANAGER"),
                                      ("1000000004", "ACCOUNT_NOT_ACTIVE"),
                                      ("1000000005", "ACCOUNT_NOT_ACTIVE"),
                                      ("", "NOT_UNDER_MANAGER"),
                                      ("abc", "NOT_UNDER_MANAGER")])
def test_manager_scope_refuses_outside_canceled_and_hidden(tree, monkeypatch, cid, code):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    with pytest.raises(rails.RailViolation) as exc:
        rails.check_customer_allowlisted(cid, "read")
    assert exc.value.code == code


def test_manager_scope_refusals_are_not_cached(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    with pytest.raises(rails.RailViolation):
        rails.check_customer_allowlisted("7777777777", "read")
    tree["accounts"].append(acct("7777777777", "Newly Linked"))
    rails.check_customer_allowlisted("7777777777", "read")


def test_manager_scope_never_widens_writes(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "1000000001")
    rails.check_customer_allowlisted("1000000001", "write")
    with pytest.raises(rails.RailViolation) as exc:
        rails.check_customer_allowlisted("1000000003", "write")
    assert exc.value.code == "NOT_ALLOWLISTED"


def test_manager_scope_write_id_must_still_be_under_the_manager(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "7777777777")
    with pytest.raises(rails.RailViolation) as exc:
        rails.check_customer_allowlisted("7777777777", "write")
    assert exc.value.code == "NOT_UNDER_MANAGER"


def test_unknown_read_scope_value_refuses_everything(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "everything")
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000001")
    with pytest.raises(rails.RailViolation) as exc:
        rails.check_customer_allowlisted("1000000001", "read")
    assert exc.value.code == "BAD_READ_SCOPE"
    with pytest.raises(rails.RailViolation):
        client.preflight()


# --- health_check and list_accounts ------------------------------------------------------

def test_health_check_list_scope_is_one_query_with_names(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000001,1000000003")
    out = tools.health_check()
    assert len(tree["calls"]) == 1
    assert out["ok"] is True and out["login_manager"] == LOGIN and out["read_scope"] == "list"
    assert out["reachable_accounts"] == 2
    assert out["accounts"] == [
        {"customer_id": "1000000001", "descriptive_name": "Smith Dental", "currency_code": "USD"},
        {"customer_id": "1000000003", "descriptive_name": "Smithfield Roofing", "currency_code": "CAD"},
    ]


def test_health_check_manager_scope_reports_a_count_not_every_account(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    tree["accounts"] += [acct(str(2000000000 + i), f"Client {i}") for i in range(300)]
    out = tools.health_check()
    assert len(tree["calls"]) == 2
    assert out["read_scope"] == "manager" and out["reachable_accounts"] == 302
    assert "accounts" not in out


def test_list_accounts_is_one_query(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1000000003,1000000001")
    assert [a["customer_id"] for a in tools.list_accounts()] == ["1000000001", "1000000003"]
    assert len(tree["calls"]) == 1


# --- picked account: reads only ----------------------------------------------------------

@pytest.fixture
def picked(tree, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_SCOPE", "manager")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "1000000001,1000000003")
    monkeypatch.setattr(tools, "_PICKED", None)
    seen = {}
    monkeypatch.setattr(client, "campaign_performance",
                        lambda cid, *a, **k: seen.setdefault("read", cid) and {"rows": []})
    monkeypatch.setattr(rails, "set_entity_status_draft",
                        lambda cid, *a, **k: seen.setdefault("write", cid) and {"draft_id": "d"})
    return seen


def test_use_account_sets_the_default_for_reads_and_says_which_account(picked):
    out = tools.use_account("100-000-0003")
    assert out["picked"]["customer_id"] == "1000000003"
    assert out["picked"]["descriptive_name"] == "Smithfield Roofing"
    assert "read" in out["note"].lower()
    assert tools.current_account()["picked"]["customer_id"] == "1000000003"
    report = tools.get_campaign_performance()
    assert picked["read"] == "1000000003"
    assert report["account"] == {"customer_id": "1000000003", "selected_by": "use_account"}


def test_explicit_customer_id_beats_the_pick_and_is_labelled(picked):
    tools.use_account("1000000003")
    report = tools.get_campaign_performance(customer_id="1000000001")
    assert report["account"] == {"customer_id": "1000000001", "selected_by": "customer_id"}


def test_use_account_refuses_an_account_it_cannot_read(picked):
    with pytest.raises(rails.RailViolation):
        tools.use_account("7777777777")
    assert tools.current_account()["picked"] is None


def test_use_account_with_nothing_clears_the_pick(picked):
    tools.use_account("1000000003")
    assert tools.use_account(None)["picked"] is None
    assert tools.current_account()["picked"] is None


def test_a_change_never_follows_the_pick(picked):
    tools.use_account("1000000003")
    with pytest.raises(rails.RailViolation) as exc:
        tools.pause_entity("campaign", "1")
    assert exc.value.code == "ACCOUNT_REQUIRED" and "write" not in picked
    tools.pause_entity("campaign", "1", customer_id="1000000001")
    assert picked["write"] == "1000000001"


def test_without_a_pick_changes_keep_the_configured_default(picked, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "1000000001")
    tools.pause_entity("campaign", "1")
    assert picked["write"] == "1000000001"
    assert tools.get_campaign_performance()["account"] == {
        "customer_id": "1000000001", "selected_by": "GOOGLE_ADS_CUSTOMER_ID"}


def test_no_tool_reads_the_default_account_except_through_the_two_helpers():
    """A change must never silently land in the configured default while another account is
    picked. Every tool therefore resolves its account through _pick_or_default (reports) or
    _change_default (changes); a tool reading the environment itself would bypass that."""
    import inspect
    source = inspect.getsource(tools)
    assert source.count("GOOGLE_ADS_CUSTOMER_ID\")") + source.count("GOOGLE_ADS_CUSTOMER_ID')") == 2


def test_recommendation_and_campaign_creation_changes_also_refuse_under_a_pick(picked):
    tools.use_account("1000000003")
    for call in (lambda: tools.apply_recommendation("1"),
                 lambda: tools.dismiss_recommendation("1"),
                 lambda: tools.remove_entity("campaign", "1")):
        with pytest.raises(rails.RailViolation) as exc:
            call()
        assert exc.value.code == "ACCOUNT_REQUIRED"
