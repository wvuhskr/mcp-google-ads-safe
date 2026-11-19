"""MCP tool surface: the minimal write slice (health_check, run_gaql, update_campaign budget,
pause_entity/enable_entity, confirm_and_apply) plus the batch of 9 read-only report/
lookup tools (get_account_info, get_campaign_performance, get_ad_performance,
get_keyword_performance, get_search_terms, get_geo_performance, get_negative_keywords,
search_geo_targets, get_entities).

Every tool here is a THIN wrapper: it builds only Intents (or plain read args) and calls
rails/client PUBLIC helpers. A tool never calls `client._dispatch` directly and never
constructs a MutationPlan -- that stays inside rails.compile(). It never imports
`google.ads.googleads` or calls `get_service(` -- the AST guard
(tests/test_no_google_imports_outside_client.py) enforces that package-wide.

The 9 read tools all share one shape: resolve customer_id -> check the READ allowlist ->
delegate to a named client.py function that runs one canned GAQL query through client.gaql
(never gaql_all/_scan_rows). No new decoding here -- the envelope's rows pass through exactly
as client.gaql() returns them (see run_gaql, the template for this pattern).
"""
import os
import re
import unicodedata
from typing import Annotated

from pydantic import Field

from . import client, rails, settings
from .app import mcp

# The account picked with use_account. Reports only: no change ever resolves through it.
# ponytail: one pick per server process. A desktop client can share one process across
# several chats, which is exactly why changes must name their account every time.
_PICKED: str | None = None


def _change_default():
    """The configured default account for a CHANGE that named no customer_id. Refuses while
    an account is picked: the pick applies to reports only, and silently sending a change to
    the configured default instead would be the wrong-account mistake this rule prevents."""
    if _PICKED:
        raise rails.RailViolation(
            "an account is picked with use_account, and the pick applies to reports only — "
            "pass customer_id explicitly for any change", code="ACCOUNT_REQUIRED")
    return os.environ.get("GOOGLE_ADS_CUSTOMER_ID")


def _pick_or_default():
    """(account, how it was chosen) for a REPORT that named no customer_id."""
    picked = _PICKED  # read once: tools run in threads and the pick can be cleared meanwhile
    if picked:
        return picked, "use_account"
    return os.environ.get("GOOGLE_ADS_CUSTOMER_ID"), "GOOGLE_ADS_CUSTOMER_ID"


def _change_account(customer_id):
    """The account a CHANGE targets: the named customer_id, else the configured default
    (refused under a pick). Also says which account's house rules and caps apply."""
    cid = _change_default() if customer_id is None else customer_id
    if cid is None:
        raise rails.RailViolation("no customer_id given and GOOGLE_ADS_CUSTOMER_ID is unset")
    settings.set_account(cid)
    return cid


def _resolve_customer_id(customer_id):
    cid = customer_id or _change_default()
    if not cid:
        raise rails.RailViolation("no customer_id given and GOOGLE_ADS_CUSTOMER_ID is unset")
    settings.set_account(cid)
    return cid


def _read_account(customer_id, strict=False):
    """Resolve and authorize the account for a REPORT: explicit customer_id, else the
    use_account pick, else the configured default. Returns (cid, label); every report
    carries the label so the answer always says which account it came from. strict keeps
    the canonical-id rule of the tools that had it."""
    if customer_id if not strict else customer_id is not None:
        cid, source = customer_id, "customer_id"
    else:
        cid, source = _pick_or_default()
    if strict:
        if type(cid) is not str or not re.fullmatch(r"[1-9][0-9]*", cid):
            raise rails.RailViolation("customer_id must be a canonical positive ASCII ID",
                                      code="BAD_ID")
    elif not cid:
        raise rails.RailViolation(
            "no customer_id given, no account picked with use_account, and "
            "GOOGLE_ADS_CUSTOMER_ID is unset")
    rails.check_customer_allowlisted(cid, "read")
    settings.set_account(cid)
    return cid, {"customer_id": rails._normalize_customer_id(cid), "selected_by": source}


def _labelled(result, account):
    return {**result, "account": account} if isinstance(result, dict) else result


@mcp.tool()
def health_check() -> dict:
    """Live sign-in check. One request (two in manager scope, for the count) proves the credential profile works and that every
    listed account sits under the login manager. Reports the login manager, the read scope
    ('list' or 'manager') and how many client accounts are reachable. In list scope it also names
    the listed accounts; in manager scope use find_accounts to search them."""
    listed = client.preflight()
    scope = rails.read_scope()
    out = {"ok": True, "login_manager": client.login_manager_id(), "read_scope": scope}
    if scope == "manager":
        out["reachable_accounts"] = client.reachable_account_count()
        out["next"] = "find_accounts(name_contains=...) searches them by name"
    else:
        read_ids = rails.read_customer_ids()
        out["accounts"] = [
            {k: a[k] for k in ("customer_id", "descriptive_name", "currency_code")}
            for a in listed if a["customer_id"] in read_ids]
        out["reachable_accounts"] = len(out["accounts"])
    return out


@mcp.tool()
def find_accounts(name_contains: str | None = None, include_managers: bool = False,
                  include_hidden: bool = False, limit: int = 50,
                  page_token: str | None = None) -> dict:
    """Find accounts under the login manager by name, e.g. find_accounts("smith dental").
    Read-only, one request, no metrics. Each account shows its number (plain and
    123-456-7890 form), name, currency, time zone, status, whether it is a manager or a test
    account, its depth under the manager, and whether reports (can_read) and changes
    (can_write) are allowed on it. Manager and hidden accounts are left out unless asked
    for. limit is 1 to 200 (default 50); pass next_page_token back as page_token for more."""
    return client.find_accounts(name_contains, include_managers, include_hidden, limit,
                                page_token)


@mcp.tool()
def use_account(customer_id: str | None = None) -> dict:
    """Pick the account that reports use when no customer_id is given, so it need not be
    repeated. Accepts 1234567890 or 123-456-7890. The pick applies to REPORTS ONLY: every
    change must still name its customer_id, and is refused while a pick is active if it
    does not. The pick lasts until changed, cleared (call with no customer_id) or the server
    restarts, and is shared by every chat using this server."""
    global _PICKED
    if not customer_id:
        _PICKED = None
        return {"picked": None, "note": "Pick cleared. Reports fall back to the configured "
                                        "default account, if any."}
    rails.check_customer_allowlisted(customer_id, "read")
    cid = rails._normalize_customer_id(customer_id)
    found = client.find_account(cid)
    if found is None:
        raise rails.RailViolation(
            f"account {customer_id!r} was not found under the login manager — use "
            "find_accounts to see the accounts this sign-in can reach",
            code="NOT_UNDER_MANAGER")
    _PICKED = cid
    return {"picked": found, "note": "Reports now default to this account. Read tools only: "
                                     "changes must name customer_id every time."}


