"""Safety rails for the Google Ads write server.

A DECLARATIVE write model:

  tool  --builds-->  Intent (dataclass, currency units, target ids)
  rails.compile(intent)  -->  CompiledPlan(preview, plan, fingerprint, validate_fn)
  rails.create_draft(...)  -->  Draft(plan + fingerprint + validate_fn), dry_run preview
  rails.apply_draft(id)    -->  re-validate fingerprint, then client._dispatch(plan)

Tools construct ONLY Intents; they never build plans and never hand rails a callable.
The blueprint's executable `apply_fn` closure is gone: preview AND executed ops now come
from the SAME compile pass over the same inputs, so they cannot diverge. client.py owns
every real API call and never imports this module (plans carry a `kind` discriminator so
client._dispatch switches by duck typing).
"""
import decimal
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import audit, client, settings


class RailViolation(ValueError):
    """A safety rail refused an operation. `code` is a short machine tag (see the audit
    codes below) logged on every phase-"refused" audit event alongside the reason."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class UnknownWriteOutcome(Exception):
    """The mutate RPC failed at the TRANSPORT boundary (network / GoogleAdsException),
    so the write MAY OR MAY NOT have landed. Carries the structured provider error
    (request_id, failure) instead of a flattened string, so the caller can reconcile
    account state rather than blindly retry a possibly-applied write."""

    code = "UNKNOWN_WRITE_OUTCOME"

    def __init__(self, message: str, request_id=None, failure=None, cause=None):
        super().__init__(message)
        self.request_id = request_id
        self.failure = failure
        self.cause = cause


# --- env parsers (prefix GOOGLE_ADS_; parsed at CALL time, fail-closed) ----------------

def parse_positive_float_env(name: str, default: float) -> float:
    """Shared numeric-rail parser, read at CALL time (never cached at import). Unset ->
    default. Set -> must parse to a finite, strictly positive float (zero is NOT positive),
    else fails closed with a RailViolation naming the var, the bad value, and the
    requirement. A bare float() would let nan/inf/negative through silently -- rail bypass."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise RailViolation(
            f"{name}={raw!r} is not a valid number — must be a finite positive number")
    if not math.isfinite(val) or val <= 0:
        raise RailViolation(
            f"{name}={raw!r} must be a finite positive number (got {val})")
    return val


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Strict boolean env parsing, read at CALL time. Unset -> default. Accepted tokens
    (stripped, lowercased): true/1 -> True, false/0 -> False. Anything else fails closed
    with a clear error naming the var, the bad value, and the accepted tokens."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in ("true", "1"):
        return True
    if val in ("false", "0"):
        return False
    raise RailViolation(
        f"{name}={raw!r} is not a valid boolean — accepted tokens: true/false/1/0 (case-insensitive)")


def max_daily_budget() -> float:
    # Cap in the account's currency (Google accounts are not all USD). Default 1000.
    return parse_positive_float_env("GOOGLE_ADS_MAX_DAILY_BUDGET", 1000)


def max_cpc() -> float:
    # Cap in the account's currency. Default 50.
    return parse_positive_float_env("GOOGLE_ADS_MAX_CPC", 50)


def max_target_cpa() -> float:
    # Separate cap for target CPA so raising it for realistic tCPA values does not silently
    # raise the manual CPC ceiling. Unset -> falls back to GOOGLE_ADS_MAX_CPC.
    return parse_positive_float_env("GOOGLE_ADS_MAX_TARGET_CPA", max_cpc())


def draft_ttl_seconds() -> float:
    # A draft previews the account at draft time; past this age the account may have moved
    # underneath it, so apply_draft refuses a stale preview. Default 3600s.
    return parse_positive_float_env("GOOGLE_ADS_DRAFT_TTL_SECONDS", 3600)


def allow_shared_budget_edit() -> bool:
    return parse_bool_env("GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT", False)


def allow_portfolio_edit() -> bool:
    return parse_bool_env("GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT", False)


def allow_conversion_goal_edit() -> bool:
    return parse_bool_env("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT", False)


def check_remove_entity_enabled() -> None:
    """Permanent removal cannot be undone from the UI, so it sits behind its own opt-in like
    the other blast-radius writes. Checked at compile AND apply."""
    if not parse_bool_env("GOOGLE_ADS_ALLOW_REMOVE_ENTITY", False):
        raise RailViolation(
            "remove_entity is disabled: set GOOGLE_ADS_ALLOW_REMOVE_ENTITY=true to allow "
            "permanent removal (in addition to GOOGLE_ADS_ENABLE_WRITES)",
            code="REMOVE_DISABLED")


def check_writes_enabled() -> None:
    """Belt-and-suspenders gate: called from BOTH create_draft and apply_draft, so a draft
    created while writes were enabled cannot be applied after the flag is cleared. Off by
    default -- strangers get a read-only server out of the box."""
    if not parse_bool_env("GOOGLE_ADS_ENABLE_WRITES", False):
        raise RailViolation(
            "mutating tools are disabled: set GOOGLE_ADS_ENABLE_WRITES=true to enable writes",
            code="WRITES_DISABLED")


# --- customer-id allowlist -------------------------------------------------------------

def _normalize_customer_id(raw) -> str:
    """Strip dashes/whitespace/any non-digit, so '123-456-7890' == '1234567890'."""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def _default_customer_id() -> str | None:
    raw = os.environ.get("GOOGLE_ADS_CUSTOMER_ID")
    norm = _normalize_customer_id(raw) if raw else ""
    return norm or None


def _parse_id_list(name: str) -> set[str] | None:
    """Comma-separated id list env -> normalized set, or None when unset/blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    ids = {_normalize_customer_id(part) for part in raw.split(",")}
    ids.discard("")
    return ids


def _allowlist(mode: str) -> set[str]:
    env = "GOOGLE_ADS_WRITE_CUSTOMER_IDS" if mode == "write" else "GOOGLE_ADS_READ_CUSTOMER_IDS"
    ids = _parse_id_list(env)
    if ids is None:
        default = _default_customer_id()
        return {default} if default else set()
    return ids


def read_customer_ids() -> set[str]:
    """The READ allowlist as normalized ids. client.preflight() reads this to prove every
    read-authorized account descends from the configured login manager (function-local
    import there — client never imports rails at load time)."""
    return _allowlist("read")


def check_customer_allowlisted(customer_id, mode: str) -> None:
    """Refuse a customer id not on the mode's ("read"|"write") allowlist.

    Unset lists => the single default account GOOGLE_ADS_CUSTOMER_ID only. WRITE must be a
    SUBSET of READ -- a write id that is not also a read id is a configuration error (you
    cannot write an account you cannot read), which fails closed here."""
    read = _allowlist("read")
    write = _allowlist("write")
    if not write.issubset(read):
        stray = sorted(write - read)
        raise RailViolation(
            f"config error: GOOGLE_ADS_WRITE_CUSTOMER_IDS {stray} not in the read allowlist "
            "— a write id must also be a read id", code="NOT_ALLOWLISTED")
    allow = write if mode == "write" else read
    cid = _normalize_customer_id(customer_id)
    if cid not in allow:
        raise RailViolation(
            f"customer_id {customer_id!r} is not allowlisted for {mode} "
            f"(set GOOGLE_ADS_{mode.upper()}_CUSTOMER_IDS or GOOGLE_ADS_CUSTOMER_ID)",
            code="NOT_ALLOWLISTED")


# --- bidding strategy classification (5 classes; enum-exhaustiveness test pins it) -----
# ENHANCED_CPC is POLICY_DENIED (fails closed) ON PURPOSE — it differs from the Microsoft
# allowlist, which permitted EnhancedCpc. Google retired eCPC in 2025; a bid/adjustment
# write under it is not owner-allowed here.
SMART_BIDDING = frozenset({
    "TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"})
SUPPORTED_MANUAL = frozenset({"MANUAL_CPC", "MANUAL_CPM", "MANUAL_CPV"})
POLICY_DENIED_BIDDING = frozenset({
    "ENHANCED_CPC", "FIXED_CPM", "MANUAL_CPA", "TARGET_SPEND", "TARGET_IMPRESSION_SHARE",
    "PERCENT_CPC", "COMMISSION", "TARGET_CPM", "TARGET_CPC", "TARGET_CPV",
    "FIXED_SHARE_OF_VOICE", "PAGE_ONE_PROMOTED", "TARGET_OUTRANK_SHARE"})
# Known non-strategy sentinels that stay UNRECOGNIZED forever and never need re-triage.
# The brief names UNKNOWN/UNSPECIFIED; google-ads v25 also ships INVALID (same category:
# a proto sentinel, not a real bidding strategy), so it is included here — otherwise the
# exhaustiveness test would flag a value the classification table already routes to
# UNRECOGNIZED. A genuinely NEW enum value is NOT in this set, so it still fails the test.
UNRECOGNIZED_SENTINELS = frozenset({"UNKNOWN", "UNSPECIFIED", "INVALID"})

_BID_CLASS_DETAIL = {
    "SMART": "is Smart Bidding — Google's optimizer sets the bids, so manual bid/adjustment "
             "writes do not apply",
    "POLICY_DENIED": "is not an owner-allowed manual bidding strategy (fails closed)",
    "UNRECOGNIZED": "is not a recognized bidding strategy, so this fails closed rather than "
                    "assuming it is safe",
    "UNREADABLE": "could not be determined, so this fails closed rather than assuming it is safe",
}


def classify_bidding_strategy(name: str):
    """Map a BiddingStrategyType enum NAME to (class, audit_code).

    Classes: SMART / SUPPORTED_MANUAL / POLICY_DENIED / UNRECOGNIZED / UNREADABLE.
    Only SUPPORTED_MANUAL is permitted for bid/adjustment writes (code None). A missing/
    empty name means the row was unreadable (UNREADABLE); any string not in the first three
    sets — including the proto sentinels and any future value — is UNRECOGNIZED."""
    if not name:
        return ("UNREADABLE", "BID_UNREADABLE")
    if name in SMART_BIDDING:
        return ("SMART", "BID_SMART")
    if name in SUPPORTED_MANUAL:
        return ("SUPPORTED_MANUAL", None)
    if name in POLICY_DENIED_BIDDING:
        return ("POLICY_DENIED", "BID_POLICY")
    return ("UNRECOGNIZED", "BID_UNRECOGNIZED")


def check_bid_write_allowed(strategy_name: str, subject: str) -> None:
    """Permit a fixed-bid / %-adjustment write ONLY under SUPPORTED_MANUAL; every other
    class raises RailViolation with the class's own audit code and a distinct message.
    Not wired to any tool yet (Smart Bidding campaigns ignore bid adjustments)."""
    cls, code = classify_bidding_strategy(strategy_name)
    if cls == "SUPPORTED_MANUAL":
        return
    raise RailViolation(
        f"{subject} rejected: effective strategy {strategy_name!r} {_BID_CLASS_DETAIL[cls]}. "
        "Allowed levers: pause/enable, schedule windows, tCPA, budget.", code=code)


# --- bid adjustments: public PERCENT input -> API multiplier ---------------------------
MIN_BID_ADJUSTMENT_PCT = -90
MAX_BID_ADJUSTMENT_PCT = 900


def bid_adjustment_pct_to_multiplier(pct) -> float:
    """Public input is a PERCENT in [-90, 900]; the API wants a multiplier in [0.1, 10.0].
    Convert ONCE, here, when the plan is built: -90->0.1, 0->1.0, 100->2.0, 900->10.0."""
    if isinstance(pct, bool):
        raise RailViolation(f"bid adjustment {pct!r} is not a valid percent")
    if not (MIN_BID_ADJUSTMENT_PCT <= pct <= MAX_BID_ADJUSTMENT_PCT):
        raise RailViolation(
            f"bid adjustment {pct}% is out of range "
            f"[{MIN_BID_ADJUSTMENT_PCT}, {MAX_BID_ADJUSTMENT_PCT}]")
    # Decimal so -90 -> 0.1 exactly (float 1 + -90/100 lands on 0.09999999999999998).
    return float(decimal.Decimal(1) + decimal.Decimal(str(pct)) / decimal.Decimal(100))


# --- amount / budget / content rails ---------------------------------------------------

def _check_valid_amount(amount, label: str) -> None:
    """Decimal-friendly guard: reject bool, non-number, non-finite (nan/inf), non-positive
    — before any cap comparison. bool is an int subclass, so it is caught explicitly."""
    if isinstance(amount, bool):
        raise RailViolation(f"{label} {amount!r} is not a valid number")
    if isinstance(amount, decimal.Decimal):
        if not amount.is_finite() or amount <= 0:
            raise RailViolation(f"{label} {amount} must be a finite positive number")
        return
    if not isinstance(amount, (int, float)):
        raise RailViolation(f"{label} {amount!r} is not a valid number")
    if not math.isfinite(amount) or amount <= 0:
        raise RailViolation(f"{label} {amount} must be a finite positive number")


def check_budget(amount) -> None:
    _check_valid_amount(amount, "daily budget")
    if amount > max_daily_budget():
        raise RailViolation(
            f"daily budget {amount} exceeds cap {max_daily_budget()} (GOOGLE_ADS_MAX_DAILY_BUDGET)",
            code="CAP_EXCEEDED")


def check_bid(amount) -> None:
    _check_valid_amount(amount, "bid")
    if amount > max_cpc():
        raise RailViolation(
            f"bid {amount} exceeds cap {max_cpc()} (GOOGLE_ADS_MAX_CPC)",
            code="CAP_EXCEEDED")


def check_target_cpa(amount) -> None:
    _check_valid_amount(amount, "target CPA")
    if amount > max_target_cpa():
        raise RailViolation(
            f"target CPA {amount} exceeds cap {max_target_cpa()} "
            "(GOOGLE_ADS_MAX_TARGET_CPA, falls back to GOOGLE_ADS_MAX_CPC)",
            code="CAP_EXCEEDED")


def check_shared_budget(budget_info: dict) -> None:
    """Refuse editing an explicitly-shared budget (one campaign's change hits every other
    campaign on that budget) unless GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT is true."""
    if budget_info.get("explicitly_shared") is True and not allow_shared_budget_edit():
        raise RailViolation(
            "budget is explicitly shared across campaigns — editing it changes every campaign "
            "on it. Set GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT=true to allow.",
            code="SHARED_BUDGET")


def check_content(texts) -> None:
    for t in texts:
        low = (t or "").lower()
        for term in settings.blocked_terms():
            if term in low:
                raise RailViolation(
                    f"blocked term '{term}' in '{t}' — see blocked_terms in {settings.config_path()}")


# --- MutationPlan variants (two, mutually exclusive; carry `kind` for client._dispatch) -

@dataclass(frozen=True)
class MutationOp:
    service: str
    operation: dict
    update_mask: list[str] | None


def safe_create_operation(service, fields):
    """Positive keyword additions start paused; serving constraints keep their schema."""
    import copy
    values = copy.deepcopy(fields)
    if service in {"CampaignService", "AdGroupService", "AdGroupAdService",
                   "CampaignAssetService", "AdGroupAssetService", "AssetGroupService", "AssetGroupAssetService"} or (
            service == "AdGroupCriterionService" and "keyword" in values and not values.get("negative", False)) or (
            service == "CampaignCriterionService" and "user_list" in values):
        values["status"] = "PAUSED"
    return MutationOp(service, {"create": values}, None)


@dataclass(frozen=True)
class EntityMutationPlan:
    """Compiles (in client, task B3) to exactly ONE atomic GoogleAdsService.mutate with
    partial_failure=False. Exactly ONE mutate_customer_id, immutable (frozen)."""
    mutate_customer_id: str
    operations: list
    validate_only_supported: bool
    kind: str = "entity"
    post_checks: list = field(default_factory=list)


@dataclass(frozen=True)
class RecommendationActionPlan:
    """One bounded recommendation action with exact apply amount and saved-state checks."""
    mutate_customer_id: str
    rpc: str  # "apply" | "dismiss"
    recommendation_resource_name: str
    kind: str = "recommendation"
    new_budget_amount_micros: int | None = None
    post_checks: list = field(default_factory=list)


def _plan_customer_id(plan):
    return getattr(plan, "mutate_customer_id", None)


def _plan_operation_count(plan):
    ops = getattr(plan, "operations", None)
    if ops is not None:
        return len(ops)
    if getattr(plan, "kind", None) == "recommendation":
        return 1
    return None


def _plan_canonical(plan) -> dict:
    """Stable dict view of a plan for digesting — EXACT operation values, no redaction."""
    kind = getattr(plan, "kind", None)
    if kind == "entity":
        return {
            "kind": "entity",
            "mutate_customer_id": plan.mutate_customer_id,
            "validate_only_supported": plan.validate_only_supported,
            "post_checks": plan.post_checks,
            "operations": [
                {"service": op.service, "operation": op.operation, "update_mask": op.update_mask}
                for op in plan.operations
            ],
        }
    if kind == "recommendation":
        return {
            "kind": "recommendation",
            "mutate_customer_id": plan.mutate_customer_id,
            "rpc": plan.rpc,
            "recommendation_resource_name": plan.recommendation_resource_name,
            "new_budget_amount_micros": plan.new_budget_amount_micros,
            "post_checks": plan.post_checks,
        }
    raise RailViolation(f"cannot digest unknown plan kind {kind!r}")


def plan_digest(plan) -> str:
    """sha256 hex over the CANONICAL bytes of the plan (service, mutate_customer_id, update
    masks, EXACT operation values incl. micros), computed BEFORE any redaction. The
    redacted human preview carries this digest so preview and executed ops are tied to one
    fingerprint."""
    blob = json.dumps(_plan_canonical(plan), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _resource_customer_id(resource_name) -> str | None:
    """Extract the customer segment from a Google resource name
    ('customers/{cid}/campaignBudgets/{id}' -> '{cid}')."""
    parts = str(resource_name).split("/")
    if len(parts) >= 2 and parts[0] == "customers":
        return _normalize_customer_id(parts[1])
    return None


# --- Intents + the compiler ------------------------------------------------------------

@dataclass(frozen=True)
class UpdateCampaignBudgetIntent:
    customer_id: str
    campaign_id: str
    new_daily_budget: object  # str | Decimal, in currency UNITS (not micros)


@dataclass
class CompiledPlan:
    """What one compile pass yields. The brief frames compile as preview+plan; the
    fingerprint and validate_fn are produced in the SAME pass (from the same reads) so they
    cannot diverge from the preview, and the thin draft helper forwards all four to
    create_draft."""
    preview: dict
    plan: object
    fingerprint: dict
    validate_fn: Callable[[], None] | None


@dataclass(frozen=True)
class SetEntityStatusIntent:
    customer_id: str
    entity_type: str   # "campaign" | "ad_group"
    entity_id: str
    new_status: str    # "PAUSED" | "ENABLED"


@dataclass(frozen=True)
class RemoveEntityIntent:
    customer_id: str
    entity_type: str
    entity_id: str
    ad_group_id: str | None = None


_SET_STATUS_SERVICE = {"campaign": "CampaignService", "ad_group": "AdGroupService"}
_ALLOWED_NEW_STATUS = {"PAUSED", "ENABLED"}


def compile(intent) -> CompiledPlan:
    """Turn a typed Intent into a CompiledPlan(preview, plan, fingerprint, validate_fn).
    Dispatches on the intent's type."""
    if isinstance(intent, DraftDemandGenAdIntent):
        compiled = _compile_demand_gen_ad(intent)
    elif isinstance(intent, RemoveAssetGroupAssetIntent):
        compiled = _compile_asset_group_asset_remove(intent)
    elif isinstance(intent, AddAssetGroupAssetsIntent):
        compiled = _compile_asset_group_asset_add(intent)
    elif isinstance(intent, SetListingGroupFilterIntent):
        return _compile_listing_filter(intent)
    elif isinstance(intent, UpdateAssetGroupIntent):
        compiled = _compile_asset_group_update(intent)
    elif isinstance(intent, CreateAssetGroupIntent):
        compiled = _compile_asset_group_creation(intent)
    elif isinstance(intent, CreatePMaxCampaignIntent):
        compiled = _compile_pmax_creation(intent)
    elif isinstance(intent, CreateDemandGenCampaignIntent):
        compiled = _compile_demand_gen_creation(intent)
    elif isinstance(intent, AttachSharedSetIntent):
        compiled = _compile_shared_negative_attach(intent)
    elif isinstance(intent, AddToSharedSetIntent):
        compiled = _compile_shared_negative_add(intent)
    elif isinstance(intent, CreateSharedNegativeSetIntent):
        compiled = _compile_shared_negative_creation(intent)
    elif isinstance(intent, CreatePortfolioBiddingStrategyIntent):
        compiled = _compile_portfolio_creation(intent)
    elif isinstance(intent, DismissRecommendationIntent):
        return _compile_dismiss_recommendation(intent)
    elif isinstance(intent, SetConversionActionPrimaryStatusIntent):
        compiled = _compile_conversion_primary_status(intent)
    elif isinstance(intent, CreateConversionActionIntent):
        compiled = _compile_conversion_action(intent)
    elif isinstance(intent, AddAudienceTargetingIntent):
        compiled = _compile_audience_targeting(intent)
    elif isinstance(intent, CreateCustomAudienceIntent):
        compiled = _compile_custom_audience(intent)
    elif isinstance(intent, UploadTextAssetIntent):
        compiled = _compile_text_upload(intent)
    elif isinstance(intent, UploadImageAssetIntent):
        compiled = _compile_image_upload(intent)
    elif isinstance(intent, RemoveExtensionIntent):
        compiled = _compile_remove_extension(intent)
    elif isinstance(intent, CreateStructuredSnippetsIntent):
        compiled = _compile_structured_snippets(intent)
    elif isinstance(intent, CreateCalloutsIntent):
        compiled = _compile_callouts(intent)
    elif isinstance(intent, DraftSitelinksIntent):
        compiled = _compile_sitelinks(intent)
    elif isinstance(intent, DraftResponsiveSearchAdIntent):
        compiled = _compile_responsive_search_ad(intent)
    elif isinstance(intent, (DraftCampaignIntent, CreateAdGroupIntent)):
        compiled = _compile_creation(intent)
    elif isinstance(intent, CriteriaIntent):
        compiled = _compile_criteria(intent)
    elif isinstance(intent, (UpdateCampaignIntent, UpdateAdGroupIntent)):
        compiled = _compile_update(intent)
    elif isinstance(intent, UpdateCampaignBudgetIntent):
        compiled = _compile_update_campaign_budget(intent)
    elif isinstance(intent, SetEntityStatusIntent):
        compiled = _compile_set_entity_status(intent)
    elif isinstance(intent, RemoveEntityIntent):
        compiled = _compile_remove_entity(intent)
    else:
        raise RailViolation(f"no compile branch for intent {type(intent).__name__}")
    client.validate_mutation_plan(compiled.plan)
    return compiled


