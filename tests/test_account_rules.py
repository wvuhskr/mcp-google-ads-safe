"""Per-account house rules in advertiser.yaml: each client account can carry its own
website domain, blocked terms, keyword-research defaults and spend caps."""
import pytest
import yaml

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID

OTHER = "2222222222"


@pytest.fixture
def rules(monkeypatch, tmp_path):
    path = tmp_path / "advertiser.yaml"

    def write(data):
        path.write_text(yaml.safe_dump(data))
        monkeypatch.setenv(settings.ENV_VAR, str(path))
        settings._reset()
        return settings.load()

    return write


BASE = {
    "advertiser_domain": "default.example",
    "blocked_terms": ["HouseBan"],
    "accounts": {
        "123-456-7890": {
            "advertiser_domain": "client-a.example",
            "blocked_terms": ["ClientBan"],
            "keyword_research": {"geo_target_constant_ids": [2840]},
            "max_daily_budget": 50,
            "max_cpc": 3,
            "max_target_cpa": 40,
        },
    },
}


# --- file validation ---------------------------------------------------------------------

@pytest.mark.parametrize("accounts,needle", [
    (["x"], "'accounts' must be a mapping"),
    ({"12345": {}}, "10-digit"),
    ({"123-456-7890": {}, 1234567890: {}}, "more than once"),
    ({CID: "x"}, "must be a mapping"),
    ({CID: {"nope": 1}}, "unknown key"),
    ({CID: {"max_cpc": 0}}, "max_cpc"),
    ({CID: {"max_daily_budget": True}}, "max_daily_budget"),
    ({CID: {"max_target_cpa": "ten"}}, "max_target_cpa"),
    ({CID: {"blocked_terms": "one"}}, "blocked_terms"),
    ({CID: {"advertiser_domain": ""}}, "advertiser_domain"),
    ({CID: {"accounts": {}}}, "unknown key"),
])
def test_a_bad_accounts_section_stops_startup_naming_the_problem(rules, accounts, needle):
    with pytest.raises(ValueError, match=needle):
        rules({"accounts": accounts})


def test_a_file_without_accounts_behaves_exactly_as_before(rules):
    rules({"advertiser_domain": "default.example", "blocked_terms": ["HouseBan"]})
    assert settings.advertiser_domain() == "default.example"   # no account context needed
    assert settings.blocked_terms() == ("houseban",)
    assert rails.max_daily_budget() == 1000


# --- which rules apply -------------------------------------------------------------------

def test_a_listed_account_gets_its_own_rules(rules, monkeypatch):
    rules(BASE)
    settings.set_account(CID)
    assert settings.advertiser_domain() == "client-a.example"
    assert settings.blocked_terms() == ("houseban", "clientban")   # house bans always apply
    assert settings.keyword_research()["geo_target_constant_ids"] == [2840]
    assert (rails.max_daily_budget(), rails.max_cpc(), rails.max_target_cpa()) == (50, 3, 40)


def test_an_unlisted_account_gets_the_defaults(rules, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MAX_CPC", "7")
    rules(BASE)
    settings.set_account(OTHER)
    assert settings.advertiser_domain() == "default.example"
    assert settings.blocked_terms() == ("houseban",)
    assert settings.keyword_research()["geo_target_constant_ids"] == []
    assert (rails.max_daily_budget(), rails.max_cpc(), rails.max_target_cpa()) == (1000, 7, 7)


def test_with_account_rules_configured_no_account_means_refusal(rules):
    rules(BASE)
    for read in (settings.advertiser_domain, settings.blocked_terms, settings.keyword_research,
                 rails.max_daily_budget, rails.max_cpc, rails.max_target_cpa):
        with pytest.raises(rails.RailViolation) as exc:
            read()
        assert exc.value.code == "NO_ACCOUNT_CONTEXT"


def test_client_b_urls_are_not_judged_by_client_a_domain(rules):
    rules(BASE)
    settings.set_account(CID)
    client.rsa_url("https://www.client-a.example/page")
    with pytest.raises(rails.RailViolation, match="outside advertiser_domain"):
        client.rsa_url("https://default.example/page")
    settings.set_account(OTHER)
    client.rsa_url("https://default.example/page")
    with pytest.raises(rails.RailViolation, match="outside advertiser_domain"):
        client.rsa_url("https://www.client-a.example/page")


def test_cap_refusals_say_which_limit_applied(rules, monkeypatch):
    rules(BASE)
    settings.set_account(CID)
    with pytest.raises(rails.RailViolation, match="max_daily_budget for account 1234567890") as exc:
        rails.check_budget(75)
    assert exc.value.code == "CAP_EXCEEDED"
    settings.set_account(OTHER)
    rails.check_budget(75)
    with pytest.raises(rails.RailViolation, match="GOOGLE_ADS_MAX_DAILY_BUDGET"):
        rails.check_budget(5000)


# --- the account is set by the tool that names it ----------------------------------------

def test_change_and_report_tools_set_the_account_before_any_rule_runs(rules, monkeypatch):
    rules(BASE)
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},{OTHER}")
    tools._resolve_customer_id("123-456-7890")
    assert settings.active_account() == CID
    tools._change_account(OTHER)
    assert settings.active_account() == OTHER
    tools._read_account(CID)
    assert settings.active_account() == CID
    tools._change_account(None)                   # falls back to the configured default
    assert settings.active_account() == CID