@mcp.tool()
def current_account() -> dict:
    """Show which account reports default to: the use_account pick if there is one,
    otherwise the configured default. Also shows the read scope."""
    return {"picked": client.find_account(_PICKED) if _PICKED else None,
            "configured_default": rails._default_customer_id(),
            "read_scope": rails.read_scope(),
            "note": "The pick applies to reports only; changes must name customer_id."}


@mcp.tool()
def run_gaql(query: str, customer_id: str | None = None, page_token: str | None = None,
             raw: bool = False) -> dict:
    """Run one read-only GAQL query on one account. Use describe-style field names
    (campaign.name, metrics.clicks, segments.date). Rows come back readable: status and
    type NAMES, only the fields selected, counts as numbers, and money both in Google's
    micros and in whole currency units (cost, *_amount). A provenance block names the
    account, its currency and time zone, the query, and how many rows came back out of how
    many. One page of up to 10,000 rows per call: add a LIMIT, and pass next_page_token
    back as page_token for more. raw=True returns Google's untouched rows (integer enums,
    micros as text) for scripts. Refused before any request if the account is not readable.

    describe_fields lists what each resource can select. Recipes, each run read-only
    against a live account on 2026-11-19 (swap the period: LAST_7_DAYS, LAST_14_DAYS,
    LAST_30_DAYS, THIS_MONTH, LAST_MONTH, or BETWEEN '2026-01-01' AND '2026-01-31'):

    By campaign:
      SELECT campaign.name, metrics.impressions, metrics.clicks, metrics.cost_micros,
      metrics.conversions FROM campaign WHERE segments.date DURING LAST_30_DAYS AND
      campaign.status != 'REMOVED' ORDER BY metrics.cost_micros DESC LIMIT 50
    By day (whole account):
      SELECT segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros,
      metrics.conversions FROM customer WHERE segments.date DURING LAST_14_DAYS
      ORDER BY segments.date
    By device:
      SELECT segments.device, metrics.impressions, metrics.clicks, metrics.cost_micros,
      metrics.conversions FROM campaign WHERE segments.date DURING LAST_30_DAYS
    By hour of day:
      SELECT segments.hour, metrics.clicks, metrics.cost_micros, metrics.conversions
      FROM campaign WHERE segments.date DURING LAST_30_DAYS
    Search impression share and what was lost to budget or rank:
      SELECT campaign.name, metrics.search_impression_share,
      metrics.search_budget_lost_impression_share,
      metrics.search_rank_lost_impression_share FROM campaign WHERE segments.date DURING
      LAST_30_DAYS AND campaign.advertising_channel_type = 'SEARCH' AND
      campaign.status = 'ENABLED'
    Conversions by conversion action:
      SELECT segments.conversion_action_name, metrics.conversions,
      metrics.conversions_value FROM customer WHERE segments.date DURING LAST_30_DAYS
    Conversion value and return on ad spend (divide conversions_value by cost):
      SELECT campaign.name, metrics.cost_micros, metrics.conversions,
      metrics.conversions_value FROM campaign WHERE segments.date DURING LAST_30_DAYS AND
      metrics.cost_micros > 0 ORDER BY metrics.cost_micros DESC LIMIT 50
    Conversions dated by when the conversion happened:
      SELECT segments.date, metrics.conversions_by_conversion_date,
      metrics.conversions_value_by_conversion_date FROM customer WHERE segments.date
      DURING LAST_14_DAYS ORDER BY segments.date
    """
    cid, account = _read_account(customer_id)
    if raw:
        return _labelled(client.gaql(query, cid, page_token), account)
    return _labelled(client.readable_report(query, cid, page_token), account)


@mcp.tool()
def describe_fields(resource: str | None = None, category: str | None = None,
                    name_contains: str | None = None) -> dict:
    """Look up what a run_gaql query can ask for, so nobody has to know Google's field names.
    With no resource: the common things to report on (campaign, ad_group, keyword_view,
    search_term_view ...) in plain words. With a resource: every field a query FROM it can
    select, with its type and whether it can be filtered or sorted, plus how many metrics
    and segments it takes. category='metrics' or 'segments' lists those; name_contains
    narrows any list (for example describe_fields('campaign', 'metrics', 'cost')). Reads
    Google's field catalog only: no account data, no account needed."""
    return client.describe_fields(resource, category, name_contains)


@mcp.tool()
def update_campaign(campaign_id: str, daily_budget=None, customer_id: str | None = None,
                    *, status: str | None = None, name: str | None = None,
                    target_cpa=None, target_roas=None, clear_target_cpa: bool = False,
                    clear_target_roas: bool = False) -> dict:
    """Live checked on paused campaigns: name and standard CPA/ROAS set/clear.
    Other update paths retain their separate verification status. Draft campaign edits.
    CPA is account currency; ROAS is a ratio (2 means 200%). Explicit clear flags remove
    optional Maximize strategy targets. Portfolio changes require the environment opt-in
    and show every attached campaign. Confirm the returned draft to apply atomically.
    """
    return rails.update_draft(rails.UpdateCampaignIntent(
        _resolve_customer_id(customer_id), campaign_id, daily_budget, status, name,
        target_cpa, target_roas, clear_target_cpa, clear_target_roas))


@mcp.tool()
def update_ad_group(ad_group_id: str, customer_id: str | None = None, *,
                    status: str | None = None, name: str | None = None,
                    target_cpa=None, cpc_bid=None, clear_target_cpa: bool = False) -> dict:
    """Live checked: Manual CPC and standard CPA set/clear with paused parent campaigns.
    Status/name and portfolio paths retain their separate verification status.
    Money is account currency. clear_target_cpa restores the campaign target. CPC requires
    standard Manual CPC. Effective bids are read after apply; a failed verification consumes
    the draft and requires reading account state before considering another write.
    """
    return rails.update_draft(rails.UpdateAdGroupIntent(
        _resolve_customer_id(customer_id), ad_group_id, status, name, target_cpa,
        cpc_bid, clear_target_cpa))