@dataclass(frozen=True)
class UpdateCampaignIntent:
    customer_id: str
    campaign_id: str
    daily_budget: object = None
    status: str | None = None
    name: str | None = None
    target_cpa: object = None
    target_roas: object = None
    clear_target_cpa: bool = False
    clear_target_roas: bool = False


@dataclass(frozen=True)
class UpdateAdGroupIntent:
    customer_id: str
    ad_group_id: str
    status: str | None = None
    name: str | None = None
    target_cpa: object = None
    cpc_bid: object = None
    clear_target_cpa: bool = False


def _amount(value, checker):
    if isinstance(value, bool):
        raise RailViolation("a boolean is not an amount", code="BAD_AMOUNT")
    try:
        amount = decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        raise RailViolation("amount must be a number", code="BAD_AMOUNT") from None
    checker(amount)
    return amount


def _checked_money_micros(value):
    """Keep conversion in client.py; surface expected failures as auditable refusals."""
    try:
        return client.to_micros(value)
    except (ValueError, decimal.DecimalException) as exc:
        raise RailViolation(str(exc), code="BAD_AMOUNT") from exc


def check_roas(value):
    _check_valid_amount(value, "target ROAS ratio")
    if not decimal.Decimal("0.01") <= value <= 1000:
        raise RailViolation("target ROAS must be a ratio from 0.01 to 1000", code="BAD_ROAS")


def check_portfolio_edit(strategy, customer_id, campaign_id):
    """Require reconciled, fully writable scope and return it for preview/fingerprint."""
    try:
        if not allow_portfolio_edit():
            raise RailViolation("portfolio editing requires GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT=true")
        owner, sid = strategy["owner_customer_id"], strategy["strategy_id"]
        check_customer_allowlisted(owner, "write")
        scope = client.portfolio_state(owner, sid, read_customer_ids())
        attachments = scope["attachments"]
        count = int(scope["strategy"]["non_removed_campaign_count"])
        identities = {(a["customer_id"], str(a["id"])) for a in attachments}
        if count != len(attachments) or len(identities) != count:
            raise RailViolation("portfolio attachment count does not reconcile")
        if (customer_id, str(campaign_id)) not in identities:
            raise RailViolation("requested campaign is absent from portfolio scope")
        for attached in attachments:
            check_customer_allowlisted(attached["customer_id"], "write")
        if scope["strategy"]["type"] != strategy["type"]:
            raise RailViolation("portfolio strategy type does not match campaign")
        expected_name = client.bidding_strategy_path(owner, sid)
        if scope["strategy"]["resource_name"] != expected_name:
            raise RailViolation("portfolio owner resource does not match")
        return scope
    except Exception as exc:
        raise RailViolation(f"portfolio scope refused: {exc}", code="PORTFOLIO_SCOPE") from exc


_TARGET_PATHS = {
    "TARGET_CPA": ("target_cpa", "target_cpa_micros"),
    "MAXIMIZE_CONVERSIONS": ("maximize_conversions", "target_cpa_micros"),
    "TARGET_ROAS": ("target_roas", "target_roas"),
    "MAXIMIZE_CONVERSION_VALUE": ("maximize_conversion_value", "target_roas"),
}
_TARGET_MASKS = {
    "TARGET_CPA": ["target_cpa.target_cpa_micros"],
    "MAXIMIZE_CONVERSIONS": ["maximize_conversions.target_cpa_micros"],
    "TARGET_ROAS": ["target_roas.target_roas"],
    "MAXIMIZE_CONVERSION_VALUE": ["maximize_conversion_value.target_roas"],
}


def _compile_update(intent):
    check_writes_enabled()
    cid = _normalize_customer_id(intent.customer_id)
    check_customer_allowlisted(cid, "write")
    campaign = isinstance(intent, UpdateCampaignIntent)
    entity_type = "campaign" if campaign else "ad_group"
    entity_id = str(intent.campaign_id if campaign else intent.ad_group_id)
    cpa = intent.target_cpa is not None or intent.clear_target_cpa
    roas = campaign and (intent.target_roas is not None or intent.clear_target_roas)
    cpc = not campaign and intent.cpc_bid is not None
    for key in ("clear_target_cpa", "clear_target_roas"):
        flag = getattr(intent, key, False)
        if type(flag) is not bool:
            raise RailViolation(f"{key} must be true or false", code="BAD_CLEAR")
        if flag and getattr(intent, key.removeprefix("clear_")) is not None:
            raise RailViolation("cannot set and clear the same target", code="CONTRADICTORY_FIELDS")
    if sum(bool(x) for x in (cpa, roas, cpc)) > 1:
        raise RailViolation("incompatible bidding fields", code="CONTRADICTORY_FIELDS")
    has_budget = campaign and intent.daily_budget is not None
    if not any((intent.status is not None, intent.name is not None, cpa, roas, cpc, has_budget)):
        raise RailViolation("no changes requested", code="EMPTY_UPDATE")
    # Preserve the original budget-only interface and its established read/preview shape.
    if has_budget and not any((intent.status is not None, intent.name is not None, cpa, roas)):
        _amount(intent.daily_budget, check_budget)
        return _compile_update_campaign_budget(
            UpdateCampaignBudgetIntent(cid, entity_id, intent.daily_budget))
    current = client.update_state(cid, entity_type, entity_id)
    if current.get("status") == "REMOVED":
        raise RailViolation("cannot edit a removed entity", code="ENTITY_REMOVED")
    if current.get("status") not in _ALLOWED_NEW_STATUS:
        raise RailViolation("entity status unreadable", code="NOT_FOUND")
    update = {"resource_name": current["resource_name"]}
    masks, operations, checks = [], [], []
    fingerprint = {"current": current}
    if intent.status is not None:
        if intent.status not in _ALLOWED_NEW_STATUS:
            raise RailViolation("status must be PAUSED or ENABLED", code="BAD_STATUS")
        update["status"] = intent.status
        masks += ["status"]
    if intent.name is not None:
        if not isinstance(intent.name, str) or not intent.name.strip():
            raise RailViolation("name must be nonempty", code="BAD_NAME")
        check_content([intent.name])
        update["name"] = intent.name
        masks += ["name"]
    owner = cid
    scope = None
    if cpa or roas or cpc:
        campaign_id = entity_id if campaign else current["campaign"].rsplit("/", 1)[-1]
        strategy = client.effective_strategy(cid, campaign_id)
        fingerprint["strategy"] = strategy
        stype = strategy["type"]
        if campaign:
            compatible = ({"TARGET_CPA", "MAXIMIZE_CONVERSIONS"} if cpa
                          else {"TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"})
            if stype not in compatible:
                raise RailViolation("target does not match effective strategy", code="BID_STRATEGY")
            clear = intent.clear_target_cpa if cpa else intent.clear_target_roas
            if clear and stype in {"TARGET_CPA", "TARGET_ROAS"}:
                raise RailViolation("this strategy requires a target; cannot clear it", code="BAD_CLEAR")
            if clear and current.get("advertising_channel_type") == "PERFORMANCE_MAX":
                # Removing a Performance Max target uncaps spend. Raise it in steps instead;
                # this tool never clears it.
                raise RailViolation(
                    "refusing to clear the target on a Performance Max campaign: this uncaps "
                    "spend. Raise the target in steps instead.", code="PMAX_TARGET_CLEAR")
            value = None if clear else (
                _checked_money_micros(_amount(intent.target_cpa, check_target_cpa)) if cpa
                else float(_amount(intent.target_roas, check_roas)))
            target_update = update
            target_masks = masks
            if strategy.get("portfolio_resource_name"):
                scope = check_portfolio_edit(strategy, cid, campaign_id)
                fingerprint["portfolio_scope"] = scope
                owner = strategy["owner_customer_id"]
                target_update = {"resource_name": client.bidding_strategy_path(
                    owner, strategy["strategy_id"])}
                target_masks = []
            parent, leaf = _TARGET_PATHS[stype]
            target_update[parent] = {} if clear else {leaf: value}
            target_masks += _TARGET_MASKS[stype]
            if scope is not None:
                operations.append(MutationOp("BiddingStrategyService", {"update": target_update},
                                             target_masks))
        else:
            if cpc:
                check_bid_write_allowed(stype, "ad group CPC")
                if stype != "MANUAL_CPC" or strategy.get("portfolio_resource_name"):
                    raise RailViolation("CPC requires standard MANUAL_CPC", code="BID_STRATEGY")
                value = _checked_money_micros(_amount(intent.cpc_bid, check_bid))
                update["cpc_bid_micros"] = value
                masks += ["cpc_bid_micros"]
                expected = {"effective_cpc_bid_micros": str(value)}
            else:
                if stype not in {"TARGET_CPA", "MAXIMIZE_CONVERSIONS"}:
                    raise RailViolation("ad group CPA requires a compatible CPA strategy",
                                        code="BID_STRATEGY")
                parent_state = client.update_state(cid, "campaign", campaign_id)
                if strategy.get("portfolio_resource_name"):
                    # No shared mutation: only read the inherited target from accessible strategy.
                    parent_state = client.accessible_target_state(cid, strategy["strategy_id"])
                fingerprint["inherited_target"] = parent_state
                parent, leaf = _TARGET_PATHS[stype]
                inherited = parent_state.get(parent, {}).get(leaf)
                if not inherited or int(inherited) <= 0:
                    raise RailViolation("campaign strategy has no effective CPA target",
                                        code="BID_STRATEGY")
                if intent.clear_target_cpa:
                    value = str(inherited)
                else:
                    value = _checked_money_micros(_amount(intent.target_cpa, check_target_cpa))
                    update["target_cpa_micros"] = value
                masks += ["target_cpa_micros"]
                expected = {"effective_target_cpa_micros": str(value),
                            "effective_target_cpa_source": (
                                "CAMPAIGN_BIDDING_STRATEGY" if intent.clear_target_cpa else "AD_GROUP")}
            checks.append({"customer_id": cid, "entity_type": "ad_group", "entity_id": entity_id,
                           "expected": expected})
    if masks:
        operations.insert(0, MutationOp(_SET_STATUS_SERVICE[entity_type], {"update": update}, masks))
    if has_budget:
        _amount(intent.daily_budget, check_budget)
        budget = _compile_update_campaign_budget(
            UpdateCampaignBudgetIntent(cid, entity_id, intent.daily_budget))
        operations.extend(budget.plan.operations)
        fingerprint["budget"] = budget.fingerprint
    for op in operations:
        if _resource_customer_id(op.operation["update"]["resource_name"]) != owner:
            raise RailViolation("combined changes need different account owners; cannot split",
                                code="CROSS_CUSTOMER")
    plan = EntityMutationPlan(owner, operations, True, post_checks=checks)
    preview = {"tool": f"update_{entity_type}", "customer_id": cid, "entity_id": entity_id,
               "current": current, "operations": _plan_canonical(plan)["operations"],
               "digest": plan_digest(plan),
               "requested_changes": {key: str(value) for key, value in vars(intent).items()
                                     if key not in {"customer_id", "campaign_id", "ad_group_id"}
                                     and value is not None and value is not False},
               "units": "daily_budget, target_cpa and cpc_bid are account currency; "
                        "target_roas is a ratio (2 means 200%)"}
    if has_budget and "affected_campaigns" in budget.preview:
        preview["budget_affected_campaigns"] = budget.preview["affected_campaigns"]
    if scope is not None:
        preview["affected_campaigns"] = scope["attachments"]
    def validate_fn():
        fresh = _compile_update(intent)
        if fresh.fingerprint != fingerprint or plan_digest(fresh.plan) != plan_digest(plan):
            raise RailViolation("account changed since preview; create a fresh draft", code="STATE_DRIFT")
    return CompiledPlan(preview, plan, fingerprint, validate_fn)


def update_draft(intent):
    tool = "update_campaign" if isinstance(intent, UpdateCampaignIntent) else "update_ad_group"
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused(tool, exc)
        raise
    return create_draft(tool, compiled.preview, compiled.plan, compiled.fingerprint,
                        compiled.validate_fn)


def _compile_update_campaign_budget(intent: UpdateCampaignBudgetIntent, expected_info=None) -> CompiledPlan:
    customer_id = intent.customer_id
    campaign_id = intent.campaign_id
    new_budget = intent.new_daily_budget

    # 1-2: writes gate + write allowlist
    check_writes_enabled()
    check_customer_allowlisted(customer_id, "write")

    # 3: read current budget
    info = client.campaign_budget(customer_id, campaign_id)
    if expected_info is not None and any(info.get(k) != v for k, v in expected_info.items()):
        raise RailViolation('campaign budget observations disagree', code='STATE_DRIFT')

    if info.get("campaign_status") == "REMOVED":
        raise RailViolation("cannot edit a removed campaign", code="ENTITY_REMOVED")

    # 4: v1 scope refusals
    period = info.get("period")
    if period != "DAILY":
        raise RailViolation(
            f"budget period is {period!r}, not DAILY — v1 only edits daily budgets",
            code="NON_DAILY_BUDGET")
    if info.get("aligned_bidding_strategy_id"):
        raise RailViolation(
            "budget is aligned to a portfolio bidding strategy — v1 does not edit aligned "
            "budgets", code="ALIGNED_BUDGET")

    # 5-6: shared-budget rail + cap check (currency units, BEFORE micros)
    check_shared_budget(info)
    check_budget(decimal.Decimal(str(new_budget)))

    # 7: money -> micros (lives only in client)
    micros = _checked_money_micros(new_budget)

    # 8: build the plan; refuse a resource name from a different customer
    budget_resource_name = info["budget_resource_name"]
    seg = _resource_customer_id(budget_resource_name)
    if seg != _normalize_customer_id(customer_id):
        raise RailViolation(
            f"budget resource {budget_resource_name!r} belongs to customer {seg}, not "
            f"{customer_id} — cross-customer intents are refused, not split", code="CROSS_CUSTOMER")
    op = MutationOp(
        service="CampaignBudgetService",
        operation={"update": {"resource_name": budget_resource_name, "amount_micros": micros}},
        update_mask=["amount_micros"])
    plan = EntityMutationPlan(
        mutate_customer_id=customer_id, operations=[op], validate_only_supported=True)

    attachments = None
    if info.get("explicitly_shared"):
        attachments = client.shared_budget_attachments(
            customer_id, campaign_id, budget_resource_name, info.get("reference_count"))

    # 9: fingerprint of the state this preview describes
    fingerprint = {
        "budget_resource_name": budget_resource_name,
        "amount_micros_current": client.to_micros(info["amount"]),
        "explicitly_shared": info.get("explicitly_shared"),
        "reference_count": info.get("reference_count"),
        "attachments": attachments,
        "period": period,
    }

    # 10: redacted human preview (carries the digest of the un-redacted plan)
    digest = plan_digest(plan)
    preview = {
        "tool": "update_campaign",
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "budget_resource_name": budget_resource_name,
        "current_daily_budget": info["amount"],
        "new_daily_budget": str(new_budget),
        "explicitly_shared": info.get("explicitly_shared"),
        "digest": digest,
    }
    if attachments is not None:
        preview["affected_campaigns"] = attachments
    currency = info.get("currency")  # omit if unknown (B2 campaign_budget has none yet)
    if currency:
        preview["currency"] = currency

    # 11: validate_fn — re-run steps 2,4,5,6 against a FRESH read and compare fingerprints
    def validate_fn() -> None:
        check_customer_allowlisted(customer_id, "write")
        fresh = client.campaign_budget(customer_id, campaign_id)
        if fresh.get("campaign_status") == "REMOVED":
            raise RailViolation("campaign was removed since draft", code="ENTITY_REMOVED")
        if fresh.get("period") != "DAILY":
            raise RailViolation(
                "budget is no longer a daily budget since draft — refusing", code="NON_DAILY_BUDGET")
        if fresh.get("aligned_bidding_strategy_id"):
            raise RailViolation(
                "budget became aligned to a bidding strategy since draft — refusing",
                code="ALIGNED_BUDGET")
        check_shared_budget(fresh)
        check_budget(decimal.Decimal(str(new_budget)))
        fresh_attachments = None
        if fresh.get("explicitly_shared"):
            fresh_attachments = client.shared_budget_attachments(
                customer_id, campaign_id, fresh["budget_resource_name"], fresh.get("reference_count"))
        fresh_fp = {
            "budget_resource_name": fresh["budget_resource_name"],
            "amount_micros_current": client.to_micros(fresh["amount"]),
            "explicitly_shared": fresh.get("explicitly_shared"),
            "reference_count": fresh.get("reference_count"),
            "attachments": fresh_attachments,
            "period": fresh.get("period"),
        }
        if fresh_fp != fingerprint:
            raise RailViolation(
                "account state drifted since the draft was previewed (the budget changed "
                "underneath it) — re-draft rather than apply a stale preview")

    return CompiledPlan(preview=preview, plan=plan, fingerprint=fingerprint, validate_fn=validate_fn)