def test_every_tool_call_starts_with_no_account(rules):
    import asyncio
    rules(BASE)
    server, seen = type(app.mcp)("probe"), []

    @server.tool()
    def probe() -> dict:
        seen.append(settings.active_account())
        return {}

    settings.set_account(OTHER)          # left over from an earlier call
    asyncio.run(server.call_tool("probe", {}))
    assert seen == [None]


def test_a_draft_names_the_account_and_the_caps_that_apply(rules, fake_client, monkeypatch):
    client._ACCOUNT_META_CACHE[CID] = {          # learned by an earlier report
        "customer_id": CID, "descriptive_name": "Client A", "currency_code": "EUR",
        "time_zone": "Europe/Paris"}
    rules(BASE)
    with pytest.raises(rails.RailViolation) as exc:
        tools.update_campaign("77", daily_budget="75", customer_id=CID)
    assert exc.value.code == "CAP_EXCEEDED"
    draft = tools.update_campaign("77", daily_budget="45", customer_id=CID)
    assert draft["account"] == {"customer_id": CID, "descriptive_name": "Client A",
                                "currency_code": "EUR"}
    assert draft["caps"] == {"max_daily_budget": 50, "max_cpc": 3, "max_target_cpa": 40,
                             "currency_code": "EUR",
                             "source": "advertiser.yaml accounts section for 1234567890"}


def test_a_draft_under_default_rules_says_so(fake_client, monkeypatch):
    draft = tools.update_campaign("77", daily_budget="75", customer_id=CID)
    assert draft["caps"]["max_daily_budget"] == 1000
    assert draft["caps"]["source"] == "environment defaults"
    assert draft["account"] == {"customer_id": CID, "descriptive_name": None,
                                "currency_code": None}   # not known yet, and not looked up


def test_confirming_a_draft_applies_that_drafts_account_rules(rules, fake_client, monkeypatch):
    rules(BASE)
    draft = tools.update_campaign("77", daily_budget="45", customer_id=CID)
    settings.set_account(None)                    # a fresh tool call
    seen = []
    real = rails.check_customer_allowlisted
    monkeypatch.setattr(rails, "check_customer_allowlisted",
                        lambda cid, mode: seen.append(settings.active_account()) or real(cid, mode))
    tools.confirm_and_apply(draft["draft_id"])
    assert seen and all(a == CID for a in seen)


def test_example_file_is_valid_and_documents_every_key(rules, monkeypatch):
    import pathlib
    example = pathlib.Path(__file__).resolve().parents[1] / "advertiser.example.yaml"
    text = example.read_text()
    monkeypatch.setenv(settings.ENV_VAR, str(example))
    settings._reset()
    loaded = settings.load()
    assert loaded["accounts"]
    for key in ("advertiser_domain", "blocked_terms", "keyword_research", "accounts",
                "max_daily_budget", "max_cpc", "max_target_cpa"):
        assert key in text


# --- review fixes ------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [{"max_daily_budget": 5}, {"max_cpc": 5},
                                   {"max_target_cpa": 5}, {"max_cpc": 5, "max_target_cpa": 9}])
def test_an_account_with_its_own_caps_must_set_budget_and_cpc_together(rules, entry):
    # Otherwise the missing one would silently come from the environment number, which is
    # written for a different currency.
    with pytest.raises(ValueError, match="max_daily_budget and max_cpc"):
        rules({"accounts": {CID: entry}})


def test_an_account_target_cpa_cap_falls_back_to_its_own_cpc_never_the_environment(
        rules, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MAX_TARGET_CPA", "200")
    rules({"accounts": {CID: {"max_daily_budget": 30000, "max_cpc": 900}}})
    settings.set_account(CID)
    assert rails.max_target_cpa() == 900
    settings.set_account(OTHER)
    assert rails.max_target_cpa() == 200


def test_partial_account_keyword_research_keeps_the_top_level_values(rules):
    rules({"keyword_research": {"geo_target_constant_ids": [2840], "language_constant_id": 1000,
                                "keyword_plan_network": "GOOGLE_SEARCH_AND_PARTNERS"},
           "accounts": {CID: {"keyword_research": {"geo_target_constant_ids": [2392]}}}})
    settings.set_account(CID)
    assert settings.keyword_research() == {
        "geo_target_constant_ids": [2392], "language_constant_id": 1000,
        "keyword_plan_network": "GOOGLE_SEARCH_AND_PARTNERS", "include_adult_keywords": False}


def test_startup_notice_lists_accounts_with_their_own_caps(rules, capsys):
    rules(BASE)
    settings.announce_account_caps()
    err = capsys.readouterr().err
    assert "1234567890" in err and "50" in err and "replace" in err
    rules({"advertiser_domain": "default.example"})
    settings.announce_account_caps()
    assert capsys.readouterr().err == ""


def test_a_change_with_no_account_anywhere_says_so_plainly(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    monkeypatch.setattr(tools, "_PICKED", None)
    for call in (lambda: tools.remove_entity("campaign", "1"),
                 lambda: tools.create_shared_negative_set("N"),
                 lambda: tools.apply_recommendation("1")):
        with pytest.raises(rails.RailViolation, match="no customer_id given"):
            call()


def test_the_draft_label_is_worked_out_before_the_draft_is_stored(fake_client, monkeypatch):
    def boom(cid):
        raise RuntimeError("label failed")
    monkeypatch.setattr(client, "known_account", boom)
    before = set(rails._DRAFTS)
    with pytest.raises(RuntimeError):
        tools.update_campaign("77", daily_budget="75", customer_id=CID)
    assert set(rails._DRAFTS) == before          # no orphan draft left behind