@mcp.tool()
def pause_entity(entity_type: str, entity_id: str, customer_id: str | None = None) -> dict:
    """Draft PAUSING a campaign or ad group (entity_type='campaign'|'ad_group'). Returns a
    dry-run draft; apply with confirm_and_apply(draft_id). Refuses if already paused, removed,
    not found, writes disabled, or the account is not write-allowlisted."""
    cid = _resolve_customer_id(customer_id)
    return rails.set_entity_status_draft(cid, entity_type, entity_id, "PAUSED")


@mcp.tool()
def enable_entity(entity_type: str, entity_id: str, customer_id: str | None = None) -> dict:
    """Draft ENABLING (un-pausing) a campaign or ad group. Same safety + draft/confirm flow."""
    cid = _resolve_customer_id(customer_id)
    return rails.set_entity_status_draft(cid, entity_type, entity_id, "ENABLED")


@mcp.tool()
def remove_entity(entity_type: str, entity_id: str, customer_id: str | None = None, *,
                  ad_group_id: str | None = None) -> dict:
    """Offline verified only. Draft permanent removal of one PAUSED standard Search
    campaign, SEARCH_STANDARD ad group, or responsive search ad. IDs use positive numeric
    strings. For an ad, entity_id is the ad ID and ad_group_id is required; for
    campaign and ad_group it is forbidden. Confirmation is required. Affected descendants
    stop being usable through a removed parent. Existing budgets, bare assets, and history
    are retained. Removal cannot be undone in place."""
    cid = _change_account(customer_id)
    return rails.remove_entity_draft(
        rails.RemoveEntityIntent(cid, entity_type, entity_id, ad_group_id))


@mcp.tool()
def confirm_and_apply(draft_id: str) -> dict:
    """Execute a previously drafted write. The ONLY tool that leads to a mutation."""
    return rails.apply_draft(draft_id)


@mcp.tool()
def undo_change(draft_id: str) -> dict:
    """Draft the reverse of an already-applied change, found by its draft_id in the audit
    log. Returns a NEW draft that still needs confirm_and_apply; nothing is sent to Google
    here. Reversible: campaign/ad-group updates (budget, name, status, targets, CPC),
    pause/enable, keyword and negative additions or removals, keyword bids, schedules, and
    enabled creations (paused). Removals and asset uploads are permanent and are reported as
    not reversible. Refuses if the original outcome was unknown or an undo already exists."""
    from .undo import undo_change as _undo
    return _undo(draft_id)


@mcp.tool()
def create_portfolio_bidding_strategy(
    name: Annotated[str, Field(strict=True)],
    strategy_type: Annotated[str, Field(strict=True)],
    target_cpa=None,
    target_roas=None,
    customer_id: Annotated[str, Field(strict=True)] | None = None,
) -> dict:
    """Draft one unattached portfolio bidding strategy owned by the selected client account.
    Supports TARGET_CPA in account currency or TARGET_ROAS as a ratio (2 means 200%).
    Requires writes, read/write account permission, portfolio opt-in and confirmation.
    This creates inventory only; it does not attach campaigns or begin serving.
    Offline-tested only; concurrent same-name creation can still race the final check.
    """
    cid = _change_account(customer_id)
    return rails.portfolio_creation_draft(
        rails.CreatePortfolioBiddingStrategyIntent(cid, name, strategy_type, target_cpa, target_roas))


# --- 9 read-only report/lookup tools ----------------------------------------------------

@mcp.tool()
def get_account_info(customer_id: str | None = None) -> dict:
    """Single-row account info (id, descriptive name, currency, time zone, auto-tagging,
    manager flag, status). No page_token -- always at most one row."""
    cid, account = _read_account(customer_id)
    return _labelled(client.account_info(cid), account)


@mcp.tool()
def get_campaign_performance(customer_id: str | None = None, date_range_start: str | None = None,
                             date_range_end: str | None = None, page_token: str | None = None,
                             limit: int = client.REPORT_LIMIT_DEFAULT) -> dict:
    """One row per campaign: impressions, clicks, cost, conversions, conversion value, CTR and
    average CPC, highest spend first. Last 30 days (not including today) unless dates are given.
    Dates are YYYY-MM-DD. limit is 1 to 1,000 rows (default 100); the provenance block says
    which account, the exact dates, and whether more rows exist than were returned."""
    cid, account = _read_account(customer_id)
    return _labelled(client.campaign_performance(cid, date_range_start, date_range_end, page_token, limit),
                     account)


@mcp.tool()
def get_ad_performance(customer_id: str | None = None, date_range_start: str | None = None,
                       date_range_end: str | None = None, page_token: str | None = None,
                       limit: int = client.REPORT_LIMIT_DEFAULT) -> dict:
    """One row per ad: responsive search ad text, final URLs, status and metrics, highest spend
    first. Last 30 days (not including today) unless dates are given.
    Dates are YYYY-MM-DD. limit is 1 to 1,000 rows (default 100); the provenance block says
    which account, the exact dates, and whether more rows exist than were returned."""
    cid, account = _read_account(customer_id)
    return _labelled(client.ad_performance(cid, date_range_start, date_range_end, page_token, limit),
                     account)


@mcp.tool()
def get_keyword_performance(customer_id: str | None = None, date_range_start: str | None = None,
                            date_range_end: str | None = None, page_token: str | None = None,
                            limit: int = client.REPORT_LIMIT_DEFAULT) -> dict:
    """One row per keyword: text, match type, quality score and metrics, highest spend first.
    Last 30 days (not including today) unless dates are given.
    Dates are YYYY-MM-DD. limit is 1 to 1,000 rows (default 100); the provenance block says
    which account, the exact dates, and whether more rows exist than were returned."""
    cid, account = _read_account(customer_id)
    return _labelled(client.keyword_performance(cid, date_range_start, date_range_end, page_token, limit),
                     account)


@mcp.tool()
def get_search_terms(customer_id: str | None = None, date_range_start: str | None = None,
                     date_range_end: str | None = None, page_token: str | None = None,
                     limit: int = client.REPORT_LIMIT_DEFAULT) -> dict:
    """One row per search term people typed, with its campaign, ad group and metrics, most
    clicks first. Last 30 days (not including today) unless dates are given.
    Dates are YYYY-MM-DD. limit is 1 to 1,000 rows (default 100); the provenance block says
    which account, the exact dates, and whether more rows exist than were returned."""
    cid, account = _read_account(customer_id)
    return _labelled(client.search_terms(cid, date_range_start, date_range_end, page_token, limit),
                     account)