def update_campaign_budget_draft(customer_id, campaign_id, new_daily_budget) -> dict:
    """Thin tool-facing helper (stands in for the future update_campaign budget tool):
    build the Intent, compile, and create the draft. On a rail refusal, audit it (phase
    "refused") and re-raise."""
    intent = UpdateCampaignBudgetIntent(
        customer_id=customer_id, campaign_id=campaign_id, new_daily_budget=new_daily_budget)
    try:
        compiled = compile(intent)
    except RailViolation as e:
        _audit_refused("update_campaign", e)
        raise
    return create_draft(
        tool="update_campaign", preview=compiled.preview, plan=compiled.plan,
        fingerprint=compiled.fingerprint, validate_fn=compiled.validate_fn)


def _check_set_status_state(info: dict, entity_type: str, entity_id: str, new_status: str) -> None:
    """Shared exists/REMOVED/no-op refusals, run against BOTH the draft-time read and the
    apply-time (validate_fn) fresh read -- so a status that drifted into the target status,
    into REMOVED, or out of existence between draft and apply is caught by the SAME specific
    code it would get at draft time, not masked behind a generic drift message."""
    if not info["exists"]:
        raise RailViolation(
            f"{entity_type} {entity_id} not found", code="NOT_FOUND")
    if info["status"] == "REMOVED":
        raise RailViolation(
            f"{entity_type} {entity_id} is REMOVED — cannot pause/enable a removed entity",
            code="ENTITY_REMOVED")
    if info["status"] == new_status:
        raise RailViolation(
            f"{entity_type} {entity_id} is already {new_status}; nothing to change",
            code="ALREADY_IN_STATUS")


def _compile_set_entity_status(intent: SetEntityStatusIntent) -> CompiledPlan:
    customer_id = intent.customer_id
    entity_type = intent.entity_type
    entity_id = intent.entity_id
    new_status = intent.new_status

    # 1-2: writes gate + write allowlist
    check_writes_enabled()
    check_customer_allowlisted(customer_id, "write")

    # 3: validate entity_type / new_status
    if entity_type not in _SET_STATUS_SERVICE:
        raise RailViolation(
            f"unsupported entity_type {entity_type!r} — must be one of "
            f"{sorted(_SET_STATUS_SERVICE)}", code="UNSUPPORTED_ENTITY")
    if new_status not in _ALLOWED_NEW_STATUS:
        raise RailViolation(
            f"new_status {new_status!r} must be one of {sorted(_ALLOWED_NEW_STATUS)}",
            code="BAD_STATUS")

    # 4: read current status; refuse not-found / removed / no-op
    info = client.entity_status(customer_id, entity_type, entity_id)
    _check_set_status_state(info, entity_type, entity_id, new_status)

    # 5: build the plan; refuse a resource name from a different customer
    rn = info["resource_name"]
    seg = _resource_customer_id(rn)
    if seg != _normalize_customer_id(customer_id):
        raise RailViolation(
            f"{entity_type} resource {rn!r} belongs to customer {seg}, not {customer_id} — "
            "cross-customer intents are refused, not split", code="CROSS_CUSTOMER")
    op = MutationOp(
        service=_SET_STATUS_SERVICE[entity_type],
        operation={"update": {"resource_name": rn, "status": new_status}},
        update_mask=["status"])
    plan = EntityMutationPlan(
        mutate_customer_id=customer_id, operations=[op], validate_only_supported=True)

    # 6: fingerprint of the state this preview describes
    fingerprint = {"resource_name": rn, "status_current": info["status"]}

    # 7: redacted human preview (carries the digest of the un-redacted plan)
    digest = plan_digest(plan)
    tool = "pause_entity" if new_status == "PAUSED" else "enable_entity"
    preview = {
        "tool": tool,
        "customer_id": customer_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "resource_name": rn,
        "current_status": info["status"],
        "new_status": new_status,
        "digest": digest,
    }

    # 8: validate_fn -- re-run the write allowlist + status checks against a FRESH read, then
    # compare fingerprints (a drift that is not already caught above, e.g. the resource_name
    # changing while the status did not).
    def validate_fn() -> None:
        check_customer_allowlisted(customer_id, "write")
        fresh = client.entity_status(customer_id, entity_type, entity_id)
        _check_set_status_state(fresh, entity_type, entity_id, new_status)
        fresh_fp = {"resource_name": fresh["resource_name"], "status_current": fresh["status"]}
        if fresh_fp != fingerprint:
            raise RailViolation(
                "account state drifted since the draft was previewed (the status changed "
                "underneath it) — re-draft rather than apply a stale preview")

    return CompiledPlan(preview=preview, plan=plan, fingerprint=fingerprint, validate_fn=validate_fn)


def set_entity_status_draft(customer_id, entity_type, entity_id, new_status) -> dict:
    """Thin tool-facing helper for pause_entity / enable_entity: build the Intent, compile,
    and create the draft. On a rail refusal, audit it (phase "refused") and re-raise."""
    tool = "pause_entity" if new_status == "PAUSED" else "enable_entity"
    intent = SetEntityStatusIntent(
        customer_id=customer_id, entity_type=entity_type, entity_id=entity_id,
        new_status=new_status)
    try:
        compiled = compile(intent)
    except RailViolation as e:
        _audit_refused(tool, e)
        raise
    return create_draft(
        tool=tool, preview=compiled.preview, plan=compiled.plan,
        fingerprint=compiled.fingerprint, validate_fn=compiled.validate_fn)


_REMOVE_ENTITY_SERVICE = {'campaign': 'CampaignService', 'ad_group': 'AdGroupService',
                          'ad': 'AdGroupAdService'}


def _compile_remove_entity(intent: RemoveEntityIntent) -> CompiledPlan:
    check_writes_enabled()
    check_remove_entity_enabled()
    if type(intent.customer_id) is not str:
        raise RailViolation('customer_id must be a positive numeric string')
    if not re.fullmatch(r'[1-9][0-9]*', intent.customer_id):
        raise RailViolation('customer_id must be a canonical positive ASCII ID', code='BAD_ID')
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if type(intent.entity_type) is not str or intent.entity_type not in _REMOVE_ENTITY_SERVICE:
        raise RailViolation("unsupported entity_type; must be exactly campaign, ad_group, or ad",
                            code='UNSUPPORTED_ENTITY')
    if type(intent.entity_id) is not str:
        raise RailViolation('entity_id must be a positive numeric string')
    if not re.fullmatch(r'[1-9][0-9]*', intent.entity_id):
        raise RailViolation('invalid numeric entity_id: must be a canonical positive ASCII ID',
                            code='BAD_ID')
    entity_id = _strict_id(intent.entity_id)
    if intent.entity_type == 'ad':
        if type(intent.ad_group_id) is not str:
            raise RailViolation('ad_group_id is required as a positive numeric string for ad removal')
        if not re.fullmatch(r'[1-9][0-9]*', intent.ad_group_id):
            raise RailViolation('ad_group_id must be a canonical positive ASCII ID', code='BAD_ID')
        ad_group_id = _strict_id(intent.ad_group_id)
    else:
        if intent.ad_group_id is not None:
            raise RailViolation('ad_group_id is forbidden for campaign and ad_group removal')
        ad_group_id = None

    state = client.removal_entity_state(cid, intent.entity_type, entity_id, ad_group_id)
    campaign = state.get('campaign', {})
    if (campaign.get('advertising_channel_type') != 'SEARCH'
            or campaign.get('advertising_channel_sub_type') != 'UNSPECIFIED'
            or campaign.get('status') not in {'ENABLED', 'PAUSED'}
            or _resource_customer_id(campaign.get('resource_name')) != cid):
        raise RailViolation('removal parent must be a readable nonremoved standard Search campaign')
    if intent.entity_type in {'ad_group', 'ad'}:
        group = state.get('ad_group', {})
        if (group.get('type', group.get('type_')) != 'SEARCH_STANDARD'
                or group.get('status') not in {'ENABLED', 'PAUSED'}
                or group.get('campaign') != campaign.get('resource_name')
                or _resource_customer_id(group.get('resource_name')) != cid):
            raise RailViolation('removal parent must be a readable nonremoved SEARCH_STANDARD ad group')
    target_key = intent.entity_type
    target = state[target_key]
    if target.get('status') != 'PAUSED':
        raise RailViolation('remove_entity requires the selected target to be exactly PAUSED')
    if (intent.entity_type == 'ad'
            and target.get('type', target.get('type_')) != 'RESPONSIVE_SEARCH_AD'):
        raise RailViolation('remove_entity supports only a responsive search ad')
    if (intent.entity_type == 'ad'
            and target.get('ad_group') != state['ad_group'].get('resource_name')):
        raise RailViolation('selected ad identity does not match its verified parent')
    resource_name = target['resource_name']
    if intent.entity_type == 'campaign':
        expected = client.campaign_path(cid, entity_id)
    elif intent.entity_type == 'ad_group':
        expected = client.ad_group_path(cid, entity_id)
    else:
        expected = client.ad_group_ad_path(cid, ad_group_id, entity_id)
    if resource_name != expected or _resource_customer_id(resource_name) != cid:
        raise RailViolation('selected removal identity belongs to another customer or is malformed')
    parent = (None if intent.entity_type == 'campaign' else
              state['campaign']['resource_name'] if intent.entity_type == 'ad_group'
              else state['ad_group']['resource_name'])
    descriptor_type = 'ad_group_ad' if intent.entity_type == 'ad' else intent.entity_type
    check = {'removal': True, 'entity_type': descriptor_type, 'customer_id': cid,
             'resource_name': resource_name, 'parent_resource_name': parent}
    plan = EntityMutationPlan(cid, [MutationOp(_REMOVE_ENTITY_SERVICE[intent.entity_type],
                                               {'remove': resource_name}, None)], True,
                              post_checks=[check])
    digest = plan_digest(plan)
    child_keys = {'campaign': ('ad_groups', 'ads', 'campaign_criteria',
                               'ad_group_criteria', 'campaign_assets', 'ad_group_assets'),
                  'ad_group': ('ads', 'ad_group_criteria', 'ad_group_assets'),
                  'ad': ()}[intent.entity_type]
    affected = {key: {'count': len(state[key]),
                      'resource_names': sorted(item['resource_name'] for item in state[key])}
                for key in child_keys}
    preview = {
        'tool': 'remove_entity', 'customer_id': cid, 'draft': True,
        'confirmation_required': True, 'entity_type': intent.entity_type,
        'entity_id': entity_id, 'resource_name': resource_name,
        'current_status': target['status'], 'target': target,
        'campaign': state['campaign'], 'affected_children': affected,
        'budget': state.get('budget'), 'operations': _plan_canonical(plan)['operations'],
        'digest': digest,
        'warnings': [
            'Removal is permanent and cannot be undone in place.',
            'The selected target is paused; affected descendants stop being usable through the removed parent.',
            'Existing budgets, bare assets, and history are not deleted by this operation.',
        ],
    }

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft',
                                code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def remove_entity_draft(intent: RemoveEntityIntent) -> dict:
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('remove_entity', exc)
        raise
    return create_draft('remove_entity', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


# --- draft store ------------------------------------------------------------------------

@dataclass
class Draft:
    id: str
    tool: str
    preview: dict
    plan: object
    fingerprint: dict
    validate_fn: Callable[[], None] | None = None
    created_at: float = field(default_factory=time.monotonic)  # monotonic, not wall clock
    digest: str = ""


_DRAFTS: dict[str, Draft] = {}
# Serializes create_draft/apply_draft. The MCP SDK may run sync tools on a thread pool, so
# peek -> validate -> pop -> dispatch must never interleave for the same draft id.
# ponytail: one global lock (applies are rare and already slow); per-draft locks if it matters.
_DRAFT_LOCK = threading.RLock()


def _prune_expired_drafts() -> None:
    # Bounds _DRAFTS with an inline sweep under _DRAFT_LOCK; no background thread.
    # Uses monotonic time.
    # ponytail: sweeps every expired draft, so an unrelated call can sweep a different
    # stale draft first (its later apply then gets the generic "unknown" instead of
    # "expired") — no safety impact, pop-before-check still prevents double-apply.
    ttl = draft_ttl_seconds()
    now = time.monotonic()
    for k in [k for k, v in _DRAFTS.items() if now - v.created_at > ttl]:
        del _DRAFTS[k]


def _audit_common(plan) -> dict:
    return {"customer_id": _plan_customer_id(plan), "operation_count": _plan_operation_count(plan)}


def _audit_refused(tool: str, exc: RailViolation, plan=None) -> None:
    """Log a phase-"refused" event: tool, the human reason, AND the machine code. A broken
    audit log must never swallow the refusal, so a failure here goes to stderr (stdout is
    the JSON-RPC channel) and the refusal still propagates."""
    data = {"reason": str(exc), "code": getattr(exc, "code", None)}
    if plan is not None:
        data.update(_audit_common(plan))
    try:
        audit.log_event(tool, "refused", data)
    except Exception as audit_exc:
        print(f"apply_draft/create_draft: audit log failed for {tool} (refused phase): {audit_exc}",
              file=sys.stderr)


def create_draft(tool: str, preview: dict, plan, fingerprint: dict,
                 validate_fn: Callable[[], None] | None = None) -> dict:
    """Store a declarative draft and return its dry-run preview. Writes-gate runs BEFORE any
    draft/audit side effect. created_at is monotonic. The "draft" audit event carries the
    plan digest, mutate_customer_id, and operation_count."""
    with _DRAFT_LOCK:
        return _create_draft_locked(tool, preview, plan, fingerprint, validate_fn)


def _create_draft_locked(tool, preview, plan, fingerprint, validate_fn) -> dict:
    check_writes_enabled()  # before any side effect
    try:
        if (isinstance(plan, RecommendationActionPlan) or getattr(plan, 'kind', None) == 'recommendation'):
            if getattr(plan, 'rpc', None) == 'apply':
                check_apply_recommendation_enabled()
            _validate_recommendation_draft(plan, preview, fingerprint, validate_fn)
    except RailViolation as exc:
        _audit_refused(tool, exc, plan)
        raise
    _prune_expired_drafts()
    digest = preview.get("digest") or plan_digest(plan)
    d = Draft(id=uuid.uuid4().hex[:12], tool=tool, preview=preview, plan=plan,
              fingerprint=fingerprint, validate_fn=validate_fn, digest=digest)
    _DRAFTS[d.id] = d
    data = {"draft_id": d.id, "preview": preview, "digest": digest}
    data.update(_audit_common(plan))
    if preview.get("currency"):
        data["currency"] = preview["currency"]
    try:
        audit.log_event(tool, "draft", data)
    except Exception:
        # Plan invariant: if the draft-audit write fails the draft is discarded and the error
        # returned — an un-audited draft must never linger to be applied later.
        _DRAFTS.pop(d.id, None)
        raise
    return {"draft_id": d.id, "dry_run": True, "preview": preview,
            "next": f"confirm_and_apply(draft_id='{d.id}')"}


def _is_transport_error(e: Exception) -> bool:
    """Delegate provider-specific classification to the sole Google integration module."""
    return client.is_transport_error(e)


def apply_draft(draft_id: str) -> dict:
    """Re-validate the draft against current env + account state, then hand its plan to the
    ONE dispatcher client._dispatch(plan). A refused apply (writes off, drift) must NOT
    consume the draft; an expired draft is consumed with a distinct message and never
    dispatched."""
    with _DRAFT_LOCK:
        return _apply_draft_locked(draft_id)


def _apply_draft_locked(draft_id: str) -> dict:
    peeked = _DRAFTS.get(draft_id)  # peek only -- tool name known from the draft
    tool = peeked.tool if peeked is not None else "unknown"
    try:
        check_writes_enabled()  # before pop -- a refused apply must NOT consume the draft
        if peeked is not None:
            if (isinstance(peeked.plan, RecommendationActionPlan) or getattr(peeked.plan, 'kind', None) == 'recommendation'):
                if getattr(peeked.plan, 'rpc', None) == 'apply':
                    check_apply_recommendation_enabled()
                client.validate_recommendation_plan(peeked.plan)
                if time.monotonic() - peeked.created_at <= draft_ttl_seconds():
                    _validate_recommendation_draft(peeked.plan, peeked.preview,
                                                   peeked.fingerprint, peeked.validate_fn)
            # Structural apply-time WRITE-allowlist re-check: does NOT ride on the tool's
            # validate_fn, so a tool that passes validate_fn=None (or forgets the check) still
            # cannot apply a write to a non-write-allowlisted customer. Runs before pop, so a
            # refusal is non-consuming AND audited "refused" by the except below.
            check_customer_allowlisted(_plan_customer_id(peeked.plan), "write")
            if peeked.tool == "remove_entity":
                check_remove_entity_enabled()
        if (peeked is not None and peeked.validate_fn is not None
                and not (isinstance(peeked.plan, RecommendationActionPlan)
                         and peeked.plan.rpc == 'apply')
                and time.monotonic() - peeked.created_at <= draft_ttl_seconds()):
            # re-run the tool's policy rails + fingerprint drift check against CURRENT state,
            # still before pop: a refusal here must not consume the draft. Expired drafts
            # skip this -- the pop path below raises the dedicated expired message without a
            # live read first.
            peeked.validate_fn()
    except RailViolation as e:
        _audit_refused(tool, e, plan=(peeked.plan if peeked is not None else None))
        raise

    d = _DRAFTS.pop(draft_id, None)
    _prune_expired_drafts()  # sweep other stale entries while we're touching the dict
    if d is None:
        e = RailViolation(f"unknown or already-applied draft_id '{draft_id}' — re-draft to retry",
                          code="UNKNOWN_DRAFT")
        _audit_refused(tool, e)
        raise e
    age = time.monotonic() - d.created_at
    ttl = draft_ttl_seconds()
    if age > ttl:
        # Distinct message on purpose: "unknown" means no such draft; this means it existed
        # and went stale, so the caller must re-draft rather than retry.
        e = RailViolation(
            f"draft_id '{draft_id}' expired: drafted {age:.0f}s ago, TTL is {ttl:.0f}s "
            "(GOOGLE_ADS_DRAFT_TTL_SECONDS) — re-draft to retry", code="DRAFT_EXPIRED")
        _audit_refused(tool, e, d.plan)
        raise e

    # MutationOp.operation is a mutable dict, so frozen=True does not stop the previewed plan
    # from diverging from what gets executed. Recompute the digest and refuse if it changed
    # since the draft was previewed/audited. After pop on purpose: a corrupted in-memory plan
    # must not be left behind to be retried.
    if plan_digest(d.plan) != d.digest:
        e = RailViolation("plan digest changed since draft — refusing", code="PLAN_TAMPERED")
        _audit_refused(tool, e, d.plan)
        raise e

    try:
        result = client._dispatch(d.plan)
    except Exception as e:
        if isinstance(e, UnknownWriteOutcome) or _is_transport_error(e):
            # UNKNOWN outcome, from either of two landed-but-unconfirmed shapes: the RPC failed
            # at the wire (transport error, may or may not have landed) OR the mutate returned
            # but its response could not be parsed (UnknownWriteOutcome from client, the write
            # DID land). Both audit phase "unknown" with the STRUCTURED provider error (not a
            # flat string) -- never phase "error", which would imply nothing landed.
            request_id = getattr(e, "request_id", None)
            failure = getattr(e, "failure", None)
            if failure is None:
                failure = client.transport_error_details(e)
            if d.tool == 'upload_image_asset':
                failure = client.image_error_codes(e)
                request_id = (request_id if type(request_id) is str
                              and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id) else None)
            unknown_data = {"draft_id": d.id, "preview": d.preview, "digest": d.digest,
                            "request_id": request_id, "failure": failure}
            unknown_data.update(_audit_common(d.plan))
            try:
                audit.log_event(d.tool, "unknown", unknown_data)
            except Exception as audit_exc:
                print(f"apply_draft: audit log failed for {d.tool} draft {d.id} (unknown phase): "
                      f"{audit_exc}", file=sys.stderr)
            if d.tool == 'upload_image_asset':
                raise UnknownWriteOutcome('image upload outcome unknown; verify account state before retrying',
                                          request_id=request_id, failure=failure) from None
            if isinstance(e, UnknownWriteOutcome):
                raise  # already the right type (parse-after-mutate) -- do NOT re-wrap it
            raise UnknownWriteOutcome(
                f"write outcome UNKNOWN for draft '{d.id}': the mutate RPC failed at the "
                "transport boundary, so the write MAY have landed — verify account state "
                "before retrying",
                request_id=request_id, failure=failure, cause=e) from e
        # pre-dispatch / validation error: nothing landed -> audit phase "error".
        try:
            error_data = {"draft_id": d.id, "preview": d.preview, "digest": d.digest, "error": "image upload failed" if d.tool == "upload_image_asset" else str(e)}
            error_data.update(_audit_common(d.plan))
            audit.log_event(d.tool, "error", error_data)
        except Exception as audit_exc:
            # A broken audit log must not eat the real fault: the `raise` below still carries
            # the ORIGINAL exception. This audit failure lands on stderr (never stdout).
            print(f"apply_draft: audit log failed for {d.tool} draft {d.id} (error phase): "
                  f"{audit_exc}", file=sys.stderr)
        if d.tool == 'upload_image_asset':
            raise RailViolation('image upload failed before a confirmed response') from None
        raise

    verification = None
    if getattr(d.plan, "post_checks", None):
        try:
            if any(check.get('listing_filter') for check in d.plan.post_checks):
                client.verify_listing_filter_result(d.plan.post_checks, result)
            elif any(check.get('pmax_asset_group_asset_removal') for check in d.plan.post_checks):
                client.verify_pmax_asset_group_asset_remove_result(d.plan.post_checks, result)
            elif any(check.get("removal") for check in d.plan.post_checks):
                client.verify_removed_result(d.plan.post_checks, result)
            elif any(check.get('pmax_asset_group_assets') for check in d.plan.post_checks):
                client.verify_created_results(d.plan.post_checks, result)
            elif any(check.get('pmax_asset_group_update') for check in d.plan.post_checks):
                client.verify_created_results(d.plan.post_checks, result)
            elif any("shared_negative_add" in check or "shared_negative_attach" in check for check in d.plan.post_checks):
                client.verify_created_results(d.plan.post_checks, result)
            elif any("result_index" in check for check in d.plan.post_checks):
                client.verify_created_results(d.plan.post_checks, result)
            else:
                client.verify_post_apply(d.plan.post_checks)
        except Exception as exc:
            verification = ('image identity/metadata verification failed' if d.tool == 'upload_image_asset'
                            else str(exc))
    if d.tool == 'upload_image_asset':
        # Provider failures/results can echo mutate-only bytes. Expose only validated identities.
        entries = result.get('results', []) if isinstance(result, dict) else []
        names = [entry['resource_name'] for entry in entries if isinstance(entry, dict)
                 and entry.get('type') == 'asset_result'
                 and type(entry.get('resource_name')) is str
                 and re.fullmatch(rf'customers/{d.plan.mutate_customer_id}/assets/[1-9][0-9]*',
                                  entry['resource_name'])]
        result = {'resource_names': names, 'results': [
            {'type': 'asset_result', 'resource_name': name} for name in names]}
    out = {"draft_id": d.id, "tool": d.tool, "applied": True, "result": result, "digest": d.digest}
    if isinstance(d.plan, RecommendationActionPlan) and d.plan.rpc == 'apply':
        out['verification_scope'] = 'saved budget and attached campaign configuration; not recommendation lifecycle or bidding outcome'
    elif isinstance(d.plan, RecommendationActionPlan) and d.plan.rpc == 'dismiss':
        out['verification_scope'] = 'projected recommendation identity and observed dismissed flag only; not suppression permanence or other account settings'
    elif d.tool == 'upload_image_asset':
        out['verification_scope'] = 'identity and metadata only; not bytes, newness, policy or serving'
    elif d.tool == 'create_custom_audience':
        out['verification_scope'] = 'identity, ownership, OPEN collection and exact rules; no tag, consent, membership count or serving proof'
    elif d.tool == 'upload_text_asset':
        out['verification_scope'] = 'identity and exact text; not newness, policy or serving'
    elif d.tool == 'update_asset_group':
        out['verification_scope'] = ('saved target fields, parent branding and sibling-name '
                                     'uniqueness; not policy, serving, traffic or business results')
    elif d.tool == 'add_asset_group_assets':
        out['verification_scope'] = ('exact saved asset-link union, creative content, target group '
                                     'and parent branding; not policy, serving, traffic or business results')
    elif d.tool == 'remove_asset_group_asset':
        out['verification_scope'] = ('this one group-role connection removal, unchanged bare asset, '
                                     'exact remaining link union, target group and parent branding; '
                                     'not policy, serving, traffic or business results')
    if verification is not None:
        out.update(verified=False, code="POST_WRITE_VERIFICATION_FAILED",
                   error=verification,
                   next="The write was dispatched successfully. Read account state before any "
                        "new draft; do not retry this write blindly.")
    elif getattr(d.plan, "post_checks", None):
        out["verified"] = True
    try:
        apply_data = {"draft_id": d.id, "preview": d.preview, "result": result, "digest": d.digest}
        apply_data.update(_audit_common(d.plan))
        if isinstance(result, dict) and result.get("request_id"):
            apply_data["request_id"] = result["request_id"]
        if verification is not None:
            apply_data.update(code="POST_WRITE_VERIFICATION_FAILED", applied=True,
                              verification_error=verification)
        # "apply_unverified" = mutate returned success but the read-back did not match.
        # Never phase "error": that phase means nothing landed.
        audit.log_event(d.tool, "apply_unverified" if verification is not None else "apply",
                        apply_data)
    except Exception as audit_exc:
        # _dispatch already landed on the live account -- an audit failure here must not tell
        # the caller the write failed (that invites a duplicate retry). Surface the gap
        # without echoing the local filesystem path the OSError carries.
        print(f"apply_draft: audit log failed for {d.tool} draft {d.id} (apply phase): "
              f"{audit_exc}", file=sys.stderr)
        out["audit_error"] = "audit log write failed; the write itself was dispatched"
    return out