@mcp.tool()
def get_geo_performance(customer_id: str | None = None, date_range_start: str | None = None,
                        date_range_end: str | None = None, page_token: str | None = None,
                        limit: int = client.REPORT_LIMIT_DEFAULT) -> dict:
    """One row per campaign and location with metrics, highest spend first. Last 30 days (not
    including today) unless dates are given.
    Dates are YYYY-MM-DD. limit is 1 to 1,000 rows (default 100); the provenance block says
    which account, the exact dates, and whether more rows exist than were returned."""
    cid, account = _read_account(customer_id)
    return _labelled(client.geo_performance(cid, date_range_start, date_range_end, page_token, limit),
                     account)


@mcp.tool()
def get_negative_keywords(customer_id: str | None = None, page_token: str | None = None) -> dict:
    """Campaign-level negative keywords. No date range."""
    cid, account = _read_account(customer_id)
    return _labelled(client.negative_keywords(cid, page_token), account)


@mcp.tool()
def search_geo_targets(query: str, customer_id: str | None = None,
                       page_token: str | None = None) -> dict:
    """Search geo_target_constant by display-name substring (e.g. 'Orlando')."""
    cid, account = _read_account(customer_id)
    return _labelled(client.geo_targets(cid, query, page_token), account)


@mcp.tool()
def get_entities(entity_type: str, ids: list[str] | None = None, parent_id: str | None = None,
                 customer_id: str | None = None, page_token: str | None = None) -> dict:
    """Generic entity lookup across campaign|ad_group|keyword|ad, optionally filtered by ids
    and/or parent_id (e.g. ad_group_ids under a campaign). campaign has no parent scope."""
    cid, account = _read_account(customer_id)
    return _labelled(client.entities(cid, entity_type, ids, parent_id, page_token), account)


@mcp.tool()
def list_accounts() -> list[dict]:
    """List the accounts named in configuration (the read list; in manager read scope, the
    read and write lists). To search every account under the manager by name, use
    find_accounts instead."""
    return client.list_accounts()


@mcp.tool()
def draft_keywords(
    ad_group_id: str, keywords: list[dict], customer_id: str | None = None
) -> dict:
    """Live checked: paused exact-match keyword creation in a paused Search campaign. Draft new positive keywords, always PAUSED. Each item needs text and match_type (EXACT/PHRASE/BROAD)."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id), ad_group_id, "draft_keywords", keywords
        )
    )


@mcp.tool()
def remove_keywords(
    ad_group_id: str, criterion_ids: list[str], customer_id: str | None = None
) -> dict:
    """Live checked: removal of a newly created paused test keyword. Remove positive keywords only. Removal is not undoable in place."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            ad_group_id,
            "remove_keywords",
            criterion_ids,
        )
    )


@mcp.tool()
def update_keyword_bid(
    ad_group_id: str,
    criterion_id: str,
    new_bid,
    customer_id: str | None = None,
    current_bid=None,
) -> dict:
    """Live checked on a paused exact-match Search keyword. Draft Manual CPC keyword bid in account currency; current_bid is ignored and read from Google. Effective CPC is checked after apply."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            ad_group_id,
            "update_keyword_bid",
            {"criterion_id": criterion_id, "new_bid": new_bid},
        )
    )


@mcp.tool()
def add_negative_keywords(
    campaign_id: str,
    keywords: list[str],
    match_type: str = "EXACT",
    customer_id: str | None = None,
) -> dict:
    """Live checked: exact-match campaign negative creation on a paused campaign. Draft campaign negatives with EXACT/PHRASE/BROAD matching."""
    values = (
        [{"text": text, "match_type": match_type} for text in keywords]
        if isinstance(keywords, list)
        else keywords
    )
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            campaign_id,
            "add_negative_keywords",
            values,
        )
    )


@mcp.tool()
def remove_negative_keywords(
    campaign_id: str, criterion_ids: list[str], customer_id: str | None = None
) -> dict:
    """Live checked: removal of a newly created exact-match campaign negative. Remove campaign negative keywords only; removal is not an in-place undo."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            campaign_id,
            "remove_negative_keywords",
            criterion_ids,
        )
    )


@mcp.tool()
def exclude_geo_target(
    campaign_id: str, geo_target_id: str, customer_id: str | None = None
) -> dict:
    """Live checked: removal/exclusion of one included ZIP on a paused campaign, with
    original inclusion restored by a private test routine. Exclude a geo constant;
    refuses existing positive or negative collisions."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            campaign_id,
            "exclude_geo_target",
            geo_target_id,
        )
    )


@mcp.tool()
def remove_geo_target(
    campaign_id: str, geo_target_id: str, customer_id: str | None = None
) -> dict:
    """Live checked: removal/exclusion of one included ZIP on a paused campaign, with
    original inclusion restored by a private test routine. Remove a positive location
    by geo constant, using its returned criterion resource. Removing the last positive
    can broaden eligibility."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            campaign_id,
            "remove_geo_target",
            geo_target_id,
        )
    )


@mcp.tool()
def set_campaign_schedule(
    campaign_id: str, schedules: list[dict], customer_id: str | None = None
) -> dict:
    """Live checked: full-week replacement and restoration on a paused campaign without bid adjustments. Replace the FULL week atomically in account time zone. Unspecified days will not serve; old status and bid modifiers are discarded."""
    return rails.criteria_draft(
        rails.CriteriaIntent(
            _resolve_customer_id(customer_id),
            campaign_id,
            "set_campaign_schedule",
            schedules,
        )
    )


@mcp.tool()
def list_extensions(customer_id: str | None = None, page_token: str | None = None) -> dict:
    """Read nonremoved campaign-level asset links, including sitelinks, callouts and
    structured snippets. This is not customer, ad-group, inherited or asset-group inventory.
    Return one paginated GAQL envelope; follow next_page_token to continue."""
    cid, account = _read_account(customer_id)
    return _labelled(client.list_extensions(cid, page_token), account)


@mcp.tool()
def get_policy_issues(
    customer_id: Annotated[str, Field(strict=True)] | None = None,
    page_token: Annotated[str, Field(strict=True)] | None = None,
) -> dict:
    """Read current ads with approval status other than APPROVED, excluding removed ads
    and removed parent campaigns/ad groups. Approved ads still under review are outside this scope.
    Ad policy only, not asset/account issues or appeals. No dates or attribution apply.
    Follow returned bound tokens; an empty page alone does not prove completion or a
    policy-clean account. Raw policy details and paging fields remain unchanged.
    """
    cid, account = _read_account(customer_id, strict=True)
    result = _labelled(client.get_policy_issues(cid, page_token), account)
    return {**result, "source": {
        "customer_id": cid,
        "tool": "Google Ads API v25 GoogleAdsService.Search",
        "scope": "non-APPROVED ads; removed ads and parent campaigns/ad groups excluded",
        "date_range": "current snapshot",
        "attribution_window": "not applicable; ad policy records, not performance metrics",
    }}


@mcp.tool()
def get_conversion_actions(customer_id: str | None = None, page_token: str | None = None) -> dict:
    """Read nonremoved conversion action configuration visible in the selected customer.
    Includes owner account and attribution settings; does not route to another account.
    This is not exhaustive goal or bidding usage: primary_for_goal=false actions can still
    be biddable in custom goals. Return one paginated GAQL envelope; no writes."""
    cid, account = _read_account(customer_id)
    return _labelled(client.get_conversion_actions(cid, page_token), account)


@mcp.tool()
def list_recommendations(customer_id: str | None = None, page_token: str | None = None) -> dict:
    """Read non-dismissed Google recommendations in one paginated GAQL envelope.
    Impact values are Google's estimates, not measured results or our approval.
    Does not rank, approve, apply or dismiss recommendations."""
    cid, account = _read_account(customer_id)
    return _labelled(client.list_recommendations(cid, page_token), account)


@mcp.tool()
def draft_campaign(campaign_name: str, daily_budget, bidding_strategy: str,
                   geo_target_ids: list[str], language_ids: list[str],
                   contains_eu_political_advertising: bool, customer_id: str | None = None,
                   target_cpa=None, target_roas=None) -> dict:
    """Offline tested, NOT live verified. Draft one PAUSED Search campaign and dedicated
    daily budget with explicit locations/languages and political declaration. Money is in
    account currency; ROAS is a ratio. No groups or ads are created. Confirm to apply."""
    return rails.creation_draft(rails.DraftCampaignIntent(
        _resolve_customer_id(customer_id), campaign_name, daily_budget, bidding_strategy,
        geo_target_ids, language_ids, contains_eu_political_advertising, target_cpa, target_roas))


@mcp.tool()
def create_ad_group(campaign_id: str, ad_group_name: str, customer_id: str | None = None,
                    cpc_bid=None) -> dict:
    """Offline tested, NOT live verified. Draft a PAUSED standard Search ad group under
    an existing Search campaign. Optional CPC is account currency and requires standard
    Manual CPC. No ads are created. Confirm the returned draft to apply."""
    return rails.creation_draft(rails.CreateAdGroupIntent(
        _resolve_customer_id(customer_id), campaign_id, ad_group_name, cpc_bid))


@mcp.tool()
def draft_responsive_search_ad(ad_group_id: str, headlines: list[str], descriptions: list[str],
                               final_url: str, customer_id: str | None = None,
                               path1: str | None = None, path2: str | None = None) -> dict:
    """Offline verified only. Draft one PAUSED responsive Search ad in an existing standard
    Search ad group. Plain, unpinned text only: 3..15 headlines, 2..4 descriptions,
    one HTTP(S) final URL and optional display paths. Confirm the draft to apply."""
    return rails.creation_draft(rails.DraftResponsiveSearchAdIntent(
        _resolve_customer_id(customer_id), ad_group_id, headlines, descriptions, final_url, path1, path2))


@mcp.tool()
def draft_sitelinks(sitelinks: list[dict], campaign_id: str | None = None,
                    ad_group_id: str | None = None, customer_id: str | None = None) -> dict:
    """Offline verified only. Draft 1..10 plain sitelinks for exactly one existing Search
    campaign or standard Search ad group. Each item requires link_text (1..25 characters)
    and one plain HTTP(S) final_url. Optional description1 and description2 must be provided
    together and are each limited to 1..35 characters. Double-width characters count as
    two. New links are explicitly PAUSED."""
    return rails.creation_draft(rails.DraftSitelinksIntent(
        _resolve_customer_id(customer_id), sitelinks, campaign_id, ad_group_id))


@mcp.tool()
def create_callouts(callouts: list[str], campaign_id: str | None = None,
                    ad_group_id: str | None = None, customer_id: str | None = None) -> dict:
    """Offline verified only. Draft a list of strings containing 1..10 plain callouts for exactly one existing
    Search campaign or standard Search ad group. Each string must contain 1..25 characters;
    Double-width characters count as two. The local cap is not a Google limit. New
    connections are explicitly PAUSED and confirmation is required before creation."""
    return rails.creation_draft(rails.CreateCalloutsIntent(
        _resolve_customer_id(customer_id), callouts, campaign_id, ad_group_id))


@mcp.tool()
def create_structured_snippets(snippets: list[dict], campaign_id: str | None = None,
                               ad_group_id: str | None = None,
                               customer_id: str | None = None) -> dict:
    """Draft 1..10 new structured snippets for exactly one existing Search campaign or
    standard Search ad group; confirmation is required before creation and connections are
    PAUSED. Each object requires exactly header and values; values is a list of 3..10 unique
    plain strings, each 1..25 characters (double-width characters count as two). Supported
    English headers: Brands, Amenities, Styles, Types, Destinations, Services, Courses,
    Neighborhoods, Shows, Insurance coverage, Degree programs, Featured hotels, Models."""
    return rails.creation_draft(rails.CreateStructuredSnippetsIntent(
        _resolve_customer_id(customer_id), snippets, campaign_id, ad_group_id))


@mcp.tool()
def remove_extension(asset_id: str, extension_type: str, campaign_id: str | None = None,
                     ad_group_id: str | None = None,
                     customer_id: str | None = None) -> dict:
    """Offline verified only. Draft removal of one SITELINK, CALLOUT, or
    STRUCTURED_SNIPPET connection from exactly one campaign or ad group. IDs must be
    positive numeric strings. Confirmation is required. This retains the underlying asset
    and every other connection; higher-level associations may still serve. list_extensions
    supplies campaign asset IDs, target, and type. For ad-group IDs, use read-only run_gaql
    as documented in docs/m2-remove-extension.md."""
    cid = _change_account(customer_id)
    return rails.remove_extension_draft(rails.RemoveExtensionIntent(
        cid, asset_id, extension_type, campaign_id, ad_group_id))