@dataclass(frozen=True)
class CriteriaIntent:
    customer_id: str
    parent_id: str
    action: str
    values: object


def _strict_id(value):
    try:
        return client.numeric_id(value)
    except ValueError as exc:
        raise RailViolation(str(exc), code="BAD_ID") from exc


def _keywords(values):
    if not isinstance(values, list) or not values:
        raise RailViolation("keywords must be a nonempty list")
    normalized = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {"text", "match_type"}:
            raise RailViolation("each keyword needs exactly text and match_type")
        if not isinstance(item["text"], str) or not item["text"].strip():
            raise RailViolation("keyword text must be nonempty")
        if item["match_type"] not in ("EXACT", "PHRASE", "BROAD"):
            raise RailViolation("match type must be EXACT, PHRASE or BROAD")
        normalized.append(
            {"text": item["text"].strip(), "match_type": item["match_type"]}
        )
    check_content([v["text"] for v in normalized])
    keys = [(v["text"].casefold(), v["match_type"]) for v in normalized]
    if len(set(keys)) != len(keys):
        raise RailViolation("duplicate keywords")
    return normalized


def _schedules(values):
    if not isinstance(values, list) or not values:
        raise RailViolation("schedule must be a nonempty full-week list")
    result, windows = [], {}
    minutes = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "day_of_week",
            "start_hour",
            "start_minute",
            "end_hour",
            "end_minute",
        }:
            raise RailViolation(
                "schedule requires exactly day and start/end hours/minutes"
            )
        day = item["day_of_week"]
        if day not in (
            "MONDAY",
            "TUESDAY",
            "WEDNESDAY",
            "THURSDAY",
            "FRIDAY",
            "SATURDAY",
            "SUNDAY",
        ):
            raise RailViolation("invalid schedule day")
        sh, sm, eh, em = [
            item[k] for k in ("start_hour", "start_minute", "end_hour", "end_minute")
        ]
        if (
            any(type(v) is not int for v in (sh, sm, eh, em))
            or not (0 <= sh <= 23 and 0 <= eh <= 24)
            or sm not in minutes
            or em not in minutes
            or (eh == 24 and em != 0)
        ):
            raise RailViolation("invalid schedule hours/minutes")
        start, end = sh * 60 + sm, eh * 60 + em
        if start >= end:
            raise RailViolation("schedule end must follow start on the same day")
        previous = windows.setdefault(day, [])
        if len(previous) >= 6 or any(start < b and end > a for a, b in previous):
            raise RailViolation("overlapping schedule or more than six windows per day")
        previous.append((start, end))
        result.append(dict(item, start_minute=minutes[sm], end_minute=minutes[em]))
    return result


def _compile_criteria(intent):
    check_writes_enabled()
    cid, pid = _strict_id(intent.customer_id), _strict_id(intent.parent_id)
    check_customer_allowlisted(cid, "write")
    check_customer_allowlisted(cid, "read")
    action = intent.action
    if action not in {
        "draft_keywords",
        "remove_keywords",
        "update_keyword_bid",
        "add_negative_keywords",
        "remove_negative_keywords",
        "exclude_geo_target",
        "remove_geo_target",
        "set_campaign_schedule",
    }:
        raise RailViolation("unknown criteria action")
    group = action in {"draft_keywords", "remove_keywords", "update_keyword_bid"}
    parent_type = "ad_group" if group else "campaign"
    parent_resource = (
        client.ad_group_path(cid, pid) if group else client.campaign_path(cid, pid)
    )
    service = "AdGroupCriterionService" if group else "CampaignCriterionService"
    values = intent.values
    if action in {"draft_keywords", "add_negative_keywords"}:
        values = _keywords(values)
    elif action == "set_campaign_schedule":
        values = _schedules(values)
    elif action in {"remove_keywords", "remove_negative_keywords"}:
        if not isinstance(values, list) or not values:
            raise RailViolation("criterion_ids must be a nonempty list")
        values = [_strict_id(v) for v in values]
        if len(set(values)) != len(values):
            raise RailViolation("duplicate criterion ids")
    elif action in {"exclude_geo_target", "remove_geo_target"}:
        values = client.geo_target_constant_path(
            _strict_id(str(values).removeprefix("geoTargetConstants/"))
        )
    elif action == "update_keyword_bid":
        values = {
            "criterion_id": _strict_id(values["criterion_id"]),
            "micros": _checked_money_micros(_amount(values["new_bid"], check_bid)),
        }
    state = client.criteria_state(cid, parent_type, pid)
    if state["parent"].get("resource_name") != parent_resource:
        raise RailViolation("parent ownership mismatch")
    for parent in (state["parent"], state.get("campaign", state["parent"])):
        if parent.get("status") not in _ALLOWED_NEW_STATUS:
            raise RailViolation("parent missing, removed or unreadable")
    if group:
        campaign_resource = state["parent"].get("campaign")
        campaign_id = _strict_id(str(campaign_resource).rsplit("/", 1)[-1])
        if campaign_resource != client.campaign_path(cid, campaign_id):
            raise RailViolation("campaign ownership mismatch")
        if (
            "campaign" in state
            and state["campaign"].get("resource_name") != campaign_resource
        ):
            raise RailViolation("campaign parent mismatch")
    rows = state["rows"]
    seen = set()
    for r in rows:
        expected = (
            client.ad_group_criterion_path if group else client.campaign_criterion_path
        )(cid, pid, _strict_id(r["criterion_id"]))
        if (
            r.get(parent_type) != parent_resource
            or r.get("resource_name") != expected
            or expected in seen
        ):
            raise RailViolation("criterion ownership or duplicate read mismatch")
        seen.add(expected)
        if (
            r.get("status") not in {"ENABLED", "PAUSED", "REMOVED"}
            or type(r.get("negative")) is not bool
        ):
            raise RailViolation("criterion status/polarity unreadable")
    active = [r for r in rows if r["status"] != "REMOVED"]
    operations, checks, warnings = [], [], []
    if action in {"draft_keywords", "add_negative_keywords"}:
        negative = not group
        existing = {
            (r["keyword"]["text"].strip().casefold(), r["keyword"]["match_type"])
            for r in active
            if r["type"] == "KEYWORD" and r["negative"] == negative
        }
        if any((v["text"].casefold(), v["match_type"]) in existing for v in values):
            raise RailViolation("keyword already exists")
        for keyword in values:
            fields = {parent_type: parent_resource, "keyword": keyword}
            if negative:
                fields["negative"] = True
            operations.append(safe_create_operation(service, fields))
    elif action == "set_campaign_schedule":
        old = [r for r in active if r["type"] == "AD_SCHEDULE"]
        if any(r["negative"] for r in old):
            raise RailViolation("invalid negative schedule")
        operations += [
            MutationOp(service, {"remove": r["resource_name"]}, None) for r in old
        ]
        operations += [
            safe_create_operation(
                service, {"campaign": parent_resource, "ad_schedule": v}
            )
            for v in values
        ]
        warnings.append(
            "Replaces the FULL week in account time zone. Unspecified days will not serve. Previous schedule status and bid modifiers are discarded; new windows have no bid adjustment."
        )
    elif action == "exclude_geo_target":
        state["geo_constant"] = client.geo_constant_state(cid, values)
        if any(
            r["type"] == "LOCATION" and r["location"]["geo_target_constant"] == values
            for r in active
        ):
            raise RailViolation("location already targeted or excluded")
        operations.append(
            safe_create_operation(
                service,
                {
                    "campaign": parent_resource,
                    "negative": True,
                    "location": {"geo_target_constant": values},
                },
            )
        )
    else:
        if action == "remove_geo_target":
            selected = [
                r
                for r in active
                if r["type"] == "LOCATION"
                and r["location"]["geo_target_constant"] == values
                and not r["negative"]
            ]
            if len(selected) != 1:
                raise RailViolation("positive location not found uniquely")
            remaining = [
                r["location"]["geo_target_constant"]
                for r in active
                if r["type"] == "LOCATION" and not r["negative"] and r not in selected
            ]
            warnings.append(
                f"Remaining positive locations: {remaining}. Removing the last positive location can broaden eligibility."
            )
        else:
            ids = [values["criterion_id"]] if action == "update_keyword_bid" else values
            selected = [r for r in active if str(r["criterion_id"]) in ids]
            negative = action == "remove_negative_keywords"
            if len(selected) != len(ids) or any(
                r["type"] != "KEYWORD" or r["negative"] != negative for r in selected
            ):
                raise RailViolation("requested keyword missing or wrong type/polarity")
            check_content([r["keyword"]["text"] for r in selected])
        if action == "update_keyword_bid":
            campaign_id = state["parent"]["campaign"].rsplit("/", 1)[-1]
            strategy = client.effective_strategy(cid, _strict_id(campaign_id))
            state["strategy"] = strategy
            check_bid_write_allowed(strategy["type"], "keyword CPC")
            if strategy["type"] != "MANUAL_CPC" or strategy.get(
                "portfolio_resource_name"
            ):
                raise RailViolation("keyword CPC requires standard MANUAL_CPC")
            rn = selected[0]["resource_name"]
            operations.append(
                MutationOp(
                    service,
                    {
                        "update": {
                            "resource_name": rn,
                            "cpc_bid_micros": values["micros"],
                        }
                    },
                    ["cpc_bid_micros"],
                )
            )
            checks.append(
                {
                    "entity_type": "keyword",
                    "customer_id": cid,
                    "parent_id": pid,
                    "resource_name": rn,
                    "expected": {"effective_cpc_bid_micros": str(values["micros"])},
                }
            )
        else:
            operations += [
                MutationOp(service, {"remove": r["resource_name"]}, None)
                for r in selected
            ]
            warnings.append(
                "Removal leaves historical rows and cannot be undone in place."
            )
    plan = EntityMutationPlan(cid, operations, True, post_checks=checks)
    preview = {
        "tool": action,
        "customer_id": cid,
        "parent_id": pid,
        "current": state,
        "warnings": warnings,
        "time_zone": state["time_zone"],
        "operations": _plan_canonical(plan)["operations"],
        "digest": plan_digest(plan),
    }

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != plan_digest(plan):
            raise RailViolation(
                "account changed since preview; create a fresh draft",
                code="STATE_DRIFT",
            )

    return CompiledPlan(preview, plan, state, validate_fn)


def criteria_draft(intent):
    # Detach caller-owned nested lists/dicts before the confirm-time policy recheck.
    import copy

    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused(intent.action, exc)
        raise
    return create_draft(
        intent.action,
        compiled.preview,
        compiled.plan,
        compiled.fingerprint,
        compiled.validate_fn,
    )


@dataclass(frozen=True)
class DraftCampaignIntent:
    customer_id: str
    campaign_name: str
    daily_budget: object
    bidding_strategy: str
    geo_target_ids: list[str]
    language_ids: list[str]
    contains_eu_political_advertising: bool
    target_cpa: object = None
    target_roas: object = None


@dataclass(frozen=True)
class CreateAdGroupIntent:
    customer_id: str
    campaign_id: str
    ad_group_name: str
    cpc_bid: object = None