@mcp.tool()
def upload_image_asset(image_base64: str, name: str, customer_id: str | None = None) -> dict:
    """Offline verified only. Draft one unlinked IMAGE asset; confirmation required.
    Supply canonical standard base64 of original single-frame JPEG/PNG bytes, no whitespace,
    data URI, URL or file path. Local limits: 5,120,000 bytes and 25,000,000 pixels.
    Name: nonblank, at most 128 characters/255 UTF-8 bytes, no controls or blocked terms.
    No conversion or visual screening. Assets have no paused status and cannot be deleted
    through the API. Google may deduplicate content and ignore name. Readback proves only
    identity and metadata, not bytes, newness, policy or serving. Role-specific dimensions
    and aspect ratios are deferred to linking. No new link is created."""
    cid = _change_account(customer_id)
    return rails.upload_image_draft(rails.UploadImageAssetIntent(cid, image_base64, name))


@mcp.tool()
def upload_text_asset(text: str, name: str, customer_id: str | None = None) -> dict:
    """Offline verified only. Draft one bare TEXT asset; confirmation required.
    Plain text has a conservative local 90-character limit, counting East Asian wide and
    full-width characters twice, not a universal Google TextAsset maximum. No surrounding
    whitespace, controls or braces. Preserve exact text. Name: nonblank, at most 128
    characters/255 UTF-8 bytes, no controls. Both text and name reject blocked terms and
    appear in preview/audit. Later role-specific attachment limits may be stricter.
    No new serving association. Bare assets have no paused status and cannot be deleted
    through the API; inventory residue persists. Google may return matching existing content
    and ignore name. Readback checks identity and exact text, not newness, policy or serving."""
    cid = _change_account(customer_id)
    return rails.upload_text_draft(rails.UploadTextAssetIntent(cid, text, name))


@mcp.tool()
def create_custom_audience(name: str, url_contains: list[str], customer_id: str | None = None) -> dict:
    """Offline verified only. Draft a website-visitor UserList, not CustomAudience interests.
    Confirmation and GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT=true required. Supply 1..10
    unique exact URL substrings, each nonblank, at most 256 UTF-8 bytes, no surrounding
    whitespace, controls, braces, wildcard shorthand or blocked terms. Name: nonblank,
    at most 128 characters/255 UTF-8 bytes, no controls or blocked terms; owned names unique.
    OPEN collection uses OR URL-CONTAINS rules with a 30-day per-rule lookback through
    existing tags. No prepopulation request or campaign/ad-group attachment; not automatically
    targeted. No membership count, tag, consent or serving proof. Discover IDs with run_gaql
    as documented in docs/m2-custom-audience.md."""
    cid = _change_account(customer_id)
    return rails.custom_audience_draft(rails.CreateCustomAudienceIntent(cid, name, url_contains))


@mcp.tool()
def add_audience_targeting(campaign_id: str, audience_id: str, targeting_mode: str,
                           customer_id: str | None = None) -> dict:
    """Draft one PAUSED campaign audience criterion, offline verified only.
    Requires a PAUSED standard SEARCH campaign and an OWNED editable OPEN RULE_BASED
    website-visitor UserList in the same account. IDs are positive ASCII numeric strings.
    Required mode is exactly OBSERVATION (does not narrow reach) or TARGETING (narrows reach).
    The campaign must already have exactly one explicit matching AUDIENCE restriction,
    with no ad-group targeting restrictions. Configure intended campaign-level mode first.
    Writes, read/write allowlists, GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT=true and confirmation
    required. Refuses any existing positive or negative selection of this list.
    No mode edits, activation, list changes, bids, tags or audience readiness proof.
    Connections shown cover the selected account only. See docs/m2-audience-targeting.md."""
    cid = _change_account(customer_id)
    return rails.audience_targeting_draft(
        rails.AddAudienceTargetingIntent(cid, campaign_id, audience_id, targeting_mode))


@mcp.tool()
def create_conversion_action(name: str, category: str, customer_id: str | None = None) -> dict:
    """Draft a fixed secondary website conversion action, with confirmation required.
    Category: DEFAULT, PURCHASE, SIGNUP, SUBMIT_LEAD_FORM, CONTACT, BOOK_APPOINTMENT,
    REQUEST_QUOTE. Name uses exact creation-name/content rules; existing names refuse.
    Fixed WEBPAGE, ENABLED, primary_for_goal=False, ONE_PER_CLICK, 30-day click and 1-day
    view windows. Requires writes, selected and owner read/write permission, conversion-goal
    opt-in and a complete accessible login manager tree with all tracking customers authorized.
    Previews existing/automatically created goals and all custom-goal campaign usage.
    Custom-goal membership can override secondary status for bidding. No tag installation,
    uploads, goal edits or primary-status changes. Value/attribution settings provider-managed.
    See docs/m2-conversion-creation.md for scope and offline verification limits."""
    cid = _change_account(customer_id)
    return rails.conversion_action_draft(rails.CreateConversionActionIntent(cid, name, category))


@mcp.tool()
def set_conversion_action_primary_status(conversion_action_id: str, primary_for_goal: Annotated[bool, Field(strict=True)],
                                         customer_id: str | None = None) -> dict:
    """Draft an existing ENABLED WEBPAGE action's primary_for_goal change only.
    Requires confirmation, writes and conversion-goal opt-in, selected/owner permissions,
    and a complete visible manager tree with every tracking account write-authorized.
    Previews unchanged ordinary goals and all custom membership/campaign usage.
    Custom goals can still bid on secondary actions. No other action or goal fields change.
    Offline-tested only; see docs/m2-conversion-primary-status.md."""
    cid = _change_account(customer_id)
    return rails.conversion_primary_status_draft(
        rails.SetConversionActionPrimaryStatusIntent(cid, conversion_action_id, primary_for_goal))


@mcp.tool()
def apply_recommendation(recommendation_id: str, customer_id: str | None = None) -> dict:
    """Draft one CAMPAIGN_BUDGET recommendation at its explicit recommended amount.
    Default-off gate and confirmation required. No validation-only or automated rollback.
    Unknown outcomes require reading account state before retrying.
    """
    cid = _change_account(customer_id)
    return rails.apply_recommendation_draft(cid, recommendation_id)


@mcp.tool()
def dismiss_recommendation(recommendation_id: str, customer_id: str | None = None) -> dict:
    """Draft dismissal of one exact suggestion. Confirmation required.
    Does not apply its proposal, disable future suggestions or auto-apply subscriptions.
    No validation-only support, automatic rollback or retry; live visibility is unverified.
    """
    cid = _change_account(customer_id)
    return rails.dismiss_recommendation_draft(rails.DismissRecommendationIntent(cid, recommendation_id))


@mcp.tool()
def discover_keywords(
    seed_keywords: Annotated[list[Annotated[str, Field(strict=True)]], Field(strict=True)],
    customer_id: Annotated[str, Field(strict=True)] | None = None,
    page_token: Annotated[str, Field(strict=True)] | None = None,
) -> dict:
    """Research keyword ideas from 1..20 exact keyword strings, read-only, one page of 50.
    Uses configured keyword_research targeting; empty geos / null language mean all.
    Historical estimates default to past 12 months, not forecasts or account results.
    Missing metrics are unavailable; monetary micros use unspecified account currency.
    Resume only with the returned token and identical seeds, account and settings.
    No URL seed, keyword addition or confirmation. See docs/keyword-discovery.md.
    """
    cid, account = _read_account(customer_id, strict=True)
    if type(seed_keywords) is not list or not 1 <= len(seed_keywords) <= 20 or any(
        type(seed) is not str or not seed or seed != seed.strip()
        or any(unicodedata.category(ch) == "Cc" for ch in seed)
        for seed in seed_keywords
    ):
        raise rails.RailViolation("seed_keywords must be 1..20 exact nonblank strings without "
                                  "outer whitespace or control characters", code="BAD_SEEDS")
    return _labelled(client.discover_keywords(seed_keywords, cid, page_token), account)


@mcp.tool()
def get_keyword_forecasts(
    keyword_texts: Annotated[list[Annotated[str, Field(strict=True)]], Field(strict=True)],
    match_type: Annotated[str, Field(strict=True)],
    forecast_start_date: Annotated[str, Field(strict=True)],
    forecast_end_date: Annotated[str, Field(strict=True)],
    max_cpc_bid_micros: Annotated[int, Field(strict=True)],
    customer_id: Annotated[str, Field(strict=True)] | None = None,
    daily_budget_micros: Annotated[int, Field(strict=True)] | None = None,
) -> dict:
    """Google campaign forecast estimates for one proposed ad group using Manual CPC.
    Requires explicit future dates, match type, bid and configured locations/language.
    Returns raw optional campaign metrics, not per-keyword or historical performance.
    Read-only; no saved plan, account changes or confirmation. See docs/keyword-forecasts.md.
    """
    cid, account = _read_account(customer_id, strict=True)
    if (type(keyword_texts) is not list or not 1 <= len(keyword_texts) <= 20 or any(
            type(text) is not str or not text or text != text.strip() or len(text) > 80
            or len(text.split()) > 10 or any(unicodedata.category(ch) == "Cc" for ch in text)
            for text in keyword_texts) or len(set(keyword_texts)) != len(keyword_texts)):
        raise rails.RailViolation("keyword_texts requires 1..20 unique exact strings, at most "
                                  "80 characters and 10 words, without outer whitespace or "
                                  "control characters", code="BAD_INPUT")
    if type(match_type) is not str or match_type not in {"EXACT", "PHRASE", "BROAD"}:
        raise rails.RailViolation("match_type requires EXACT, PHRASE or BROAD", code="BAD_INPUT")
    for value in [max_cpc_bid_micros, *([] if daily_budget_micros is None else [daily_budget_micros])]:
        if type(value) is not int or not 0 < value <= 2**63 - 1:
            raise rails.RailViolation("money requires a positive signed-int64 integer in micros",
                                      code="BAD_INPUT")
    return _labelled(client.keyword_forecasts(
        keyword_texts, match_type, forecast_start_date, forecast_end_date,
        max_cpc_bid_micros, cid, daily_budget_micros), account)


@mcp.tool()
def create_pmax_campaign(campaign_name: str, asset_group_name: str, daily_budget,
                         target_cpa, geo_target_ids: list[str], language_ids: list[str],
                         headlines: list[str], long_headlines: list[str], descriptions: list[str],
                         business_name: str, final_url: str, landscape_image_asset_id: str,
                         square_image_asset_id: str, logo_asset_id: str,
                         contains_eu_political_advertising: bool, customer_id: str | None = None) -> dict:
    """Offline tested; provider acceptance unverified. Draft one PAUSED non-retail
    Performance Max campaign, dedicated DAILY budget and PAUSED asset group/links.
    daily_budget and explicit target_cpa use account currency. Requires canonical string
    IDs, explicit geography/languages, configured-domain HTTPS URL and existing owned
    JPEG/PNG images. No feeds, portfolio bidding or uploads. Brand guidelines enabled;
    business name/logo link to campaign. Automated call to action; Google may generate
    video. Confirm the returned draft to apply atomically. See docs/m2-pmax-creation.md."""
    cid = _change_account(customer_id)
    return rails.creation_draft(rails.CreatePMaxCampaignIntent(
        cid, campaign_name, asset_group_name, daily_budget, target_cpa, geo_target_ids,
        language_ids, headlines, long_headlines, descriptions, business_name, final_url,
        landscape_image_asset_id, square_image_asset_id, logo_asset_id,
        contains_eu_political_advertising))


@mcp.tool()
def create_demand_gen_campaign(
    campaign_name: Annotated[str, Field(strict=True)],
    daily_budget,
    geo_target_ids: list[str],
    language_ids: list[str],
    contains_eu_political_advertising: Annotated[bool, Field(strict=True)],
    customer_id: Annotated[str, Field(strict=True)] | None = None,
) -> dict:
    """Draft one paused Demand Gen campaign with a dedicated daily budget and explicit
    campaign-level locations and languages. The targeting ownership choice is fixed at
    creation and cannot later be upgraded on this campaign. No groups or ads are created.
    Offline-tested only; confirmation is required and provider acceptance is unverified.
    """
    cid = _change_account(customer_id)
    return rails.creation_draft(rails.CreateDemandGenCampaignIntent(
        cid, campaign_name, daily_budget, geo_target_ids, language_ids,
        contains_eu_political_advertising))