@dataclass(frozen=True)
class CreatePortfolioBiddingStrategyIntent:
    customer_id: str
    name: str
    strategy_type: str
    target_cpa: object = None
    target_roas: object = None


def _compile_portfolio_creation(intent):
    check_writes_enabled()
    if not allow_portfolio_edit():
        raise RailViolation("portfolio creation requires GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT=true")
    if type(intent.customer_id) is not str or not re.fullmatch(r'[1-9][0-9]*', intent.customer_id):
        raise RailViolation('customer_id must be a canonical positive ASCII ID', code='BAD_ID')
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    name = client.creation_name(intent.name)
    check_content([name])
    if type(intent.strategy_type) is not str or intent.strategy_type not in {'TARGET_CPA', 'TARGET_ROAS'}:
        raise RailViolation('strategy_type requires TARGET_CPA or TARGET_ROAS')
    if intent.strategy_type == 'TARGET_CPA':
        if intent.target_cpa is None or intent.target_roas is not None:
            raise RailViolation('TARGET_CPA requires target_cpa and forbids target_roas')
        target = _checked_money_micros(_amount(intent.target_cpa, check_target_cpa))
        scheme, parameters = 'target_cpa', {'target_cpa_micros': target}
    else:
        if intent.target_roas is None or intent.target_cpa is not None:
            raise RailViolation('TARGET_ROAS requires target_roas and forbids target_cpa')
        target = _amount(intent.target_roas, check_roas)
        scheme, parameters = 'target_roas', {'target_roas': float(target)}
    state = client.portfolio_creation_state(cid, name)
    values = {'name': name, scheme: parameters}
    check = {'result_index': 0, 'entity_type': 'bidding_strategy', 'customer_id': cid,
             'portfolio_creation': True, 'expected': values,
             'currency_code': state['account']['currency_code']}
    plan = EntityMutationPlan(cid, [safe_create_operation('BiddingStrategyService', values)],
                              True, post_checks=[check])
    client.validate_mutation_plan(plan)
    preview = {'tool': 'create_portfolio_bidding_strategy', 'customer_id': cid, 'owner_customer_id': cid,
               'name': name, 'account_currency': state['account']['currency_code'],
               'strategy_type': intent.strategy_type,
               'target': client.from_micros(target) if scheme == 'target_cpa' else str(target),
               'attached_campaigns': [],
               'limitation': 'Created as unattached inventory; it does not serve until separately attached to a campaign.',
               'warnings': ['Names are checked again before apply but concurrent creation can race this check.'],
               'operations': _plan_canonical(plan)['operations'], 'digest': plan_digest(plan)}
    digest = plan_digest(plan)

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def portfolio_creation_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('create_portfolio_bidding_strategy', exc)
        raise
    return create_draft('create_portfolio_bidding_strategy', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _creation_ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise RailViolation("targets must contain from 1 to 100 unique positive IDs")
    ids = []
    for value in values:
        identity = _strict_id(value)
        if not isinstance(value, str) or identity != str(int(identity)):
            raise RailViolation("target IDs must be canonical positive strings")
        ids.append(identity)
    if len(set(ids)) != len(ids):
        raise RailViolation("duplicate target IDs")
    return ids


def _compile_creation(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    campaign = isinstance(intent, DraftCampaignIntent)
    name = client.creation_name(intent.campaign_name if campaign else intent.ad_group_name)
    check_content([name])
    operations, checks = [], []
    if campaign:
        budget_micros = _checked_money_micros(_amount(intent.daily_budget, check_budget))
        geo_ids, language_ids = _creation_ids(intent.geo_target_ids), _creation_ids(intent.language_ids)
        if type(intent.contains_eu_political_advertising) is not bool:
            raise RailViolation("political declaration must be an explicit boolean")
        strategies = {'MANUAL_CPC': 'manual_cpc', 'MAXIMIZE_CONVERSIONS': 'maximize_conversions',
                      'MAXIMIZE_CONVERSION_VALUE': 'maximize_conversion_value'}
        if not isinstance(intent.bidding_strategy, str) or intent.bidding_strategy not in strategies:
            raise RailViolation("unsupported creation strategy")
        strategy = strategies[intent.bidding_strategy]
        params = {}
        if intent.target_cpa is not None:
            if strategy != 'maximize_conversions':
                raise RailViolation("target CPA only applies to Maximize Conversions")
            params['target_cpa_micros'] = _checked_money_micros(_amount(intent.target_cpa, check_target_cpa))
        if intent.target_roas is not None:
            if strategy != 'maximize_conversion_value':
                raise RailViolation("target ROAS only applies to Maximize Conversion Value")
            params['target_roas'] = float(_amount(intent.target_roas, check_roas))
        state = client.creation_state(cid, name, geo_ids, language_ids)
        budget_rn, campaign_rn = client.creation_paths(cid)
        budget = {'resource_name': budget_rn, 'amount_micros': budget_micros,
                  'explicitly_shared': False, 'period': 'DAILY', 'delivery_method': 'STANDARD'}
        fields = {'resource_name': campaign_rn, 'name': name, 'campaign_budget': budget_rn,
                  'advertising_channel_type': 'SEARCH', strategy: params,
                  'network_settings': dict(client.SEARCH_NETWORKS),
                  'geo_target_type_setting': dict(client.SEARCH_GEO_OPTIONS),
                  'contains_eu_political_advertising': client.POLITICAL_DECLARATIONS[intent.contains_eu_political_advertising]}
        operations.append(safe_create_operation('CampaignBudgetService', budget))
        operations.append(safe_create_operation('CampaignService', fields))
        checks.extend([
            {'result_index': 0, 'entity_type': 'campaign_budget', 'customer_id': cid,
             'expected': {key: value for key, value in budget.items() if key != 'resource_name'}},
            {'result_index': 1, 'entity_type': 'campaign', 'customer_id': cid, 'budget_result_index': 0,
             'expected': {key: value for key, value in operations[1].operation['create'].items()
                          if key not in {'resource_name', 'campaign_budget', strategy}},
             'strategy': intent.bidding_strategy, 'strategy_parameters': params}])
        for kind, ids, helper, field in [('location', geo_ids, client.geo_target_constant_path, 'geo_target_constant'),
                                         ('language', language_ids, client.language_constant_path, 'language_constant')]:
            for identity in ids:
                values = {'campaign': campaign_rn, 'negative': False, kind: {field: helper(identity)}}
                index = len(operations)
                operations.append(safe_create_operation('CampaignCriterionService', values))
                checks.append({'result_index': index, 'entity_type': 'campaign_criterion', 'customer_id': cid,
                               'campaign_result_index': 1, 'expected': {'status': 'ENABLED', 'negative': False, kind: values[kind]}})
        details = {'effective_budget_name': name, 'daily_budget': client.from_micros(budget_micros),
                   'bidding_strategy': intent.bidding_strategy, 'strategy_parameters': params,
                   'contains_eu_political_advertising': intent.contains_eu_political_advertising,
                   'network_settings': dict(client.SEARCH_NETWORKS), 'geo_target_type_setting': dict(client.SEARCH_GEO_OPTIONS),
                   'locations': state['locations'], 'languages': state['languages'],
                   'ad_groups_created': False, 'ads_created': False}
    else:
        pid = _strict_id(intent.campaign_id)
        state = client.creation_state(cid, name, campaign_id=pid)
        strategy = state['strategy']
        classification, code = classify_bidding_strategy(strategy.get('type'))
        if strategy.get('type') != 'MANUAL_CPC' and classification != 'SMART':
            raise RailViolation("ad group needs known supported Search bidding", code=code)
        fields = {'name': name, 'campaign': client.campaign_path(cid, pid), 'type_': 'SEARCH_STANDARD'}
        if intent.cpc_bid is not None:
            check_bid_write_allowed(strategy.get('type'), 'ad group CPC')
            if strategy.get('type') != 'MANUAL_CPC' or strategy.get('portfolio_resource_name'):
                raise RailViolation("CPC requires standard MANUAL_CPC")
            fields['cpc_bid_micros'] = _checked_money_micros(_amount(intent.cpc_bid, check_bid))
        operations.append(safe_create_operation('AdGroupService', fields))
        expected = dict(operations[0].operation['create'])
        if 'cpc_bid_micros' in expected:
            expected['effective_cpc_bid_micros'] = expected['cpc_bid_micros']
        checks.append({'result_index': 0, 'entity_type': 'ad_group', 'customer_id': cid, 'expected': expected})
        details = {'parent': state['parent'], 'strategy': strategy,
                   'bid_note': 'Inherits campaign bidding; no default CPC or target is invented.' if intent.cpc_bid is None else 'Explicit CPC in account currency.',
                   'ads_created': False}
    if state['collisions']:
        raise RailViolation("requested campaign, budget or ad group name already exists")
    plan = EntityMutationPlan(cid, operations, True, post_checks=checks)
    client.validate_mutation_plan(plan)
    tool = 'draft_campaign' if campaign else 'create_ad_group'
    preview = {'tool': tool, 'customer_id': cid, 'name': name, 'status': 'PAUSED',
               'account': state['account'], **details,
               'warnings': ['Names are checked again before apply but concurrent creation can race this check.'],
               'operations': _plan_canonical(plan)['operations'], 'digest': plan_digest(plan)}

    original_digest = plan_digest(plan)

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != original_digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def creation_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    tool = ('draft_demand_gen_ad' if isinstance(intent, DraftDemandGenAdIntent) else
            'create_demand_gen_campaign' if isinstance(intent, CreateDemandGenCampaignIntent) else
            'create_asset_group' if isinstance(intent, CreateAssetGroupIntent) else
            'create_pmax_campaign' if isinstance(intent, CreatePMaxCampaignIntent) else
            'create_structured_snippets' if isinstance(intent, CreateStructuredSnippetsIntent) else
            'create_callouts' if isinstance(intent, CreateCalloutsIntent) else
            'draft_sitelinks' if isinstance(intent, DraftSitelinksIntent) else
            'draft_responsive_search_ad' if isinstance(intent, DraftResponsiveSearchAdIntent) else
            'draft_campaign' if isinstance(intent, DraftCampaignIntent) else 'create_ad_group')
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused(tool, exc)
        raise
    return create_draft(tool, compiled.preview, compiled.plan, compiled.fingerprint, compiled.validate_fn)


@dataclass(frozen=True)
class DraftResponsiveSearchAdIntent:
    customer_id: str
    ad_group_id: str
    headlines: list[str]
    descriptions: list[str]
    final_url: str
    path1: str | None = None
    path2: str | None = None


@dataclass(frozen=True)
class DraftSitelinksIntent:
    customer_id: str
    sitelinks: list[dict]
    campaign_id: str | None = None
    ad_group_id: str | None = None


@dataclass(frozen=True)
class CreateCalloutsIntent:
    customer_id: str
    callouts: list[str]
    campaign_id: str | None = None
    ad_group_id: str | None = None


@dataclass(frozen=True)
class CreateStructuredSnippetsIntent:
    customer_id: str
    snippets: list[dict]
    campaign_id: str | None = None
    ad_group_id: str | None = None


@dataclass(frozen=True)
class RemoveExtensionIntent:
    customer_id: str
    asset_id: str
    extension_type: str
    campaign_id: str | None = None
    ad_group_id: str | None = None


def remove_extension_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('remove_extension', exc)
        raise
    return create_draft('remove_extension', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_remove_extension(intent):
    check_writes_enabled()
    for name in ('customer_id', 'asset_id'):
        if type(getattr(intent, name)) is not str:
            raise RailViolation(f'{name} must be a positive numeric string')
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if type(intent.extension_type) is not str:
        raise RailViolation('extension_type must be a string')
    field_numbers = {'CALLOUT': '11', 'STRUCTURED_SNIPPET': '12', 'SITELINK': '13'}
    if intent.extension_type not in field_numbers:
        raise RailViolation('extension_type must be SITELINK, CALLOUT, or STRUCTURED_SNIPPET')
    asset_id = _strict_id(intent.asset_id)
    if (intent.campaign_id is None) == (intent.ad_group_id is None):
        raise RailViolation('provide exactly one campaign_id or ad_group_id')
    target_type = 'campaign' if intent.campaign_id is not None else 'ad_group'
    target_value = intent.campaign_id if target_type == 'campaign' else intent.ad_group_id
    if type(target_value) is not str:
        raise RailViolation(f'{target_type}_id must be a positive numeric string')
    target_id = _strict_id(target_value)
    reader = {'SITELINK': client.sitelink_state, 'CALLOUT': client.callout_state,
              'STRUCTURED_SNIPPET': client.structured_snippet_state}[intent.extension_type]
    state = reader(cid, target_type, target_id)
    asset = client.asset_path(cid, asset_id)
    matches = [row for row in state['links'] if row.get('asset') == asset]
    if len(matches) != 1:
        raise RailViolation('selected extension association is missing, removed, or ambiguous')
    selected = matches[0]
    expected = (f'customers/{cid}/'
                f'{"campaignAssets" if target_type == "campaign" else "adGroupAssets"}/'
                f'{target_id}~{asset_id}~{field_numbers[intent.extension_type]}')
    if selected.get('resource_name') != expected or selected.get('status') not in {'ENABLED', 'PAUSED'}:
        raise RailViolation('selected extension association identity or status mismatch')
    service = 'CampaignAssetService' if target_type == 'campaign' else 'AdGroupAssetService'
    check = {'removal': True, 'entity_type': target_type + '_asset', 'customer_id': cid,
             'resource_name': expected, 'target_type': target_type, 'target_id': target_id,
             'target_resource_name': state['parent']['resource_name'],
             'asset_id': asset_id, 'field_type': intent.extension_type}
    plan = EntityMutationPlan(cid, [MutationOp(service, {'remove': expected}, None)], True,
                              post_checks=[check])
    digest = plan_digest(plan)
    preview = {'tool': 'remove_extension', 'customer_id': cid, 'draft': True,
               'account': state['account'], 'parent': state['parent'],
               'target_type': target_type, 'target': state['parent']['resource_name'],
               'extension_type': intent.extension_type, 'asset_id': asset_id,
               'content': selected['content'], 'current_status': selected['status'],
               'association': expected, 'operations': _plan_canonical(plan)['operations'],
               'digest': digest,
               'warnings': ['Confirmation removes only this selected connection. The underlying asset and other connections remain.',
                            'Higher-level or other asset associations may still serve.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


SITELINK_BATCH_MAX = 10  # local safety cap, not a Google Ads provider limit


def _closed_asset_pair(cid, target_type, target, index, field_type, content):
    """Build one alternating asset/link pair for the closed asset families."""
    asset = client.asset_path(cid, f'-{index}')
    if field_type == 'CALLOUT':
        asset_values = {'resource_name': asset,
                        'callout_asset': {'callout_text': content}}
    elif field_type == 'SITELINK':
        asset_values = {'resource_name': asset, 'final_urls': [content['final_url']],
                        'sitelink_asset': {k: value for k, value in content.items()
                                           if k != 'final_url'}}
    elif field_type == 'STRUCTURED_SNIPPET':
        asset_values = {'resource_name': asset, 'structured_snippet_asset': content}
    else:
        raise RailViolation('unsupported closed asset family')
    link_values = {target_type: target, 'asset': asset, 'field_type': field_type}
    asset_op = safe_create_operation('AssetService', asset_values)
    link_op = safe_create_operation(
        'CampaignAssetService' if target_type == 'campaign' else 'AdGroupAssetService',
        link_values)
    asset_index = 2 * (index - 1)
    checks = (
        {'result_index': asset_index, 'entity_type': 'asset', 'customer_id': cid,
         'expected': asset_values},
        {'result_index': asset_index + 1, 'entity_type': target_type + '_asset',
         'customer_id': cid, 'asset_result_index': asset_index,
         'expected': link_op.operation['create']},
    )
    return (asset_op, link_op), checks


def _compile_structured_snippets(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if (intent.campaign_id is None) == (intent.ad_group_id is None):
        raise RailViolation('provide exactly one campaign_id or ad_group_id')
    target_type = 'campaign' if intent.campaign_id is not None else 'ad_group'
    target_id = _strict_id(intent.campaign_id if intent.campaign_id is not None else intent.ad_group_id)
    if type(intent.snippets) is not list or not 1 <= len(intent.snippets) <= SITELINK_BATCH_MAX:
        raise RailViolation(f'snippets must be a nonempty list of at most {SITELINK_BATCH_MAX} items')
    normalized, keys = [], []
    for item in intent.snippets:
        if not isinstance(item, dict) or set(item) != {'header', 'values'}:
            raise RailViolation('each structured snippet requires exactly header and values')
        header, values = item['header'], item['values']
        if type(header) is not str or header not in client.STRUCTURED_SNIPPET_HEADERS:
            raise RailViolation('unsupported structured snippet header; use an exact supported English header')
        if type(values) is not list or not 3 <= len(values) <= 10:
            raise RailViolation('structured snippet values must be a list containing 3..10 strings')
        texts = [client.rsa_text(value, 25) for value in values]
        check_content([header, *texts])
        folded = [value.casefold() for value in texts]
        if len(set(folded)) != len(folded):
            raise RailViolation('duplicate structured snippet values')
        normalized.append({'header': header, 'values': texts})
        keys.append((header, tuple(sorted(folded))))
    if len(set(keys)) != len(keys):
        raise RailViolation('duplicate structured snippets in batch')
    state = client.structured_snippet_state(cid, target_type, target_id)
    existing = {(row['content']['base'][0], tuple(sorted(v.casefold() for v in row['content']['base'][1])))
                for row in state['links'] if row['content']['plain']}
    if any(key in existing for key in keys):
        raise RailViolation('identical structured snippet is already associated with the target')
    target = state['parent']['resource_name']
    operations, checks = [], []
    for index, value in enumerate(normalized, start=1):
        pair, pair_checks = _closed_asset_pair(
            cid, target_type, target, index, 'STRUCTURED_SNIPPET', value)
        operations.extend(pair)
        checks.extend(pair_checks)
    plan = EntityMutationPlan(cid, operations, True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'create_structured_snippets', 'customer_id': cid, 'draft': True,
               'target_type': target_type, 'target': target, 'link_status': 'PAUSED',
               'snippets': normalized, 'account': state['account'], 'parent': state['parent'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['English structured snippet headers only. Confirmation is required before the atomic request is sent.',
                            f'This tool uses a local safety cap of {SITELINK_BATCH_MAX} snippets per draft; it is not a Google Ads provider limit.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_callouts(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if (intent.campaign_id is None) == (intent.ad_group_id is None):
        raise RailViolation('provide exactly one campaign_id or ad_group_id')
    target_type = 'campaign' if intent.campaign_id is not None else 'ad_group'
    target_id = _strict_id(intent.campaign_id if intent.campaign_id is not None else intent.ad_group_id)
    if type(intent.callouts) is not list or not 1 <= len(intent.callouts) <= SITELINK_BATCH_MAX:
        raise RailViolation(f'callouts must be a nonempty list of at most {SITELINK_BATCH_MAX} strings')
    normalized = [client.rsa_text(value, 25) for value in intent.callouts]
    check_content(normalized)
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise RailViolation('duplicate callouts in batch')
    state = client.callout_state(cid, target_type, target_id)
    existing = {row['content']['base'].casefold() for row in state['links'] if row['content']['plain']}
    if any(value.casefold() in existing for value in normalized):
        raise RailViolation('identical callout is already associated with the target')
    target = state['parent']['resource_name']
    operations, checks = [], []
    for index, text in enumerate(normalized, start=1):
        pair, pair_checks = _closed_asset_pair(
            cid, target_type, target, index, 'CALLOUT', text)
        operations.extend(pair)
        checks.extend(pair_checks)
    plan = EntityMutationPlan(cid, operations, True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'create_callouts', 'customer_id': cid, 'draft': True,
               'target_type': target_type, 'target': target, 'link_status': 'PAUSED',
               'callouts': normalized, 'account': state['account'], 'parent': state['parent'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['This creates new assets rather than selecting existing inventory. Google may deduplicate identical asset creates; confirmation is required before the atomic request is sent.',
                            f'This tool uses a local safety cap of {SITELINK_BATCH_MAX} callouts per draft; it is not a Google Ads provider limit.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_sitelinks(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if (intent.campaign_id is None) == (intent.ad_group_id is None):
        raise RailViolation('provide exactly one campaign_id or ad_group_id')
    target_type = 'campaign' if intent.campaign_id is not None else 'ad_group'
    target_id = _strict_id(intent.campaign_id if intent.campaign_id is not None else intent.ad_group_id)
    if type(intent.sitelinks) is not list or not 1 <= len(intent.sitelinks) <= SITELINK_BATCH_MAX:
        raise RailViolation(f'sitelinks must be a nonempty list of at most {SITELINK_BATCH_MAX} items')
    normalized = []
    for item in intent.sitelinks:
        if not isinstance(item, dict) or not set(item) <= {'link_text', 'final_url', 'description1', 'description2'} \
                or not {'link_text', 'final_url'} <= set(item):
            raise RailViolation('each sitelink permits only link_text, final_url and paired descriptions')
        if ('description1' in item) != ('description2' in item):
            raise RailViolation('description1 and description2 must be provided together')
        values = {'link_text': client.rsa_text(item['link_text'], 25),
                  'final_url': client.rsa_url(item['final_url'])}
        if 'description1' in item:
            values.update(description1=client.rsa_text(item['description1'], 35),
                          description2=client.rsa_text(item['description2'], 35))
        check_content(values.values())
        normalized.append(values)
    keys = [(v['link_text'].casefold(), v['final_url'], v.get('description1'), v.get('description2'))
            for v in normalized]
    if len(set(keys)) != len(keys):
        raise RailViolation('duplicate sitelinks in batch')
    state = client.sitelink_state(cid, target_type, target_id)
    existing = {tuple(row['content']['base']) for row in state['links'] if row['content']['plain']}
    requested = [client.sitelink_content({
        'final_urls': [v['final_url']],
        'sitelink_asset': {k: value for k, value in v.items() if k != 'final_url'},
    }) for v in normalized]
    if any(content in existing for content in requested):
        raise RailViolation('identical sitelink is already associated with the target')
    target = state['parent']['resource_name']
    operations, checks = [], []
    for index, value in enumerate(normalized, start=1):
        pair, pair_checks = _closed_asset_pair(
            cid, target_type, target, index, 'SITELINK', value)
        operations.extend(pair)
        checks.extend(pair_checks)
    plan = EntityMutationPlan(cid, operations, True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'draft_sitelinks', 'customer_id': cid, 'target_type': target_type,
               'target': target, 'link_status': 'PAUSED', 'sitelinks': normalized,
               'account': state['account'], 'parent': state['parent'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['Google may deduplicate identical asset creates. The atomic batch prevents a link failure from leaving a new bare asset, but a later link removal can leave the asset in inventory.',
                            f'This tool uses a local safety cap of {SITELINK_BATCH_MAX} sitelinks per draft; it is not a Google Ads provider limit.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_responsive_search_ad(intent):
    check_writes_enabled()
    cid, gid = _strict_id(intent.customer_id), _strict_id(intent.ad_group_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    rsa = {}
    for key in ('headlines', 'descriptions'):
        texts = getattr(intent, key)
        if type(texts) is not list or any(type(t) is not str for t in texts):
            raise RailViolation('headlines and descriptions must be lists of strings')
        rsa[key] = [{'text': text} for text in texts]
    for key in ('path1', 'path2'):
        if getattr(intent, key) is not None:
            rsa[key] = getattr(intent, key)
    op = safe_create_operation('AdGroupAdService', {'ad_group': client.ad_group_path(cid, gid),
                               'ad': {'final_urls': [intent.final_url], 'responsive_search_ad': rsa}})
    client.validate_mutation_operation(op, cid)
    state = client.responsive_search_ad_state(cid, gid)
    content = client._rsa_content(dict(op.operation['create']['ad'], type_='RESPONSIVE_SEARCH_AD'))
    if any(row['content'] == content for row in state['ads']):
        raise RailViolation('identical responsive search ad already exists')
    checks = [{'result_index': 0, 'entity_type': 'ad_group_ad', 'customer_id': cid,
               'expected': op.operation['create']}]
    plan = EntityMutationPlan(cid, [op], True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'draft_responsive_search_ad', 'customer_id': cid, 'status': 'PAUSED',
               'account': state['account'], 'parent': state['parent'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['Concurrent ad creation can race duplicate checks; provider limits still apply.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class UploadImageAssetIntent:
    customer_id: str
    image_base64: str = field(repr=False)
    name: str


def upload_image_draft(intent):
    try:
        compiled = compile(intent)
    except Exception as exc:
        refusal = RailViolation('image upload draft refused; check account, name and JPEG/PNG input',
                                code=getattr(exc, 'code', None))
        _audit_refused('upload_image_asset', refusal)
        raise refusal from None
    return create_draft('upload_image_asset', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_image_upload(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    name = client.creation_name(intent.name)
    check_content([name])
    image = client.image_metadata(intent.image_base64)
    state = {'account': client._creation_account(cid), 'image': image}
    values = {'name': name, 'image_asset': {'data': intent.image_base64,
                                          'mime_type': image['mime_type']}}
    checks = [{'result_index': 0, 'entity_type': 'asset', 'customer_id': cid, 'image': True,
               'expected': {'image_asset': {'mime_type': image['mime_type'],
                   'file_size': image['bytes'], 'full_size': {
                       'width_pixels': image['width'], 'height_pixels': image['height']}}}}]
    plan = EntityMutationPlan(cid, [safe_create_operation('AssetService', values)], True,
                              post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'upload_image_asset', 'customer_id': cid, 'account': state['account'],
               'name': name, 'image': image, 'digest': digest, 'confirmation_required': True,
               'warnings': [
                   'Creates no new link. Bare assets have no paused status and cannot be deleted through the API; inventory residue persists.',
                   'Google may deduplicate identical bytes and ignore the requested name.',
                   'Visual content is not screened by text rules. No policy or serving approval is established.',
                   'Readback verifies identity and metadata only, not byte content or newness.']}

    def validate_fn():
        try:
            fresh = compile(intent)
            if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
                raise RailViolation('image or account changed since preview', code='STATE_DRIFT')
        except Exception as exc:
            raise RailViolation('image confirmation refused; create a fresh draft after checking inputs and account',
                                code=getattr(exc, 'code', None)) from None

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class UploadTextAssetIntent:
    customer_id: str
    text: str
    name: str


def upload_text_draft(intent):
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('upload_text_asset', exc)
        raise
    return create_draft('upload_text_asset', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_text_upload(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    name = client.creation_name(intent.name)
    text = client.rsa_text(intent.text, 90)
    check_content([name, text])
    values = {'name': name, 'text_asset': {'text': text}}
    state = {'account': client._creation_account(cid), 'name': name, 'text': text}
    checks = [{'result_index': 0, 'entity_type': 'asset', 'customer_id': cid,
               'text': True, 'expected': {'text_asset': {'text': text}}}]
    plan = EntityMutationPlan(cid, [safe_create_operation('AssetService', values)], True,
                              post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'upload_text_asset', 'customer_id': cid, 'account': state['account'],
               'name': name, 'text': text, 'digest': digest, 'confirmation_required': True,
               'warnings': [
                   'Creates no new serving association. Bare assets have no paused status and cannot be deleted through the API; inventory residue persists.',
                   'Google may return existing matching content and ignore the requested name.',
                   'Readback verifies identity and exact text, not newness, policy or serving. Later role-specific text limits may be stricter.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('text, name or account changed since preview; create a fresh draft',
                                code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class CreateCustomAudienceIntent:
    customer_id: str
    name: str
    url_contains: list[str]


def custom_audience_draft(intent):
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('create_custom_audience', exc)
        raise
    return create_draft('create_custom_audience', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_custom_audience(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if not parse_bool_env('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', False):
        raise RailViolation('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT=true is required')
    values = {'name': intent.name, 'membership_status': 'OPEN',
              'rule_based_user_list': client.audience_rules(intent.url_contains)}
    client.validate_audience_create(values)
    state = {'account': client._creation_account(cid),
             'owned_lists': client.audience_inventory(cid, intent.name)}
    op = safe_create_operation('UserListService', values)
    checks = [{'result_index': 0, 'entity_type': 'user_list', 'customer_id': cid, 'expected': values}]
    plan = EntityMutationPlan(cid, [op], True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'create_custom_audience', 'customer_id': cid, 'account': state['account'],
               'name': intent.name, 'url_contains': list(intent.url_contains),
               'membership_status': 'OPEN', 'lookback_window_days': 30,
               'attached_campaigns': [], 'operations': _plan_canonical(plan)['operations'],
               'digest': digest, 'confirmation_required': True, 'warnings': [
                   'Creates a website-visitor UserList, not a CustomAudience interest segment.',
                   'OPEN can collect matching future visitors through existing tags, with a 30-day per-rule lookback. No prepopulation is requested.',
                   'No new campaign or ad-group attachment; not automatically targeted. New standalone creation needs no existing shared-resource consumer scan.',
                   'No tag, consent configuration, membership count or ad serving proof. Concurrent creation can race the owned-name check.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account, inventory or input changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class AddAudienceTargetingIntent:
    customer_id: str
    campaign_id: str
    audience_id: str
    targeting_mode: str


def audience_targeting_draft(intent):
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('add_audience_targeting', exc)
        raise
    return create_draft('add_audience_targeting', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_audience_targeting(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if not parse_bool_env('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', False):
        raise RailViolation('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT=true is required')
    if any(type(value) is not str for value in (intent.customer_id, intent.campaign_id, intent.audience_id)):
        raise RailViolation('IDs must be positive ASCII numeric strings')
    campaign_id, audience_id = _strict_id(intent.campaign_id), _strict_id(intent.audience_id)
    if type(intent.targeting_mode) is not str or intent.targeting_mode not in {'OBSERVATION', 'TARGETING'}:
        raise RailViolation('targeting_mode must be exactly OBSERVATION or TARGETING')
    state = client.audience_targeting_state(cid, campaign_id, audience_id, intent.targeting_mode)
    values = {'campaign': client.campaign_path(cid, campaign_id),
              'user_list': {'user_list': f'customers/{cid}/userLists/{audience_id}'},
              'negative': False, 'status': 'PAUSED'}
    checks = [{'result_index': 0, 'entity_type': 'campaign_criterion', 'customer_id': cid,
               'expected': values, 'audience_targeting': {'campaign_id': campaign_id,
                   'audience_id': audience_id, 'targeting_mode': intent.targeting_mode,
                   'state': state}}]
    plan = EntityMutationPlan(cid, [safe_create_operation('CampaignCriterionService', values)], True,
                              post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'add_audience_targeting', 'customer_id': cid, 'account': state['account'],
               'campaign': state['campaign'], 'audience': state['list'],
               'existing_connections': state['usage'],
               'attached_campaigns': sorted({values['campaign']} | {
                   item['campaign'] for item in state['usage']}),
               'targeting_mode': intent.targeting_mode, 'status': 'PAUSED',
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'confirmation_required': True, 'warnings': [
                   'The criterion and campaign remain PAUSED. The requested mode applies only after both are activated later.',
                   'OBSERVATION does not narrow reach; TARGETING narrows reach.',
                   'Existing campaign-level mode must already match; no targeting settings are changed.',
                   'Connections cover this account only. No list content, membership, tags or bids are changed.',
                   'No audience size, eligibility, tag readiness or live serving proof.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account, campaign, audience, settings or connections changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class CreateConversionActionIntent:
    customer_id: str
    name: str
    category: str


def conversion_action_draft(intent):
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('create_conversion_action', exc)
        raise
    return create_draft('create_conversion_action', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_conversion_action(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if not parse_bool_env('GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT', False):
        raise RailViolation('GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT=true is required')
    values = dict(client.CONVERSION_FIXED, name=intent.name, category=intent.category)
    client.validate_conversion_create(values)
    state = client.conversion_creation_state(cid, intent.category)
    if any(item['name'].casefold() == intent.name.casefold() for item in state['actions']):
        raise RailViolation('conversion action name already exists')
    owner = state['scope']['owner']
    checks = [{'result_index': 0, 'entity_type': 'conversion_action', 'customer_id': owner,
               'expected': values, 'conversion_creation': {'selected': cid, 'state': state}}]
    plan = EntityMutationPlan(owner, [safe_create_operation('ConversionActionService', values)],
                              True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'create_conversion_action', 'customer_id': cid, 'mutate_customer_id': owner,
               'action': values, 'control_scope': state['scope'], 'goal_effects': state['predictions'],
               'goal_and_custom_usage': state['accounts'], 'existing_custom_membership': [],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'confirmation_required': True, 'warnings': [
                   'Creates an ENABLED secondary WEBPAGE action. It can record conversions when tracking sends them; no website tag is installed and no events are uploaded.',
                   'A campaign using a custom goal containing this action can still bid on it regardless of primary status.',
                   'Membership is empty because the new action does not yet exist. All current custom goals and campaign usage are inventoried.',
                   'Google may automatically create the previewed goals. No explicit goal edits are sent; existing flags must remain unchanged.',
                   'Scope proves only the configured login manager tree, not invisible customers outside that tree.',
                   'Value and attribution settings are provider-managed; no revenue or attribution defaults are promised.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('conversion input, scope or goal settings changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class SetConversionActionPrimaryStatusIntent:
    customer_id: str
    conversion_action_id: str
    primary_for_goal: bool


def conversion_primary_status_draft(intent):
    tool = 'set_conversion_action_primary_status'
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused(tool, exc)
        raise
    return create_draft(tool, compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_conversion_primary_status(intent):
    check_writes_enabled()
    cid = _strict_id(intent.customer_id)
    if any(type(value) is not str or not re.fullmatch(r'[1-9][0-9]*', value)
           for value in (intent.customer_id, intent.conversion_action_id)):
        raise RailViolation('customer and conversion action IDs must be canonical positive numeric strings')
    action_id = _strict_id(intent.conversion_action_id)
    if type(intent.primary_for_goal) is not bool:
        raise RailViolation('primary_for_goal must be an exact boolean')
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if not allow_conversion_goal_edit():
        raise RailViolation('GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT=true is required')
    state = client.conversion_inventory_state(cid)
    owner = state['scope']['owner']
    rn = f'customers/{owner}/conversionActions/{action_id}'
    matches = [item for item in state['actions'] if item['resource_name'] == rn]
    if len(matches) != 1:
        raise RailViolation('existing conversion action must resolve exactly once')
    action = matches[0]
    if action['type_'] != 'WEBPAGE' or action['status'] != 'ENABLED':
        raise RailViolation('only existing ENABLED WEBPAGE actions are supported')
    if action['primary_for_goal'] is intent.primary_for_goal:
        raise RailViolation('conversion action already has requested primary status')
    expected = dict(action, primary_for_goal=intent.primary_for_goal)
    values = {'resource_name': rn, 'primary_for_goal': intent.primary_for_goal}
    checks = [{'result_index': 0, 'entity_type': 'conversion_action', 'customer_id': owner,
               'expected': expected, 'conversion_primary_status': {'selected': cid, 'state': state}}]
    plan = EntityMutationPlan(owner, [MutationOp('ConversionActionService', {'update': values},
                              ['primary_for_goal'])], True, post_checks=checks)
    digest = plan_digest(plan)
    effects, custom = [], {}
    for account, data in state['accounts'].items():
        configs = {item['campaign']: item for item in data['conversion_goal_campaign_config']}
        campaigns = {item['resource_name']: item for item in data['campaign']}
        for entity in ('customer_conversion_goal', 'campaign_conversion_goal'):
            for goal in data[entity]:
                if (goal['category'], goal['origin']) != (action['category'], action['origin']):
                    continue
                campaign = goal.get('campaign')
                config = configs.get(campaign)
                active_level = campaign is None or (config is not None and config['goal_config_level'] == 'CAMPAIGN')
                effects.append({'customer_id': account, 'entity': entity, 'goal': goal,
                                'campaign': campaigns.get(campaign), 'config': config,
                                'ordinary_eligible_before': goal['biddable'] and active_level and action['primary_for_goal'],
                                'ordinary_eligible_after': goal['biddable'] and active_level and intent.primary_for_goal,
                                'customer_settings_campaigns': [campaigns[key] for key, cfg in configs.items()
                                    if cfg['goal_config_level'] == 'CUSTOMER'] if campaign is None else [],
                                'effect': 'unchanged goal flag; eligibility only, not guaranteed serving'})
        for goal in data['custom_conversion_goal']:
            if rn in goal['conversion_actions']:
                custom.setdefault(goal['resource_name'], {'goal': goal, 'queried_customers': [], 'campaigns': []})
                membership = custom[goal['resource_name']]
                membership['queried_customers'].append(account)
                membership['campaigns'].extend({'campaign': campaigns[key], 'config': cfg}
                    for key, cfg in configs.items() if cfg['custom_conversion_goal'] == goal['resource_name'])
    preview = {'tool': 'set_conversion_action_primary_status', 'customer_id': cid,
               'mutate_customer_id': owner, 'action_before': action, 'action_after': expected,
               'control_scope': state['scope'], 'goal_effects': effects,
               'custom_membership': list(custom.values()), 'goal_and_custom_usage': state['accounts'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'confirmation_required': True, 'warnings': [
                   'A campaign using a custom goal containing this action can still bid on it regardless of primary status.',
                   'Primary is eligible only for relevant biddable ordinary goals, not guaranteed serving. Customer settings apply at CUSTOMER level; campaign settings at CAMPAIGN level.',
                   'Only primary_for_goal changes. No goal auto-creation or goal flag changes are expected.',
                   'Scope proves only the visible configured login manager tree, not invisible customers outside that tree.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('conversion input, scope or goal settings changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def check_apply_recommendation_enabled():
    if not parse_bool_env('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION', False):
        raise RailViolation('GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION=true is required')


def apply_recommendation_draft(customer_id, recommendation_id):
    try:
        compiled = _compile_apply_recommendation(customer_id, recommendation_id)
    except RailViolation as exc:
        _audit_refused('apply_recommendation', exc)
        raise
    return create_draft('apply_recommendation', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_apply_recommendation(cid, recommendation_id):
    import copy
    check_writes_enabled()
    check_apply_recommendation_enabled()
    if type(cid) is not str or not re.fullmatch(r'[1-9][0-9]*', cid):
        raise RailViolation('customer_id must be a canonical positive numeric string')
    if type(recommendation_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', recommendation_id):
        raise RailViolation('recommendation_id must be a nonempty safe opaque identifier')
    check_customer_allowlisted(cid, 'read')
    check_customer_allowlisted(cid, 'write')
    rec = client.recommendation_state(cid, recommendation_id)
    state = client.recommendation_budget_state(cid, rec['campaign_budget'])
    budget, attachments = state['budget'], state['attachments']
    if rec.get('campaign') and rec['campaign'] not in [c['resource_name'] for c in attachments]:
        raise RailViolation('recommendation campaign does not belong to budget')
    payload = rec['campaign_budget_recommendation']
    amount = payload['recommended_budget_amount_micros']
    if payload['current_budget_amount_micros'] != budget['amount_micros'] or amount == budget['amount_micros']:
        raise RailViolation('recommendation current amount differs or change is a no-op')
    anchor = attachments[0]
    compiled = _compile_update_campaign_budget(UpdateCampaignBudgetIntent(
        cid, anchor['id'], decimal.Decimal(amount) / 1000000), expected_info={
            'campaign_status': anchor['status'], 'budget_resource_name': budget['resource_name'],
            'amount': client.from_micros(budget['amount_micros']),
            'explicitly_shared': budget['explicitly_shared'], 'reference_count': budget['reference_count'],
            'period': budget['period'], 'aligned_bidding_strategy_id': None})
    fp = compiled.fingerprint
    if (fp['budget_resource_name'] != budget['resource_name']
            or fp['amount_micros_current'] != budget['amount_micros']
            or fp['explicitly_shared'] != budget['explicitly_shared']
            or fp['reference_count'] != budget['reference_count']
            or (budget['explicitly_shared'] and fp['attachments'] != attachments)
            or client.recommendation_budget_state(cid, rec['campaign_budget']) != state):
        raise RailViolation('budget reads disagree; re-draft', code='STATE_DRIFT')
    expected = copy.deepcopy(state)
    expected['budget']['amount_micros'] = amount
    checks = [{'recommendation_budget': True, 'customer_id': cid,
               'budget_resource_name': rec['campaign_budget'], 'expected': expected}]
    plan = RecommendationActionPlan(cid, 'apply', rec['resource_name'],
                                    new_budget_amount_micros=amount, post_checks=checks)
    client.validate_recommendation_plan(plan)
    fingerprint = {'recommendation': rec, 'state': state}
    digest = plan_digest(plan)
    preview = {'tool': 'apply_recommendation', 'customer_id': cid, 'recommendation': rec,
               'current_daily_budget': str(decimal.Decimal(budget['amount_micros']) / 1000000),
               'new_daily_budget': str(decimal.Decimal(amount) / 1000000), 'currency': budget['currency'],
               'affected_campaigns': attachments, 'digest': digest, 'confirmation_required': True,
               'required_gate': 'GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION=true',
               'warnings': ['No validation-only support or automated rollback. Reads and apply are not atomic.',
                            'An uncertain outcome requires reading account state before any retry.',
                            'Verification covers saved budget and campaign configuration, not recommendation disappearance or bidding results.']}

    def validate_fn():
        fresh = _compile_apply_recommendation(cid, recommendation_id)
        if fresh.fingerprint != fingerprint or plan_digest(fresh.plan) != digest:
            raise RailViolation('recommendation or budget state changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, fingerprint, validate_fn)


def _validate_recommendation_draft(plan, preview, fingerprint, validate_fn):
    """Mandatory bounded compilation, independent of a mutable draft callback or tool name."""
    client.validate_recommendation_plan(plan)
    if not callable(validate_fn):
        raise RailViolation('recommendation draft requires its validation callback')
    rid = plan.recommendation_resource_name.rsplit('/', 1)[1]
    fresh = (_compile_apply_recommendation(plan.mutate_customer_id, rid) if plan.rpc == 'apply'
             else compile(DismissRecommendationIntent(plan.mutate_customer_id, rid)))
    if (fresh.fingerprint != fingerprint or plan_digest(fresh.plan) != plan_digest(plan)
            or fresh.preview != preview):
        raise RailViolation('recommendation draft differs from fresh bounded approval', code='STATE_DRIFT')


@dataclass(frozen=True)
class DismissRecommendationIntent:
    customer_id: str
    recommendation_id: str


def dismiss_recommendation_draft(intent):
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('dismiss_recommendation', exc)
        raise
    return create_draft('dismiss_recommendation', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_dismiss_recommendation(intent):
    cid, rid = intent.customer_id, intent.recommendation_id
    check_writes_enabled()
    if type(cid) is not str or not re.fullmatch(r'[1-9][0-9]*', cid):
        raise RailViolation('customer_id must be a canonical positive numeric string')
    if type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', rid):
        raise RailViolation('recommendation_id must be a nonempty safe opaque identifier')
    check_customer_allowlisted(cid, 'read')
    check_customer_allowlisted(cid, 'write')
    rec = client.recommendation_dismiss_state(cid, rid)
    if rec['dismissed']:
        raise RailViolation('recommendation already dismissed; no change to draft')
    expected = dict(rec, dismissed=True)
    plan = RecommendationActionPlan(cid, 'dismiss', rec['resource_name'], post_checks=[{
        'recommendation_dismiss': True, 'customer_id': cid,
        'resource_name': rec['resource_name'], 'expected': expected}])
    client.validate_recommendation_plan(plan)
    fingerprint = {'intent': {'customer_id': cid, 'recommendation_id': rid}, 'recommendation': rec}
    digest = plan_digest(plan)
    preview = {'tool': 'dismiss_recommendation', 'customer_id': cid, 'recommendation_id': rid,
               'recommendation': rec, 'expected': expected, 'digest': digest,
               'confirmation_required': True,
               'warnings': ['Dismisses only this suggestion; does not execute its proposal or alter ads, budget or bidding.',
                            'Does not disable future suggestions or auto-apply subscriptions; no guarantee of suppression duration or undo.',
                            'No validation-only support, automatic rollback or automatic retry. Reads and dismissal are not atomic.',
                            'Verification covers only the projected recommendation identity and dismissed flag; live visibility and timing are unverified.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != fingerprint or plan_digest(fresh.plan) != digest:
            raise RailViolation('recommendation changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, fingerprint, validate_fn)


@dataclass(frozen=True)
class CreatePMaxCampaignIntent:
    customer_id: str
    campaign_name: str
    asset_group_name: str
    daily_budget: object
    target_cpa: object
    geo_target_ids: list[str]
    language_ids: list[str]
    headlines: list[str]
    long_headlines: list[str]
    descriptions: list[str]
    business_name: str
    final_url: str
    landscape_image_asset_id: str
    square_image_asset_id: str
    logo_asset_id: str
    contains_eu_political_advertising: bool


@dataclass(frozen=True)
class CreateDemandGenCampaignIntent:
    customer_id: str
    campaign_name: str
    daily_budget: object
    geo_target_ids: list[str]
    language_ids: list[str]
    contains_eu_political_advertising: bool


@dataclass(frozen=True)
class CreateAssetGroupIntent:
    customer_id: str
    campaign_id: str
    asset_group_name: str
    headlines: list[str]
    long_headlines: list[str]
    descriptions: list[str]
    final_url: str
    landscape_image_asset_id: str
    square_image_asset_id: str


@dataclass(frozen=True)
class UpdateAssetGroupIntent:
    customer_id: str
    asset_group_id: str
    name: str | None = None
    final_url: str | None = None


@dataclass(frozen=True)
class AddAssetGroupAssetsIntent:
    customer_id: str
    asset_group_id: str
    assets: list[dict]


@dataclass(frozen=True)
class RemoveAssetGroupAssetIntent:
    customer_id: str
    asset_group_id: str
    asset_id: str
    field_type: str


def asset_group_asset_add_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('add_asset_group_assets', exc)
        raise
    return create_draft('add_asset_group_assets', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def asset_group_asset_remove_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('remove_asset_group_asset', exc)
        raise
    return create_draft('remove_asset_group_asset', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def asset_group_update_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('update_asset_group', exc)
        raise
    return create_draft('update_asset_group', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_asset_group_update(intent):
    check_writes_enabled()
    cid, group_id = client.pmax_id(intent.customer_id), client.pmax_id(intent.asset_group_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if intent.name is None and intent.final_url is None:
        raise RailViolation('asset-group update requires name and/or final_url', code='BAD_INPUT')
    if intent.name is not None and type(intent.name) is not str:
        raise RailViolation('asset-group name must be an actual string', code='BAD_INPUT')
    if intent.final_url is not None and type(intent.final_url) is not str:
        raise RailViolation('asset-group final_url must be an actual string', code='BAD_INPUT')
    if any(value is not None and value.strip() == 'null'
           for value in (intent.name, intent.final_url)):
        raise RailViolation('literal null is not an asset-group update value', code='BAD_INPUT')
    name = None if intent.name is None else client.creation_name(intent.name)
    if name is not None:
        check_content([name])
    url = None if intent.final_url is None else client.pmax_url(intent.final_url)
    state = client.pmax_asset_group_update_state(cid, group_id, name)
    target = state['target']
    if name is not None and name == target['name']:
        raise RailViolation('requested asset-group name is unchanged', code='NO_CHANGES')
    if url is not None and [url] == target['final_urls']:
        raise RailViolation('requested asset-group final URL is unchanged', code='NO_CHANGES')
    values = {'resource_name': target['resource_name']}
    masks = []
    if name is not None:
        values['name'] = name
        masks.append('name')
    if url is not None:
        values['final_urls'] = [url]
        masks.append('final_urls')
    expected = dict(target)
    expected.update({key: value for key, value in values.items() if key != 'resource_name'})
    check = {'pmax_asset_group_update': True, 'customer_id': cid,
             'resource_name': target['resource_name'], 'campaign': target['campaign'],
             'before': target, 'expected': expected,
             'parent_proof': state['parent_proof'], 'asset_groups': state['asset_groups']}
    plan = EntityMutationPlan(cid, [MutationOp('AssetGroupService', {'update': values}, masks)],
                              True, post_checks=[check])
    digest = plan_digest(plan)
    preview = {'tool': 'update_asset_group', 'customer_id': cid,
               'target': target['resource_name'], 'parent': target['campaign'],
               'target_status': target['status'],
               'parent_status': state['parent_proof']['parent']['status'],
               'inherited_branding': state['parent_proof']['branding'],
               'old': {'name': target['name'], 'final_url': target['final_urls'][0]},
               'new': {'name': expected['name'], 'final_url': expected['final_urls'][0]},
               'update_mask': masks, 'operations': _plan_canonical(plan)['operations'],
               'digest': digest,
               'warnings': ['Campaign, asset-group status, branding, creatives, bidding, targeting and automation remain unchanged.',
                            'Provider acceptance, policy approval and serving are not established offline.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account, parent, branding, target or sibling state changed since preview',
                                code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_asset_group_asset_add(intent):
    import copy

    check_writes_enabled()
    cid, group_id = client.pmax_id(intent.customer_id), client.pmax_id(intent.asset_group_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    if type(intent.assets) is not list or not 1 <= len(intent.assets) <= 65:
        raise RailViolation('assets requires an actual nonempty list of at most 65 items',
                            code='BAD_INPUT')
    additions, seen = [], set()
    for item in intent.assets:
        if (type(item) is not dict or set(item) != {'asset_id', 'field_type'}
                or type(item.get('asset_id')) is not str
                or type(item.get('field_type')) is not str):
            raise RailViolation('each asset requires exactly string asset_id and field_type',
                                code='BAD_INPUT')
        asset_id = client.pmax_id(item['asset_id'])
        role = item['field_type']
        if role not in client.PMAX_ASSET_GROUP_ROLES:
            raise RailViolation('asset field_type is outside the supported PMax roles',
                                code='BAD_INPUT')
        pair = (asset_id, role)
        if pair in seen:
            raise RailViolation('requested asset and field_type pairs must be distinct',
                                code='BAD_INPUT')
        seen.add(pair)
        additions.append({'asset': client.asset_path(cid, asset_id), 'field_type': role})
    state = client.pmax_asset_group_asset_state(cid, group_id, additions)
    proof_by_pair = {(item['asset'], item['field_type']): item
                     for item in state['requested_assets']}
    ordered = [copy.deepcopy(proof_by_pair[(item['asset'], item['field_type'])])
               for item in additions]
    operations = [safe_create_operation('AssetGroupAssetService', {
        'asset_group': state['target']['resource_name'], 'asset': item['asset'],
        'field_type': item['field_type']}) for item in additions]
    check = {'pmax_asset_group_assets': True, 'customer_id': cid,
             'asset_group': state['target']['resource_name'],
             'campaign': state['target']['campaign'],
             'target': copy.deepcopy(state['target']),
             'parent_proof': copy.deepcopy(state['parent_proof']),
             'existing_assets': copy.deepcopy(state['existing_assets']),
             'requested_assets': copy.deepcopy(state['requested_assets']),
             'additions': copy.deepcopy(ordered)}
    plan = EntityMutationPlan(cid, operations, True, post_checks=[check])
    digest = plan_digest(plan)
    current_counts = client.pmax_asset_group_role_counts(state['existing_assets'])
    result_counts = client.pmax_asset_group_role_counts(
        [*state['existing_assets'], *ordered])
    preview = {'tool': 'add_asset_group_assets', 'customer_id': cid,
               'asset_group': copy.deepcopy(state['target']),
               'campaign': copy.deepcopy(state['parent_proof']['parent']),
               'inherited_branding': copy.deepcopy(state['parent_proof']['branding']),
               'current_counts': current_counts, 'resulting_counts': result_counts,
               'additions': copy.deepcopy(ordered), 'status': 'PAUSED',
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': [
                   'The asset group, parent campaign, inherited branding and existing links remain unchanged.',
                   'Only existing owned assets are linked; no asset content is created or uploaded.',
                   'Provider acceptance, policy approval and serving are not established offline.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation(
                'account, parent, branding, asset group, links or requested assets changed since preview',
                code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_asset_group_asset_remove(intent):
    import copy
    check_writes_enabled()
    cid = client.pmax_id(intent.customer_id)
    group_id = client.pmax_id(intent.asset_group_id)
    asset_id = client.pmax_id(intent.asset_id)
    if type(intent.field_type) is not str or intent.field_type not in client.PMAX_ASSET_GROUP_ROLES:
        raise RailViolation('field_type is outside the supported PMax roles', code='BAD_INPUT')
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    role = intent.field_type
    asset_rn = client.asset_path(cid, asset_id)
    state = client.pmax_asset_group_asset_state(cid, group_id)
    matches = [item for item in state['existing_assets']
               if item['asset'] == asset_rn and item['field_type'] == role]
    if len(matches) != 1:
        raise RailViolation('target asset-group connection is missing, removed or ambiguous')
    selected = client._pmax_requested_asset_proofs(
        cid, [{'asset': asset_rn, 'field_type': role}])
    if len(selected) != 1 or selected[0] != {
            'asset': asset_rn, 'field_type': role, 'content': matches[0]['content']}:
        raise RailViolation('selected bare asset proof does not match the target connection')
    target_link = copy.deepcopy(matches[0])
    remaining = copy.deepcopy([item for item in state['existing_assets']
                               if item['resource_name'] != target_link['resource_name']])
    client._validate_pmax_asset_group_creatives(
        cid, state['target']['resource_name'], remaining, [], require_complete=True)
    resource_name = client.pmax_asset_group_asset_path(
        cid, group_id, asset_id, client.PMAX_FIELD_NUMBERS[role])
    if resource_name != target_link['resource_name']:
        raise RailViolation('target compound connection identity does not reconcile')
    check = {'pmax_asset_group_asset_removal': True, 'customer_id': cid,
             'resource_name': resource_name, 'asset_group': state['target']['resource_name'],
             'campaign': state['target']['campaign'], 'field_type': role,
             'target': copy.deepcopy(state['target']),
             'parent_proof': copy.deepcopy(state['parent_proof']),
             'current_assets': copy.deepcopy(state['existing_assets']),
             'target_link': copy.deepcopy(target_link),
             'selected_asset': copy.deepcopy(selected[0]),
             'remaining_assets': copy.deepcopy(remaining)}
    plan = EntityMutationPlan(
        cid, [MutationOp('AssetGroupAssetService', {'remove': resource_name}, None)],
        True, post_checks=[check])
    digest = plan_digest(plan)
    preview = {'tool': 'remove_asset_group_asset', 'customer_id': cid,
               'asset_group': copy.deepcopy(state['target']),
               'campaign': copy.deepcopy(state['parent_proof']['parent']),
               'inherited_branding': copy.deepcopy(state['parent_proof']['branding']),
               'connection': copy.deepcopy(target_link),
               'selected_asset': copy.deepcopy(selected[0]),
               'current_counts': client.pmax_asset_group_role_counts(state['existing_assets']),
               'remaining_counts': client.pmax_asset_group_role_counts(remaining),
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': [
                   'Only this one asset-group role connection is removed; the bare asset remains.',
                   'The group, parent, branding and every unrelated connection remain unchanged.',
                   'Provider acceptance, policy approval and serving are not established offline.']}
    fingerprint = {**copy.deepcopy(state), 'selected_asset': copy.deepcopy(selected[0])}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != fingerprint or plan_digest(fresh.plan) != digest:
            raise RailViolation(
                'account, parent, branding, asset group, links or selected asset changed since preview',
                code='STATE_DRIFT')

    return CompiledPlan(preview, plan, fingerprint, validate_fn)


def _compile_asset_group_creation(intent):
    check_writes_enabled()
    cid, campaign_id = client.pmax_id(intent.customer_id), client.pmax_id(intent.campaign_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    name = client.creation_name(intent.asset_group_name)
    check_content([name])
    texts = {role: client.pmax_texts(role, items) for role, items in (
        ('HEADLINE', intent.headlines), ('LONG_HEADLINE', intent.long_headlines),
        ('DESCRIPTION', intent.descriptions))}
    url = client.pmax_url(intent.final_url)
    images = {role: client.asset_path(cid, client.pmax_id(identity)) for role, identity in (
        ('MARKETING_IMAGE', intent.landscape_image_asset_id),
        ('SQUARE_MARKETING_IMAGE', intent.square_image_asset_id))}
    state = client.pmax_asset_group_state(cid, campaign_id, name, images)
    image_proof = {role: client.pmax_image(state['parent_proof']['images'][role], cid, role, images[role])
                   for role in images}
    group_rn = client.pmax_temporary_path(cid, 'assetGroups', -3)
    campaign_rn = client.campaign_path(cid, campaign_id)
    operations = [safe_create_operation('AssetGroupService', {
        'resource_name': group_rn, 'campaign': campaign_rn, 'name': name, 'final_urls': [url]})]
    definitions = {}
    for items in texts.values():
        for text in items:
            if text not in definitions:
                rn = client.pmax_temporary_path(cid, 'assets', -4 - len(definitions))
                definitions[text] = rn
                operations.append(safe_create_operation(
                    'AssetService', {'resource_name': rn, 'text_asset': {'text': text}}))
    for role, items in texts.items():
        operations.extend(safe_create_operation('AssetGroupAssetService', {
            'asset_group': group_rn, 'asset': definitions[text], 'field_type': role}) for text in items)
    for role in images:
        operations.append(safe_create_operation('AssetGroupAssetService', {
            'asset_group': group_rn, 'asset': images[role], 'field_type': role}))
    checks = client.pmax_checks(cid, operations, image_proof,
                                parent_proof=state['parent_proof'], asset_group=True)
    plan = EntityMutationPlan(cid, operations, True, post_checks=checks)
    digest = plan_digest(plan)
    preview = {'tool': 'create_asset_group', 'customer_id': cid, 'account': state['parent_proof']['account'],
               'campaign': state['parent_proof']['parent'], 'inherited_branding': state['parent_proof']['branding'],
               'asset_group_name': name, 'status': 'PAUSED', 'final_url': url,
               'images': image_proof, 'existing_asset_groups': state['asset_groups'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['The existing campaign settings, business name and logo are inherited unchanged.',
                            'Google may automatically generate video when no video is supplied.',
                            'Provider acceptance, policy approval and serving are not established offline.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account, parent campaign, branding, images or asset-group names changed since preview',
                                code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_pmax_creation(intent):
    check_writes_enabled()
    cid = client.pmax_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    name, group_name = client.creation_name(intent.campaign_name), client.creation_name(intent.asset_group_name)
    check_content([name, group_name])
    budget_micros = _checked_money_micros(_amount(intent.daily_budget, check_budget))
    target_micros = _checked_money_micros(_amount(intent.target_cpa, check_target_cpa))
    geo_ids, language_ids = _creation_ids(intent.geo_target_ids), _creation_ids(intent.language_ids)
    texts = {role: client.pmax_texts(role, items) for role, items in (
        ('HEADLINE', intent.headlines), ('LONG_HEADLINE', intent.long_headlines),
        ('DESCRIPTION', intent.descriptions), ('BUSINESS_NAME', [intent.business_name]))}
    url = client.pmax_url(intent.final_url)
    if type(intent.contains_eu_political_advertising) is not bool:
        raise RailViolation('PMax political declaration must be an explicit boolean')
    images = {role: client.asset_path(cid, client.pmax_id(identity)) for role, identity in zip(
        client.PMAX_IMAGE_ROLES, (intent.landscape_image_asset_id, intent.square_image_asset_id, intent.logo_asset_id))}
    state = client.pmax_creation_state(cid, name, geo_ids, language_ids, images)
    image_proof = {role: client.pmax_image(state['images'][role], cid, role, images[role]) for role in images}
    budget_rn, campaign_rn = client.creation_paths(cid)
    group_rn = client.pmax_temporary_path(cid, 'assetGroups', -3)
    operations = [safe_create_operation('CampaignBudgetService', {
        'resource_name': budget_rn, 'amount_micros': budget_micros, 'explicitly_shared': False,
        'period': 'DAILY', 'delivery_method': 'STANDARD'}), safe_create_operation('CampaignService', {
        'resource_name': campaign_rn, 'name': name, 'campaign_budget': budget_rn,
        'advertising_channel_type': 'PERFORMANCE_MAX', 'brand_guidelines_enabled': True,
        'maximize_conversions': {'target_cpa_micros': target_micros},
        'geo_target_type_setting': dict(client.SEARCH_GEO_OPTIONS),
        'contains_eu_political_advertising': client.POLITICAL_DECLARATIONS[intent.contains_eu_political_advertising],
        'asset_automation_settings': [{'asset_automation_type': kind, 'asset_automation_status': 'OPTED_OUT'}
                                      for kind in client.PMAX_AUTOMATIONS]})]
    for role, ids, constant_field, helper in [('location', geo_ids, 'geo_target_constant', client.geo_target_constant_path),
                                     ('language', language_ids, 'language_constant', client.language_constant_path)]:
        operations.extend(safe_create_operation('CampaignCriterionService', {
            'campaign': campaign_rn, 'negative': False, role: {constant_field: helper(identity)}}) for identity in ids)
    operations.append(safe_create_operation('AssetGroupService', {
        'resource_name': group_rn, 'campaign': campaign_rn, 'name': group_name, 'final_urls': [url]}))
    definitions = {}
    for items in texts.values():
        for text in items:
            if text not in definitions:
                rn = client.pmax_temporary_path(cid, 'assets', -4 - len(definitions))
                definitions[text] = rn
                operations.append(safe_create_operation('AssetService', {'resource_name': rn, 'text_asset': {'text': text}}))
    for role in ('HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION'):
        operations.extend(safe_create_operation('AssetGroupAssetService', {
            'asset_group': group_rn, 'asset': definitions[text], 'field_type': role}) for text in texts[role])
    for role in ('MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'):
        operations.append(safe_create_operation('AssetGroupAssetService', {
            'asset_group': group_rn, 'asset': images[role], 'field_type': role}))
    operations.extend([safe_create_operation('CampaignAssetService', {
        'campaign': campaign_rn, 'asset': definitions[intent.business_name], 'field_type': 'BUSINESS_NAME'}),
        safe_create_operation('CampaignAssetService', {'campaign': campaign_rn, 'asset': images['LOGO'], 'field_type': 'LOGO'})])
    plan = EntityMutationPlan(cid, operations, True, post_checks=client.pmax_checks(cid, operations, image_proof))
    digest = plan_digest(plan)
    preview = {'tool': 'create_pmax_campaign', 'customer_id': cid, 'account': state['account'],
               'campaign_name': name, 'asset_group_name': group_name, 'status': 'PAUSED',
               'daily_budget': client.from_micros(budget_micros), 'target_cpa': client.from_micros(target_micros),
               'locations': state['locations'], 'languages': state['languages'], 'images': image_proof,
               'automated_call_to_action': True, 'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['Google may automatically generate video when no video is supplied.',
                            'Legal business-name verification and provider acceptance are not established offline.',
                            'Identical text assets may be reused by Google; new allocation is not guaranteed.',
                            'Names are checked again before apply but concurrent creation can race this check.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def _compile_demand_gen_creation(intent):
    check_writes_enabled()
    cid = client.pmax_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    name = client.creation_name(intent.campaign_name)
    check_content([name])
    budget_micros = _checked_money_micros(_amount(intent.daily_budget, check_budget))
    geo_ids = _creation_ids(intent.geo_target_ids)
    language_ids = _creation_ids(intent.language_ids)
    if type(intent.contains_eu_political_advertising) is not bool:
        raise RailViolation('Demand Gen political declaration must be an explicit boolean')
    state = client.demand_gen_creation_state(cid, name, geo_ids, language_ids)
    if state['collisions']:
        raise RailViolation('requested Demand Gen campaign or budget name already exists')
    budget_rn, campaign_rn = client.creation_paths(cid)
    operations = [safe_create_operation('CampaignBudgetService', {
        'resource_name': budget_rn, 'amount_micros': budget_micros,
        'explicitly_shared': False, 'period': 'DAILY', 'delivery_method': 'STANDARD'}),
        safe_create_operation('CampaignService', {
            'resource_name': campaign_rn, 'name': name, 'campaign_budget': budget_rn,
            'advertising_channel_type': 'DEMAND_GEN', 'maximize_conversions': {},
            'demand_gen_campaign_settings': {'upgraded_targeting': False},
            'geo_target_type_setting': dict(client.SEARCH_GEO_OPTIONS),
            'contains_eu_political_advertising':
                client.POLITICAL_DECLARATIONS[intent.contains_eu_political_advertising]})]
    for role, ids, target_field, helper in (
            ('location', geo_ids, 'geo_target_constant', client.geo_target_constant_path),
            ('language', language_ids, 'language_constant', client.language_constant_path)):
        operations.extend(safe_create_operation('CampaignCriterionService', {
            'campaign': campaign_rn, 'negative': False,
            role: {target_field: helper(identity)}})
            for identity in ids)
    family = {'strategy': 'MAXIMIZE_CONVERSIONS', 'upgraded_targeting_present': True,
              'upgraded_targeting': False,
              'locations': [client.geo_target_constant_path(identity) for identity in geo_ids],
              'languages': [client.language_constant_path(identity) for identity in language_ids]}
    plan = EntityMutationPlan(
        cid, operations, True,
        post_checks=client.demand_gen_checks(cid, operations, family))
    client.validate_mutation_plan(plan)
    digest = plan_digest(plan)
    preview = {
        'tool': 'create_demand_gen_campaign', 'customer_id': cid, 'account': state['account'],
        'campaign_name': name, 'effective_budget_name': name, 'status': 'PAUSED',
        'daily_budget': client.from_micros(budget_micros),
        'bidding_strategy': 'MAXIMIZE_CONVERSIONS', 'strategy_target': None,
        'locations': state['locations'], 'languages': state['languages'],
        'targeting_ownership': 'CAMPAIGN_LEVEL_IMMUTABLE',
        'contains_eu_political_advertising': intent.contains_eu_political_advertising,
        'ad_groups_created': False, 'ads_created': False,
        'operations': _plan_canonical(plan)['operations'], 'digest': digest,
        'warnings': [
            'Locations and languages belong to the campaign; this campaign cannot later switch to upgraded ad-group targeting.',
            'Names are checked again before apply but concurrent creation can race this check.',
            'An authorized live creation leaves a paused campaign, its dedicated budget and targeting resources in the account, including if saved-state verification fails. Creation is not reversible through this tool; this tool does not automatically undo creation.',
            'Offline proof does not establish Google acceptance, policy approval or serving.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account, names or targeting changed since preview', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class SetListingGroupFilterIntent:
    customer_id: str
    asset_group_id: str
    product_item_ids: list[str]


def listing_filter_draft(intent):
    import copy
    try:
        compiled = compile(copy.deepcopy(intent))
    except RailViolation as exc:
        _audit_refused('set_listing_group_filter', exc)
        raise
    return create_draft('set_listing_group_filter', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


def _compile_listing_filter(intent):
    import copy
    check_writes_enabled()
    cid, group_id = client.pmax_id(intent.customer_id), client.pmax_id(intent.asset_group_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    ids = client.listing_item_ids(intent.product_item_ids)
    state = client.listing_filter_state(cid, group_id)
    old, current = client._listing_tree(state['tree'], cid, group_id)
    if current == ids:
        raise RailViolation('requested product item ID set is unchanged', code='NO_CHANGES')
    check = {'listing_filter': True, 'customer_id': cid, 'group_id': group_id,
             'before': copy.deepcopy(state), 'item_ids': ids}
    plan = EntityMutationPlan(cid, client.listing_filter_operations(cid, group_id, old, ids),
                              True, post_checks=[check])
    client.validate_listing_filter_plan(plan)
    group, campaign = client._validate_listing_proof(state['proof'], cid, group_id)
    digest = plan_digest(plan)
    preview = {'tool': 'set_listing_group_filter', 'customer_id': cid,
               'asset_group': group.resource_name, 'asset_group_status': 'PAUSED',
               'campaign': campaign.resource_name, 'campaign_status': 'PAUSED',
               'merchant_id': str(campaign.shopping_setting.merchant_id),
               'feed_label': campaign.shopping_setting.feed_label,
               'old_item_ids': current, 'old_all_products': current is None,
               'new_item_ids': ids, 'remaining_products': 'All remaining product IDs are explicitly excluded.',
               'remove_count': len(old), 'create_count': len(ids) + 2,
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'warnings': ['Draft only. Replacing an all-products root narrows this group to the listed IDs.',
                            'Merchant/feed identify configured scope only; product existence, eligibility, availability and serving are not proved offline.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('listing account, group, retail parent or old tree changed', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class DraftDemandGenAdIntent:
    customer_id: str
    ad_group_id: str
    headline: str
    description: str
    business_name: str
    square_marketing_image_asset_id: str
    logo_image_asset_id: str
    final_url: str


def _compile_demand_gen_ad(intent):
    check_writes_enabled()
    cid, gid = client.demand_gen_ad_id(intent.customer_id), client.demand_gen_ad_id(intent.ad_group_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    inputs = {key: getattr(intent, key) for key in client.DEMAND_GEN_AD_INPUTS}
    operation = client.demand_gen_ad_operation(cid, inputs)
    state = client.demand_gen_ad_state(cid, gid, inputs)
    plan = EntityMutationPlan(cid, [operation], True,
                              post_checks=client.demand_gen_ad_checks(cid, inputs, state))
    client.validate_demand_gen_ad_plan(plan)
    digest = plan_digest(plan)
    preview = {'tool': 'draft_demand_gen_ad', 'customer_id': cid, 'account': state['account'],
               'campaign': state['campaign'], 'ad_group': state['group'],
               'family': 'Demand Gen single-image multi-asset ad', 'status': 'PAUSED',
               'creative': inputs, 'assets': state['assets'], 'roles': state['roles'],
               'operations': _plan_canonical(plan)['operations'], 'digest': digest,
               'evidence_tier': 'Offline schema and tests only; provider acceptance unproven',
               'warnings': ['Requires an externally existing PAUSED Demand Gen ad group and campaign; '
                            'the existing create_ad_group tool remains Search-only.',
                            'Creates one PAUSED ad, with no inventory upload or asset associations.',
                            'Creation is not reversible; any later authorized live use can leave paused '
                            'residue. This tool does not automatically undo creation.']}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('account changed since preview; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


@dataclass(frozen=True)
class CreateSharedNegativeSetIntent:
    customer_id: str
    name: str


def _compile_shared_negative_creation(intent):
    check_writes_enabled()
    if not parse_bool_env('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', False):
        raise RailViolation('shared negative sets require GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT=true')
    cid = client.demand_gen_ad_id(intent.customer_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    operation = client.shared_negative_create_operation(intent.name)
    state = client.shared_negative_creation_state(cid, intent.name)
    plan = EntityMutationPlan(cid, [operation], True,
        post_checks=client.shared_negative_create_checks(cid, intent.name, state))
    client.validate_mutation_plan(plan)
    digest = plan_digest(plan)
    preview = {'tool': 'create_shared_negative_set', 'customer_id': cid,
        'account': state['account'], 'account_currency': state['account']['currency_code'],
        'name': intent.name, 'type': 'NEGATIVE_KEYWORDS', 'attached_campaigns': [],
        'limitation': 'Empty unattached inventory has no serving effect. Adding exclusions, '
                      'attaching to a PAUSED standard Search campaign and later enabling '
                      'that campaign are separate prerequisites for serving effects. '
                      'Saved verification does not prove provider serving approval.',
        'warnings': ['Name checks are point-in-time evidence; external creation can race them.'],
        'operations': _plan_canonical(plan)['operations'], 'digest': digest}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('shared negative inventory changed; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def shared_negative_creation_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('create_shared_negative_set', exc)
        raise
    return create_draft('create_shared_negative_set', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


@dataclass(frozen=True)
class AddToSharedSetIntent:
    customer_id: str
    shared_set_id: str
    keywords: list[dict]


def _compile_shared_negative_add(intent):
    check_writes_enabled()
    if not parse_bool_env('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', False):
        raise RailViolation('shared negative sets require GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT=true')
    cid = client.demand_gen_ad_id(intent.customer_id)
    sid = client.demand_gen_ad_id(intent.shared_set_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    keywords = client.shared_negative_keywords(intent.keywords)
    operations = client.shared_negative_add_operations(cid, sid, keywords)
    state = client.shared_negative_population(cid, sid)
    plan = EntityMutationPlan(cid, operations, True,
        post_checks=client.shared_negative_add_checks(cid, sid, keywords, state))
    client.validate_mutation_plan(plan)
    digest = plan_digest(plan)
    preview = {'tool': 'add_to_shared_set', 'customer_id': cid, 'shared_set': state['set'],
        'account': state['account'], 'keywords': keywords, 'attached_campaigns': state['campaigns'],
        'provider_member_count': state['set']['member_count'],
        'provider_reference_count': state['set']['reference_count'],
        'complete_member_population_count': len(state['members']),
        'complete_active_campaign_count': len(state['campaigns']),
        'limitation': 'Adding keywords modifies the shared list used by every listed campaign, '
                     'not only one selected campaign. Later enabling campaigns activates these exclusions. '
                     'PAUSED status is point-in-time proof; outside changes can race dispatch. '
                     'Offline verification does not establish provider acceptance or serving.',
        'operations': _plan_canonical(plan)['operations'], 'digest': digest}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('shared negative population changed; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def shared_negative_add_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('add_to_shared_set', exc)
        raise
    return create_draft('add_to_shared_set', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)


@dataclass(frozen=True)
class AttachSharedSetIntent:
    customer_id: str
    shared_set_id: str
    campaign_id: str


def _compile_shared_negative_attach(intent):
    check_writes_enabled()
    if not parse_bool_env('GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT', False):
        raise RailViolation('shared negative sets require GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT=true')
    cid = client.demand_gen_ad_id(intent.customer_id)
    sid = client.demand_gen_ad_id(intent.shared_set_id)
    campaign_id = client.demand_gen_ad_id(intent.campaign_id)
    check_customer_allowlisted(cid, 'write')
    check_customer_allowlisted(cid, 'read')
    operation = client.shared_negative_attach_operation(cid, sid, campaign_id)
    proof = client.shared_negative_population(cid, sid)
    target = client.shared_negative_campaign(cid, client.campaign_path(cid, campaign_id))
    state = {'proof': proof, 'target': target}
    plan = EntityMutationPlan(cid, [operation], True,
        post_checks=client.shared_negative_attach_checks(cid, sid, campaign_id, proof, target))
    client.validate_mutation_plan(plan)
    digest = plan_digest(plan)
    warnings = ['The link has no pause control: its ENABLED status is output-only. Exclusions take effect '
                'when this campaign is enabled. Later list edits affect all attached campaigns.',
                'PAUSED campaign status is point-in-time proof; outside changes can race dispatch. '
                'Offline checks do not prove provider acceptance or serving.']
    if not proof['members']:
        warnings.append('This list currently contains no exclusions; its shared membership can change later.')
    preview = {'tool': 'attach_shared_set', 'customer_id': cid, 'shared_set': proof['set'],
        'account': proof['account'], 'account_currency': proof['account']['currency_code'],
        'keywords': [item['keyword'] for item in proof['members']],
        'attached_campaigns': proof['campaigns'],
        'proposed_attached_campaigns': sorted(proof['campaigns'] + [target], key=lambda item: item['resource_name']),
        'provider_member_count': proof['set']['member_count'],
        'provider_reference_count': proof['set']['reference_count'],
        'complete_member_population_count': len(proof['members']),
        'complete_active_campaign_count': len(proof['campaigns']),
        'warnings': warnings, 'operations': _plan_canonical(plan)['operations'], 'digest': digest}

    def validate_fn():
        fresh = compile(intent)
        if fresh.fingerprint != state or plan_digest(fresh.plan) != digest:
            raise RailViolation('shared population or target changed; create a fresh draft', code='STATE_DRIFT')

    return CompiledPlan(preview, plan, state, validate_fn)


def shared_negative_attach_draft(intent):
    import copy
    intent = copy.deepcopy(intent)
    try:
        compiled = compile(intent)
    except RailViolation as exc:
        _audit_refused('attach_shared_set', exc)
        raise
    return create_draft('attach_shared_set', compiled.preview, compiled.plan,
                        compiled.fingerprint, compiled.validate_fn)