@mcp.tool()
def create_asset_group(campaign_id: str, asset_group_name: str, headlines: list[str],
                       long_headlines: list[str], descriptions: list[str], final_url: str,
                       landscape_image_asset_id: str, square_image_asset_id: str,
                       customer_id: str | None = None) -> dict:
    """Offline-verified draft for one PAUSED asset group in one existing PAUSED,
    non-retail Performance Max campaign. Reuses two existing image assets and inherits the
    campaign's existing business name, logo, bidding, geography, language, political and
    automation settings without changing them. Confirmation sends one atomic request; no
    campaign, budget, brand, feed, signal or listing-group write is included. Provider
    acceptance, policy approval and serving are not proved offline."""
    cid = _change_account(customer_id)
    return rails.creation_draft(rails.CreateAssetGroupIntent(
        cid, campaign_id, asset_group_name, headlines, long_headlines, descriptions,
        final_url, landscape_image_asset_id, square_image_asset_id))


@mcp.tool()
def update_asset_group(asset_group_id: str, *, name: str | None = None,
                       final_url: str | None = None,
                       customer_id: str | None = None) -> dict:
    """Draft a name and/or final URL update for one existing PAUSED asset group under
    its existing eligible PAUSED non-retail Performance Max campaign. At least one field
    must change. Confirmation sends one atomic update without changing status, parent,
    branding, assets, bidding, targeting or automation. Provider acceptance and serving
    are not proved offline. See docs/m3-asset-group-update.md."""
    cid = _change_account(customer_id)
    return rails.asset_group_update_draft(
        rails.UpdateAssetGroupIntent(cid, asset_group_id, name, final_url))


@mcp.tool()
def add_asset_group_assets(asset_group_id: str, assets: list[dict],
                           customer_id: str | None = None) -> dict:
    """Draft PAUSED connections from existing owned assets to one existing PAUSED
    Performance Max asset group. Each item must contain only asset_id and field_type.
    Supported roles are HEADLINE, LONG_HEADLINE, DESCRIPTION, MARKETING_IMAGE and
    SQUARE_MARKETING_IMAGE. The resulting group must meet all documented creative
    minimums and maximums. Confirmation sends one atomic link-only request without
    uploads, content creation, status changes, branding, campaign or group edits.
    Provider acceptance, policy approval and serving are not proved offline.
    See docs/m3-asset-group-add-assets.md."""
    cid = _change_account(customer_id)
    return rails.asset_group_asset_add_draft(
        rails.AddAssetGroupAssetsIntent(cid, asset_group_id, assets))


@mcp.tool()
def remove_asset_group_asset(asset_group_id: str, asset_id: str, field_type: str,
                             customer_id: str | None = None) -> dict:
    """Draft removal of exactly one existing PAUSED connection between an owned asset
    and one eligible PAUSED Performance Max asset group. The five supported field roles
    match add_asset_group_assets, and the remaining group must stay complete and valid.
    Confirmation sends one atomic connection removal. The bare asset and every other
    connection remain unchanged. Evidence is offline only; policy and serving are not proved.
    See docs/m3-asset-group-remove-asset.md."""
    cid = _change_account(customer_id)
    return rails.asset_group_asset_remove_draft(
        rails.RemoveAssetGroupAssetIntent(cid, asset_group_id, asset_id, field_type))


@mcp.tool()
def set_listing_group_filter(asset_group_id: str, product_item_ids: list[str],
                             customer_id: str | None = None) -> dict:
    """Draft exact product item IDs for one PAUSED retail Performance Max asset group
    under a PAUSED campaign. All remaining products are excluded by this group filter.
    Confirmation atomically replaces the admitted shallow SHOPPING tree. Local caps:
    1 through 20 exact IDs, at most 50 UTF-8 bytes each. No product scan, enablement,
    Merchant Center linkage or product availability/serving proof. Offline contract:
    docs/m3-listing-group-filter.md. Omitted account uses the configured default;
    explicit JSON null is invalid."""
    cid = _change_account(customer_id)
    return rails.listing_filter_draft(
        rails.SetListingGroupFilterIntent(cid, asset_group_id, product_item_ids))


@mcp.tool()
def draft_demand_gen_ad(ad_group_id: str, headline: str, description: str,
                        business_name: str, square_marketing_image_asset_id: str,
                        logo_image_asset_id: str, final_url: str,
                        customer_id: str | None = None) -> dict:
    """Offline tested only. Draft one PAUSED Demand Gen single-image ad using existing
    square image and logo inventory. Requires an externally existing PAUSED Demand Gen
    ad group and PAUSED campaign. This tool does not create that group or upload assets.
    Confirm the draft to apply; creation can leave paused residue and is not reversible."""
    return rails.creation_draft(rails.DraftDemandGenAdIntent(
        _change_account(customer_id), ad_group_id, headline, description, business_name,
        square_marketing_image_asset_id, logo_image_asset_id, final_url))


@mcp.tool()
def create_shared_negative_set(name: str, customer_id: str | None = None) -> dict:
    """Draft one empty owned negative keyword set, unattached and without serving effect.
    Requires the independent shared-negative edit gate. Confirm to create inventory;
    attachment and later campaign enablement are separate prerequisites. Names can race
    external creates. Offline proof does not establish provider acceptance or serving.
    Omitted customer uses the default; explicit JSON null refuses.
    See docs/m3-shared-negative-sets.md.
    """
    cid = _change_account(customer_id)
    return rails.shared_negative_creation_draft(rails.CreateSharedNegativeSetIntent(cid, name))


@mcp.tool()
def add_to_shared_set(shared_set_id: str, keywords: list[dict], customer_id: str | None = None) -> dict:
    """Draft ordered negative keyword additions to one owned shared set. Every attached
    campaign must be PAUSED standard Search. Changes affect the shared list used by all
    listed campaigns; later enablement activates exclusions. Requires the shared edit gate.
    Offline tested only. Confirm the draft to apply. See docs/m3-shared-negative-sets.md.
    """
    cid = _change_account(customer_id)
    return rails.shared_negative_add_draft(rails.AddToSharedSetIntent(cid, shared_set_id, keywords))


@mcp.tool()
def attach_shared_set(shared_set_id: str, campaign_id: str, customer_id: str | None = None) -> dict:
    """Draft one owned negative keyword set attachment to a PAUSED standard Search campaign.
    All existing attached campaigns must also be PAUSED. The enabled link has no pause control;
    future campaign enablement activates exclusions and later list edits affect all attachments.
    Empty lists are allowed; existing active or removed target links refuse. Offline tested only.
    See docs/m3-shared-negative-sets.md.
    """
    cid = _change_account(customer_id)
    return rails.shared_negative_attach_draft(rails.AttachSharedSetIntent(cid, shared_set_id, campaign_id))
