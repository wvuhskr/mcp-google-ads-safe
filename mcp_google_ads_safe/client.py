"""client.py — the SOLE Google-Ads-API choke point.

This module is the ONLY place in the package that is allowed to import
`google.ads.googleads` and the only place that opens a network connection or issues a
gRPC call. rails.py imports client (never the reverse), so client MUST NOT import rails at
load time: where a rail must be raised, it does a FUNCTION-LOCAL `from .rails import
RailViolation`, and `_dispatch` switches on `plan.kind` / `plan.operations` by DUCK TYPING,
never by importing the MutationPlan dataclasses.

Everything Google-API-shaped goes through a single GoogleAdsClient obtained from `gads()`
(the one monkeypatch point in tests): search reads via a fail-closed complete-scan
primitive, and the ONE state-changing path `_dispatch`. Money<->micros lives only here.
"""
import base64
import binascii
import decimal
import hashlib
import io
import json
import os
import re
import warnings

import yaml
from google.ads.googleads.client import GoogleAdsClient
from google.api_core import exceptions as api_exceptions
from PIL import Image, ImageFile

_INT64_MAX = 2**63 - 1
_MICROS = decimal.Decimal(1_000_000)

_LIMIT_RE = re.compile(r"\bLIMIT\b", re.IGNORECASE)
# ponytail: fail-closed ceiling on an internal scan (10M rows at 10k/page). If a real safety
# scan legitimately needs more, raise this AND revisit whether it should be scanning that wide.
_SCAN_PAGE_CAP = 1000
_ANCESTRY_DEPTH_CAP = 3
_ANCESTRY_COUNT_CAP = 500
# Only these keys are forwarded from the credential YAML to GoogleAdsClient.load_from_dict.
# Anything else (notably `logging`, which reaches logging.config.dictConfig) is refused.
_CREDENTIAL_KEYS = frozenset({
    "developer_token", "client_id", "client_secret", "refresh_token",
    "json_key_file_path", "impersonated_email", "login_customer_id", "linked_customer_id",
    "use_proto_plus", "use_cloud_org_for_api_access",
})


# --- pure money helpers (money<->micros lives ONLY here) ------------------------------

def to_micros(value) -> int:
    """Convert an account-currency amount (str/int/float/Decimal) to integer micros.

    Parses with decimal.Decimal to avoid binary-float noise, multiplies by 1_000_000,
    and range-checks to a positive int64. Rejects bool, NaN/inf, negative, zero, and any
    amount with sub-micro precision (which int() would silently truncate) — every one of
    those is a money bug, so it fails loudly with ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"amount {value!r} is not a valid money value (got a bool)")
    try:
        amount = decimal.Decimal(str(value))
    except (decimal.InvalidOperation, ValueError, TypeError):
        raise ValueError(f"amount {value!r} is not a valid decimal money value")
    if not amount.is_finite():
        raise ValueError(f"amount {value!r} must be a finite number (not NaN/inf)")
    micros = amount * _MICROS
    if micros != micros.to_integral_value():
        raise ValueError(
            f"amount {value!r} has sub-micro precision that cannot be represented as micros")
    micros_int = int(micros)
    if not (0 < micros_int <= _INT64_MAX):
        raise ValueError(
            f"amount {value!r} -> {micros_int} micros is out of range "
            f"(require 0 < micros <= {_INT64_MAX})")
    return micros_int


def from_micros(micros) -> str:
    """Convert integer micros back to a decimal currency string (e.g. 12500000 -> '12.5')."""
    return str(decimal.Decimal(int(micros)) / _MICROS)


# --- small local helpers (no rails import at load) ------------------------------------

def _digits(value) -> str:
    """Strip everything but digits: '123-456-7890' -> '1234567890'."""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _resource_customer_id(resource_name):
    """Customer segment of a resource name ('customers/{cid}/...' -> '{cid}'), else None."""
    parts = str(resource_name).split("/")
    if len(parts) >= 2 and parts[0] == "customers":
        return _digits(parts[1])
    return None


def _snake(pascal: str) -> str:
    """CamelCase -> snake_case ('CampaignBudget' -> 'campaign_budget')."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", pascal).lower()


def _has_limit(query) -> bool:
    """True iff the GAQL has a LIMIT *token* (so 'LIMITED' in a filter does not trip it)."""
    return bool(_LIMIT_RE.search(query or ""))


def _max_pages() -> int:
    """GOOGLE_ADS_MAX_PAGES (default 1). Each Google page is up to 10,000 rows. Fails closed
    on a non-int / < 1 value rather than silently defaulting."""
    raw = os.environ.get("GOOGLE_ADS_MAX_PAGES")
    if raw is None:
        return 1
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"GOOGLE_ADS_MAX_PAGES={raw!r} is not an integer (require an int >= 1)")
    if val < 1:
        raise ValueError(f"GOOGLE_ADS_MAX_PAGES={raw!r} must be >= 1 (got {val})")
    return val


# --- the GoogleAdsClient factory (the ONLY monkeypatch point) --------------------------

_CLIENT_CACHE: dict = {}


def _resolve_yaml_path(profile: str | None) -> str:
    """Resolve an explicit profile path, otherwise GOOGLE_ADS_YAML, and expand `~`."""
    raw = profile if profile is not None else os.environ.get("GOOGLE_ADS_YAML")
    if raw is None or not raw.strip():
        raise ValueError(
            "set GOOGLE_ADS_YAML or pass an explicit credential profile path"
        )
    return os.path.expanduser(raw)


def gads(profile: str | None = None):  # -> GoogleAdsClient
    """Build/return the GoogleAdsClient for the resolved credential profile, cached per path.

    Loads the yaml into a dict, forces `use_proto_plus=True` (the whole module assumes
    proto-plus messages), and always pins `version="v25"`. Secret values are never logged."""
    path = _resolve_yaml_path(profile)
    cached = _CLIENT_CACHE.get(path)
    if cached is not None:
        return cached
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: credential profile must be a mapping")
    unknown = set(raw) - _CREDENTIAL_KEYS
    if unknown:
        raise ValueError(f"{path}: unsupported key(s) in credential profile: {sorted(unknown)}")
    cfg = dict(raw)
    cfg["use_proto_plus"] = True
    built = GoogleAdsClient.load_from_dict(cfg, version="v25")
    _CLIENT_CACHE[path] = built
    return built


# --- search primitives -----------------------------------------------------------------

def _search_one_page(query, customer_id, raw_page_token):
    """One search RPC. Returns (raw GoogleAdsRow protos, next_raw_token|None, total_count).

    Requests the total-results count on every page (v25: search_settings, NOT a top-level
    request field). Takes exactly ONE response from the pager so paging happens on explicit
    page-token boundaries the caller controls."""
    c = gads()
    svc = c.get_service("GoogleAdsService")
    req = c.get_type("SearchGoogleAdsRequest")
    req.customer_id = _digits(customer_id)
    req.query = query
    if raw_page_token:
        req.page_token = raw_page_token
    req.search_settings.return_total_results_count = True
    pager = svc.search(request=req)
    response = next(iter(pager.pages))
    rows = list(response.results)
    next_token = response.next_page_token or None
    return rows, next_token, response.total_results_count


def _scan_rows(query, customer_id):
    """Page a query to COMPLETION and return raw GoogleAdsRow protos — the fail-closed
    primitive under every INTERNAL safety scan (gaql_all, effective_strategy,
    campaign_budget, list_accounts, the ancestry walk). Refuses a LIMIT (a capped scan
    cannot prove completeness) and reconciles the returned row count against
    total_results_count so a rail never authorizes from a partial population."""
    from .rails import RailViolation
    if _has_limit(query):
        raise RailViolation(
            "internal scan must not use a LIMIT clause — a capped scan cannot prove the "
            "population is complete", code="SCAN_INCOMPLETE")
    rows = []
    total = None
    token = None
    pages = 0
    while True:
        page_rows, next_token, page_total = _search_one_page(query, customer_id, token)
        rows.extend(page_rows)
        if pages == 0:
            total = page_total
        pages += 1
        if not next_token:
            break
        token = next_token
        if pages >= _SCAN_PAGE_CAP:
            raise RailViolation(
                f"internal scan exceeded {_SCAN_PAGE_CAP} pages without completing — refusing "
                "to authorize from a partial population", code="SCAN_INCOMPLETE")
    if total is not None and len(rows) != total:
        raise RailViolation(
            f"internal scan returned {len(rows)} rows but total_results_count is {total} — a "
            "page is missing; refusing to authorize from a partial population",
            code="SCAN_INCOMPLETE")
    return rows


# --- resume-token binding (envelope tokens are bound to their query + customer) ---------

def _token_digest(query, customer_id) -> str:
    return hashlib.sha256(f"{query}||{_digits(customer_id)}".encode("utf-8")).hexdigest()


def _wrap_token(raw_token, query, customer_id) -> str:
    return f"{_token_digest(query, customer_id)}:{raw_token}"


def _unwrap_token(page_token, query, customer_id):
    """A public resume token is '<sha256>:<raw google token>' bound to the (query,
    customer_id) it came from. A resume whose digest does not match is refused (a token
    cannot be replayed against a different query/account). A token without the bound prefix
    is passed through as a raw Google token (internal use)."""
    if page_token is None:
        return None
    prefix, sep, raw = page_token.partition(":")
    if sep and len(prefix) == 64 and all(ch in "0123456789abcdef" for ch in prefix):
        if prefix != _token_digest(query, customer_id):
            from .rails import RailViolation
            raise RailViolation(
                "resume page_token does not match this (query, customer_id) — refusing a "
                "mismatched resume", code="TOKEN_MISMATCH")
        return raw
    return page_token


def gaql(query, customer_id, page_token=None) -> dict:
    """Run up to GOOGLE_ADS_MAX_PAGES pages of a GAQL query and return an envelope:
    {rows, returned_count, total_results_count, query_limited, pages_complete,
    next_page_token}. Truncation happens only on a page boundary and the returned
    next_page_token (bound to this query+customer) resumes exactly where it stopped."""
    raw_token = _unwrap_token(page_token, query, customer_id)
    max_pages = _max_pages()
    rows = []
    total = None
    token = raw_token
    pages = 0
    next_raw = None
    while pages < max_pages:
        page_rows, next_token, page_total = _search_one_page(query, customer_id, token)
        rows.extend(page_rows)
        if pages == 0:
            total = page_total
        pages += 1
        next_raw = next_token
        token = next_token
        if not next_token:
            break
    return {
        "rows": [type(r).to_dict(r) for r in rows],
        "returned_count": len(rows),
        "total_results_count": total,
        "query_limited": _has_limit(query),
        "pages_complete": not next_raw,
        "next_page_token": _wrap_token(next_raw, query, customer_id) if next_raw else None,
    }


def gaql_all(query, customer_id) -> list[dict]:
    """Run a GAQL query to COMPLETION and return all rows as proto-plus dicts. Fail-closed:
    a LIMIT clause or a count-mismatch raises rails.RailViolation(code='SCAN_INCOMPLETE')
    (imported function-locally). Every internal safety scan uses this."""
    return [type(r).to_dict(r) for r in _scan_rows(query, customer_id)]


# --- the ONE state-changing path -------------------------------------------------------

def _dispatch(plan, validate_only: bool = False) -> dict:
    """The ONE mutation RPC path. Private; called ONLY by rails.apply_draft. Closed union on
    `plan.kind` (duck-typed): 'entity' -> exactly one atomic GoogleAdsService.mutate with
    partial_failure=False; 'recommendation' -> RecommendationService apply/dismiss. Anything
    else raises (never a generic passthrough).

    A transport failure of the mutate itself is deliberately NOT caught here: the real
    GoogleAdsException / grpc error propagates unflattened so rails._is_transport_error
    recognizes it and reports UNKNOWN_WRITE_OUTCOME (the write MAY have landed). Only
    pre-RPC programming/rail errors (cross-customer, unknown kind) raise here — as
    non-transport errors, correctly telling rails nothing landed."""
    kind = getattr(plan, "kind", None)
    if kind == "entity":
        return _dispatch_entity(plan, validate_only)
    if kind == "recommendation":
        return _dispatch_recommendation(plan, validate_only)
    from .rails import RailViolation
    raise RailViolation(
        f"_dispatch: unsupported plan kind {kind!r} — closed union is entity|recommendation")


def numeric_id(value) -> str:
    """Return a strict positive ASCII numeric ID; never sanitize caller input."""
    from .rails import RailViolation
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(
            r"[0-9]+", str(value)) or int(value) <= 0:
        raise RailViolation(f"invalid numeric ID {value!r}", code="BAD_RESOURCE")
    return str(value)


def campaign_path(customer_id, campaign_id):
    from google.ads.googleads.v25.services.services.campaign_service import (
        CampaignServiceClient,
    )
    return CampaignServiceClient.campaign_path(numeric_id(customer_id), numeric_id(campaign_id))


def ad_group_path(customer_id, ad_group_id):
    from google.ads.googleads.v25.services.services.ad_group_service import (
        AdGroupServiceClient,
    )
    return AdGroupServiceClient.ad_group_path(numeric_id(customer_id), numeric_id(ad_group_id))


def ad_group_ad_path(customer_id, ad_group_id, ad_id):
    from google.ads.googleads.v25.services.services.ad_group_ad_service import (
        AdGroupAdServiceClient,
    )
    return AdGroupAdServiceClient.ad_group_ad_path(
        numeric_id(customer_id), numeric_id(ad_group_id), numeric_id(ad_id))


def asset_path(customer_id, asset_id):
    from google.ads.googleads.v25.services.services.asset_service import (
        AssetServiceClient,
    )
    cid, aid = numeric_id(customer_id), str(asset_id)
    if not re.fullmatch(r'-[1-9][0-9]*|[1-9][0-9]*', aid):
        raise ValueError(f'invalid asset ID {asset_id!r}')
    return AssetServiceClient.asset_path(cid, aid)


def asset_group_path(customer_id, asset_group_id):
    from google.ads.googleads.v25.services.services.asset_group_service import (
        AssetGroupServiceClient,
    )
    return AssetGroupServiceClient.asset_group_path(
        numeric_id(customer_id), numeric_id(asset_group_id))


def campaign_criterion_path(customer_id, campaign_id, criterion_id):
    from google.ads.googleads.v25.services.services.campaign_criterion_service import (
        CampaignCriterionServiceClient,
    )
    return CampaignCriterionServiceClient.campaign_criterion_path(
        numeric_id(customer_id), numeric_id(campaign_id), numeric_id(criterion_id))


def ad_group_criterion_path(customer_id, ad_group_id, criterion_id):
    from google.ads.googleads.v25.services.services.ad_group_criterion_service import (
        AdGroupCriterionServiceClient,
    )
    return AdGroupCriterionServiceClient.ad_group_criterion_path(
        numeric_id(customer_id), numeric_id(ad_group_id), numeric_id(criterion_id))


def geo_target_constant_path(criterion_id):
    from google.ads.googleads.v25.services.services.geo_target_constant_service import (
        GeoTargetConstantServiceClient,
    )
    return GeoTargetConstantServiceClient.geo_target_constant_path(numeric_id(criterion_id))


_RESOURCE_KINDS = {
    "CampaignBudget": "campaignBudgets", "Campaign": "campaigns", "AdGroup": "adGroups",
    "BiddingStrategy": "biddingStrategies", "CampaignCriterion": "campaignCriteria",
    "AdGroupCriterion": "adGroupCriteria", "AdGroupAd": "adGroupAds", "Asset": "assets",
    "ConversionAction": "conversionActions", "UserList": "userLists", "CampaignAsset": "campaignAssets", "AdGroupAsset": "adGroupAssets",
}
_UPDATE_FIELDS = {
        "CampaignBudget": {"amount_micros"},
        "Campaign": {"status", "name", "target_cpa.target_cpa_micros", "target_roas.target_roas",
                     "maximize_conversions.target_cpa_micros",
                     "maximize_conversion_value.target_roas"},
        "AdGroup": {"status", "name", "target_cpa_micros", "cpc_bid_micros"},
        "AdGroupCriterion": {"cpc_bid_micros"},
        "BiddingStrategy": {"target_cpa.target_cpa_micros", "target_roas.target_roas",
                            "maximize_conversions.target_cpa_micros",
                            "maximize_conversion_value.target_roas"},
    }

_CREATE_FIELDS = {
    "BiddingStrategy": {"name", "target_cpa.target_cpa_micros", "target_roas.target_roas"},
    "CampaignCriterion": {"campaign", "negative", "keyword.text", "keyword.match_type",
                          "ad_schedule.day_of_week", "ad_schedule.start_hour",
                          "ad_schedule.start_minute", "ad_schedule.end_hour",
                          "ad_schedule.end_minute", "location.geo_target_constant", "language.language_constant"},
    "AdGroupCriterion": {"ad_group", "negative", "status", "keyword.text", "keyword.match_type"},
    "Asset": {"resource_name", "final_urls", "sitelink_asset.link_text",
              "sitelink_asset.description1", "sitelink_asset.description2",
              "callout_asset.callout_text", "structured_snippet_asset.header",
              "structured_snippet_asset.values"},
    "CampaignAsset": {"campaign", "asset", "field_type", "status"},
    "AdGroupAsset": {"ad_group", "asset", "field_type", "status"},
}


def _validate_resource(value, kind, customer_id):
    from .rails import RailViolation
    if kind in {"campaignAssets", "adGroupAssets"}:
        suffix = r"[0-9]+~[0-9]+~[0-9]+"
    else:
        suffix = r"[0-9]+~[0-9]+" if kind in {"campaignCriteria", "adGroupCriteria", "adGroupAds"} else r"[0-9]+"
    match = re.fullmatch(r"customers/([0-9]+)/" + kind + "/(" + suffix + ")", str(value))
    if not match:
        raise RailViolation(f"malformed {kind} resource {value!r}", code="BAD_RESOURCE")
    if match[1] != customer_id:
        raise RailViolation(f"resource {value!r} belongs to another customer", code="CROSS_CUSTOMER")
    for part in match[2].split("~"):
        numeric_id(part)


def _populated_paths(values, masks=(), prefix="", empty_allowed=()):
    from .rails import RailViolation
    paths = set()
    if not isinstance(values, dict):
        raise RailViolation("action fields must be a dictionary", code="BAD_FIELD")
    for key, value in values.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise RailViolation("malformed field name", code="BAD_FIELD")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.update(_populated_paths(value, masks, path, empty_allowed))
            if not value and path in empty_allowed:
                paths.add(path)
            elif not value and not any(mask.startswith(path + ".") for mask in masks):
                raise RailViolation("unmasked empty field", code="BAD_MASK")
        else:
            paths.add(path)
    return paths


def validate_mutation_operation(op, customer_id, temporary_context=None):
    """Offline closed-field and ownership checks shared by compiler and dispatcher."""
    from .rails import RailViolation
    cid = numeric_id(customer_id)
    if op.service == 'AssetGroupListingGroupFilterService' and type(temporary_context) is not _ListingFilterContext:
        raise RailViolation('listing node construction requires its dedicated closed context')
    if op.service in _SHARED_NEGATIVE_SERVICES:
        if type(temporary_context) is not _SharedNegativeContext:
            raise RailViolation('shared operations require a closed shared-negative proof')
        validate_shared_negative_plan(temporary_context.plan)
        if temporary_context.plan.mutate_customer_id != cid or op not in temporary_context.plan.operations:
            raise RailViolation('operation is not a member of the closed shared-negative plan')
        return
    if type(temporary_context) is _DemandGenAdContext:
        validate_demand_gen_ad_plan(temporary_context.plan)
        if temporary_context.plan.mutate_customer_id != cid or op not in temporary_context.plan.operations:
            raise RailViolation('operation is not a member of the closed Demand Gen ad graph')
        return
    if type(temporary_context) is _DemandGenContext:
        validate_demand_gen_plan(temporary_context.plan)
        if temporary_context.plan.mutate_customer_id != cid or op not in temporary_context.plan.operations:
            raise RailViolation('operation is not a member of this closed Demand Gen graph')
        return
    if isinstance(temporary_context, (_PMaxContext, _PMaxAssetGroupContext)):
        # ponytail: revalidate the bounded graph per operation; cache only if measured slow.
        validate_mutation_plan(temporary_context.plan)
        if temporary_context.plan.mutate_customer_id != cid or op not in temporary_context.plan.operations:
            raise RailViolation('operation is not a member of this closed PMax graph')
        return
    if type(temporary_context) is _ListingFilterContext:
        validate_listing_filter_plan(temporary_context.plan)
        if temporary_context.plan.mutate_customer_id != cid or op not in temporary_context.plan.operations:
            raise RailViolation('operation is not a member of the closed listing graph')
        return
    services = {entity + "Service": entity for entity in _RESOURCE_KINDS}
    entity = services.get(op.service)
    if entity is None:
        raise RailViolation(f"unsupported service {op.service!r}")
    action = op.operation
    if not isinstance(action, dict) or len(action) != 1 or not set(action) <= {"create", "update", "remove"}:
        raise RailViolation("operation requires exactly one create, update, or remove action")
    name, values = next(iter(action.items()))
    if name != "update" and op.update_mask:
        raise RailViolation("only updates accept a mask", code="BAD_MASK")
    if entity == 'BiddingStrategy' and name == 'create':
        if (type(values) is not dict or set(values) not in
                ({'name', 'target_cpa'}, {'name', 'target_roas'})):
            raise RailViolation('portfolio create requires name and exactly one target scheme')
        creation_name(values['name'])
        from .rails import check_content, check_roas
        check_content([values['name']])
        if 'target_cpa' in values:
            if type(values['target_cpa']) is not dict or set(values['target_cpa']) != {'target_cpa_micros'}:
                raise RailViolation('TARGET_CPA create contains forbidden fields')
            _payload_micros(values['target_cpa']['target_cpa_micros'])
        else:
            if type(values['target_roas']) is not dict or set(values['target_roas']) != {'target_roas'}:
                raise RailViolation('TARGET_ROAS create contains forbidden fields')
            check_roas(values['target_roas']['target_roas'])
        return
    if entity == "CampaignCriterion" and isinstance(values, dict) and "user_list" in values:
        if name != "create":
            raise RailViolation("audience targeting permits only creation")
        validate_audience_targeting_create(values, cid)
        return
    if entity == "ConversionAction":
        if name == "create":
            validate_conversion_create(values)
        elif name == "update":
            if (not isinstance(values, dict) or set(values) != {'resource_name', 'primary_for_goal'}
                    or type(values['primary_for_goal']) is not bool
                    or type(op.update_mask) is not list or op.update_mask != ['primary_for_goal']):
                raise RailViolation('conversion update permits only exact primary_for_goal boolean and mask')
            if not re.fullmatch(r'[1-9][0-9]*', cid):
                raise RailViolation('conversion owner must be a canonical positive numeric identity')
            if type(values['resource_name']) is not str or not re.fullmatch(
                    rf'customers/{cid}/conversionActions/[1-9][0-9]*', values['resource_name']):
                raise RailViolation('conversion update resource must be exact owner-qualified canonical identity')
            _validate_resource(values['resource_name'], 'conversionActions', cid)
        else:
            raise RailViolation('ConversionAction removal is unsupported')
        return
    if entity == "UserList":
        if name != "create":
            raise RailViolation("UserList permits only standalone creation")
        validate_audience_create(values)
        return
    if entity == "AdGroupAd":
        if name == 'remove':
            _validate_resource(values, 'adGroupAds', cid)
            return
        if name != "create":
            raise RailViolation("AdGroupAd supports only paused RSA creation or removal")
        _validate_rsa_create(values, cid)
        return
    if entity == 'AssetGroupAsset' and name == 'remove':
        field_numbers = '|'.join(PMAX_FIELD_NUMBERS[role]
                                 for role in PMAX_ASSET_GROUP_ROLES)
        if (not isinstance(temporary_context, _PMaxAssetGroupAssetRemoveContext)
                or not isinstance(values, str)
                or not re.fullmatch(
                    rf'customers/{cid}/assetGroupAssets/[1-9][0-9]*~[1-9][0-9]*~({field_numbers})',
                    values)):
            raise RailViolation('invalid supported PMax asset-group connection removal')
        return
    if entity in {'Asset', 'CampaignAsset', 'AdGroupAsset'}:
        if entity in {'CampaignAsset', 'AdGroupAsset'} and name == 'remove':
            kind = 'campaignAssets' if entity == 'CampaignAsset' else 'adGroupAssets'
            if not isinstance(values, str) or not re.fullmatch(
                    rf'customers/{cid}/{kind}/[1-9][0-9]*~[1-9][0-9]*~(11|12|13)', values):
                raise RailViolation('invalid supported asset association removal')
            return
        if name != 'create':
            raise RailViolation('supported assets and links permit only creation')
        _validate_asset_create(entity, values, cid, temporary_context)
        return
    if name == "remove":
        _validate_resource(values, _RESOURCE_KINDS[entity], cid)
        return
    masks = op.update_mask or []
    if not isinstance(masks, list) or any(not isinstance(m, str) for m in masks):
        raise RailViolation("invalid update mask", code="BAD_MASK")
    paths = _populated_paths(values, masks, empty_allowed=CREATION_STRATEGIES if name == "create" and entity == "Campaign" else ())
    if name == "update":
        if not masks or len(set(masks)) != len(masks) or not set(masks) <= _UPDATE_FIELDS.get(entity, set()):
            raise RailViolation("update mask contains a forbidden field", code="BAD_MASK")
        if not paths - {"resource_name"} <= set(masks):
            raise RailViolation("update includes an unmasked field", code="BAD_MASK")
        _validate_resource(values.get("resource_name"), _RESOURCE_KINDS[entity], cid)
    else:
        if entity not in _CREATE_FIELDS or not paths <= _CREATE_FIELDS[entity]:
            raise RailViolation("create includes a forbidden field", code="BAD_FIELD")
        if entity in {"CampaignBudget", "Campaign", "AdGroup"}:
            _validate_entity_create(entity, values, cid, temporary_context)
            return
        if sum(key in values for key in ("keyword", "ad_schedule", "location", "language")) != 1:
            raise RailViolation("create requires exactly one criterion type", code="BAD_FIELD")
        if "negative" in values and not isinstance(values["negative"], bool):
            raise RailViolation("negative must be a boolean", code="BAD_FIELD")
        if entity == "CampaignCriterion" and "keyword" in values and not values.get("negative"):
            raise RailViolation("campaign keywords must be negative", code="BAD_FIELD")
        if entity == "AdGroupCriterion" and not values.get("negative", False) and values.get("status") != "PAUSED":
            raise RailViolation("positive keywords must be explicitly PAUSED", code="BAD_FIELD")
        if entity == "AdGroupCriterion" and values.get("negative") and "status" in values:
            raise RailViolation("negative keywords cannot carry status", code="BAD_FIELD")
        parent = "campaign" if entity == "CampaignCriterion" else "ad_group"
        _creation_reference(values.get(parent), "campaigns" if parent == "campaign" else "adGroups", cid, temporary_context)
        if "location" in values:
            geo = values["location"].get("geo_target_constant")
            if not isinstance(geo, str) or not re.fullmatch(r"geoTargetConstants/[0-9]+", geo):
                raise RailViolation("malformed geo target constant", code="BAD_RESOURCE")
            numeric_id(geo.split("/")[1])

        if "language" in values:
            language = values["language"].get("language_constant")
            if not isinstance(language, str) or not re.fullmatch(r"languageConstants/[1-9][0-9]*", language):
                raise RailViolation("malformed language constant", code="BAD_RESOURCE")


def _build_mutate_operation(c, op, temporary_context=None):
    """Build one explicitly validated action using real v25 message assignment."""
    from .rails import RailViolation
    # Keep the builder safe for direct offline callers as well as the batch dispatcher.
    action_values = op.operation if isinstance(op.operation, dict) else {}
    values = next(iter(action_values.values()), {})
    reference = values if isinstance(values, str) else (
        values.get("resource_name") or values.get("campaign") or values.get("ad_group") or values.get("asset_group")
        if isinstance(values, dict) else None)
    customer = _resource_customer_id(reference) or "1"
    if type(temporary_context) is _SharedNegativeContext:
        customer = temporary_context.plan.mutate_customer_id
    validate_mutation_operation(op, customer, temporary_context)
    entity_name = op.service[:-len("Service")]
    action, values = next(iter(op.operation.items()))
    sub_op = c.get_type(entity_name + "Operation")
    try:
        if action == "remove":
            sub_op.remove = values
        else:
            entity = c.get_type(entity_name)
            for field, value in values.items():
                if entity_name == 'Asset' and field == 'image_asset':
                    value = dict(value, data=base64.b64decode(value['data'], validate=True))
                setattr(entity, field, value)
            setattr(sub_op, action, entity)
            if action == "update":
                sub_op.update_mask.paths.extend(op.update_mask)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        if entity_name == 'Asset' and 'image_asset' in values:
            raise RailViolation('invalid image mutation field', code='BAD_FIELD') from None
        raise RailViolation(f"invalid mutation field: {exc}", code="BAD_FIELD") from exc
    mutate_op = c.get_type("MutateOperation")
    setattr(mutate_op, _snake(entity_name) + "_operation", sub_op)
    return mutate_op


def _dispatch_entity(plan, validate_only):
    from .rails import RailViolation
    cid = numeric_id(plan.mutate_customer_id)
    if not plan.operations:
        raise RailViolation("empty mutation plan")
    context = validate_mutation_plan(plan)
    c = gads()
    mutate_ops = [_build_mutate_operation(c, op, context) for op in plan.operations]
    svc = c.get_service("GoogleAdsService")
    # partial_failure / validate_only are FIELDS on MutateGoogleAdsRequest, NOT kwargs of the
    # convenience mutate() method (which takes only request/customer_id/mutate_operations).
    # Build the request object -- same pattern as _search_one_page and _dispatch_recommendation.
    req = c.get_type("MutateGoogleAdsRequest")
    req.customer_id = cid
    req.mutate_operations.extend(mutate_ops)
    req.partial_failure = False
    req.validate_only = validate_only
    response = svc.mutate(request=req)
    # svc.mutate has RETURNED -> the write has LANDED. A transport error above is deliberately
    # not caught here (it propagates raw so rails._is_transport_error classifies it). But a
    # failure PARSING the response is a landed-but-unconfirmed outcome, never a generic error
    # that rails would log as phase "error" (implying nothing landed -> invites a retry).
    try:
        result = _mutate_result(response)
        if validate_only:
            result["validate_only"] = True
        return result
    except Exception as parse_exc:
        from .rails import UnknownWriteOutcome
        raise UnknownWriteOutcome(
            "mutate RPC returned but the response could not be parsed — the write landed; "
            "verify account state",
            request_id=None, failure=None, cause=parse_exc) from parse_exc


def _mutate_result(response) -> dict:
    op_results = []
    resource_names = []
    for op_resp in response.mutate_operation_responses:
        which = op_resp._pb.WhichOneof("response")
        entry = {"type": which}
        if which:
            rn = getattr(getattr(op_resp, which), "resource_name", "") or None
            entry["resource_name"] = rn
            if rn:
                resource_names.append(rn)
        op_results.append(entry)
    # GoogleAdsService.mutate carries request_id in trailing metadata, not the response
    # proto — surface None rather than invent one; rails treats it as optional.
    return {"results": op_results, "resource_names": resource_names, "request_id": None}


def validate_recommendation_plan(plan):
    from .rails import RailViolation, RecommendationActionPlan
    if type(plan) is not RecommendationActionPlan or plan.kind != 'recommendation':
        raise RailViolation('recommendation actions require the bounded plan variant')
    cid = plan.mutate_customer_id
    if type(cid) is not str or not re.fullmatch(r'[1-9][0-9]*', cid):
        raise RailViolation('recommendation owner must be canonical')
    rn = plan.recommendation_resource_name
    if type(rn) is not str or not re.fullmatch(rf'customers/{cid}/recommendations/[A-Za-z0-9_-]+', rn):
        raise RailViolation('invalid recommendation resource/owner')
    if set(vars(plan)) != {'mutate_customer_id', 'rpc', 'recommendation_resource_name',
                           'kind', 'new_budget_amount_micros', 'post_checks'}:
        raise RailViolation('unexpected recommendation plan fields')
    if plan.rpc == 'apply':
        positive_int64(plan.new_budget_amount_micros)
        if type(plan.new_budget_amount_micros) is not int:
            raise RailViolation('approved budget amount must be an integer')
        checks = plan.post_checks
        if type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict:
            raise RailViolation('recommendation apply requires one complete budget postcheck')
        check = checks[0]
        if (set(check) != {'recommendation_budget', 'customer_id', 'budget_resource_name', 'expected'}
                or check['recommendation_budget'] is not True or check['customer_id'] != cid
                or type(check['budget_resource_name']) is not str
                or not re.fullmatch(rf'customers/{cid}/campaignBudgets/[1-9][0-9]*', check['budget_resource_name'])
                or type(check['expected']) is not dict
                or set(check['expected']) != {'budget', 'attachments'}):
            raise RailViolation('invalid recommendation budget postcheck descriptor')
        budget = check['expected']['budget']
        attached = check['expected']['attachments']
        if (type(budget) is not dict or budget.get('resource_name') != check['budget_resource_name']
                or type(budget.get('amount_micros')) is not int
                or budget['amount_micros'] != plan.new_budget_amount_micros
                or budget.get('period') != 'DAILY'
                or type(budget.get('explicitly_shared')) is not bool
                or type(budget.get('reference_count')) is not int
                or type(budget.get('currency')) is not str
                or not re.fullmatch('[A-Z]{3}', budget['currency'])
                or 'aligned_bidding_strategy_id' not in budget
                or budget['aligned_bidding_strategy_id'] not in (None, '', '0', 0)
                or type(attached) is not list or not attached
                or len(attached) != budget['reference_count']):
            raise RailViolation('incomplete recommendation budget verification state')
        identities = set()
        for campaign in attached:
            if (type(campaign) is not dict or type(campaign.get('id')) is not str
                    or not re.fullmatch(r'[1-9][0-9]*', campaign['id'])
                    or campaign.get('resource_name') != f"customers/{cid}/campaigns/{campaign['id']}"
                    or campaign.get('customer_id') != cid
                    or campaign.get('campaign_budget') != check['budget_resource_name']
                    or campaign.get('status') not in {'ENABLED', 'PAUSED'}
                    or campaign['id'] in identities):
                raise RailViolation('incomplete recommendation campaign verification state')
            identities.add(campaign['id'])

    elif plan.rpc == 'dismiss' and plan.new_budget_amount_micros is None:
        checks = plan.post_checks
        if type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict:
            raise RailViolation('dismiss requires one complete identity postcheck')
        check = checks[0]
        if (set(check) != {'recommendation_dismiss', 'customer_id', 'resource_name', 'expected'}
                or check['recommendation_dismiss'] is not True or check['customer_id'] != cid
                or check['resource_name'] != rn):
            raise RailViolation('invalid dismissal postcheck descriptor')
        _validate_dismiss_snapshot(cid, rn, check['expected'])
        if check['expected']['dismissed'] is not True:
            raise RailViolation('dismissal postcheck must observe dismissed=true')
    else:
        raise RailViolation('invalid recommendation action parameters')


def _dispatch_recommendation(plan, validate_only):
    from .rails import RailViolation, UnknownWriteOutcome
    if validate_only:
        raise RailViolation('recommendation actions do not support validate_only')
    validate_recommendation_plan(plan)
    c = gads()
    svc = c.get_service('RecommendationService')
    req = c.get_type('ApplyRecommendationRequest' if plan.rpc == 'apply' else 'DismissRecommendationRequest')
    req.customer_id = plan.mutate_customer_id
    req.partial_failure = False
    operation = {'resource_name': plan.recommendation_resource_name}
    if plan.rpc == 'apply':
        operation['campaign_budget'] = {'new_budget_amount_micros': plan.new_budget_amount_micros}
    req.operations.append(operation)
    try:
        response = (svc.apply_recommendation(request=req) if plan.rpc == 'apply'
                    else svc.dismiss_recommendation(request=req))
    except Exception as exc:
        raise UnknownWriteOutcome('recommendation outcome uncertain; read account state before retrying',
                                  request_id=getattr(exc, 'request_id', None),
                                  failure=getattr(exc, 'failure', None) or transport_error_details(exc),
                                  cause=exc) from exc
    try:
        error = type(response).to_dict(response).get('partial_failure_error', {})
        if (not isinstance(error, dict) or set(error) - {'code', 'message', 'details'}
                or type(error.get('code', 0)) is not int
                or type(error.get('message', '')) is not str
                or type(error.get('details', [])) is not list
                or any(error.get(k) for k in ('code', 'message', 'details'))):
            raise UnknownWriteOutcome('recommendation response contains an error', failure=error)
        names = [r.resource_name for r in response.results]
        if names != [plan.recommendation_resource_name]:
            raise ValueError('recommendation result identity/count mismatch')
    except UnknownWriteOutcome:
        raise
    except Exception as exc:
        raise UnknownWriteOutcome('recommendation response could not be verified', cause=exc) from exc
    return {'results': [{'resource_name': n} for n in names], 'resource_names': names,
            'rpc': plan.rpc, 'request_id': None}


def positive_int64(value):
    from .rails import RailViolation
    if (type(value) not in (str, int) or not re.fullmatch(r'[1-9][0-9]*', str(value))
            or int(value) > 9223372036854775807):
        raise RailViolation('amount must be positive exact int64 micros')
    return int(value)


def _validate_dismiss_snapshot(cid, rn, rec):
    from google.ads.googleads.v25.enums.types.recommendation_type import (
        RecommendationTypeEnum,
    )

    from .rails import RailViolation
    if (type(rec) is not dict or set(rec) != {'resource_name', 'type_', 'dismissed',
                                            'campaign_budget', 'campaign', 'ad_group'}
            or rec['resource_name'] != rn or type(rec['dismissed']) is not bool
            or type(rec['type_']) is not str
            or rec['type_'] not in RecommendationTypeEnum.RecommendationType.__members__
            or rec['type_'] in {'UNSPECIFIED', 'UNKNOWN'}):
        raise RailViolation('invalid recommendation identity snapshot')
    for field, collection in [('campaign_budget', 'campaignBudgets'), ('campaign', 'campaigns'),
                              ('ad_group', 'adGroups')]:
        value = rec[field]
        if type(value) is not str or (value != '' and not re.fullmatch(
                rf'customers/{cid}/{collection}/[1-9][0-9]*', value)):
            raise RailViolation('invalid recommendation association')


def recommendation_dismiss_state(cid, recommendation_id):
    from google.ads.googleads.v25.enums.types.recommendation_type import (
        RecommendationTypeEnum,
    )
    from google.ads.googleads.v25.services.types.google_ads_service import GoogleAdsRow

    from .rails import RailViolation
    if (type(cid) is not str or not re.fullmatch(r'[1-9][0-9]*', cid)
            or type(recommendation_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', recommendation_id)):
        raise RailViolation('invalid recommendation query identity')
    rn = f'customers/{cid}/recommendations/{recommendation_id}'
    rows = _scan_rows('SELECT recommendation.resource_name, recommendation.type, '
                    'recommendation.dismissed, recommendation.campaign_budget, recommendation.campaign, '
                    'recommendation.ad_group '
                    f"FROM recommendation WHERE recommendation.resource_name = '{rn}'", cid)
    if len(rows) != 1:
        raise RailViolation('recommendation must resolve exactly once')
    row = rows[0]
    raw = type(row).to_dict(row).get('recommendation')
    if type(raw) is not dict:
        raise RailViolation('recommendation identity missing')
    if isinstance(row, GoogleAdsRow):
        for field in ('campaign_budget', 'campaign', 'ad_group'):
            raw[field] = getattr(row.recommendation, field)
    rec = {field: raw.get(field) for field in
           ('resource_name', 'dismissed', 'campaign_budget', 'campaign', 'ad_group')}
    value = raw.get('type_', raw.get('type'))
    if type(value) is int:
        try:
            value = RecommendationTypeEnum.RecommendationType(value).name
        except ValueError:
            raise RailViolation('unknown recommendation type') from None
    rec['type_'] = value
    _validate_dismiss_snapshot(cid, rn, rec)
    return rec


def recommendation_state(cid, recommendation_id):
    from .rails import RailViolation
    if (type(cid) is not str or not re.fullmatch(r'[1-9][0-9]*', cid)
            or type(recommendation_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', recommendation_id)):
        raise RailViolation('invalid recommendation query identity')
    rn = f'customers/{cid}/recommendations/{recommendation_id}'
    rows = gaql_all('SELECT recommendation.resource_name, recommendation.type, '
                    'recommendation.dismissed, recommendation.campaign_budget, recommendation.campaign, '
                    'recommendation.ad_group, recommendation.campaign_budget_recommendation '
                    f"FROM recommendation WHERE recommendation.resource_name = '{rn}'", cid)
    if len(rows) != 1 or rows[0].get('recommendation', {}).get('resource_name') != rn:
        raise RailViolation('recommendation must resolve exactly once')
    rec = dict(rows[0]['recommendation'])
    rec['type_'] = _enum_name('RecommendationTypeEnum', rec.get('type_', rec.get('type')))
    rec.pop('type', None)
    if rec['type_'] != 'CAMPAIGN_BUDGET':
        raise RailViolation('only CAMPAIGN_BUDGET is supported', code='UNSUPPORTED_RECOMMENDATION')
    if rec.get('dismissed') is not False or rec.get('ad_group'):
        raise RailViolation('recommendation dismissed or has unexpected association')
    budget = rec.get('campaign_budget')
    if type(budget) is not str or not re.fullmatch(rf'customers/{cid}/campaignBudgets/[1-9][0-9]*', budget):
        raise RailViolation('invalid recommendation budget identity')
    payload = rec.get('campaign_budget_recommendation')
    if not isinstance(payload, dict):
        raise RailViolation('budget recommendation payload missing')
    payload = dict(payload)
    for key in ('current_budget_amount_micros', 'recommended_budget_amount_micros'):
        payload[key] = positive_int64(payload.get(key))
    options = payload.get('budget_options', [])
    if not isinstance(options, list):
        raise RailViolation('budget options malformed')
    payload['budget_options'] = [{'budget_amount_micros': positive_int64(v.get('budget_amount_micros'))}
                                 for v in options if isinstance(v, dict)]
    if len(payload['budget_options']) != len(options):
        raise RailViolation('budget option malformed')
    rec['campaign_budget_recommendation'] = payload
    return rec


def recommendation_budget_state(cid, budget):
    from .rails import RailViolation
    if (type(cid) is not str or not re.fullmatch(r'[1-9][0-9]*', cid)
            or type(budget) is not str or not re.fullmatch(rf'customers/{cid}/campaignBudgets/[1-9][0-9]*', budget)):
        raise RailViolation('invalid budget query identity')
    rows = gaql_all('SELECT campaign_budget.resource_name, campaign_budget.amount_micros, '
                    'campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign_budget.period, '
                    'campaign_budget.total_amount_micros, campaign_budget.aligned_bidding_strategy_id, '
                    f"customer.currency_code FROM campaign_budget WHERE campaign_budget.resource_name = '{budget}'", cid)
    if len(rows) != 1:
        raise RailViolation('budget must resolve exactly once')
    info = dict(rows[0].get('campaign_budget', {}))
    currency = rows[0].get('customer', {}).get('currency_code')
    if (info.get('resource_name') != budget or type(currency) is not str
            or not re.fullmatch('[A-Z]{3}', currency) or type(info.get('explicitly_shared')) is not bool):
        raise RailViolation('budget identity/currency/shared state unreadable')
    info['amount_micros'] = positive_int64(info.get('amount_micros'))
    info['reference_count'] = positive_int64(info.get('reference_count'))
    info['period'] = _enum_name('BudgetPeriodEnum', info.get('period'))
    if info['period'] != 'DAILY' or info.get('aligned_bidding_strategy_id') not in (None, '', '0', 0):
        raise RailViolation('only unaligned DAILY budgets supported')
    info['currency'] = currency
    attachments = shared_budget_attachments(cid, None, budget, info['reference_count'])
    return {'budget': info, 'attachments': attachments}


# --- reads ------------------------------------------------------------------------------

def _enum_name(enum_type, value):
    if isinstance(value, str):
        return value
    enum = getattr(gads().get_type(enum_type), enum_type.removesuffix("Enum"))
    try:
        return enum(value).name
    except (ValueError, TypeError):
        return "UNKNOWN"


def effective_strategy(customer_id, campaign_id) -> dict:
    """Resolve portfolio ownership from accessible_bidding_strategy, never the client name."""
    from .rails import RailViolation
    rows = gaql_all(
        "SELECT campaign.id, campaign.bidding_strategy_type, campaign.bidding_strategy, "
        "accessible_bidding_strategy.id, accessible_bidding_strategy.owner_customer_id "
        f"FROM campaign WHERE campaign.id = {int(campaign_id)}", customer_id)
    if len(rows) != 1:
        raise RailViolation("campaign strategy not readable", code="BID_UNREADABLE")
    campaign = rows[0]["campaign"]
    portfolio = campaign.get("bidding_strategy") or None
    out = {"type": _enum_name("BiddingStrategyTypeEnum", campaign.get("bidding_strategy_type")),
           "portfolio_resource_name": portfolio, "owner_customer_id": None}
    if portfolio:
        accessible = rows[0].get("accessible_bidding_strategy", {})
        owner, sid = accessible.get("owner_customer_id"), accessible.get("id")
        if not owner or not sid or str(owner) == "0" or str(sid) == "0":
            raise RailViolation("portfolio owner is unreadable", code="PORTFOLIO_SCOPE")
        out.update(owner_customer_id=str(owner), strategy_id=str(sid))
    return out


def bidding_strategy_path(customer_id, strategy_id):
    """Build an owner-qualified strategy name using the generated v25 helper; no RPC."""
    from google.ads.googleads.v25.services.services.bidding_strategy_service import (
        BiddingStrategyServiceClient,
    )
    return BiddingStrategyServiceClient.bidding_strategy_path(_digits(customer_id), str(strategy_id))


def update_state(customer_id, entity_type, entity_id):
    """Complete, sparse update fingerprint including current values and parent linkage."""
    from .rails import RailViolation
    fields = {
        "campaign": "campaign.name, campaign.advertising_channel_type, "
                    "campaign.target_cpa.target_cpa_micros, "
                    "campaign.target_roas.target_roas, "
                    "campaign.maximize_conversions.target_cpa_micros, "
                    "campaign.maximize_conversion_value.target_roas",
        "ad_group": "ad_group.name, ad_group.campaign, ad_group.cpc_bid_micros, "
                    "ad_group.target_cpa_micros, ad_group.effective_cpc_bid_micros, "
                    "ad_group.effective_target_cpa_micros, ad_group.effective_target_cpa_source",
    }
    rows = gaql_all(f"SELECT {entity_type}.resource_name, {entity_type}.status, "
                    f"{fields[entity_type]} FROM {entity_type} "
                    f"WHERE {entity_type}.id = {int(entity_id)}", customer_id)
    if len(rows) != 1:
        raise RailViolation(f"{entity_type} not found", code="NOT_FOUND")
    state = rows[0][entity_type]
    state["status"] = _enum_name(
        "CampaignStatusEnum" if entity_type == "campaign" else "AdGroupStatusEnum", state["status"])
    if entity_type == "ad_group":
        state["effective_target_cpa_source"] = _enum_name(
            "BiddingSourceEnum", state["effective_target_cpa_source"])
    else:
        state["advertising_channel_type"] = _enum_name(
            "AdvertisingChannelTypeEnum", state.get("advertising_channel_type"))
    return state


def portfolio_state(owner_customer_id, strategy_id, read_customers):
    """Read owner strategy and every visible non-removed attachment with complete scans."""
    from .rails import RailViolation
    rows = gaql_all(
        "SELECT bidding_strategy.resource_name, bidding_strategy.type, "
        "bidding_strategy.non_removed_campaign_count, "
        "bidding_strategy.target_cpa.target_cpa_micros, bidding_strategy.target_roas.target_roas, "
        "bidding_strategy.maximize_conversions.target_cpa_micros, "
        "bidding_strategy.maximize_conversion_value.target_roas "
        f"FROM bidding_strategy WHERE bidding_strategy.id = {int(strategy_id)}", owner_customer_id)
    if len(rows) != 1:
        raise RailViolation("owner strategy unreadable", code="PORTFOLIO_SCOPE")
    attached = []
    for cid in sorted(read_customers):
        population = gaql_all(
            "SELECT campaign.id, campaign.name, campaign.resource_name, campaign.status, "
            "campaign.bidding_strategy, accessible_bidding_strategy.id, "
            "accessible_bidding_strategy.owner_customer_id "
            "FROM campaign WHERE campaign.status != 'REMOVED'", cid)
        for row in population:
            strategy = row.get("accessible_bidding_strategy", {})
            if (str(strategy.get("owner_customer_id")) == str(owner_customer_id)
                    and str(strategy.get("id")) == str(strategy_id)):
                attachment = {"customer_id": cid, **row["campaign"]}
                attachment["status"] = _enum_name("CampaignStatusEnum", attachment["status"])
                attached.append(attachment)
    attached.sort(key=lambda c: (c["customer_id"], c["resource_name"]))
    strategy = rows[0]["bidding_strategy"]
    strategy["type"] = _enum_name("BiddingStrategyTypeEnum", strategy.pop("type_"))
    return {"strategy": strategy, "attachments": attached}


def shared_budget_attachments(customer_id, campaign_id, budget_resource_name, reference_count):
    """Reconcile the active budget population (ENABLED/PAUSED) before authorizing a write."""
    from .rails import RailViolation
    cid = _digits(customer_id)
    if not re.fullmatch(rf"customers/{cid}/campaignBudgets/[0-9]+", budget_resource_name):
        raise RailViolation("invalid budget owner/resource", code="SHARED_BUDGET_SCOPE")
    rows = gaql_all(
        "SELECT campaign.id, campaign.resource_name, campaign.name, campaign.status, "
        "campaign.campaign_budget FROM campaign WHERE campaign.status != 'REMOVED' "
        f"AND campaign.campaign_budget = '{budget_resource_name}'", cid)
    attachments = []
    identities = set()
    for row in rows:
        campaign = row.get("campaign", {})
        identity = str(campaign.get("id", ""))
        status = _enum_name("CampaignStatusEnum", campaign.get("status"))
        if (not identity.isdigit() or identity == "0" or identity in identities
                or campaign.get("resource_name") != campaign_path(cid, identity)
                or campaign.get("campaign_budget") != budget_resource_name
                or status not in {"ENABLED", "PAUSED"}):
            raise RailViolation("budget attachment identity/status unreadable or inconsistent",
                                code="SHARED_BUDGET_SCOPE")
        identities.add(identity)
        attachments.append({**campaign, "id": identity, "customer_id": cid, "status": status})
    if (reference_count is None or str(reference_count) != str(len(attachments))
            or not identities or (campaign_id is not None and str(campaign_id) not in identities)):
        raise RailViolation("budget attachment count or requested campaign does not reconcile",
                            code="SHARED_BUDGET_SCOPE")
    return sorted(attachments, key=lambda c: c["resource_name"])


def is_transport_error(error):
    """Recognize actual Google remapped failures while keeping construction errors distinct.
    Any GoogleAPICallError raised by an RPC (Aborted, Unknown, Cancelled, ResourceExhausted...)
    means the request may have reached Google, so the outcome is unknown, never "error"."""
    return (isinstance(error, api_exceptions.GoogleAPICallError)
            or (type(error).__module__ or "").startswith("grpc")
            or type(error).__name__ == "GoogleAdsException"
            or (hasattr(error, "request_id") and hasattr(error, "failure")))


def transport_error_details(error):
    """Retain Google API error details alongside the original exception cause."""
    if isinstance(error, api_exceptions.GoogleAPICallError):
        return {"errors": list(error.errors), "details": list(error.details), "code": error.code}
    return None


def campaign_budget(customer_id, campaign_id) -> dict:
    """Read a campaign's budget, joining campaign -> campaign_budget on campaign.id.

    Returns {budget_resource_name, amount (decimal str via from_micros), explicitly_shared,
    reference_count, period (enum NAME e.g. 'DAILY'), total_amount_micros (int|None),
    aligned_bidding_strategy_id (str|None)}."""
    from .rails import RailViolation
    query = (
        "SELECT campaign.id, campaign.status, campaign_budget.resource_name, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared, campaign_budget.reference_count, "
        "campaign_budget.period, campaign_budget.total_amount_micros, "
        "campaign_budget.aligned_bidding_strategy_id "
        f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
    )
    rows = _scan_rows(query, customer_id)
    if not rows:
        raise RailViolation(
            f"campaign {campaign_id} not found in customer {customer_id}", code="NOT_FOUND")
    if len(rows) != 1 or str(rows[0].campaign.id) != str(campaign_id):
        raise RailViolation('campaign budget parent identity/count does not reconcile')
    b = rows[0].campaign_budget
    aligned = b.aligned_bidding_strategy_id or None
    total = b.total_amount_micros if b._pb.HasField("total_amount_micros") else None
    return {
        "budget_resource_name": b.resource_name,
        "campaign_status": rows[0].campaign.status.name,
        "amount": from_micros(b.amount_micros),
        "explicitly_shared": bool(b.explicitly_shared),
        "reference_count": int(b.reference_count),
        "period": b.period.name,
        "total_amount_micros": total,
        "aligned_bidding_strategy_id": str(aligned) if aligned else None,
    }


_STATUS_READ = {
    "campaign": ("campaign", "SELECT campaign.id, campaign.status, campaign.resource_name "
                             "FROM campaign WHERE campaign.id = {id}"),
    "ad_group": ("ad_group", "SELECT ad_group.id, ad_group.status, ad_group.resource_name "
                             "FROM ad_group WHERE ad_group.id = {id}"),
}


def entity_status(customer_id, entity_type, entity_id) -> dict:
    """Read one campaign|ad_group's status.

    Returns {exists: bool, resource_name: str|None, status: NAME str|None}. status is the
    decoded enum NAME (e.g. 'ENABLED'). Unknown entity_type raises RailViolation
    (function-local import, code 'UNSUPPORTED_ENTITY'). Uses _scan_rows (fail-closed
    complete scan)."""
    from .rails import RailViolation
    resolved = _STATUS_READ.get(entity_type)
    if resolved is None:
        raise RailViolation(
            f"unsupported entity_type {entity_type!r} — must be one of "
            f"{sorted(_STATUS_READ)}", code="UNSUPPORTED_ENTITY")
    field, query_tmpl = resolved
    query = query_tmpl.format(id=int(entity_id))
    rows = _scan_rows(query, customer_id)
    if not rows:
        return {"exists": False, "resource_name": None, "status": None}
    obj = getattr(rows[0], field)
    return {"exists": True, "resource_name": obj.resource_name, "status": obj.status.name}


def list_accounts() -> list[dict]:
    """The READ-allowlisted accounts, each {customer_id, descriptive_name, currency_code}."""
    from .rails import read_customer_ids
    out = []
    for cid in sorted(read_customer_ids()):
        rows = _scan_rows(
            "SELECT customer.id, customer.descriptive_name, customer.currency_code FROM customer",
            cid)
        cust = rows[0].customer if rows else None
        out.append({
            "customer_id": cid,
            "descriptive_name": cust.descriptive_name if cust else None,
            "currency_code": cust.currency_code if cust else None,
        })
    return out


# --- canned-GAQL report/lookup functions -------------------------------------------------
# Every function here builds a fixed GAQL string and returns gaql(query, customer_id,
# page_token) DIRECTLY -- never gaql_all/_scan_rows (those hard-refuse a LIMIT, and
# search_terms below needs one). No decoding: the envelope's rows are proto-plus's raw
# to_dict() dicts (snake_case keys, integer enums, string *_micros/id int64 fields) exactly
# like run_gaql already returns today -- callers decode themselves.

def _date_clause(date_range_start, date_range_end) -> str:
    """GAQL date filter fragment (no leading AND). Explicit range -> BETWEEN; otherwise
    default to the last 30 days so a report never silently returns lifetime totals.
    Dates must be YYYY-MM-DD: they are interpolated into GAQL, so nothing else is allowed."""
    if date_range_start and date_range_end:
        from .rails import RailViolation
        for d in (date_range_start, date_range_end):
            if not isinstance(d, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise RailViolation(f"date {d!r} must be YYYY-MM-DD", code="BAD_DATE")
        return f"segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'"
    return "segments.date DURING LAST_30_DAYS"


def account_info(customer_id) -> dict:
    """Single-row account info: id, descriptive name, currency, time zone, auto-tagging
    flag, manager flag, status. No page_token -- LIMIT 1 always returns at most one row."""
    query = " ".join([
        "SELECT customer.id, customer.descriptive_name, customer.currency_code,",
        "customer.time_zone, customer.auto_tagging_enabled, customer.manager,",
        "customer.status",
        "FROM customer",
        "LIMIT 1",
    ])
    return gaql(query, customer_id)


def campaign_performance(customer_id, date_range_start=None, date_range_end=None,
                         page_token=None) -> dict:
    """Campaign performance report (impressions/clicks/cost/conversions), ordered by spend
    descending. Defaults to the last 30 days when no explicit date range is given."""
    query = " ".join([
        "SELECT campaign.id, campaign.name, campaign.status,",
        "campaign.advertising_channel_type, campaign.bidding_strategy_type,",
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,",
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc",
        "FROM campaign",
        f"WHERE campaign.status != 'REMOVED' AND {_date_clause(date_range_start, date_range_end)}",
        "ORDER BY metrics.cost_micros DESC",
    ])
    return gaql(query, customer_id, page_token)


def ad_performance(customer_id, date_range_start=None, date_range_end=None,
                   page_token=None) -> dict:
    """Ad-level performance report (responsive search ad text + metrics), ordered by spend
    descending. Defaults to the last 30 days when no explicit date range is given."""
    query = " ".join([
        "SELECT campaign.name, campaign.id, ad_group.name, ad_group.id,",
        "ad_group_ad.ad.id, ad_group_ad.ad.type,",
        "ad_group_ad.ad.responsive_search_ad.headlines,",
        "ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.final_urls,",
        "ad_group_ad.status, metrics.impressions, metrics.clicks, metrics.ctr,",
        "metrics.conversions, metrics.cost_micros",
        "FROM ad_group_ad",
        f"WHERE ad_group_ad.status != 'REMOVED' AND {_date_clause(date_range_start, date_range_end)}",
        "ORDER BY metrics.cost_micros DESC",
    ])
    return gaql(query, customer_id, page_token)


def keyword_performance(customer_id, date_range_start=None, date_range_end=None,
                        page_token=None) -> dict:
    """Keyword-level performance report (quality score + metrics), ordered by spend
    descending. Defaults to the last 30 days when no explicit date range is given."""
    query = " ".join([
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text,",
        "ad_group_criterion.keyword.match_type,",
        "ad_group_criterion.quality_info.quality_score, metrics.impressions,",
        "metrics.clicks, metrics.ctr, metrics.average_cpc, metrics.cost_micros,",
        "metrics.conversions",
        "FROM keyword_view",
        f"WHERE ad_group_criterion.status != 'REMOVED' AND {_date_clause(date_range_start, date_range_end)}",
        "ORDER BY metrics.cost_micros DESC",
    ])
    return gaql(query, customer_id, page_token)


def search_terms(customer_id, date_range_start=None, date_range_end=None,
                 page_token=None) -> dict:
    """Search-terms report, ordered by clicks descending, capped at 200 rows (an account can
    have far more distinct search terms than fit in one page). The date clause is the ONLY
    WHERE condition -- no leading AND."""
    query = " ".join([
        "SELECT search_term_view.search_term, campaign.name, ad_group.name,",
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions",
        "FROM search_term_view",
        f"WHERE {_date_clause(date_range_start, date_range_end)}",
        "ORDER BY metrics.clicks DESC",
        "LIMIT 200",
    ])
    return gaql(query, customer_id, page_token)


def geo_performance(customer_id, date_range_start=None, date_range_end=None,
                    page_token=None) -> dict:
    """Geographic performance report, ordered by spend descending. The date clause is the
    ONLY WHERE condition -- no leading AND."""
    query = " ".join([
        "SELECT campaign.name, geographic_view.country_criterion_id,",
        "geographic_view.location_type, metrics.impressions, metrics.clicks,",
        "metrics.cost_micros, metrics.conversions",
        "FROM geographic_view",
        f"WHERE {_date_clause(date_range_start, date_range_end)}",
        "ORDER BY metrics.cost_micros DESC",
    ])
    return gaql(query, customer_id, page_token)


def negative_keywords(customer_id, page_token=None) -> dict:
    """Campaign-level negative keywords. No date range."""
    query = " ".join([
        "SELECT campaign.id, campaign.name, campaign_criterion.keyword.text,",
        "campaign_criterion.keyword.match_type, campaign_criterion.negative,",
        "campaign_criterion.criterion_id",
        "FROM campaign_criterion",
        "WHERE campaign_criterion.negative = TRUE AND campaign_criterion.status != 'REMOVED'",
    ])
    return gaql(query, customer_id, page_token)


def geo_targets(customer_id, query, page_token=None) -> dict:
    """Search geo_target_constant by display-name substring (LIKE '%<query>%'). `query` is
    interpolated directly into the GAQL string, so a literal single quote is escaped first --
    Backslash is escaped first so a trailing backslash cannot un-escape the quote."""
    escaped = query.replace("\\", "\\\\").replace("'", "\\'")
    q = " ".join([
        "SELECT geo_target_constant.id, geo_target_constant.name,",
        "geo_target_constant.canonical_name, geo_target_constant.country_code,",
        "geo_target_constant.target_type",
        "FROM geo_target_constant",
        f"WHERE geo_target_constant.name LIKE '%{escaped}%'",
    ])
    return gaql(q, customer_id, page_token)


_ENTITY_READ = {
    "campaign": {
        "from": "campaign",
        "fields": "campaign.id, campaign.name, campaign.status, "
                 "campaign.advertising_channel_type, campaign.bidding_strategy_type",
        "status_field": "campaign.status",
        "parent_field": None,
        "ids_field": "campaign.id",
    },
    "ad_group": {
        "from": "ad_group",
        "fields": "ad_group.id, ad_group.name, ad_group.status, ad_group.type, "
                 "campaign.id, campaign.name",
        "status_field": "ad_group.status",
        "parent_field": "campaign.id",
        "ids_field": "ad_group.id",
    },
    "keyword": {
        "from": "ad_group_criterion",
        "fields": "ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
                 "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
                 "ad_group.id, ad_group.name, campaign.id, campaign.name",
        "status_field": "ad_group_criterion.status",
        "parent_field": "ad_group.id",
        "ids_field": "ad_group_criterion.criterion_id",
        "extra_where": "ad_group_criterion.type = 'KEYWORD'",
    },
    "ad": {
        "from": "ad_group_ad",
        "fields": "ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
                 "ad_group.id, ad_group.name, campaign.id, campaign.name",
        "status_field": "ad_group_ad.status",
        "parent_field": "ad_group.id",
        "ids_field": "ad_group_ad.ad.id",
    },
}


def entities(customer_id, entity_type, ids=None, parent_id=None, page_token=None) -> dict:
    """Generic entity lookup across campaign|ad_group|keyword|ad, optionally filtered by ids
    and/or parent_id. Unsupported entity_type raises RailViolation
    (code='UNSUPPORTED_ENTITY'); campaign has no parent scope, so a parent_id given with
    entity_type='campaign' raises RailViolation. ids/parent_id are int(...)-cast before use --
    never interpolate a raw caller string into a numeric GAQL filter."""
    from .rails import RailViolation
    resolved = _ENTITY_READ.get(entity_type)
    if resolved is None:
        raise RailViolation(
            f"unsupported entity_type {entity_type!r} — must be one of "
            f"{sorted(_ENTITY_READ)}", code="UNSUPPORTED_ENTITY")
    if entity_type == "campaign" and parent_id is not None:
        raise RailViolation("campaign has no parent_id")

    where = [f"{resolved['status_field']} != 'REMOVED'"]
    if resolved.get("extra_where"):
        where.append(resolved["extra_where"])
    if parent_id is not None:
        where.append(f"{resolved['parent_field']} = {int(parent_id)}")
    if ids:
        where.append(f"{resolved['ids_field']} IN ({', '.join(str(int(i)) for i in ids)})")

    query = f"SELECT {resolved['fields']} FROM {resolved['from']} WHERE " + " AND ".join(where)
    return gaql(query, customer_id, page_token)


# --- startup: prove the read allowlist descends from the login manager -----------------

def _customer_client_children(manager_id):
    """Direct children (level 1) of a manager account, as [{id, manager}]. Uses the
    complete-scan primitive so a partial page cannot hide a child."""
    query = (
        "SELECT customer_client.id, customer_client.manager, customer_client.level "
        "FROM customer_client WHERE customer_client.level = 1"
    )
    return [{"id": str(r.customer_client.id), "manager": bool(r.customer_client.manager)}
            for r in _scan_rows(query, manager_id)]


def verify_login_manager_ancestry():
    """Startup ancestry check: every READ-allowlisted customer_id must be a descendant of the
    configured login manager. Walks customer_client from the login manager, depth-capped at
    3 and count-capped at 500. If the walk TRUNCATES (hits a cap) while a required id is
    still unconfirmed, it fails LOUD rather than guessing; a required id that the completed
    walk never reached raises 'login manager <id> is not an ancestor of <id>'."""
    from .rails import RailViolation, read_customer_ids
    login_id = _digits(getattr(gads(), "login_customer_id", "") or "")
    if not login_id:
        raise RailViolation(
            "no login_customer_id configured — cannot verify manager ancestry",
            code="NO_LOGIN_MANAGER")
    required = {_digits(x) for x in read_customer_ids()}
    required.discard("")

    seen = {login_id}
    frontier = [(login_id, 0)]
    count = 0
    truncated = False
    while frontier:
        manager_id, depth = frontier.pop()
        for child in _customer_client_children(manager_id):
            count += 1
            if count > _ANCESTRY_COUNT_CAP:
                raise RailViolation(
                    f"customer-client walk exceeded {_ANCESTRY_COUNT_CAP} nodes before "
                    "completing — truncated, refusing to authorize the read allowlist",
                    code="ANCESTRY_TRUNCATED")
            child_id = _digits(child["id"])
            if not child_id or child_id in seen:
                continue
            seen.add(child_id)
            if child["manager"]:
                if depth + 1 >= _ANCESTRY_DEPTH_CAP:
                    truncated = True  # a deeper manager we will not expand
                else:
                    frontier.append((child_id, depth + 1))

    missing = required - seen
    if missing and truncated:
        raise RailViolation(
            f"customer-client walk truncated at depth {_ANCESTRY_DEPTH_CAP} before confirming "
            f"{sorted(missing)} — refusing rather than guessing", code="ANCESTRY_TRUNCATED")
    if missing:
        raise RailViolation(
            f"login manager {login_id} is not an ancestor of {sorted(missing)[0]}",
            code="ANCESTRY_NOT_DESCENDANT")


def preflight():
    """Alias for verify_login_manager_ancestry(), wired into server startup elsewhere."""
    return verify_login_manager_ancestry()


def accessible_target_state(customer_id, strategy_id):
    """Read the inherited portfolio target through the client-visible strategy."""
    from .rails import RailViolation
    rows = gaql_all(
        "SELECT accessible_bidding_strategy.target_cpa.target_cpa_micros, "
        "accessible_bidding_strategy.maximize_conversions.target_cpa_micros "
        f"FROM accessible_bidding_strategy WHERE accessible_bidding_strategy.id = {int(strategy_id)}",
        customer_id)
    if len(rows) != 1:
        raise RailViolation("inherited portfolio target unreadable", code="BID_UNREADABLE")
    return rows[0]["accessible_bidding_strategy"]


def verify_post_apply(checks):
    """Read-only effective-field confirmation after successful dispatch; never retries."""
    for check in checks:
        if check.get('recommendation_dismiss'):
            actual = recommendation_dismiss_state(check['customer_id'], check['resource_name'].rsplit('/', 1)[1])
            if actual != check['expected']:
                raise ValueError('saved recommendation identity differs from approved dismissal')
            continue
        if check.get('recommendation_budget'):
            actual = recommendation_budget_state(check['customer_id'], check['budget_resource_name'])
            if actual != check['expected']:
                raise ValueError('saved budget/campaign population differs from approval')
            continue
        if check["entity_type"] == "keyword":
            snapshot = criteria_state(check["customer_id"], "ad_group", check["parent_id"])
            matches = [r for r in snapshot["rows"] if r["resource_name"] == check["resource_name"]]
            if len(matches) != 1:
                raise ValueError("keyword missing after write")
            state = matches[0]
        else:
            state = update_state(check["customer_id"], check["entity_type"], check["entity_id"])
        for key, expected in check["expected"].items():
            if str(state.get(key)) != str(expected):
                raise ValueError(f"effective field {key} did not match requested value")


def criteria_state(customer_id, parent_type, parent_id):
    """Read complete parent/criterion state for write safety; never capped reports."""
    from .rails import RailViolation

    cid, pid = numeric_id(customer_id), numeric_id(parent_id)
    parent = update_state(cid, parent_type, pid)
    expected_parent = (
        ad_group_path(cid, pid)
        if parent_type == "ad_group"
        else campaign_path(cid, pid)
    )
    if parent.get("resource_name") != expected_parent:
        raise RailViolation("parent ownership mismatch")
    if parent_type == "ad_group":
        _validate_resource(parent.get("campaign"), "campaigns", cid)
    campaign = (
        parent
        if parent_type == "campaign"
        else update_state(cid, "campaign", parent["campaign"].rsplit("/", 1)[-1])
    )
    entity = "ad_group_criterion" if parent_type == "ad_group" else "campaign_criterion"
    fields = [
        "resource_name",
        parent_type,
        "criterion_id",
        "status",
        "type",
        "negative",
        "keyword.text",
        "keyword.match_type",
    ]
    if parent_type == "campaign":
        fields += [
            "location.geo_target_constant",
            "bid_modifier",
            "ad_schedule.day_of_week",
            "ad_schedule.start_hour",
            "ad_schedule.start_minute",
            "ad_schedule.end_hour",
            "ad_schedule.end_minute",
        ]
    else:
        fields += ["cpc_bid_micros", "effective_cpc_bid_micros"]
    rows = gaql_all(
        "SELECT "
        + ", ".join(entity + "." + f for f in fields)
        + f" FROM {entity} WHERE {parent_type}.id = {pid}",
        cid,
    )
    criteria = []
    for row in rows:
        item = row[entity]
        item["status"] = _enum_name(
            "AdGroupCriterionStatusEnum"
            if parent_type == "ad_group"
            else "CampaignCriterionStatusEnum",
            item["status"],
        )
        item["type"] = _enum_name(
            "CriterionTypeEnum", item.pop("type_", item.get("type"))
        )
        if item.get("keyword"):
            item["keyword"]["match_type"] = _enum_name(
                "KeywordMatchTypeEnum", item["keyword"]["match_type"]
            )
        if item.get("ad_schedule"):
            for key, enum in [
                ("day_of_week", "DayOfWeekEnum"),
                ("start_minute", "MinuteOfHourEnum"),
                ("end_minute", "MinuteOfHourEnum"),
            ]:
                item["ad_schedule"][key] = _enum_name(enum, item["ad_schedule"][key])
        criteria.append(item)
    account = gaql_all("SELECT customer.time_zone FROM customer", cid)
    if len(account) != 1 or not account[0].get("customer", {}).get("time_zone"):
        raise RailViolation("account time zone unavailable")
    return {
        "parent": parent,
        "campaign": campaign,
        "rows": sorted(criteria, key=lambda x: x["resource_name"]),
        "time_zone": account[0]["customer"]["time_zone"],
    }


def geo_constant_state(customer_id, resource_name):
    """Resolve the requested geo constant through a complete safety read."""
    from .rails import RailViolation

    gid = numeric_id(resource_name.removeprefix("geoTargetConstants/"))
    rows = gaql_all(
        "SELECT geo_target_constant.resource_name, geo_target_constant.status, "
        "geo_target_constant.name, geo_target_constant.target_type "
        f"FROM geo_target_constant WHERE geo_target_constant.id = {gid}",
        customer_id,
    )
    if len(rows) != 1:
        raise RailViolation("geo constant not found uniquely")
    state = rows[0]["geo_target_constant"]
    state["status"] = _enum_name("GeoTargetConstantStatusEnum", state.get("status"))
    if state.get("resource_name") != resource_name or state["status"] != "ENABLED":
        raise RailViolation("geo constant unavailable")
    return state


def list_extensions(customer_id, page_token=None) -> dict:
    """Read nonremoved campaign-level asset links, including sitelinks, callouts and
    structured snippets. This is not customer, ad-group, inherited or asset-group inventory.
    Return one paginated GAQL envelope; follow next_page_token to continue."""
    query = " ".join([
        "SELECT campaign_asset.resource_name, campaign_asset.campaign, campaign_asset.asset,",
        "campaign_asset.field_type, campaign_asset.status, asset.resource_name, asset.name,",
        "asset.type, asset.sitelink_asset.link_text, asset.sitelink_asset.description1,",
        "asset.sitelink_asset.description2, asset.callout_asset.callout_text,",
        "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values",
        'FROM campaign_asset',
        "WHERE campaign_asset.status != 'REMOVED'",
    ])
    return gaql(query, customer_id, page_token)


def get_policy_issues(customer_id, page_token=None) -> dict:
    """Non-APPROVED nonremoved ads and parents, retaining raw policy details."""
    query = " ".join([
        "SELECT campaign.id, campaign.name, campaign.status,",
        "ad_group.id, ad_group.name, ad_group.status,",
        "ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad.id, ad_group_ad.ad.name,",
        "ad_group_ad.policy_summary.approval_status, ad_group_ad.policy_summary.review_status,",
        "ad_group_ad.policy_summary.policy_topic_entries",
        "FROM ad_group_ad",
        "WHERE ad_group_ad.policy_summary.approval_status != 'APPROVED'",
        "AND ad_group_ad.status != 'REMOVED'",
        "AND campaign.status != 'REMOVED'",
        "AND ad_group.status != 'REMOVED'",
        "ORDER BY campaign.id, ad_group.id, ad_group_ad.ad.id",
    ])
    if page_token is not None:
        if type(page_token) is not str or not re.fullmatch(r"[0-9a-f]{64}:.+", page_token):
            from .rails import RailViolation
            raise RailViolation("page_token must be a nonempty bound continuation token",
                                code="BAD_TOKEN")
        _unwrap_token(page_token, query, customer_id)
    return gaql(query, customer_id, page_token)


def get_conversion_actions(customer_id, page_token=None) -> dict:
    """Read nonremoved conversion action configuration visible in the selected customer.
    Includes owner account and attribution settings; does not route to another account.
    This is not exhaustive goal or bidding usage: primary_for_goal=false actions can still
    be biddable in custom goals. Return one paginated GAQL envelope; no writes."""
    query = " ".join([
        "SELECT conversion_action.resource_name, conversion_action.owner_customer,",
        "conversion_action.id, conversion_action.name, conversion_action.type,",
        "conversion_action.status, conversion_action.category, conversion_action.counting_type,",
        "conversion_action.primary_for_goal, conversion_action.value_settings.default_value,",
        "conversion_action.click_through_lookback_window_days,",
        "conversion_action.view_through_lookback_window_days,",
        "conversion_action.attribution_model_settings.attribution_model",
        'FROM conversion_action',
        "WHERE conversion_action.status != 'REMOVED'",
    ])
    return gaql(query, customer_id, page_token)


def list_recommendations(customer_id, page_token=None) -> dict:
    """Read non-dismissed Google recommendations in one paginated GAQL envelope.
    Impact values are Google's estimates, not measured results or our approval.
    Does not rank, approve, apply or dismiss recommendations."""
    query = " ".join([
        "SELECT recommendation.resource_name, recommendation.type, recommendation.impact,",
        "recommendation.campaign, recommendation.ad_group, recommendation.dismissed",
        'FROM recommendation',
        'WHERE recommendation.dismissed = FALSE',
    ])
    return gaql(query, customer_id, page_token)


# Closed paused creation surface. Temporary identities never escape this batch.
CREATION_STRATEGIES = frozenset({"manual_cpc", "maximize_conversions", "maximize_conversion_value"})
SEARCH_NETWORKS = {"target_google_search": True, "target_search_network": False,
                   "target_content_network": False, "target_partner_search_network": False}
SEARCH_GEO_OPTIONS = {"positive_geo_target_type": "PRESENCE", "negative_geo_target_type": "PRESENCE"}
POLITICAL_DECLARATIONS = {True: "CONTAINS_EU_POLITICAL_ADVERTISING",
                          False: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}
STRUCTURED_SNIPPET_HEADERS = frozenset({
    'Brands', 'Amenities', 'Styles', 'Types', 'Destinations', 'Services', 'Courses',
    'Neighborhoods', 'Shows', 'Insurance coverage', 'Degree programs',
    'Featured hotels', 'Models',
})
_CREATE_FIELDS.update({
    "CampaignBudget": {"resource_name", "amount_micros", "explicitly_shared", "period", "delivery_method"},
    "Campaign": {"resource_name", "name", "status", "advertising_channel_type", "campaign_budget",
                 "contains_eu_political_advertising", *CREATION_STRATEGIES,
                 "maximize_conversions.target_cpa_micros", "maximize_conversion_value.target_roas",
                 *("network_settings." + key for key in SEARCH_NETWORKS),
                 *("geo_target_type_setting." + key for key in SEARCH_GEO_OPTIONS)},
    "AdGroup": {"name", "status", "campaign", "type_", "cpc_bid_micros"},
})


def language_constant_path(language_id):
    from google.ads.googleads.v25.services.services.google_ads_service import (
        GoogleAdsServiceClient,
    )
    return GoogleAdsServiceClient.language_constant_path(numeric_id(language_id))


def creation_paths(customer_id):
    """Only these two negative definitions are available, using generated helpers."""
    from google.ads.googleads.v25.services.services.campaign_budget_service import (
        CampaignBudgetServiceClient,
    )
    from google.ads.googleads.v25.services.services.campaign_service import (
        CampaignServiceClient,
    )
    cid = numeric_id(customer_id)
    return (CampaignBudgetServiceClient.campaign_budget_path(cid, "-1"),
            CampaignServiceClient.campaign_path(cid, "-2"))


def _creation_reference(value, kind, cid, context):
    if context is not None and value in context and context[value] == kind:
        return
    _validate_resource(value, kind, cid)


def _payload_micros(value):
    from .rails import RailViolation
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise RailViolation("create money must be positive int64 micros")


def _validate_entity_create(entity, values, cid, context):
    from .rails import RailViolation
    if entity == "CampaignBudget":
        if set(values) != _CREATE_FIELDS[entity]:
            raise RailViolation("budget create requires all dedicated daily budget fields")
        if (values['resource_name'] != creation_paths(cid)[0] or context is None
                or context.get(values['resource_name']) != 'campaignBudgets'):
            raise RailViolation("budget requires validated temporary definition")
        if values['explicitly_shared'] is not False or values['period'] != 'DAILY' or values['delivery_method'] != 'STANDARD':
            raise RailViolation("budget must be nonshared DAILY STANDARD")
        _payload_micros(values['amount_micros'])
        return
    if values.get('status') != 'PAUSED':
        raise RailViolation("serving creates must be explicitly PAUSED")
    creation_name(values.get('name'))
    if entity == 'AdGroup':
        if not {'name', 'status', 'campaign', 'type_'} <= set(values) or values['type_'] != 'SEARCH_STANDARD':
            raise RailViolation("ad group requires SEARCH_STANDARD and parent")
        _validate_resource(values['campaign'], 'campaigns', cid)
        if 'cpc_bid_micros' in values:
            _payload_micros(values['cpc_bid_micros'])
        return
    required = {'resource_name', 'name', 'status', 'advertising_channel_type', 'campaign_budget',
                'contains_eu_political_advertising', 'network_settings', 'geo_target_type_setting'}
    if not required <= set(values) or values['advertising_channel_type'] != 'SEARCH':
        raise RailViolation("campaign requires explicit Search creation settings")
    if (values['resource_name'] != creation_paths(cid)[1] or context is None
            or context.get(values['resource_name']) != 'campaigns'
            or values['campaign_budget'] != creation_paths(cid)[0]):
        raise RailViolation("campaign requires validated dedicated new budget and temporary identity")
    _creation_reference(values['campaign_budget'], 'campaignBudgets', cid, context)
    if (values['network_settings'] != SEARCH_NETWORKS
            or any(type(v) is not bool for v in values['network_settings'].values())
            or values['geo_target_type_setting'] != SEARCH_GEO_OPTIONS
            or values['contains_eu_political_advertising'] not in POLITICAL_DECLARATIONS.values()):
        raise RailViolation("invalid explicit Search settings or political declaration")
    strategies = set(values) & CREATION_STRATEGIES
    if len(strategies) != 1:
        raise RailViolation("create requires exactly one supported bidding message")
    strategy = next(iter(strategies))
    params = values[strategy]
    allowed = {'manual_cpc': set(), 'maximize_conversions': {'target_cpa_micros'},
               'maximize_conversion_value': {'target_roas'}}[strategy]
    if not isinstance(params, dict) or not set(params) <= allowed:
        raise RailViolation("invalid strategy create fields")
    if 'target_cpa_micros' in params:
        _payload_micros(params['target_cpa_micros'])
    if 'target_roas' in params:
        from .rails import check_roas
        check_roas(params['target_roas'])


def validate_mutation_plan(plan):
    """Validate the entire dependency graph before client creation or dispatch."""
    from .rails import RailViolation
    cid = numeric_id(plan.mutate_customer_id)
    if (any(op.service in _SHARED_NEGATIVE_SERVICES for op in plan.operations)
            or any(isinstance(check, dict) and ('shared_negative_create' in check or 'shared_negative_add' in check or 'shared_negative_attach' in check) for check in plan.post_checks)):
        return validate_shared_negative_plan(plan)
    if (any(isinstance(check, dict) and 'demand_gen_ad' in check for check in plan.post_checks)
            or any(isinstance(op.operation, dict)
                   and isinstance(op.operation.get('create'), dict)
                   and isinstance(op.operation['create'].get('ad'), dict)
                   and 'demand_gen_multi_asset_ad' in op.operation['create']['ad']
                   for op in plan.operations)):
        return validate_demand_gen_ad_plan(plan)
    if (any(isinstance(check, dict) and check.get('demand_gen') for check in plan.post_checks)
            or any(isinstance(op.operation, dict) and isinstance(op.operation.get('create'), dict)
                   and op.operation['create'].get('advertising_channel_type') == 'DEMAND_GEN'
                   for op in plan.operations)):
        return validate_demand_gen_plan(plan)
    if (any(op.service == 'AssetGroupListingGroupFilterService' for op in plan.operations)
            or any(isinstance(check, dict) and 'listing_filter' in check for check in plan.post_checks)):
        return validate_listing_filter_plan(plan)
    if any(isinstance(check, dict) and check.get('pmax_asset_group_asset_removal')
           for check in plan.post_checks):
        return validate_pmax_asset_group_asset_remove_plan(plan)
    if any(isinstance(check, dict) and check.get('pmax_asset_group_assets')
           for check in plan.post_checks):
        return validate_pmax_asset_group_asset_add_plan(plan)
    if any(isinstance(check, dict) and check.get('pmax_asset_group_update')
           for check in plan.post_checks):
        return validate_pmax_asset_group_update_plan(plan)
    if any(isinstance(check, dict) and check.get('pmax_asset_group') for check in plan.post_checks):
        return validate_pmax_asset_group_plan(plan)
    if (any(op.service in {'AssetGroupService', 'AssetGroupAssetService'} for op in plan.operations)
            or any(isinstance(op.operation, dict) and isinstance(op.operation.get('create'), dict)
                   and op.operation['create'].get('advertising_channel_type') == 'PERFORMANCE_MAX'
                   for op in plan.operations)
            or any(isinstance(check, dict) and check.get('pmax') for check in plan.post_checks)):
        return validate_pmax_plan(plan)
    entity_removals = [op for op in plan.operations if isinstance(op.operation, dict)
                       and 'remove' in op.operation
                       and op.service in {'CampaignService', 'AdGroupService',
                                          'AdGroupAdService'}]
    if entity_removals:
        expected = {'CampaignService': ('campaign', 'campaigns'),
                    'AdGroupService': ('ad_group', 'adGroups'),
                    'AdGroupAdService': ('ad_group_ad', 'adGroupAds')}
        if len(plan.operations) != 1 or len(entity_removals) != 1:
            raise RailViolation('entity removal must be exactly one standalone operation')
        op = entity_removals[0]
        entity_type, kind = expected[op.service]
        checks = plan.post_checks
        if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
                or set(checks[0]) != {'removal', 'entity_type', 'customer_id',
                                      'resource_name', 'parent_resource_name'}
                or checks[0]['removal'] is not True
                or checks[0]['entity_type'] != entity_type
                or checks[0]['customer_id'] != cid
                or checks[0]['resource_name'] != op.operation['remove']
                or (checks[0]['parent_resource_name'] is not None
                    and type(checks[0]['parent_resource_name']) is not str)):
            raise RailViolation('entity removal requires one coherent postcheck descriptor')
        _validate_resource(op.operation['remove'], kind, cid)
        parent = checks[0]['parent_resource_name']
        parent_kind = {'campaign': None, 'ad_group': 'campaigns',
                       'ad_group_ad': 'adGroups'}[entity_type]
        if (parent_kind is None and parent is not None) or (
                parent_kind is not None and parent is None):
            raise RailViolation('entity removal postcheck parent is incoherent')
        if parent_kind:
            _validate_resource(parent, parent_kind, cid)
    portfolios = [op for op in plan.operations if op.service == 'BiddingStrategyService'
                  and isinstance(op.operation, dict) and 'create' in op.operation]
    if portfolios and (len(plan.operations) != 1 or len(portfolios) != 1):
        raise RailViolation('portfolio creation requires one standalone operation')
    if portfolios:
        checks = plan.post_checks
        values = portfolios[0].operation['create']
        if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
                or set(checks[0]) != {'result_index', 'entity_type', 'customer_id',
                                      'portfolio_creation', 'expected', 'currency_code'}
                or checks[0]['result_index'] != 0
                or checks[0]['entity_type'] != 'bidding_strategy'
                or checks[0]['customer_id'] != cid
                or checks[0]['portfolio_creation'] is not True
                or checks[0]['expected'] != values
                or type(checks[0]['currency_code']) is not str
                or not re.fullmatch(r'[A-Z]{3}', checks[0]['currency_code'])):
            raise RailViolation('portfolio creation requires one coherent postcheck descriptor')
    targeting = [op for op in plan.operations if isinstance(op.operation, dict)
                 and any(isinstance(v, dict) and 'user_list' in v for v in op.operation.values())]
    if targeting and (len(plan.operations) != 1 or targeting[0].service != 'CampaignCriterionService'):
        raise RailViolation('audience targeting requires one standalone CampaignCriterionService create')
    if any(op.service == "ConversionActionService" for op in plan.operations) and len(plan.operations) != 1:
        raise RailViolation("conversion mutation requires one standalone operation")
    audiences = [op for op in plan.operations if op.service == 'UserListService']
    if audiences and len(plan.operations) != 1:
        raise RailViolation('audience creation requires one standalone UserListService create')
    images = [op for op in plan.operations if isinstance(op.operation, dict)
              and isinstance(op.operation.get('create'), dict)
              and 'image_asset' in op.operation['create']]
    if images and (len(plan.operations) != 1 or images[0].service != 'AssetService'):
        raise RailViolation('image upload requires one standalone AssetService create')
    texts = [op for op in plan.operations if isinstance(op.operation, dict)
             and isinstance(op.operation.get('create'), dict)
             and 'text_asset' in op.operation['create']]
    if texts and (len(plan.operations) != 1 or texts[0].service != 'AssetService'):
        raise RailViolation('text upload requires one standalone AssetService create')
    removals = [op for op in plan.operations if isinstance(op.operation, dict)
                and 'remove' in op.operation
                and op.service in {'CampaignAssetService', 'AdGroupAssetService'}]
    if removals and (len(plan.operations) != 1 or len(removals) != 1):
        raise RailViolation('asset association removal must be exactly one standalone operation')
    context, budget_consumers, asset_consumers = {}, 0, {}
    asset_targets, asset_services, asset_families = set(), set(), set()
    budget, campaign = creation_paths(cid)
    for op in plan.operations:
        values = op.operation.get('create') if isinstance(op.operation, dict) else None
        if isinstance(values, dict):
            definition = values.get('resource_name')
            if definition is not None:
                expected = {'CampaignBudgetService': (budget, 'campaignBudgets'),
                            'CampaignService': (campaign, 'campaigns')}.get(op.service)
                if op.service == 'AssetService' and re.fullmatch(
                        rf'customers/{cid}/assets/-[1-9][0-9]*', str(definition)):
                    families = set(values) & {
                        'sitelink_asset', 'callout_asset', 'structured_snippet_asset'}
                    if len(families) == 1:
                        family = next(iter(families)).removesuffix('_asset').upper()
                        expected = (definition, 'assets:' + family)
                if expected is None or definition != expected[0] or definition in context:
                    raise RailViolation("unsupported or duplicate temporary definition")
                if op.service == 'CampaignService':
                    if budget not in context or values.get('campaign_budget') != budget:
                        raise RailViolation("campaign budget is forward, dangling or wrong kind")
                    budget_consumers += 1
                context[definition] = expected[1]
                if op.service == 'AssetService':
                    asset_consumers[definition] = 0
        if op.service in {'CampaignAssetService', 'AdGroupAssetService'} and isinstance(values, dict):
            asset = values.get('asset')
            kind = context.get(asset, '').removeprefix('assets:')
            if kind not in {'SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET'}:
                raise RailViolation('asset reference is forward, dangling or wrong kind')
            asset_consumers[asset] += 1
            parent = 'campaign' if op.service == 'CampaignAssetService' else 'ad_group'
            asset_targets.add(values.get(parent))
            asset_services.add(op.service)
            asset_families.add(kind)
        validate_mutation_operation(op, cid, context)
    if budget in context and (campaign not in context or budget_consumers != 1):
        raise RailViolation("new dedicated budget must have exactly one new campaign consumer")
    if budget in context:
        targets = {'location': set(), 'language': set()}
        if len(plan.operations) < 4:
            raise RailViolation("new Search campaign requires locations and languages")
        for op in plan.operations[2:]:
            values = op.operation.get('create', {})
            kind = 'location' if 'location' in values else 'language'
            if (op.service != 'CampaignCriterionService' or values.get('campaign') != campaign
                    or values.get('negative') is not False or kind not in values):
                raise RailViolation("new campaign batch permits only its positive locations and languages")
            constant = next(iter(values[kind].values()))
            if constant in targets[kind]:
                raise RailViolation("duplicate campaign target")
            targets[kind].add(constant)
        if any(not 1 <= len(values) <= 100 for values in targets.values()):
            raise RailViolation("new campaign requires 1 to 100 locations and languages each")
    if asset_consumers:
        if any(count != 1 for count in asset_consumers.values()):
            raise RailViolation('each new asset must have exactly one target link')
        if (len(asset_consumers) > 10 or len(plan.operations) != 2 * len(asset_consumers)
                or len(asset_targets) != 1 or len(asset_services) != 1
                or len(asset_families) != 1):
            raise RailViolation('asset batch exceeds its local cap or contains mixed or unrelated operations')
    return context


def creation_name(value):
    import unicodedata

    from .rails import RailViolation
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or any(unicodedata.category(c).startswith('C') for c in value)
            or len(value.encode('utf-8')) > 255):
        raise RailViolation("name must be nonblank, at most 128 characters/255 UTF-8 bytes and contain no controls")
    return value


def _creation_account(customer_id, include_manager=False, include_status=False):
    from .rails import RailViolation
    envelope = account_info(customer_id)
    if not isinstance(envelope, dict) or envelope.get('error') or envelope.get('errors'):
        raise RailViolation('account identity unreadable')
    rows = envelope.get('rows', [])
    if (not isinstance(rows, list) or len(rows) != 1 or envelope.get('next_page_token')
            or envelope.get('pages_complete') is not True or envelope.get('returned_count') != 1
            or envelope.get('total_results_count') != 1):
        raise RailViolation("account identity unreadable")
    account = _creation_row(rows[0], 'customer')
    if (str(account.get('id')) != customer_id or not isinstance(account.get('currency_code'), str)
            or not re.fullmatch('[A-Z]{3}', account['currency_code'])
            or not isinstance(account.get('time_zone'), str) or not account['time_zone']):
        raise RailViolation("account identity/currency/time zone unreadable")
    result = {key: account[key] for key in ('id', 'currency_code', 'time_zone')}
    if include_status:
        result['status'] = _inventory_enum('CustomerStatusEnum', account.get('status'))
        if result['status'] != 'ENABLED':
            raise RailViolation('account must be enabled')
    if include_manager:
        if type(account.get('manager')) is not bool:
            raise RailViolation('account manager status unreadable')
        result['manager'] = account['manager']
    return result


def _inventory_enum(enum_type, value):
    """Decode only installed, readable enum members without constructing a provider client."""
    from google.ads.googleads.v25 import enums

    from .rails import RailViolation
    enum = getattr(getattr(enums, enum_type), enum_type.removesuffix('Enum'))
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RailViolation('inventory status/type is unreadable')
    try:
        name = enum[value].name if isinstance(value, str) else enum(value).name
    except (KeyError, ValueError, TypeError):
        raise RailViolation('inventory status/type is unreadable') from None
    if name in {'UNKNOWN', 'UNSPECIFIED', 'INVALID', 'REMOVED'}:
        raise RailViolation('inventory status/type is unreadable')
    return name


def portfolio_creation_state(cid, name):
    """Exact non-manager owner plus complete owned standalone-strategy name inventory."""
    from .rails import RailViolation
    account = _creation_account(cid, include_manager=True)
    if account['manager'] is not False:
        raise RailViolation('portfolio creation requires a non-manager client account')
    rows = gaql_all('SELECT bidding_strategy.resource_name, bidding_strategy.id, '
                    'bidding_strategy.name, bidding_strategy.status, bidding_strategy.type '
                    "FROM bidding_strategy WHERE bidding_strategy.status != 'REMOVED'", cid)
    inventory, seen = [], set()
    for row in rows:
        item = dict(_creation_row(row, 'bidding_strategy'))
        rn = item.get('resource_name')
        _validate_resource(rn, 'biddingStrategies', cid)
        status = _inventory_enum('BiddingStrategyStatusEnum', item.get('status'))
        strategy_type = _inventory_enum('BiddingStrategyTypeEnum', item.get('type_', item.get('type')))
        if (rn in seen or str(item.get('id')) != rn.rsplit('/', 1)[1]
                or type(item.get('name')) is not str or not item['name'].strip()
                or status != 'ENABLED'):
            raise RailViolation('portfolio inventory ownership, identity, name, type or status unreadable')
        seen.add(rn)
        item.update(status=status, type=strategy_type)
        inventory.append(item)
    if any(item['name'] == name for item in inventory):
        raise RailViolation('owned portfolio strategy name already exists')
    return {'account': account, 'strategies': sorted(inventory, key=lambda item: item['resource_name'])}


def _creation_collisions(cid, entity, name, parent=None, strict_owner=False):
    from .rails import RailViolation
    extra = ', ad_group.campaign' if parent else ''
    where = f"{entity}.status != 'REMOVED'"
    if parent:
        where += f" AND ad_group.campaign = '{parent}'"
    rows = gaql_all(f"SELECT {entity}.resource_name, {entity}.name, {entity}.status{extra} FROM {entity} WHERE {where}", cid)
    enums = {'campaign': 'CampaignStatusEnum', 'campaign_budget': 'BudgetStatusEnum', 'ad_group': 'AdGroupStatusEnum'}
    kinds = {'campaign': 'campaigns', 'campaign_budget': 'campaignBudgets', 'ad_group': 'adGroups'}
    seen, matches = set(), []
    for row in rows:
        state = _creation_row(row, entity)
        rn = state.get('resource_name')
        _validate_resource(rn, kinds[entity], cid)
        if strict_owner:
            _pmax_owned(rn, kinds[entity], cid)
        status = _inventory_enum(enums[entity], state.get('status'))
        allowed = {'ENABLED'} if entity == 'campaign_budget' else {'ENABLED', 'PAUSED'}
        if rn in seen or not isinstance(state.get('name'), str) or not state['name'].strip() or status not in allowed or (parent and state.get('campaign') != parent):
            raise RailViolation("name population ownership/status/name unreadable or duplicate")
        seen.add(rn)
        if state['name'] == name:
            matches.append(state)
    return matches


def _creation_constants(cid, entity, ids):
    from .rails import RailViolation
    geo = entity == 'geo_target_constant'
    field = 'status' if geo else 'targetable'
    rows = gaql_all(f"SELECT {entity}.resource_name, {entity}.name, {entity}.{field} FROM {entity} WHERE {entity}.id IN ({','.join(ids)})", cid)
    requested = {(geo_target_constant_path if geo else language_constant_path)(i) for i in ids}
    found = {}
    for row in rows:
        state = dict(_creation_row(row, entity))
        rn = state.get('resource_name')
        if geo:
            state['status'] = _inventory_enum('GeoTargetConstantStatusEnum', state.get('status'))
        if (rn not in requested or rn in found or not isinstance(state.get('name'), str)
                or not state['name'].strip() or (state.get('status') != 'ENABLED' if geo else state.get('targetable') is not True)):
            raise RailViolation("constant missing, duplicate, disabled or unreadable")
        state['id'] = rn.rsplit('/', 1)[1]
        found[rn] = state
    if set(found) != requested:
        raise RailViolation("requested constants did not reconcile")
    return [found[rn] for rn in sorted(found)]


def creation_parent(cid, campaign_id):
    from .rails import RailViolation
    fields = 'resource_name, name, status, advertising_channel_type, advertising_channel_sub_type, campaign_budget'
    rows = gaql_all('SELECT ' + ', '.join('campaign.' + f for f in fields.split(', ')) + f' FROM campaign WHERE campaign.id = {numeric_id(campaign_id)}', cid)
    if len(rows) != 1:
        raise RailViolation("creation parent not readable uniquely")
    state = dict(_creation_row(rows[0], 'campaign'))
    for field, enum in [('status', 'CampaignStatusEnum'), ('advertising_channel_type', 'AdvertisingChannelTypeEnum'), ('advertising_channel_sub_type', 'AdvertisingChannelSubTypeEnum')]:
        state[field] = _enum_name(enum, state.get(field))
    if (state.get('resource_name') != campaign_path(cid, campaign_id)
            or state['status'] not in {'ENABLED', 'PAUSED'} or state['advertising_channel_type'] != 'SEARCH'
            or state['advertising_channel_sub_type'] != 'UNSPECIFIED'
            or not isinstance(state.get('name'), str) or not state['name'].strip()):
        raise RailViolation("parent must be a readable nonremoved standard Search campaign")
    _validate_resource(state.get('campaign_budget'), 'campaignBudgets', cid)
    return state


def creation_state(cid, name, geo_ids=None, language_ids=None, campaign_id=None):
    state = {'account': _creation_account(cid)}
    if campaign_id is None:
        state.update(collisions=_creation_collisions(cid, 'campaign', name) + _creation_collisions(cid, 'campaign_budget', name),
                     locations=_creation_constants(cid, 'geo_target_constant', geo_ids),
                     languages=_creation_constants(cid, 'language_constant', language_ids))
    else:
        parent = creation_parent(cid, campaign_id)
        state.update(parent=parent, strategy=effective_strategy(cid, campaign_id),
                     collisions=_creation_collisions(cid, 'ad_group', name, parent['resource_name']))
    return state


def demand_gen_account_state(cid):
    """Complete raw account proof with both canonical account identities."""
    from .rails import RailViolation
    cid = pmax_id(cid)
    fields = ('resource_name', 'id', 'descriptive_name', 'currency_code', 'time_zone',
              'manager', 'status')
    rows = _scan_rows('SELECT ' + ', '.join('customer.' + field for field in fields)
                      + ' FROM customer', cid)
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('Demand Gen account proof requires one complete raw provider row')
    raw = dict(_creation_row(_pmax_raw_dict(rows[0]), 'customer'))
    status = _inventory_enum('CustomerStatusEnum', raw.get('status'))
    if (raw.get('resource_name') != f'customers/{cid}' or str(raw.get('id')) != cid
            or type(raw.get('descriptive_name')) is not str
            or not raw['descriptive_name'].strip()
            or type(raw.get('currency_code')) is not str
            or not re.fullmatch(r'[A-Z]{3}', raw['currency_code'])
            or type(raw.get('time_zone')) is not str or not raw['time_zone']
            or type(raw.get('manager')) is not bool or status != 'ENABLED'):
        raise RailViolation('Demand Gen account descriptive name unreadable')
    account = {key: raw[key] for key in (
        'resource_name', 'id', 'descriptive_name', 'currency_code', 'time_zone', 'manager')}
    account['id'], account['status'] = cid, status
    if account['manager'] is not False:
        raise RailViolation('Demand Gen creation requires a non-manager client account')
    return account


def demand_gen_creation_state(cid, name, geo_ids, language_ids):
    """Strict owner, name population and enabled constants for Demand Gen creation."""
    return {
        'account': demand_gen_account_state(cid),
        'collisions': (_creation_collisions(cid, 'campaign', name, strict_owner=True)
                       + _creation_collisions(cid, 'campaign_budget', name, strict_owner=True)),
        'locations': _creation_constants(cid, 'geo_target_constant', geo_ids),
        'languages': _creation_constants(cid, 'language_constant', language_ids),
    }


_CREATED_READ_FIELDS = {
    'bidding_strategy': ['name', 'type', 'status', 'currency_code', 'effective_currency_code',
                         'non_removed_campaign_count', 'target_cpa.target_cpa_micros',
                         'target_roas.target_roas'],
    'user_list': ['name', 'type', 'membership_status', 'access_reason', 'read_only', 'rule_based_user_list'],
    'ad_group_ad': ['ad_group', 'status', 'ad.type', 'ad.final_urls',
                    'ad.responsive_search_ad.headlines', 'ad.responsive_search_ad.descriptions',
                    'ad.responsive_search_ad.path1', 'ad.responsive_search_ad.path2'],
    'campaign_budget': ['amount_micros', 'explicitly_shared', 'period', 'delivery_method'],
    'campaign': ['name', 'status', 'campaign_budget', 'advertising_channel_type',
                 'bidding_strategy_type', 'contains_eu_political_advertising',
                 *('network_settings.' + key for key in SEARCH_NETWORKS),
                 *('geo_target_type_setting.' + key for key in SEARCH_GEO_OPTIONS),
                 'maximize_conversions.target_cpa_micros', 'maximize_conversion_value.target_roas'],
    'ad_group': ['name', 'status', 'campaign', 'type', 'cpc_bid_micros', 'effective_cpc_bid_micros'],
    'campaign_criterion': ['campaign', 'status', 'negative', 'location.geo_target_constant', 'language.language_constant'],
    'asset': ['type', 'final_urls', 'final_mobile_urls', 'tracking_url_template',
              'final_url_suffix', 'url_custom_parameters', 'sitelink_asset.link_text',
              'sitelink_asset.description1', 'sitelink_asset.description2',
              'sitelink_asset.start_date', 'sitelink_asset.end_date',
              'sitelink_asset.ad_schedule_targets', 'callout_asset.callout_text',
              'callout_asset.start_date', 'callout_asset.end_date',
              'callout_asset.ad_schedule_targets', 'structured_snippet_asset.header',
              'structured_snippet_asset.values'],
    'campaign_asset': ['campaign', 'asset', 'field_type', 'status'],
    'ad_group_asset': ['ad_group', 'asset', 'field_type', 'status'],
}
_CREATED_KINDS = {'bidding_strategy': 'biddingStrategies', 'conversion_action': 'conversionActions', 'user_list': 'userLists', 'ad_group_ad': 'adGroupAds', 'campaign_budget': 'campaignBudgets', 'campaign': 'campaigns',
                  'ad_group': 'adGroups', 'campaign_criterion': 'campaignCriteria', 'asset': 'assets',
                  'campaign_asset': 'campaignAssets', 'ad_group_asset': 'adGroupAssets'}
_CREATED_ENUMS = {'bidding_strategy': {'type_': 'BiddingStrategyTypeEnum', 'status': 'BiddingStrategyStatusEnum'}, 'user_list': {}, 'ad_group_ad': {'status': 'AdGroupAdStatusEnum'}, 'campaign_budget': {'period': 'BudgetPeriodEnum', 'delivery_method': 'BudgetDeliveryMethodEnum'},
                  'campaign': {'status': 'CampaignStatusEnum', 'advertising_channel_type': 'AdvertisingChannelTypeEnum',
                               'bidding_strategy_type': 'BiddingStrategyTypeEnum',
                               'contains_eu_political_advertising': 'EuPoliticalAdvertisingStatusEnum'},
                  'ad_group': {'status': 'AdGroupStatusEnum', 'type_': 'AdGroupTypeEnum'},
                  'campaign_criterion': {'status': 'CampaignCriterionStatusEnum'},
                  'asset': {'type_': 'AssetTypeEnum'},
                  'campaign_asset': {'field_type': 'AssetFieldTypeEnum', 'status': 'AssetLinkStatusEnum'},
                  'ad_group_asset': {'field_type': 'AssetFieldTypeEnum', 'status': 'AssetLinkStatusEnum'}}


def created_resource_state(cid, entity, resource_name, image=False, text=False):
    """Closed exact-resource reads, only after actual positive result identities exist."""
    from .rails import RailViolation
    _validate_resource(resource_name, _CREATED_KINDS[entity], cid)
    fields = (['resource_name', 'name', 'type', 'image_asset.mime_type',
               'image_asset.file_size', 'image_asset.full_size.width_pixels',
               'image_asset.full_size.height_pixels'] if image else
              ['resource_name', *_CREATED_READ_FIELDS[entity]])
    if text:
        fields = ['resource_name', 'type', 'text_asset.text']
    rows = gaql_all('SELECT ' + ', '.join(entity + '.' + key for key in fields)
                    + f" FROM {entity} WHERE {entity}.resource_name = '{resource_name}'", cid)
    if len(rows) != 1 or rows[0].get(entity, {}).get('resource_name') != resource_name:
        raise RailViolation('created resource readback missing or identity mismatch')
    state = dict(rows[0][entity])
    for key, enum in _CREATED_ENUMS[entity].items():
        state[key] = (_inventory_enum(enum, state.get(key))
                      if entity == 'bidding_strategy' and key == 'status'
                      else _enum_name(enum, state.get(key)))
    if entity == 'campaign':
        geo = dict(state.get('geo_target_type_setting', {}))
        for key, enum in [('positive_geo_target_type', 'PositiveGeoTargetTypeEnum'),
                          ('negative_geo_target_type', 'NegativeGeoTargetTypeEnum')]:
            geo[key] = _enum_name(enum, geo.get(key))
        state['geo_target_type_setting'] = geo
    return state


def _created_match(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _created_match(actual[key], value) for key, value in expected.items())
    if type(expected) is bool:
        return actual is expected
    if isinstance(expected, (int, float)):
        try:
            return not isinstance(actual, bool) and decimal.Decimal(str(actual)) == decimal.Decimal(str(expected))
        except decimal.InvalidOperation:
            return False
    return actual == expected


def verify_pmax_asset_group_update_result(checks, result):
    """Reconcile the exact update result before rereading target, parent and siblings."""
    from .rails import RailViolation
    if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
            or checks[0].get('pmax_asset_group_update') is not True
            or type(result) is not dict or result.get('validate_only')
            or type(result.get('results')) is not list or len(result['results']) != 1):
        raise RailViolation('PMax asset-group update result count/validation state does not reconcile')
    check, entry = checks[0], result['results'][0]
    cid, rn = pmax_id(check['customer_id']), check['resource_name']
    if (type(entry) is not dict or set(entry) != {'type', 'resource_name'}
            or entry['type'] != 'asset_group_result' or entry['resource_name'] != rn):
        raise RailViolation('PMax asset-group update result identity mismatch')
    _pmax_owned(rn, 'assetGroups', cid)
    fresh = pmax_asset_group_update_state(cid, rn.rsplit('/', 1)[1],
                                          check['expected']['name'])
    if fresh['target'] != check['expected']:
        raise RailViolation('PMax saved asset-group fields do not match the exact update proof')
    if fresh['parent_proof'] != check['parent_proof']:
        raise RailViolation('PMax parent or inherited branding changed after update')
    expected_inventory = {key: check['expected'][key] for key in
                          ('resource_name', 'id', 'campaign', 'name', 'status')}
    matches = [item for item in fresh['asset_groups']
               if item['resource_name'] == check['resource_name']]
    if matches != [expected_inventory] or any(
            item['resource_name'] != check['resource_name']
            and item['name'] == check['expected']['name']
            for item in fresh['asset_groups']):
        raise RailViolation('PMax saved target or sibling-name inventory does not reconcile')
    return True


def verify_pmax_asset_group_asset_add_result(checks, result):
    """Resolve ordered link identities before proving the exact full saved union."""
    import copy

    from .rails import RailViolation
    if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
            or checks[0].get('pmax_asset_group_assets') is not True
            or type(result) is not dict or result.get('validate_only')
            or type(result.get('results')) is not list):
        raise RailViolation('PMax asset-group add result validation state does not reconcile')
    check, results = checks[0], result['results']
    additions = check['additions']
    if len(results) != len(additions):
        raise RailViolation('PMax asset-group add result count does not reconcile')
    cid, group_rn = pmax_id(check['customer_id']), check['asset_group']
    expected_new, seen = [], set()
    for entry, addition in zip(results, additions):
        expected_rn = (f'customers/{cid}/assetGroupAssets/'
                       f'{group_rn.rsplit("/", 1)[1]}~'
                       f'{addition["asset"].rsplit("/", 1)[1]}~'
                       f'{PMAX_FIELD_NUMBERS[addition["field_type"]]}')
        if (type(entry) is not dict or set(entry) != {'type', 'resource_name'}
                or entry['type'] != 'asset_group_asset_result'
                or entry['resource_name'] != expected_rn or expected_rn in seen):
            raise RailViolation('PMax asset-group add ordered result identity mismatch')
        _pmax_owned(expected_rn, 'assetGroupAssets', cid)
        seen.add(expected_rn)
        expected_new.append({'resource_name': expected_rn, 'asset_group': group_rn,
                             'asset': addition['asset'],
                             'field_type': addition['field_type'], 'status': 'PAUSED',
                             'content': copy.deepcopy(addition['content'])})
    fresh = pmax_asset_group_asset_state(cid, group_rn.rsplit('/', 1)[1])
    expected_inventory = sorted(
        [*copy.deepcopy(check['existing_assets']), *expected_new],
        key=lambda item: item['resource_name'])
    if fresh['target'] != check['target']:
        raise RailViolation('PMax asset group changed after asset linking')
    if fresh['parent_proof'] != check['parent_proof']:
        raise RailViolation('PMax parent or inherited branding changed after asset linking')
    if fresh['existing_assets'] != expected_inventory:
        raise RailViolation('PMax saved asset-group link union does not reconcile exactly')
    return True


def pmax_asset_group_asset_removed_state(check):
    """Complete exact-resource read: absence or one exact REMOVED tombstone succeeds."""
    from .rails import RailViolation
    cid, rn = pmax_id(check['customer_id']), check['resource_name']
    _pmax_owned(rn, 'assetGroupAssets', cid)
    fields = ('resource_name', 'asset_group', 'asset', 'field_type', 'status')
    rows = _scan_rows(
        'SELECT ' + ', '.join('asset_group_asset.' + field for field in fields)
        + f" FROM asset_group_asset WHERE asset_group_asset.resource_name = '{rn}'", cid)
    if not rows:
        return None
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('PMax removed connection readback is ambiguous or incomplete')
    item = dict(_creation_row(_pmax_raw_dict(rows[0]), 'asset_group_asset'))
    role = _pmax_enum_exact('AssetFieldTypeEnum', item.get('field_type'),
                            set(PMAX_ASSET_GROUP_ROLES))
    status = _pmax_enum_exact('AssetLinkStatusEnum', item.get('status'), {'REMOVED'})
    target = check['target_link']
    if (item.get('resource_name') != rn or item.get('asset_group') != check['asset_group']
            or item.get('asset') != target['asset'] or role != check['field_type']):
        raise RailViolation('PMax removed connection tombstone identity mismatch')
    return status


def verify_pmax_asset_group_asset_remove_result(checks, result):
    """Verify result identity, tombstone, bare asset, remaining union and immutable parents."""
    from .rails import RailViolation
    if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
            or checks[0].get('pmax_asset_group_asset_removal') is not True
            or type(result) is not dict or result.get('validate_only')
            or type(result.get('results')) is not list or len(result['results']) != 1):
        raise RailViolation('PMax asset-group remove result validation state does not reconcile')
    check, entry = checks[0], result['results'][0]
    if (type(entry) is not dict or set(entry) != {'type', 'resource_name'}
            or entry['type'] != 'asset_group_asset_result'
            or entry['resource_name'] != check['resource_name']):
        raise RailViolation('PMax asset-group remove result identity mismatch')
    status = pmax_asset_group_asset_removed_state(check)
    if status not in {None, 'REMOVED'}:
        raise RailViolation('PMax target connection remains active after removal')
    cid = pmax_id(check['customer_id'])
    target = check['target_link']
    selected = _pmax_requested_asset_proofs(
        cid, [{'asset': target['asset'], 'field_type': check['field_type']}])
    if selected != [check['selected_asset']]:
        raise RailViolation('PMax selected bare asset was deleted or changed after link removal')
    fresh = pmax_asset_group_asset_state(cid, check['asset_group'].rsplit('/', 1)[1])
    if fresh['target'] != check['target']:
        raise RailViolation('PMax asset group changed after connection removal')
    if fresh['parent_proof'] != check['parent_proof']:
        raise RailViolation('PMax parent or inherited branding changed after connection removal')
    if fresh['existing_assets'] != check['remaining_assets']:
        raise RailViolation('PMax remaining asset-group link union does not reconcile exactly')
    return True


def verify_created_results(checks, result):
    """Resolve ordered provider identities before any read. Failure never retries a write."""
    from .rails import RailViolation
    if any(isinstance(check, dict) and 'shared_negative_attach' in check for check in checks):
        return verify_shared_negative_attach_results(checks, result)
    if any(isinstance(check, dict) and 'shared_negative_add' in check for check in checks):
        return verify_shared_negative_add_results(checks, result)
    if any(isinstance(check, dict) and 'shared_negative_create' in check for check in checks):
        return verify_shared_negative_create_results(checks, result)
    if any(isinstance(check, dict) and 'demand_gen_ad' in check for check in checks):
        return verify_demand_gen_ad_results(checks, result)
    if any(isinstance(check, dict) and check.get('demand_gen') for check in checks):
        return verify_demand_gen_results(checks, result)
    if any(isinstance(check, dict) and check.get('pmax_asset_group_assets') for check in checks):
        return verify_pmax_asset_group_asset_add_result(checks, result)
    if any(isinstance(check, dict) and check.get('pmax_asset_group_update') for check in checks):
        return verify_pmax_asset_group_update_result(checks, result)
    if any(isinstance(check, dict) and check.get('pmax') for check in checks):
        return verify_pmax_results(checks, result)
    if not isinstance(result, dict) or result.get('validate_only'):
        raise RailViolation('validation-only response cannot verify application')
    results = result.get('results')
    if not isinstance(results, list) or len(results) != len(checks):
        raise RailViolation('created result count does not reconcile')
    resolved, seen = {}, set()
    for index, check in enumerate(checks):
        entity, cid = check['entity_type'], numeric_id(check['customer_id'])
        if check['result_index'] != index or entity not in _CREATED_KINDS:
            raise RailViolation('invalid result-dependent verification descriptor')
        entry = results[index]
        if not isinstance(entry, dict) or entry.get('type') != entity + '_result':
            raise RailViolation('created result type mismatch')
        rn = entry.get('resource_name')
        _validate_resource(rn, _CREATED_KINDS[entity], cid)
        if check.get('conversion_primary_status') and rn != check['expected']['resource_name']:
            raise RailViolation('conversion update result identity mismatch')
        if rn in seen:
            raise RailViolation('duplicate created result identity')
        seen.add(rn)
        resolved[index] = rn
    # Relationships are checked before any read, including criterion compound IDs.
    for check in checks:
        entity = check['entity_type']
        if entity == 'ad_group_ad':
            parent = check['expected']['ad_group']
            _validate_resource(parent, 'adGroups', check['customer_id'])
            if resolved[check['result_index']].rsplit('/', 1)[1].split('~')[0] != parent.rsplit('/', 1)[1]:
                raise RailViolation('created ad belongs to unexpected ad group')
        if entity == 'campaign_criterion':
            campaign_rn = (check['expected']['campaign'] if check.get('audience_targeting')
                           else resolved[check['campaign_result_index']])
            if resolved[check['result_index']].rsplit('/', 1)[1].split('~')[0] != campaign_rn.rsplit('/', 1)[1]:
                raise RailViolation('created criterion belongs to unexpected campaign')
        if entity in {'campaign_asset', 'ad_group_asset'}:
            asset_rn = resolved[check['asset_result_index']]
            parts = resolved[check['result_index']].rsplit('/', 1)[1].split('~')
            parent = check['expected']['campaign' if entity == 'campaign_asset' else 'ad_group']
            field_number = {'SITELINK': '13', 'CALLOUT': '11',
                            'STRUCTURED_SNIPPET': '12'}.get(check['expected'].get('field_type'))
            if (parts[0] != parent.rsplit('/', 1)[1] or parts[1] != asset_rn.rsplit('/', 1)[1]
                    or parts[2] != field_number):
                raise RailViolation('created asset association identity mismatch')
    for check in checks:
        entity, cid = check['entity_type'], check['customer_id']
        if check.get('conversion_primary_status'):
            verify_conversion_primary_status(check, resolved[check['result_index']])
            continue
        if check.get('conversion_creation'):
            verify_conversion_creation(check, resolved[check['result_index']])
            continue
        if check.get('audience_targeting'):
            verify_audience_targeting(check, resolved[check['result_index']])
            continue
        if check.get('portfolio_creation'):
            state = created_resource_state(cid, entity, resolved[check['result_index']])
            expected = check['expected']
            scheme = 'target_cpa' if 'target_cpa' in expected else 'target_roas'
            expected_type = 'TARGET_CPA' if scheme == 'target_cpa' else 'TARGET_ROAS'
            currencies = [state.get(key) for key in ('currency_code', 'effective_currency_code')
                          if state.get(key) not in (None, '')]
            if (state.get('name') != expected['name'] or state.get('type_') != expected_type
                    or state.get('status') != 'ENABLED'
                    or not _created_match(state.get(scheme), expected[scheme])
                    or not _created_match(state.get('non_removed_campaign_count'), 0)
                    or any(currency != check['currency_code'] for currency in currencies)):
                raise RailViolation('created portfolio strategy settings or attachment count mismatch')
            continue
        state = (created_resource_state(cid, entity, resolved[check['result_index']], image=True)
                 if check.get('image') else
                 created_resource_state(cid, entity, resolved[check['result_index']], text=True)
                 if check.get('text') else
                 created_resource_state(cid, entity, resolved[check['result_index']]))
        expected = dict(check['expected'])
        if entity == 'user_list':
            verify_audience_state(state, expected, cid)
            continue
        if entity == 'ad_group_ad':
            if (state.get('ad_group') != expected['ad_group'] or state.get('status') != 'PAUSED'
                    or _rsa_content(state.get('ad')) != _rsa_content(dict(expected['ad'], type_='RESPONSIVE_SEARCH_AD'))):
                raise RailViolation('created responsive search ad does not match requested content')
            continue
        if entity == 'asset':
            if check.get('text'):
                saved = state.get('text_asset')
                if (state.get('type_') != 'TEXT' or not isinstance(saved, dict)
                        or type(saved.get('text')) is not str
                        or saved['text'] != expected['text_asset']['text']):
                    raise RailViolation('saved text asset identity or exact content mismatch')
                continue
            if check.get('image'):
                image = state.get('image_asset')
                if not isinstance(image, dict) or type(state.get('name')) is not str or not state['name']:
                    raise RailViolation('image metadata unreadable')
                image = dict(image, mime_type=_enum_name('MimeTypeEnum', image.get('mime_type')))
                if state.get('type_') != 'IMAGE' or not _created_match(image, expected['image_asset']):
                    raise RailViolation('saved image identity or metadata mismatch')
                continue
            if 'sitelink_asset' in expected:
                matches = state.get('type_') == 'SITELINK' and sitelink_content(state) == sitelink_content(expected)
            elif 'structured_snippet_asset' in expected:
                matches = (state.get('type_') == 'STRUCTURED_SNIPPET'
                           and structured_snippet_content(state) == structured_snippet_content(expected))
            else:
                matches = state.get('type_') == 'CALLOUT' and callout_content(state) == callout_content(expected)
            if not matches:
                raise RailViolation('created asset does not match requested content')
            continue
        if entity in {'campaign_asset', 'ad_group_asset'}:
            expected['asset'] = resolved[check['asset_result_index']]
            if not _created_match(state, expected):
                raise RailViolation('created asset association does not match requested target/status')
            continue
        if entity == 'campaign':
            expected['campaign_budget'] = resolved[check['budget_result_index']]
            expected['bidding_strategy_type'] = check['strategy']
            if check['strategy_parameters']:
                expected[check['strategy'].lower()] = check['strategy_parameters']
            elif check['strategy'] in {'MAXIMIZE_CONVERSIONS', 'MAXIMIZE_CONVERSION_VALUE'}:
                # v25 omits an absent strategy message; an empty message serializes its
                # unset target as 0 (a string for int64 micros, a float for ROAS).
                field = ('target_cpa_micros' if check['strategy'] == 'MAXIMIZE_CONVERSIONS'
                         else 'target_roas')
                parameters = state.get(check['strategy'].lower(), {})
                if (not isinstance(parameters, dict)
                        or not _created_match(parameters.get(field, 0), 0)):
                    raise RailViolation('created campaign has an unexpected optional target')
        if entity == 'campaign_criterion':
            expected['campaign'] = resolved[check['campaign_result_index']]
        if not _created_match(state, expected):
            raise RailViolation('created resource settings did not match requested values')



def removed_association_state(check):
    """Complete exact-resource read including REMOVED rows; absence is a valid tombstone."""
    from .rails import RailViolation
    entity, cid, rn = check['entity_type'], numeric_id(check['customer_id']), check['resource_name']
    kind = 'campaignAssets' if entity == 'campaign_asset' else 'adGroupAssets'
    _validate_resource(rn, kind, cid)
    target = 'campaign' if entity == 'campaign_asset' else 'ad_group'
    fields = ('resource_name', target, 'asset', 'field_type', 'status')
    rows = gaql_all('SELECT ' + ', '.join(entity + '.' + field for field in fields)
                    + f" FROM {entity} WHERE {entity}.resource_name = '{rn}'"
                    + f" AND {entity}.status IN ('ENABLED', 'PAUSED', 'REMOVED')", cid)
    if not rows:
        return None
    if len(rows) != 1:
        raise RailViolation('removed association readback is ambiguous')
    row = _creation_row(rows[0], entity)
    status = _enum_name('AssetLinkStatusEnum', row.get('status'))
    field_type = _enum_name('AssetFieldTypeEnum', row.get('field_type'))
    parts = rn.rsplit('/', 1)[1].split('~')
    field_number = {'CALLOUT': '11', 'STRUCTURED_SNIPPET': '12', 'SITELINK': '13'}[check['field_type']]
    if (row.get('resource_name') != rn or row.get(target) != check['target_resource_name']
            or row.get('asset') != asset_path(cid, check['asset_id'])
            or parts != [check['target_id'], check['asset_id'], field_number]
            or field_type != check['field_type'] or status not in {'ENABLED', 'PAUSED', 'REMOVED'}):
        raise RailViolation('removed association readback identity, type, or status mismatch')
    return status


def _removal_one(rows, entity, message):
    from .rails import RailViolation
    if len(rows) != 1:
        raise RailViolation(message)
    return dict(_creation_row(rows[0], entity))


def _removal_enum(item, field, enum):
    item[field] = _enum_name(enum, item.get(field))
    return item


def _removal_inventory(cid, query, entity, kind, parent_field=None, parent=None,
                       enums=None):
    """Read one complete nonremoved child population and validate every identity/link."""
    from .rails import RailViolation
    rows, result, seen = gaql_all(query, cid), [], set()
    for raw in rows:
        item = dict(_creation_row(raw, entity))
        rn = item.get('resource_name')
        _validate_resource(rn, kind, cid)
        if rn in seen or (parent_field and item.get(parent_field) != parent):
            raise RailViolation('removal child population ownership/linkage is unreadable or duplicate')
        seen.add(rn)
        for field, enum in (enums or {}).items():
            raw_value = item.get(field, item.get('type') if field == 'type_' else None)
            item[field] = _inventory_enum(enum, raw_value)
        parts = rn.rsplit('/', 1)[1].split('~')
        linked = True
        if entity == 'ad_group':
            linked = str(item.get('id')) == parts[0]
        elif entity == 'ad_group_ad':
            ad = item.get('ad')
            if not isinstance(ad, dict):
                raise RailViolation('removal child ad type is unreadable')
            ad = dict(ad)
            ad['type_'] = _inventory_enum('AdTypeEnum', ad.get('type_', ad.get('type')))
            item['ad'] = ad
            linked = (str(ad.get('id')) == parts[1]
                      and item.get('ad_group', '').rsplit('/', 1)[-1] == parts[0])
        elif entity in {'campaign_criterion', 'ad_group_criterion'}:
            parent_key = 'campaign' if entity == 'campaign_criterion' else 'ad_group'
            linked = item.get(parent_key, '').rsplit('/', 1)[-1] == parts[0]
        elif entity in {'campaign_asset', 'ad_group_asset'}:
            parent_key = 'campaign' if entity == 'campaign_asset' else 'ad_group'
            from google.ads.googleads.v25.enums.types.asset_field_type import (
                AssetFieldTypeEnum,
            )
            try:
                field_number = str(AssetFieldTypeEnum.AssetFieldType[item['field_type']].value)
            except (KeyError, TypeError):
                field_number = None
            _validate_resource(item.get('asset'), 'assets', cid)
            linked = (item.get(parent_key, '').rsplit('/', 1)[-1] == parts[0]
                      and item.get('asset') == asset_path(cid, parts[1])
                      and field_number == parts[2])
        if not linked:
            raise RailViolation('removal child population composite identity does not reconcile')
        result.append(item)
    return sorted(result, key=lambda item: item['resource_name'])


def removal_entity_state(customer_id, entity_type, entity_id, ad_group_id=None):
    """Complete state used to authorize one paused standard-Search entity removal."""
    from .rails import RailViolation
    cid, eid = numeric_id(customer_id), numeric_id(entity_id)
    account = _creation_account(cid)
    campaign_fields = ('resource_name, id, name, status, advertising_channel_type, '
                       'advertising_channel_sub_type, campaign_budget')

    def campaign_state(campaign_id):
        rows = gaql_all('SELECT ' + ', '.join('campaign.' + f for f in campaign_fields.split(', '))
                        + f' FROM campaign WHERE campaign.id = {numeric_id(campaign_id)}', cid)
        item = _removal_one(rows, 'campaign', 'removal campaign is missing or ambiguous')
        for field, enum in [('status', 'CampaignStatusEnum'),
                            ('advertising_channel_type', 'AdvertisingChannelTypeEnum'),
                            ('advertising_channel_sub_type', 'AdvertisingChannelSubTypeEnum')]:
            _removal_enum(item, field, enum)
        rn = campaign_path(cid, campaign_id)
        if (item.get('resource_name') != rn or str(item.get('id')) != numeric_id(campaign_id)
                or type(item.get('name')) is not str or not item['name'].strip()
                or item['status'] not in {'ENABLED', 'PAUSED'}):
            raise RailViolation('removal campaign identity/status is unreadable')
        if (item['advertising_channel_type'] != 'SEARCH'
                or item['advertising_channel_sub_type'] != 'UNSPECIFIED'):
            raise RailViolation('removal requires a standard Search campaign')
        _validate_resource(item.get('campaign_budget'), 'campaignBudgets', cid)
        return item

    def group_state(group_id):
        fields = 'resource_name, id, name, status, type, campaign'
        rows = gaql_all('SELECT ' + ', '.join('ad_group.' + f for f in fields.split(', '))
                        + f' FROM ad_group WHERE ad_group.id = {numeric_id(group_id)}', cid)
        item = _removal_one(rows, 'ad_group', 'removal ad group is missing or ambiguous')
        _removal_enum(item, 'status', 'AdGroupStatusEnum')
        item['type'] = _enum_name('AdGroupTypeEnum', item.get('type_', item.get('type')))
        if (item.get('resource_name') != ad_group_path(cid, group_id)
                or str(item.get('id')) != numeric_id(group_id)
                or type(item.get('name')) is not str or not item['name'].strip()
                or item['status'] not in {'ENABLED', 'PAUSED'}
                or item['type'] != 'SEARCH_STANDARD'):
            raise RailViolation('removal requires a readable nonremoved SEARCH_STANDARD ad group')
        _validate_resource(item.get('campaign'), 'campaigns', cid)
        return item

    group = None
    if entity_type == 'campaign':
        campaign = campaign_state(eid)
    elif entity_type == 'ad_group':
        group = group_state(eid)
        campaign = campaign_state(group['campaign'].rsplit('/', 1)[1])
    else:
        gid = numeric_id(ad_group_id)
        group = group_state(gid)
        campaign = campaign_state(group['campaign'].rsplit('/', 1)[1])
    if group is not None and group['campaign'] != campaign['resource_name']:
        raise RailViolation('removal parent chain does not reconcile')

    state = {'account': account, 'campaign': campaign}
    if group is not None:
        state['ad_group'] = group
    if entity_type == 'ad':
        fields = ('resource_name, ad_group, status, ad.id, ad.type, ad.final_urls, '
                  'ad.responsive_search_ad.headlines, ad.responsive_search_ad.descriptions, '
                  'ad.responsive_search_ad.path1, ad.responsive_search_ad.path2')
        rows = gaql_all('SELECT ' + ', '.join('ad_group_ad.' + f if not f.startswith('ad.')
                                              else 'ad_group_ad.' + f
                                              for f in fields.split(', '))
                        + f' FROM ad_group_ad WHERE ad_group_ad.ad.id = {eid} '
                          f"AND ad_group_ad.ad_group = '{group['resource_name']}'", cid)
        item = _removal_one(rows, 'ad_group_ad', 'removal ad is missing or ambiguous')
        item['status'] = _enum_name('AdGroupAdStatusEnum', item.get('status'))
        ad = item.get('ad')
        if not isinstance(ad, dict):
            raise RailViolation('removal ad content is unreadable')
        ad_type = _enum_name('AdTypeEnum', ad.get('type_', ad.get('type')))
        rn = ad_group_ad_path(cid, gid, eid)
        if (item.get('resource_name') != rn or item.get('ad_group') != group['resource_name']
                or str(ad.get('id')) != eid or item['status'] == 'REMOVED'
                or ad_type != 'RESPONSIVE_SEARCH_AD'
                or not isinstance(ad.get('final_urls'), list)
                or not isinstance(ad.get('responsive_search_ad'), dict)
                or not isinstance(ad['responsive_search_ad'].get('headlines'), list)
                or not isinstance(ad['responsive_search_ad'].get('descriptions'), list)
                or type(ad['responsive_search_ad'].get('path1', '')) is not str
                or type(ad['responsive_search_ad'].get('path2', '')) is not str):
            raise RailViolation('removal requires a readable responsive search ad with exact parent')
        state['ad'] = {'resource_name': rn, 'id': eid, 'ad_group': item['ad_group'],
                       'status': item['status'], 'type': ad_type,
                       'final_urls': ad.get('final_urls', []),
                       'responsive_search_ad': ad['responsive_search_ad']}
        return state

    campaign_rn = campaign['resource_name']
    group_filter = (f"ad_group.resource_name = '{group['resource_name']}'" if group
                    else f"ad_group.campaign = '{campaign_rn}'")
    state['ads'] = _removal_inventory(
        cid, 'SELECT ad_group_ad.resource_name, ad_group_ad.ad_group, ad_group_ad.status, '
             'ad_group_ad.ad.id, ad_group_ad.ad.type FROM ad_group_ad WHERE '
             + group_filter + " AND ad_group_ad.status != 'REMOVED'", 'ad_group_ad',
        'adGroupAds', 'ad_group', group['resource_name'] if group else None,
        {'status': 'AdGroupAdStatusEnum'}) if group else _removal_inventory(
            cid, 'SELECT ad_group_ad.resource_name, ad_group_ad.ad_group, ad_group_ad.status, '
                 'ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group.campaign FROM ad_group_ad '
                 f"WHERE ad_group.campaign = '{campaign_rn}' AND ad_group_ad.status != 'REMOVED'",
            'ad_group_ad', 'adGroupAds', enums={'status': 'AdGroupAdStatusEnum'})
    if entity_type == 'campaign':
        state['ad_groups'] = _removal_inventory(
            cid, 'SELECT ad_group.resource_name, ad_group.id, ad_group.name, ad_group.status, '
                 f"ad_group.type, ad_group.campaign FROM ad_group WHERE ad_group.campaign = '{campaign_rn}' "
                 "AND ad_group.status != 'REMOVED'", 'ad_group', 'adGroups', 'campaign',
            campaign_rn, {'status': 'AdGroupStatusEnum', 'type_': 'AdGroupTypeEnum'})
    target_group = group['resource_name'] if group else None
    state['campaign_criteria'] = ([] if group else _removal_inventory(
        cid, 'SELECT campaign_criterion.resource_name, campaign_criterion.campaign, '
             f"campaign_criterion.status FROM campaign_criterion WHERE campaign_criterion.campaign = '{campaign_rn}' "
             "AND campaign_criterion.status != 'REMOVED'", 'campaign_criterion',
        'campaignCriteria', 'campaign', campaign_rn, {'status': 'CampaignCriterionStatusEnum'}))
    state['ad_group_criteria'] = _removal_inventory(
        cid, 'SELECT ad_group_criterion.resource_name, ad_group_criterion.ad_group, '
             f"ad_group_criterion.status FROM ad_group_criterion WHERE {group_filter} "
             "AND ad_group_criterion.status != 'REMOVED'", 'ad_group_criterion',
        'adGroupCriteria', 'ad_group', target_group,
        {'status': 'AdGroupCriterionStatusEnum'}) if group else _removal_inventory(
            cid, 'SELECT ad_group_criterion.resource_name, ad_group_criterion.ad_group, '
                 f"ad_group_criterion.status, ad_group.campaign FROM ad_group_criterion WHERE ad_group.campaign = '{campaign_rn}' "
                 "AND ad_group_criterion.status != 'REMOVED'", 'ad_group_criterion',
            'adGroupCriteria', enums={'status': 'AdGroupCriterionStatusEnum'})
    state['campaign_assets'] = ([] if group else _removal_inventory(
        cid, 'SELECT campaign_asset.resource_name, campaign_asset.campaign, campaign_asset.asset, '
             f"campaign_asset.field_type, campaign_asset.status FROM campaign_asset WHERE campaign_asset.campaign = '{campaign_rn}' "
             "AND campaign_asset.status != 'REMOVED'", 'campaign_asset', 'campaignAssets',
        'campaign', campaign_rn, {'status': 'AssetLinkStatusEnum',
                                  'field_type': 'AssetFieldTypeEnum'}))
    state['ad_group_assets'] = _removal_inventory(
        cid, 'SELECT ad_group_asset.resource_name, ad_group_asset.ad_group, ad_group_asset.asset, '
             f"ad_group_asset.field_type, ad_group_asset.status FROM ad_group_asset WHERE {group_filter} "
             "AND ad_group_asset.status != 'REMOVED'", 'ad_group_asset', 'adGroupAssets',
        'ad_group', target_group, {'status': 'AssetLinkStatusEnum',
                                   'field_type': 'AssetFieldTypeEnum'}) if group else _removal_inventory(
            cid, 'SELECT ad_group_asset.resource_name, ad_group_asset.ad_group, ad_group_asset.asset, '
                 f"ad_group_asset.field_type, ad_group_asset.status, ad_group.campaign FROM ad_group_asset WHERE ad_group.campaign = '{campaign_rn}' "
                 "AND ad_group_asset.status != 'REMOVED'", 'ad_group_asset', 'adGroupAssets',
            enums={'status': 'AssetLinkStatusEnum', 'field_type': 'AssetFieldTypeEnum'})
    if not group:
        asset_groups = gaql_all('SELECT asset_group.resource_name, asset_group.campaign, asset_group.status '
                                f"FROM asset_group WHERE asset_group.campaign = '{campaign_rn}' "
                                "AND asset_group.status != 'REMOVED'", cid)
        if asset_groups:
            raise RailViolation('standard Search campaign has inconsistent asset-group population')
        budget_rn = campaign['campaign_budget']
        budget_rows = gaql_all('SELECT campaign_budget.resource_name, campaign_budget.amount_micros, '
                               'campaign_budget.explicitly_shared, campaign_budget.reference_count '
                               f"FROM campaign_budget WHERE campaign_budget.resource_name = '{budget_rn}'", cid)
        budget = _removal_one(budget_rows, 'campaign_budget', 'campaign budget state is unreadable')
        _validate_resource(budget.get('resource_name'), 'campaignBudgets', cid)
        if (budget['resource_name'] != budget_rn or type(budget.get('explicitly_shared')) is not bool
                or not str(budget.get('amount_micros', '')).isdigit()
                or not str(budget.get('reference_count', '')).isdigit()):
            raise RailViolation('campaign budget identity, amount, sharing or references unreadable')
        state['budget'], state['asset_groups'] = budget, []
        groups = {item['resource_name'] for item in state['ad_groups']}
        populations = (state['ads'], state['ad_group_criteria'], state['ad_group_assets'])
        if any(item.get('ad_group') not in groups for population in populations
               for item in population):
            raise RailViolation('campaign child population does not reconcile to its ad groups')
    return state


def removed_entity_state(check):
    """Exact complete post-write read; None is an explicit successful tombstone."""
    from .rails import RailViolation
    entity, cid, rn = check['entity_type'], numeric_id(check['customer_id']), check['resource_name']
    config = {'campaign': ('campaign', 'campaigns', 'CampaignStatusEnum', None),
              'ad_group': ('ad_group', 'adGroups', 'AdGroupStatusEnum', 'campaign'),
              'ad_group_ad': ('ad_group_ad', 'adGroupAds', 'AdGroupAdStatusEnum', 'ad_group')}
    row_entity, kind, enum, parent_field = config[entity]
    _validate_resource(rn, kind, cid)
    fields = ['resource_name', 'status'] + ([parent_field] if parent_field else [])
    rows = gaql_all('SELECT ' + ', '.join(row_entity + '.' + field for field in fields)
                    + f" FROM {row_entity} WHERE {row_entity}.resource_name = '{rn}' "
                      f"AND {row_entity}.status IN ('ENABLED', 'PAUSED', 'REMOVED')", cid)
    if not rows:
        return None
    item = _removal_one(rows, row_entity, 'removed entity readback is ambiguous')
    status = _enum_name(enum, item.get('status'))
    if item.get('resource_name') != rn or (parent_field and item.get(parent_field)
                                           != check['parent_resource_name']):
        raise RailViolation('removed entity readback identity or parent mismatch')
    return status


def verify_removed_result(checks, result):
    """Verify the one ordered removal result before issuing its saved-state read."""
    from .rails import RailViolation
    if (not isinstance(result, dict) or result.get('validate_only')
            or not isinstance(result.get('results'), list) or len(result['results']) != 1
            or len(checks) != 1):
        raise RailViolation('removed result count does not reconcile')
    check, entry = checks[0], result['results'][0]
    if (not isinstance(entry, dict) or entry.get('type') != check['entity_type'] + '_result'
            or entry.get('resource_name') != check['resource_name']):
        raise RailViolation('removed result type or identity mismatch')
    status = (removed_entity_state(check) if check['entity_type'] in
              {'campaign', 'ad_group', 'ad_group_ad'} else removed_association_state(check))
    if status not in {None, 'REMOVED'}:
        raise RailViolation('removed resource remains active after removal')


def _creation_row(row, entity):
    from .rails import RailViolation
    if not isinstance(row, dict) or not isinstance(row.get(entity), dict):
        raise RailViolation('creation state row unreadable')
    return row[entity]


def rsa_text(value, limit):
    import unicodedata

    from .rails import RailViolation
    if (type(value) is not str or not value or value != value.strip()
            or any(unicodedata.category(c).startswith('C') or c in '{}' for c in value)
            or sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in value) > limit):
        raise RailViolation('ad text must be nonblank plain text within its character limit, without surrounding whitespace')
    return value


def _rsa_hostname(value):
    import ipaddress
    import unicodedata

    from .rails import RailViolation
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(c.isspace() or unicodedata.category(c).startswith('C') for c in value)):
        raise RailViolation('invalid hostname')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        hostname = value.removesuffix('.').encode('idna').decode('ascii').lower()
    except UnicodeError as exc:
        raise RailViolation('invalid hostname') from exc
    if (len(hostname) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                       for label in hostname.split('.'))):
        raise RailViolation('invalid hostname')
    return hostname


def rsa_url(value):
    import unicodedata
    from urllib.parse import urlsplit

    from . import settings
    from .rails import RailViolation, check_content
    if (type(value) is not str or not value or len(value) > 2048
            or any(c.isspace() or unicodedata.category(c).startswith('C') or c in '{}\\' for c in value)):
        raise RailViolation('final URL must be an absolute HTTP(S) URL within 2048 characters')
    try:
        parsed = urlsplit(value)
        host = _rsa_hostname(parsed.hostname)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.netloc.endswith(':') or parsed.port == 0):
            raise ValueError('invalid URL authority')
    except ValueError as exc:
        raise RailViolation('invalid final URL') from exc
    domain = settings.advertiser_domain()
    if domain != '':
        allowed = _rsa_hostname(domain)
        if host != allowed and not host.endswith('.' + allowed):
            raise RailViolation('final URL hostname is outside advertiser_domain')
    check_content([value])
    return value


def _validate_rsa_create(values, cid):
    from .rails import RailViolation, check_content
    if (not isinstance(values, dict) or set(values) != {'ad_group', 'status', 'ad'}
            or values['status'] != 'PAUSED'):
        raise RailViolation('RSA create requires ad group, ad and explicit PAUSED status')
    _validate_resource(values['ad_group'], 'adGroups', cid)
    ad = values['ad']
    if not isinstance(ad, dict) or set(ad) != {'final_urls', 'responsive_search_ad'}:
        raise RailViolation('RSA ad permits only final_urls and responsive_search_ad')
    urls, rsa = ad['final_urls'], ad['responsive_search_ad']
    if type(urls) is not list or len(urls) != 1:
        raise RailViolation('RSA requires exactly one final URL')
    rsa_url(urls[0])
    if (not isinstance(rsa, dict) or not {'headlines', 'descriptions'} <= set(rsa)
            or not set(rsa) <= {'headlines', 'descriptions', 'path1', 'path2'}):
        raise RailViolation('unsupported RSA fields')
    texts = []
    for key, minimum, maximum, limit in [('headlines', 3, 15, 30), ('descriptions', 2, 4, 90)]:
        assets = rsa[key]
        if type(assets) is not list or not minimum <= len(assets) <= maximum:
            raise RailViolation('invalid RSA asset count')
        for asset in assets:
            if not isinstance(asset, dict) or set(asset) != {'text'}:
                raise RailViolation('RSA assets permit only text')
            rsa_text(asset['text'], limit)
        values_text = [a['text'] for a in assets]
        if len(set(values_text)) != len(values_text):
            raise RailViolation('RSA text must be unique within each asset list')
        texts.extend(values_text)
    if 'path2' in rsa and 'path1' not in rsa:
        raise RailViolation('path2 requires path1')
    for key in ('path1', 'path2'):
        if key in rsa:
            path = rsa_text(rsa[key], 15)
            if '/' in path or '\\' in path:
                raise RailViolation('display paths cannot contain slashes')
            texts.append(path)
    check_content(texts)


def sitelink_content(value):
    """Canonical submitted/read sitelink content, excluding provider metadata."""
    from .rails import RailViolation
    if not isinstance(value, dict):
        raise RailViolation('unreadable sitelink content')
    urls = value.get('final_urls')
    sitelink = value.get('sitelink_asset')
    if (type(urls) is not list or len(urls) != 1 or not isinstance(sitelink, dict)
            or value.get('final_mobile_urls') not in (None, [])
            or value.get('tracking_url_template') not in (None, '')
            or value.get('final_url_suffix') not in (None, '')
            or value.get('url_custom_parameters') not in (None, [])):
        raise RailViolation('unreadable sitelink URL or fields')
    allowed = {'link_text', 'description1', 'description2', 'start_date', 'end_date',
               'ad_schedule_targets'}
    if (not {'link_text'} <= set(sitelink) <= allowed
            or any(sitelink.get(key) not in ('', None) for key in ('start_date', 'end_date'))
            or sitelink.get('ad_schedule_targets') not in (None, [])):
        raise RailViolation('unreadable sitelink fields')
    description1 = sitelink.get('description1') or None
    description2 = sitelink.get('description2') or None
    if (description1 is None) != (description2 is None):
        raise RailViolation('unreadable sitelink fields')
    return (sitelink['link_text'], urls[0], description1, description2)


def existing_sitelink_content(value):
    """Snapshot valid broader provider inventory; mark only tool-equivalent rows plain."""
    from .rails import RailViolation
    if not isinstance(value, dict) or not isinstance(value.get('sitelink_asset'), dict):
        raise RailViolation('unreadable existing sitelink content')
    sl, urls = value['sitelink_asset'], value.get('final_urls')
    if (type(urls) is not list or not urls or any(type(url) is not str or not url for url in urls)
            or type(sl.get('link_text')) is not str or not sl['link_text']):
        raise RailViolation('unreadable existing sitelink content')
    description1 = sl.get('description1') or None
    description2 = sl.get('description2') or None
    if (description1 is None) != (description2 is None):
        raise RailViolation('unreadable existing sitelink descriptions')
    extras = {
        'final_urls': urls[1:],
        'final_mobile_urls': value.get('final_mobile_urls', []),
        'tracking_url_template': value.get('tracking_url_template', ''),
        'final_url_suffix': value.get('final_url_suffix', ''),
        'url_custom_parameters': value.get('url_custom_parameters', []),
        'start_date': sl.get('start_date', ''),
        'end_date': sl.get('end_date', ''),
        'ad_schedule_targets': sl.get('ad_schedule_targets', []),
    }
    plain = all(extra in ('', [], None) for extra in extras.values())
    return {'base': (sl['link_text'], urls[0], description1, description2),
            'plain': plain, 'extras': extras}


def callout_content(value):
    """Canonical plain callout content, excluding provider metadata."""
    from .rails import RailViolation
    if not isinstance(value, dict) or not isinstance(value.get('callout_asset'), dict):
        raise RailViolation('unreadable callout content')
    callout = value['callout_asset']
    allowed = {'callout_text', 'start_date', 'end_date', 'ad_schedule_targets'}
    if (set(callout) - allowed or type(callout.get('callout_text')) is not str
            or not callout['callout_text']
            or any(callout.get(key) not in ('', None) for key in ('start_date', 'end_date'))
            or callout.get('ad_schedule_targets') not in (None, [])
            or value.get('final_urls') not in (None, [])
            or value.get('final_mobile_urls') not in (None, [])
            or value.get('tracking_url_template') not in (None, '')
            or value.get('final_url_suffix') not in (None, '')
            or value.get('url_custom_parameters') not in (None, [])
            or value.get('sitelink_asset') not in (None, {})):
        raise RailViolation('unreadable callout fields')
    return callout['callout_text']


def existing_callout_content(value):
    """Snapshot valid broader provider inventory; mark only tool-equivalent rows plain."""
    from .rails import RailViolation
    if not isinstance(value, dict) or not isinstance(value.get('callout_asset'), dict):
        raise RailViolation('unreadable existing callout content')
    callout = value['callout_asset']
    text = callout.get('callout_text')
    if type(text) is not str or not text:
        raise RailViolation('unreadable existing callout content')
    extras = {
        'final_urls': value.get('final_urls', []),
        'final_mobile_urls': value.get('final_mobile_urls', []),
        'tracking_url_template': value.get('tracking_url_template', ''),
        'final_url_suffix': value.get('final_url_suffix', ''),
        'url_custom_parameters': value.get('url_custom_parameters', []),
        'start_date': callout.get('start_date', ''),
        'end_date': callout.get('end_date', ''),
        'ad_schedule_targets': callout.get('ad_schedule_targets', []),
    }
    return {'base': text, 'plain': all(extra in ('', [], None) for extra in extras.values()),
            'extras': extras}


def structured_snippet_content(value):
    """Canonical plain structured-snippet content, excluding provider metadata."""
    from .rails import RailViolation
    if not isinstance(value, dict) or not isinstance(value.get('structured_snippet_asset'), dict):
        raise RailViolation('unreadable structured snippet content')
    snippet = value['structured_snippet_asset']
    header, values = snippet.get('header'), snippet.get('values')
    if (set(snippet) != {'header', 'values'} or type(header) is not str or not header
            or type(values) is not list or not values
            or any(type(item) is not str or not item for item in values)
            or value.get('final_urls') not in (None, [])
            or value.get('final_mobile_urls') not in (None, [])
            or value.get('tracking_url_template') not in (None, '')
            or value.get('final_url_suffix') not in (None, '')
            or value.get('url_custom_parameters') not in (None, [])
            or value.get('sitelink_asset') not in (None, {})
            or value.get('callout_asset') not in (None, {})):
        raise RailViolation('unreadable structured snippet fields')
    return header, tuple(values)


def existing_structured_snippet_content(value):
    """Snapshot broader/localized inventory; mark only tool-equivalent rows plain."""
    from .rails import RailViolation
    if not isinstance(value, dict) or not isinstance(value.get('structured_snippet_asset'), dict):
        raise RailViolation('unreadable existing structured snippet content')
    snippet = value['structured_snippet_asset']
    header, values = snippet.get('header'), snippet.get('values')
    if (type(header) is not str or not header or type(values) is not list or not values
            or any(type(item) is not str or not item for item in values)):
        raise RailViolation('unreadable existing structured snippet content')
    extras = {
        'final_urls': value.get('final_urls', []),
        'final_mobile_urls': value.get('final_mobile_urls', []),
        'tracking_url_template': value.get('tracking_url_template', ''),
        'final_url_suffix': value.get('final_url_suffix', ''),
        'url_custom_parameters': value.get('url_custom_parameters', []),
        'alternate_content': any(value.get(key) not in (None, {})
                                 for key in ('sitelink_asset', 'callout_asset')),
    }
    return {'base': (header, tuple(values)),
            'plain': all(extra in ('', [], None, False) for extra in extras.values()),
            'extras': extras}


IMAGE_BYTE_CAP = 5_120_000
IMAGE_PIXEL_CAP = 25_000_000


def image_metadata(encoded):
    """Validate canonical in-memory bytes without changing pixels or global decoder safety."""
    from .rails import RailViolation
    if type(encoded) is not str or not encoded or len(encoded) > 4 * ((IMAGE_BYTE_CAP + 2) // 3):
        raise RailViolation('image requires standard base64 within the local byte cap', code='BAD_IMAGE')
    try:
        if ImageFile.LOAD_TRUNCATED_IMAGES:
            raise ValueError('strict image decoder required')
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > IMAGE_BYTE_CAP or base64.b64encode(raw).decode('ascii') != encoded:
            raise ValueError('noncanonical or oversized')
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                fmt, (width, height) = image.format, image.size
                if (fmt not in {'JPEG', 'PNG'} or getattr(image, 'n_frames', 1) != 1
                        or width <= 0 or height <= 0 or width * height > IMAGE_PIXEL_CAP):
                    raise ValueError('unsupported image')
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
    except (ValueError, TypeError, binascii.Error, OSError, SyntaxError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise RailViolation('invalid, corrupt, animated, unsupported or oversized JPEG/PNG image',
                            code='BAD_IMAGE') from None
    return {'format': fmt, 'width': width, 'height': height, 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'mime_type': 'IMAGE_JPEG' if fmt == 'JPEG' else 'IMAGE_PNG'}


def _validate_asset_create(entity, values, cid, context):
    from .rails import RailViolation, check_content
    if not isinstance(values, dict):
        raise RailViolation('sitelink create fields must be a dictionary')
    if entity == 'Asset' and 'text_asset' in values:
        if (set(values) != {'name', 'text_asset'} or type(values['text_asset']) is not dict
                or set(values['text_asset']) != {'text'}):
            raise RailViolation('text asset permits only name and text_asset.text')
        name = creation_name(values['name'])
        text = rsa_text(values['text_asset']['text'], 90)
        check_content([name, text])
        return
    if entity == 'Asset' and 'image_asset' in values:
        if set(values) != {'name', 'image_asset'} or type(values['image_asset']) is not dict \
                or set(values['image_asset']) != {'data', 'mime_type'}:
            raise RailViolation('image asset permits only name, data and MIME type')
        creation_name(values['name'])
        check_content([values['name']])
        metadata = image_metadata(values['image_asset']['data'])
        if values['image_asset']['mime_type'] != metadata['mime_type']:
            raise RailViolation('image MIME type does not match decoded format')
        return
    if entity == 'Asset':
        rn = values.get('resource_name')
        if not isinstance(rn, str):
            raise RailViolation('asset requires a temporary resource identity')
        kind = context.get(rn) if context is not None else None
        if kind == 'assets:CALLOUT':
            if set(values) != {'resource_name', 'callout_asset'} \
                    or not isinstance(values['callout_asset'], dict) \
                    or set(values['callout_asset']) != {'callout_text'}:
                raise RailViolation('callout asset permits only temporary identity and callout text')
            text = rsa_text(values['callout_asset']['callout_text'], 25)
            check_content([text])
            return
        if kind == 'assets:STRUCTURED_SNIPPET':
            if (set(values) != {'resource_name', 'structured_snippet_asset'}
                    or not isinstance(values['structured_snippet_asset'], dict)
                    or set(values['structured_snippet_asset']) != {'header', 'values'}):
                raise RailViolation('structured snippet asset permits only temporary identity, header and values')
            snippet = values['structured_snippet_asset']
            if type(snippet['header']) is not str \
                    or snippet['header'] not in STRUCTURED_SNIPPET_HEADERS \
                    or type(snippet['values']) is not list:
                raise RailViolation('invalid structured snippet asset fields')
            texts = [rsa_text(item, 25) for item in snippet['values']]
            if not 3 <= len(texts) <= 10:
                raise RailViolation('structured snippet requires 3..10 values')
            if len({text.casefold() for text in texts}) != len(texts):
                raise RailViolation('structured snippet values must be unique')
            check_content([snippet['header'], *texts])
            return
        if kind != 'assets:SITELINK':
            raise RailViolation('asset requires a validated temporary identity')
        if not {'resource_name', 'final_urls', 'sitelink_asset'} == set(values):
            raise RailViolation('sitelink asset permits only temporary identity, final URL and sitelink')
        urls, sl = values['final_urls'], values['sitelink_asset']
        if type(urls) is not list or len(urls) != 1 or not isinstance(sl, dict) \
                or not {'link_text'} <= set(sl) <= {'link_text', 'description1', 'description2'} \
                or ('description1' in sl) != ('description2' in sl):
            raise RailViolation('invalid plain sitelink asset fields')
        rsa_url(urls[0])
        texts = [rsa_text(sl['link_text'], 25)]
        if 'description1' in sl:
            texts.extend((rsa_text(sl['description1'], 35), rsa_text(sl['description2'], 35)))
        check_content(texts)
        return
    parent = 'campaign' if entity == 'CampaignAsset' else 'ad_group'
    if (set(values) != {parent, 'asset', 'field_type', 'status'}
            or type(values['field_type']) is not str
            or values['field_type'] not in {'SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET'}
            or values['status'] != 'PAUSED'):
        raise RailViolation('asset link requires target, matching field type and explicit PAUSED status')
    _validate_resource(values[parent], 'campaigns' if parent == 'campaign' else 'adGroups', cid)
    if not isinstance(values['asset'], str):
        raise RailViolation('asset link requires a temporary asset identity')
    _validate_resource(values['asset'].replace('/-', '/'), 'assets', cid)
    if context is None or context.get(values['asset']) != 'assets:' + values['field_type']:
        raise RailViolation('asset link kind does not match its temporary asset')


def _rsa_content(ad):
    """Relevant content only; provider output metadata does not affect equality."""
    from .rails import RailViolation
    if not isinstance(ad, dict) or _enum_name('AdTypeEnum', ad.get('type_')) != 'RESPONSIVE_SEARCH_AD':
        raise RailViolation('unreadable responsive search ad type')
    rsa, urls = ad.get('responsive_search_ad'), ad.get('final_urls')
    if not isinstance(rsa, dict) or type(urls) is not list or not urls or any(type(v) is not str or not v for v in urls):
        raise RailViolation('unreadable responsive search ad content')
    result = {'final_urls': urls}
    for key in ('headlines', 'descriptions'):
        assets = rsa.get(key)
        if type(assets) is not list or not assets:
            raise RailViolation('unreadable RSA assets')
        texts = []
        for asset in assets:
            if not isinstance(asset, dict) or type(asset.get('text')) is not str or not asset['text']:
                raise RailViolation('unreadable RSA asset text')
            pin = _enum_name('ServedAssetFieldTypeEnum', asset.get('pinned_field', 'UNSPECIFIED'))
            if pin not in {'UNSPECIFIED', 'HEADLINE_1', 'HEADLINE_2', 'HEADLINE_3', 'DESCRIPTION_1', 'DESCRIPTION_2'}:
                raise RailViolation('unreadable RSA pin')
            texts.append((asset['text'], pin))
        result[key] = sorted(texts)
    for key in ('path1', 'path2'):
        value = rsa.get(key, '')
        if type(value) is not str:
            raise RailViolation('unreadable display path')
        result[key] = value
    return result


def responsive_search_ad_state(cid, group_id):
    from .rails import RailViolation
    cid, group_id = numeric_id(cid), numeric_id(group_id)
    group_rn = ad_group_path(cid, group_id)
    rows = gaql_all('SELECT ad_group.resource_name, ad_group.status, ad_group.type, ad_group.campaign '
                    f'FROM ad_group WHERE ad_group.id = {group_id}', cid)
    if len(rows) != 1:
        raise RailViolation('ad group not readable uniquely')
    row = _creation_row(rows[0], 'ad_group')
    group = {key: row.get(key) for key in ('resource_name', 'status', 'type_', 'campaign')}
    group['status'] = _enum_name('AdGroupStatusEnum', group.get('status'))
    group['type_'] = _enum_name('AdGroupTypeEnum', group.get('type_'))
    if group.get('resource_name') != group_rn or group['status'] not in {'ENABLED', 'PAUSED'} or group['type_'] != 'SEARCH_STANDARD':
        raise RailViolation('parent must be a nonremoved standard Search ad group')
    campaign_rn = group.get('campaign')
    _validate_resource(campaign_rn, 'campaigns', cid)
    campaign_id = campaign_rn.rsplit('/', 1)[1]
    fields = ('resource_name', 'status', 'advertising_channel_type', 'advertising_channel_sub_type')
    rows = gaql_all('SELECT ' + ', '.join('campaign.' + f for f in fields)
                    + f' FROM campaign WHERE campaign.id = {campaign_id}', cid)
    if len(rows) != 1:
        raise RailViolation('campaign not readable uniquely')
    row = _creation_row(rows[0], 'campaign')
    campaign = {key: row.get(key) for key in fields}
    for field, enum in [('status', 'CampaignStatusEnum'), ('advertising_channel_type', 'AdvertisingChannelTypeEnum'),
                        ('advertising_channel_sub_type', 'AdvertisingChannelSubTypeEnum')]:
        campaign[field] = _enum_name(enum, campaign.get(field))
    if (campaign.get('resource_name') != campaign_rn or campaign['status'] not in {'ENABLED', 'PAUSED'}
            or campaign['advertising_channel_type'] != 'SEARCH' or campaign['advertising_channel_sub_type'] != 'UNSPECIFIED'):
        raise RailViolation('campaign must be nonremoved standard Search')
    rows = gaql_all('SELECT ' + ', '.join('ad_group_ad.' + f for f in ['resource_name', *_CREATED_READ_FIELDS['ad_group_ad']])
                    + f" FROM ad_group_ad WHERE ad_group.id = {group_id} AND ad_group_ad.status != 'REMOVED'"
                    + " AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'", cid)
    population, seen = [], set()
    for row in rows:
        state = _creation_row(row, 'ad_group_ad')
        rn = state.get('resource_name')
        _validate_resource(rn, 'adGroupAds', cid)
        status = _enum_name('AdGroupAdStatusEnum', state.get('status'))
        if (rn in seen or state.get('ad_group') != group_rn or rn.rsplit('/', 1)[1].split('~')[0] != group_id
                or status not in {'ENABLED', 'PAUSED'}):
            raise RailViolation('RSA population identity/status mismatch')
        seen.add(rn)
        population.append({'resource_name': rn, 'status': status, 'content': _rsa_content(state.get('ad'))})
    return {'account': _creation_account(cid), 'parent': group, 'campaign': campaign,
            'ads': sorted(population, key=lambda r: r['resource_name'])}


def _asset_target_parent(cid, target_type, target_id):
    """Resolve the parent shared by the two closed campaign/ad-group asset draft families."""
    from .rails import RailViolation
    cid, target_id = numeric_id(cid), numeric_id(target_id)
    if target_type == 'campaign':
        parent = creation_parent(cid, target_id)
        campaign = None
    elif target_type == 'ad_group':
        group_rn = ad_group_path(cid, target_id)
        rows = gaql_all('SELECT ad_group.resource_name, ad_group.status, ad_group.type, ad_group.campaign '
                        f'FROM ad_group WHERE ad_group.id = {target_id}', cid)
        if len(rows) != 1:
            raise RailViolation('ad group not readable uniquely')
        row = _creation_row(rows[0], 'ad_group')
        parent = {key: row.get(key) for key in ('resource_name', 'status', 'type_', 'campaign')}
        parent['status'] = _enum_name('AdGroupStatusEnum', parent['status'])
        parent['type_'] = _enum_name('AdGroupTypeEnum', parent['type_'])
        if (parent['resource_name'] != group_rn or parent['status'] not in {'ENABLED', 'PAUSED'}
                or parent['type_'] != 'SEARCH_STANDARD'):
            raise RailViolation('parent must be a nonremoved standard Search ad group')
        campaign_rn = parent.get('campaign')
        _validate_resource(campaign_rn, 'campaigns', cid)
        campaign = creation_parent(cid, campaign_rn.rsplit('/', 1)[1])
        if campaign['resource_name'] != campaign_rn:
            raise RailViolation('ad group campaign ownership mismatch')
    else:
        raise RailViolation('unsupported asset target')
    return cid, target_id, parent, campaign


def sitelink_state(cid, target_type, target_id):
    """Complete target and nonremoved sitelink-association snapshot for draft/apply drift checks."""
    from .rails import RailViolation
    cid, target_id, parent, campaign = _asset_target_parent(cid, target_type, target_id)
    entity = target_type + '_asset'
    target_field = target_type
    fields = ['resource_name', target_field, 'asset', 'field_type', 'status']
    asset_fields = ['resource_name', 'type', 'final_urls', 'final_mobile_urls',
                    'tracking_url_template', 'final_url_suffix', 'url_custom_parameters',
                    'sitelink_asset.link_text',
                    'sitelink_asset.description1', 'sitelink_asset.description2']
    asset_fields.extend(('sitelink_asset.start_date', 'sitelink_asset.end_date',
                         'sitelink_asset.ad_schedule_targets'))
    query = ('SELECT ' + ', '.join([*(entity + '.' + f for f in fields),
                                    *('asset.' + f for f in asset_fields)])
             + f" FROM {entity} WHERE {target_field}.id = {target_id}"
             + f" AND {entity}.field_type = 'SITELINK' AND {entity}.status != 'REMOVED'")
    rows = gaql_all(query, cid)
    links, seen = [], set()
    kind = 'campaignAssets' if target_type == 'campaign' else 'adGroupAssets'
    for row in rows:
        link, asset = _creation_row(row, entity), _creation_row(row, 'asset')
        rn, asset_rn = link.get('resource_name'), link.get('asset')
        _validate_resource(rn, kind, cid)
        _validate_resource(asset_rn, 'assets', cid)
        parts = rn.rsplit('/', 1)[1].split('~')
        status = _enum_name('AssetLinkStatusEnum', link.get('status'))
        field_type = _enum_name('AssetFieldTypeEnum', link.get('field_type'))
        asset_type = _enum_name('AssetTypeEnum', asset.get('type_'))
        if (rn in seen or link.get(target_field) != parent['resource_name']
                or parts != [target_id, asset_rn.rsplit('/', 1)[1], '13']
                or status not in {'ENABLED', 'PAUSED'} or field_type != 'SITELINK'
                or asset.get('resource_name') != asset_rn or asset_type != 'SITELINK'):
            raise RailViolation('sitelink population identity/type/status mismatch')
        seen.add(rn)
        links.append({'resource_name': rn, 'status': status, 'asset': asset_rn,
                      'content': existing_sitelink_content(asset)})
    state = {'account': _creation_account(cid), 'parent': parent,
             'links': sorted(links, key=lambda r: r['resource_name'])}
    if target_type == 'ad_group':
        state['campaign'] = campaign
    return state


def callout_state(cid, target_type, target_id):
    """Complete target and nonremoved callout-link snapshot for draft/apply drift checks."""
    from .rails import RailViolation
    cid, target_id, parent, campaign = _asset_target_parent(cid, target_type, target_id)
    entity, target_field = target_type + '_asset', target_type
    fields = ['resource_name', target_field, 'asset', 'field_type', 'status']
    asset_fields = ['resource_name', 'type', 'final_urls', 'final_mobile_urls',
                    'tracking_url_template', 'final_url_suffix', 'url_custom_parameters',
                    'callout_asset.callout_text', 'callout_asset.start_date',
                    'callout_asset.end_date', 'callout_asset.ad_schedule_targets']
    query = ('SELECT ' + ', '.join([*(entity + '.' + f for f in fields),
                                    *('asset.' + f for f in asset_fields)])
             + f" FROM {entity} WHERE {target_field}.id = {target_id}"
             + f" AND {entity}.field_type = 'CALLOUT' AND {entity}.status != 'REMOVED'")
    rows = gaql_all(query, cid)
    links, seen = [], set()
    kind = 'campaignAssets' if target_type == 'campaign' else 'adGroupAssets'
    for row in rows:
        link, asset = _creation_row(row, entity), _creation_row(row, 'asset')
        rn, asset_rn = link.get('resource_name'), link.get('asset')
        _validate_resource(rn, kind, cid)
        _validate_resource(asset_rn, 'assets', cid)
        parts = rn.rsplit('/', 1)[1].split('~')
        status = _enum_name('AssetLinkStatusEnum', link.get('status'))
        field_type = _enum_name('AssetFieldTypeEnum', link.get('field_type'))
        asset_type = _enum_name('AssetTypeEnum', asset.get('type_'))
        if (rn in seen or link.get(target_field) != parent['resource_name']
                or parts != [target_id, asset_rn.rsplit('/', 1)[1], '11']
                or status not in {'ENABLED', 'PAUSED'} or field_type != 'CALLOUT'
                or asset.get('resource_name') != asset_rn or asset_type != 'CALLOUT'):
            raise RailViolation('callout population identity/type/status mismatch')
        seen.add(rn)
        links.append({'resource_name': rn, 'status': status, 'asset': asset_rn,
                      'content': existing_callout_content(asset)})
    state = {'account': _creation_account(cid), 'parent': parent,
             'links': sorted(links, key=lambda row: row['resource_name'])}
    if target_type == 'ad_group':
        state['campaign'] = campaign
    return state


def structured_snippet_state(cid, target_type, target_id):
    """Complete target and structured-snippet association snapshot for drift checks."""
    from .rails import RailViolation
    cid, target_id, parent, campaign = _asset_target_parent(cid, target_type, target_id)
    entity, target_field = target_type + '_asset', target_type
    fields = ['resource_name', target_field, 'asset', 'field_type', 'status']
    asset_fields = ['resource_name', 'type', 'final_urls', 'final_mobile_urls',
                    'tracking_url_template', 'final_url_suffix', 'url_custom_parameters',
                    'sitelink_asset.link_text', 'callout_asset.callout_text',
                    'structured_snippet_asset.header', 'structured_snippet_asset.values']
    query = ('SELECT ' + ', '.join([*(entity + '.' + f for f in fields),
                                    *('asset.' + f for f in asset_fields)])
             + f" FROM {entity} WHERE {target_field}.id = {target_id}"
             + f" AND {entity}.field_type = 'STRUCTURED_SNIPPET'"
             + f" AND {entity}.status != 'REMOVED'")
    rows = gaql_all(query, cid)
    links, seen = [], set()
    kind = 'campaignAssets' if target_type == 'campaign' else 'adGroupAssets'
    for row in rows:
        link, asset = _creation_row(row, entity), _creation_row(row, 'asset')
        rn, asset_rn = link.get('resource_name'), link.get('asset')
        _validate_resource(rn, kind, cid)
        _validate_resource(asset_rn, 'assets', cid)
        parts = rn.rsplit('/', 1)[1].split('~')
        status = _enum_name('AssetLinkStatusEnum', link.get('status'))
        field_type = _enum_name('AssetFieldTypeEnum', link.get('field_type'))
        asset_type = _enum_name('AssetTypeEnum', asset.get('type_'))
        if (rn in seen or link.get(target_field) != parent['resource_name']
                or parts != [target_id, asset_rn.rsplit('/', 1)[1], '12']
                or status not in {'ENABLED', 'PAUSED'} or field_type != 'STRUCTURED_SNIPPET'
                or asset.get('resource_name') != asset_rn or asset_type != 'STRUCTURED_SNIPPET'):
            raise RailViolation('structured snippet population identity/type/status mismatch')
        seen.add(rn)
        links.append({'resource_name': rn, 'status': status, 'asset': asset_rn,
                      'content': existing_structured_snippet_content(asset)})
    state = {'account': _creation_account(cid), 'parent': parent,
             'links': sorted(links, key=lambda row: row['resource_name'])}
    if target_type == 'ad_group':
        state['campaign'] = campaign
    return state


def image_error_codes(error):
    """Retain provider code enums and operation indices, never free text or triggers."""
    from google.ads.googleads.v25.errors.types.errors import GoogleAdsFailure

    failure = getattr(error, 'failure', None)
    if not isinstance(failure, GoogleAdsFailure):
        return None
    errors = []
    for item in failure.errors:
        codes = {field.name: int(value) for field, value in item.error_code._pb.ListFields()
                 if field.enum_type is not None}
        indices = [part.index for part in item.location.field_path_elements
                   if part.field_name in {'operations', 'mutate_operations'} and part.index >= 0]
        errors.append({'error_code': codes, 'operation_indices': indices})
    return {'errors': errors}


# UserList website visitors, deliberately distinct from Google's CustomAudience interests.
def audience_rules(url_contains):
    import unicodedata

    from .rails import RailViolation, check_content
    if type(url_contains) is not list or not 1 <= len(url_contains) <= 10:
        raise RailViolation('supply 1 to 10 URL substrings')
    for value in url_contains:
        if (type(value) is not str or not value or value != value.strip()
                or any(unicodedata.category(c).startswith('C') for c in value)
                or len(value.encode('utf-8')) > 256 or any(c in value for c in '{}*?')):
            raise RailViolation('invalid exact URL substring')
    if len({value.casefold() for value in url_contains}) != len(url_contains):
        raise RailViolation('duplicate URL substring')
    check_content(url_contains)
    return {'flexible_rule_user_list': {'inclusive_rule_operator': 'OR',
        'inclusive_operands': [{'lookback_window_days': 30, 'rule': {
            'rule_type': 'AND_OF_ORS', 'rule_item_groups': [{'rule_items': [
                {'name': 'url__', 'string_rule_item': {'operator': 'CONTAINS', 'value': value}}
            ]}]}} for value in url_contains]}}


def validate_audience_create(values):
    from .rails import RailViolation, check_content
    if type(values) is not dict or set(values) != {'name', 'membership_status', 'rule_based_user_list'}:
        raise RailViolation('audience create contains forbidden fields')
    check_content([creation_name(values['name'])])
    try:
        operands = values['rule_based_user_list']['flexible_rule_user_list']['inclusive_operands']
        urls = [o['rule']['rule_item_groups'][0]['rule_items'][0]['string_rule_item']['value'] for o in operands]
        expected = audience_rules(urls)
        if any(type(o['lookback_window_days']) is not int for o in operands):
            raise RailViolation('lookback must be the integer 30')
    except (KeyError, IndexError, TypeError) as exc:
        raise RailViolation('invalid audience rule tree') from exc
    if values['membership_status'] != 'OPEN' or values['rule_based_user_list'] != expected:
        raise RailViolation('audience must use exact OPEN collection and 30-day URL-CONTAINS OR rules')


def audience_inventory(cid, name):
    from .rails import RailViolation
    rows = gaql_all('SELECT user_list.resource_name, user_list.id, user_list.name, '
                    'user_list.access_reason, user_list.read_only, user_list.type, user_list.rule_based_user_list '
                    'FROM user_list', cid)
    result, seen = [], set()
    for row in rows:
        item = dict(_creation_row(row, 'user_list'))
        rn = item.get('resource_name')
        _validate_resource(rn, 'userLists', cid)
        ownership = _enum_name('AccessReasonEnum', item.get('access_reason'))
        list_type = _enum_name('UserListTypeEnum', item.get('type_', item.get('type')))
        if (rn in seen or str(item.get('id')) != rn.rsplit('/', 1)[-1]
                or type(item.get('name')) is not str or not item['name'].strip()
                or type(item.get('read_only')) is not bool
                or list_type not in {'RULE_BASED', 'REMARKETING', 'LOGICAL', 'EXTERNAL_REMARKETING', 'SIMILAR', 'CRM_BASED', 'LOOKALIKE'}
                or (list_type == 'RULE_BASED' and type(item.get('rule_based_user_list')) is not dict)
                or ownership not in {'OWNED', 'SHARED', 'LICENSED', 'SUBSCRIBED', 'AFFILIATED'}):
            raise RailViolation('audience inventory ownership or identity unreadable')
        seen.add(rn)
        item['access_reason'] = ownership
        if ownership == 'OWNED':
            if item['name'].casefold() == name.casefold():
                raise RailViolation('owned audience name already exists')
            result.append(item)
    return sorted(result, key=lambda item: item['resource_name'])


def verify_audience_state(state, expected, cid):
    from google.ads.googleads.v25.services.services.user_list_service import (
        UserListServiceClient,
    )

    from .rails import RailViolation
    rn = state['resource_name']
    if (rn != UserListServiceClient.user_list_path(cid, numeric_id(rn.rsplit('/', 1)[-1]))
            or _enum_name('UserListTypeEnum', state.get('type_', state.get('type'))) != 'RULE_BASED'
            or _enum_name('AccessReasonEnum', state.get('access_reason')) != 'OWNED'
            or state.get('read_only') is not False
            or _enum_name('UserListMembershipStatusEnum', state.get('membership_status')) != 'OPEN'
            or state.get('name') != expected['name']):
        raise RailViolation('saved audience identity, ownership, collection or name mismatch')
    # Parse the full selected message, rejecting unknown fields; canonical provider defaults
    # and repeated order may differ, while every text and every rule must remain exact.
    def canonical(value):
        obj = gads().get_type('RuleBasedUserListInfo')
        for key, content in value.items():
            setattr(obj, key, content)
        normalized = type(obj).to_dict(obj)
        normalized.pop('prepopulation_status', None)
        flex = normalized.get('flexible_rule_user_list', {})
        import json
        flex['inclusive_operands'] = sorted(flex.get('inclusive_operands', []), key=lambda x: json.dumps(x, sort_keys=True))
        return normalized
    if canonical(state.get('rule_based_user_list', {})) != canonical(expected['rule_based_user_list']):
        raise RailViolation('saved audience rules do not match requested content')


def validate_audience_targeting_create(values, cid):
    from .rails import RailViolation
    if (set(values) != {'campaign', 'user_list', 'negative', 'status'}
            or values['negative'] is not False or values['status'] != 'PAUSED'
            or type(values['user_list']) is not dict or set(values['user_list']) != {'user_list'}):
        raise RailViolation('audience targeting requires exact paused positive UserList fields')
    _validate_resource(values['campaign'], 'campaigns', cid)
    _validate_resource(values['user_list']['user_list'], 'userLists', cid)


def audience_targeting_state(cid, campaign_id, audience_id, mode, added_resource=None):
    from .rails import RailViolation
    account = _creation_account(cid)
    campaign = creation_parent(cid, campaign_id)
    if campaign['status'] != 'PAUSED':
        raise RailViolation('audience target campaign must be PAUSED')
    parent, list_rn = campaign['resource_name'], f'customers/{cid}/userLists/{audience_id}'
    rows = gaql_all('SELECT campaign.resource_name, campaign.targeting_setting.target_restrictions '
                    + f"FROM campaign WHERE campaign.resource_name = '{parent}'", cid)
    refusal = 'Configure the intended explicit campaign-level AUDIENCE mode before using this tool'
    if len(rows) != 1 or rows[0].get('campaign', {}).get('resource_name') != parent:
        raise RailViolation(refusal)
    setting = rows[0]['campaign'].get('targeting_setting')
    restrictions = setting.get('target_restrictions') if type(setting) is dict else None
    if type(restrictions) is not list:
        raise RailViolation(refusal)
    normalized, seen = [], set()
    for item in restrictions:
        if type(item) is not dict or set(item) != {'targeting_dimension', 'bid_only'}:
            raise RailViolation(refusal)
        dimension = _enum_name('TargetingDimensionEnum', item['targeting_dimension'])
        if dimension not in {'KEYWORD', 'AUDIENCE', 'TOPIC', 'GENDER', 'AGE_RANGE', 'PLACEMENT', 'PARENTAL_STATUS', 'INCOME_RANGE'} or dimension in seen or type(item['bid_only']) is not bool:
            raise RailViolation(refusal)
        seen.add(dimension)
        normalized.append({'targeting_dimension': dimension, 'bid_only': item['bid_only']})
    if {'targeting_dimension': 'AUDIENCE', 'bid_only': mode == 'OBSERVATION'} not in normalized:
        raise RailViolation(refusal)
    groups = []
    rows = gaql_all('SELECT ad_group.resource_name, ad_group.campaign, ad_group.targeting_setting.target_restrictions '
                    + f"FROM ad_group WHERE ad_group.campaign = '{parent}'", cid)
    for row in rows:
        item = dict(_creation_row(row, 'ad_group'))
        _validate_resource(item.get('resource_name'), 'adGroups', cid)
        settings = item.get('targeting_setting')
        if (item.get('campaign') != parent or type(settings) is not dict
                or set(settings) != {'target_restrictions'} or settings['target_restrictions'] != []
                or item['resource_name'] in {g['resource_name'] for g in groups}):
            raise RailViolation(refusal + '; conflicting or unreadable ad-group settings')
        groups.append(item)
    selected = created_resource_state(cid, 'user_list', list_rn)
    if type(selected.get('name')) is not str or not selected['name'].strip() or not selected.get('rule_based_user_list'):
        raise RailViolation('selected list content unreadable')
    try:
        operands = selected['rule_based_user_list']['flexible_rule_user_list']['inclusive_operands']
        urls = [o['rule']['rule_item_groups'][0]['rule_items'][0]['string_rule_item']['value'] for o in operands]
        expected_list = {'name': selected['name'], 'rule_based_user_list': audience_rules(urls)}
    except (KeyError, IndexError, TypeError) as exc:
        raise RailViolation('selected list must use supported website URL-CONTAINS rules') from exc
    try:
        verify_audience_state(selected, expected_list, cid)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RailViolation('selected list rule content unreadable') from exc
    inventory = _audience_connections(cid, 'campaign_criterion', f"campaign_criterion.campaign = '{parent}'")
    if any(item['campaign'] != parent for item in inventory):
        raise RailViolation('campaign inventory returned an unexpected parent')
    filtered = [item for item in inventory if item['resource_name'] != added_resource]
    if any(item['user_list']['user_list'] == list_rn for item in filtered):
        raise RailViolation('selected list already has a positive or negative campaign criterion')
    usage = []
    for entity in ('campaign_criterion', 'ad_group_criterion'):
        usage.extend(_audience_connections(cid, entity, f"{entity}.user_list.user_list = '{list_rn}'"))
    if added_resource:
        expected = {'resource_name': added_resource, 'campaign': parent, 'type_': 'USER_LIST',
                    'status': 'PAUSED', 'negative': False, 'user_list': {'user_list': list_rn}}
        for connections in (inventory, usage):
            added = [item for item in connections if item['resource_name'] == added_resource]
            if len(added) != 1 or not _created_match(added[0], expected):
                raise RailViolation('created criterion missing or changed in complete connections')
    if any(item['user_list']['user_list'] != list_rn for item in usage):
        raise RailViolation('list usage returned an unexpected list')
    return {'account': account, 'campaign': campaign, 'mode': sorted(normalized, key=lambda x: x['targeting_dimension']),
            'ad_groups': sorted(groups, key=lambda x: x['resource_name']), 'list': selected,
            'inventory': filtered, 'usage': sorted([item for item in usage if item['resource_name'] != added_resource], key=lambda x: x['resource_name'])}


def _audience_connections(cid, entity, condition):
    from .rails import RailViolation
    is_group = entity == 'ad_group_criterion'
    parent_field = 'ad_group' if is_group else 'campaign'
    fields = [f'{entity}.{field}' for field in ('resource_name', parent_field, 'type', 'status', 'negative', 'user_list.user_list')]
    if is_group:
        fields += ['ad_group.resource_name', 'ad_group.campaign']
    rows = gaql_all('SELECT ' + ', '.join(fields) + f" FROM {entity} WHERE {condition} AND {entity}.type = 'USER_LIST' AND {entity}.status != 'REMOVED'", cid)
    result, seen = [], set()
    for row in rows:
        item = dict(_creation_row(row, entity))
        _validate_resource(item.get('resource_name'), 'adGroupCriteria' if is_group else 'campaignCriteria', cid)
        _validate_resource(item.get(parent_field), 'adGroups' if is_group else 'campaigns', cid)
        if item['resource_name'].rsplit('/', 1)[1].split('~')[0] != item[parent_field].rsplit('/', 1)[1]:
            raise RailViolation('audience connection parent mismatch')
        item['type_'] = _enum_name('CriterionTypeEnum', item.pop('type', item.get('type_')))
        item['status'] = _enum_name('AdGroupCriterionStatusEnum' if is_group else 'CampaignCriterionStatusEnum', item.get('status'))
        if (item['resource_name'] in seen or item['type_'] != 'USER_LIST' or item['status'] not in {'ENABLED', 'PAUSED'}
                or type(item.get('negative')) is not bool or type(item.get('user_list')) is not dict
                or set(item['user_list']) != {'user_list'}):
            raise RailViolation('audience connection content unreadable')
        _validate_resource(item['user_list']['user_list'], 'userLists', cid)
        if is_group:
            group = _creation_row(row, 'ad_group')
            if group.get('resource_name') != item['ad_group']:
                raise RailViolation('audience connection ad-group identity mismatch')
            _validate_resource(group.get('campaign'), 'campaigns', cid)
            item['campaign'] = group['campaign']
        seen.add(item['resource_name'])
        result.append(item)
    return sorted(result, key=lambda item: item['resource_name'])


def verify_audience_targeting(check, rn):
    from .rails import RailViolation
    cid, expected = check['customer_id'], check['expected']
    rows = gaql_all('SELECT campaign_criterion.resource_name, campaign_criterion.campaign, campaign_criterion.type, '
                    'campaign_criterion.status, campaign_criterion.negative, campaign_criterion.user_list.user_list '
                    + f"FROM campaign_criterion WHERE campaign_criterion.resource_name = '{rn}'", cid)
    if len(rows) != 1:
        raise RailViolation('saved audience criterion not uniquely readable')
    actual = dict(_creation_row(rows[0], 'campaign_criterion'))
    actual['status'] = _enum_name('CampaignCriterionStatusEnum', actual.get('status'))
    if (actual.get('resource_name') != rn or _enum_name('CriterionTypeEnum', actual.get('type_', actual.get('type'))) != 'USER_LIST'
            or not _created_match(actual, expected)):
        raise RailViolation('saved audience criterion content mismatch')
    target = check['audience_targeting']
    fresh = audience_targeting_state(cid, target['campaign_id'], target['audience_id'], target['targeting_mode'], rn)
    if fresh != target['state']:
        raise RailViolation('campaign, list, mode or connections changed after dispatch')


CONVERSION_CATEGORIES = {'DEFAULT', 'PURCHASE', 'SIGNUP', 'SUBMIT_LEAD_FORM', 'CONTACT',
                         'BOOK_APPOINTMENT', 'REQUEST_QUOTE'}
CONVERSION_FIXED = {'type_': 'WEBPAGE', 'status': 'ENABLED', 'primary_for_goal': False,
                    'counting_type': 'ONE_PER_CLICK', 'click_through_lookback_window_days': 30,
                    'view_through_lookback_window_days': 1}
_CONVERSION_FIELDS = {
    'customer': ['resource_name', 'id', 'manager', 'status',
                 'conversion_tracking_setting.google_ads_conversion_customer'],
    'customer_client': ['resource_name', 'client_customer', 'id', 'manager', 'level', 'status', 'hidden'],
    'conversion_action': ['resource_name', 'owner_customer', 'name', 'type', 'status', 'category',
                          'origin', 'primary_for_goal', 'counting_type',
                          'click_through_lookback_window_days', 'view_through_lookback_window_days',
                          'value_settings.default_value', 'value_settings.default_currency_code',
                          'value_settings.always_use_default_value',
                          'attribution_model_settings.attribution_model',
                          'attribution_model_settings.data_driven_model_status'],
    'custom_conversion_goal': ['resource_name', 'id', 'name', 'status', 'conversion_actions'],
    'customer_conversion_goal': ['resource_name', 'category', 'origin', 'biddable'],
    'campaign': ['resource_name', 'id', 'name', 'status', 'advertising_channel_type'],
    'campaign_conversion_goal': ['resource_name', 'campaign', 'category', 'origin', 'biddable'],
    'conversion_goal_campaign_config': ['resource_name', 'campaign', 'custom_conversion_goal', 'goal_config_level'],
}


def validate_conversion_create(values):
    from .rails import RailViolation, check_content
    if (not isinstance(values, dict) or set(values) != {'name', 'category', *CONVERSION_FIXED}
            or type(values.get('category')) is not str or values['category'] not in CONVERSION_CATEGORIES
            or any(type(values[k]) is not type(v) or values[k] != v for k, v in CONVERSION_FIXED.items())):
        raise RailViolation('conversion requires exact secondary WEBPAGE fields')
    check_content([creation_name(values['name'])])


def _conversion_refuse(message):
    from .rails import RailViolation
    raise RailViolation(message, code='CONVERSION_SCOPE')


def _conversion_enum(enum_type, value, allow_unspecified=False):
    if isinstance(value, bool):
        _conversion_refuse('invalid conversion enum')
    enum = getattr(gads().get_type(enum_type), enum_type.removesuffix('Enum'))
    try:
        member = enum[value] if isinstance(value, str) else enum(value)
    except (KeyError, ValueError, TypeError):
        _conversion_refuse('unknown conversion enum')
    if member.name == 'UNKNOWN' or (member.name == 'UNSPECIFIED' and not allow_unspecified):
        _conversion_refuse('unspecified conversion enum')
    return member.name, member.value


def _conversion_rows(cid, entity, condition=''):
    """Uncapped safety-only paging, with page totals, tokens and identities reconciled."""
    query = 'SELECT ' + ', '.join(entity + '.' + x for x in _CONVERSION_FIELDS[entity]) + ' FROM ' + entity + condition
    rows, tokens, identities = [], set(), set()
    token, total = None, None
    while True:
        try:
            page, next_token, count = _search_one_page(query, cid, token)
        except Exception:
            _conversion_refuse('conversion control scan unreadable')
        if type(count) is not int or count < 0 or (total is not None and count != total):
            _conversion_refuse('conversion scan totals inconsistent')
        total = count
        for raw in page:
            row = raw if isinstance(raw, dict) else type(raw).to_dict(raw)
            item = row.get(entity) if isinstance(row, dict) else None
            rn = item.get('resource_name') if isinstance(item, dict) else None
            if type(rn) is not str or not rn or rn in identities:
                _conversion_refuse('conversion scan missing or duplicate identity')
            identities.add(rn)
            projected = {}
            for field in _CONVERSION_FIELDS[entity]:
                parts = field.split('.')
                source, target = item, projected
                for part in parts[:-1]:
                    source = source.get(part, {}) if isinstance(source, dict) else {}
                    target = target.setdefault(part, {})
                if not isinstance(source, dict):
                    _conversion_refuse('conversion scan nested configuration malformed')
                key = parts[-1]
                key = 'type_' if key == 'type' and 'type_' in source else key
                if key in source:
                    target[key] = source[key]
            rows.append(projected)
        if len(rows) > total:
            _conversion_refuse('conversion scan count exceeds total')
        if next_token is None or next_token == '':
            break
        if type(next_token) is not str or next_token in tokens:
            _conversion_refuse('conversion scan repeats page token')
        tokens.add(next_token)
        token = next_token
    if len(rows) != total:
        _conversion_refuse('conversion scan incomplete')
    return sorted(rows, key=lambda item: item['resource_name'])


def _conversion_customer_ref(value):
    if type(value) is not str or not re.fullmatch(r'customers/[1-9][0-9]*', value):
        _conversion_refuse('conversion owner/customer identity malformed')
    return numeric_id(value.split('/')[1])


def _conversion_customer(cid):
    rows = _conversion_rows(cid, 'customer')
    if len(rows) != 1:
        _conversion_refuse('customer must resolve exactly once')
    item = rows[0]
    if (item['resource_name'] != f'customers/{cid}' or str(item.get('id')) != cid
            or type(item.get('manager')) is not bool
            or _conversion_enum('CustomerStatusEnum', item.get('status'))[0] != 'ENABLED'):
        _conversion_refuse('customer identity, manager or status unreadable')
    tracking = item.get('conversion_tracking_setting')
    if not isinstance(tracking, dict):
        _conversion_refuse('customer conversion tracking settings missing')
    owner = _conversion_customer_ref(tracking.get('google_ads_conversion_customer'))
    return {'resource_name': item['resource_name'], 'id': cid, 'manager': item['manager'],
            'status': 'ENABLED', 'owner': owner}


def conversion_control_scope(selected):
    from .rails import check_customer_allowlisted
    account = _conversion_customer(selected)
    owner = account['owner']
    check_customer_allowlisted(owner, 'write')
    check_customer_allowlisted(owner, 'read')
    root = getattr(gads(), 'login_customer_id', None)
    if type(root) is not str or not re.fullmatch(r'[1-9][0-9]*', root):
        _conversion_refuse('configured login manager required')
    nodes, links, pending, discovered = {}, [], [root], {}
    while pending:
        cid = pending.pop()
        if cid in nodes:
            continue
        node = _conversion_customer(cid)
        nodes[cid] = node
        if cid == root and not node['manager']:
            _conversion_refuse('login root must be a manager')
        if node['manager']:
            for child in _conversion_rows(cid, 'customer_client', ' WHERE customer_client.level = 1'):
                child_id = _conversion_customer_ref(child.get('client_customer'))
                if (child.get('resource_name') != f'customers/{cid}/customerClients/{child_id}'
                        or str(child.get('id')) != child_id or str(child.get('level')) != '1'
                        or type(child.get('manager')) is not bool or child.get('hidden') is not False
                        or _conversion_enum('CustomerStatusEnum', child.get('status'))[0] != 'ENABLED'):
                    _conversion_refuse('hierarchy node malformed, hidden or inaccessible')
                if child_id in discovered and discovered[child_id] != child['manager']:
                    _conversion_refuse('hierarchy manager identity conflicts')
                discovered[child_id] = child['manager']
                links.append((cid, child_id))
                pending.append(child_id)
    if nodes.get(selected) != account or owner not in nodes or nodes[owner]['owner'] != owner:
        _conversion_refuse('selected customer or consistent conversion owner outside scope')
    if any(nodes[cid]['manager'] != manager for cid, manager in discovered.items()):
        _conversion_refuse('hierarchy discovery disagrees with customer identity')
    # Iterative topological check accepts shared descendants, refuses any cycle at any depth.
    incoming = {cid: 0 for cid in nodes}
    children = {cid: [] for cid in nodes}
    for parent, child in links:
        incoming[child] += 1
        children[parent].append(child)
    ready = [cid for cid, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        cid = ready.pop()
        visited += 1
        for child in children[cid]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if visited != len(nodes):
        _conversion_refuse('conversion manager hierarchy contains a cycle')
    affected = sorted(cid for cid, node in nodes.items() if node['owner'] == owner)
    for cid in affected:
        try:
            check_customer_allowlisted(cid, 'write')
        except Exception:
            _conversion_refuse('every tracking customer must be write allowlisted')
    return {'selected': selected, 'owner': owner, 'root': root, 'nodes': nodes,
            'links': sorted(links), 'affected': affected}


def _conversion_action(item, owner):
    _validate_resource(item.get('resource_name'), 'conversionActions', owner)
    if item.get('owner_customer') != f'customers/{owner}' or type(item.get('name')) is not str:
        _conversion_refuse('conversion action ownership/name unreadable')
    for field, enum in [('type_', 'ConversionActionTypeEnum'), ('status', 'ConversionActionStatusEnum'),
                        ('category', 'ConversionActionCategoryEnum'), ('origin', 'ConversionOriginEnum'),
                        ('counting_type', 'ConversionActionCountingTypeEnum')]:
        item[field] = _conversion_enum(enum, item.pop('type', None) if field == 'type_' and 'type' in item else item.get(field))[0]
    if type(item.get('primary_for_goal')) is not bool:
        _conversion_refuse('conversion primary status unreadable')
    for field in ('click_through_lookback_window_days', 'view_through_lookback_window_days'):
        value = item.get(field)
        if type(value) not in (str, int) or not re.fullmatch(r'[0-9]+', str(value)):
            _conversion_refuse('conversion window unreadable')
        item[field] = int(value)
    values, attribution = item.get('value_settings'), item.get('attribution_model_settings')
    if (not isinstance(values, dict) or set(values) != {
            'default_value', 'default_currency_code', 'always_use_default_value'}
            or type(values['default_value']) not in (int, float)
            or not decimal.Decimal(str(values['default_value'])).is_finite()
            or type(values['default_currency_code']) is not str
            or (values['default_currency_code'] and not re.fullmatch(r'[A-Z]{3}', values['default_currency_code']))
            or type(values['always_use_default_value']) is not bool
            or not isinstance(attribution, dict) or set(attribution) != {
                'attribution_model', 'data_driven_model_status'}):
        _conversion_refuse('provider-managed value or attribution settings unreadable')
    attribution['attribution_model'] = _conversion_enum(
        'AttributionModelEnum', attribution['attribution_model'])[0]
    # UNSPECIFIED is an observed status, not a guessed model availability default.
    attribution['data_driven_model_status'] = _conversion_enum(
        'DataDrivenModelStatusEnum', attribution['data_driven_model_status'], allow_unspecified=True)[0]
    return item


def conversion_creation_state(selected, category):
    return conversion_inventory_state(selected, creation_category=category)


def conversion_inventory_state(selected, *, creation_category=None):
    """Complete shared inventory; optional creation-only goal predictions."""
    category = creation_category
    scope = conversion_control_scope(selected)
    owner = scope['owner']
    actions = [_conversion_action(item, owner) for item in _conversion_rows(owner, 'conversion_action')]
    action_ids = {item['resource_name'] for item in actions}
    accounts, shared, predictions = {}, {}, []
    category_number = _conversion_enum('ConversionActionCategoryEnum', category)[1] if category is not None else None
    website_number = _conversion_enum('ConversionOriginEnum', 'WEBSITE')[1]
    for cid in scope['affected']:
        data = {entity: _conversion_rows(cid, entity) for entity in (
            'campaign', 'customer_conversion_goal', 'campaign_conversion_goal',
            'conversion_goal_campaign_config', 'custom_conversion_goal')}
        campaigns = {}
        for item in data['campaign']:
            rn = item['resource_name']
            _validate_resource(rn, 'campaigns', cid)
            if str(item.get('id')) != rn.rsplit('/', 1)[1] or type(item.get('name')) is not str:
                _conversion_refuse('campaign identity incomplete')
            item['status'] = _conversion_enum('CampaignStatusEnum', item.get('status'))[0]
            item['advertising_channel_type'] = _conversion_enum('AdvertisingChannelTypeEnum', item.get('advertising_channel_type'))[0]
            campaigns[rn] = item
        for item in data['custom_conversion_goal']:
            rn = item['resource_name']
            _validate_resource(rn, 'customConversionGoals', owner)
            if str(item.get('id')) != rn.rsplit('/', 1)[1] or type(item.get('name')) is not str:
                _conversion_refuse('custom goal identity unreadable')
            item['status'] = _conversion_enum('CustomConversionGoalStatusEnum', item.get('status'))[0]
            refs = item.get('conversion_actions')
            if not isinstance(refs, list) or any(type(ref) is not str or ref not in action_ids for ref in refs) or len(set(refs)) != len(refs):
                _conversion_refuse('custom goal has unresolved action references')
            item['conversion_actions'] = sorted(refs)
            if rn in shared and shared[rn] != item:
                _conversion_refuse('shared custom goal content conflicts')
            shared[rn] = item
        configs = {}
        for item in data['conversion_goal_campaign_config']:
            campaign = item.get('campaign')
            if campaign not in campaigns or item['resource_name'] != f"customers/{cid}/conversionGoalCampaignConfigs/{campaign.rsplit('/', 1)[1]}" or campaign in configs:
                _conversion_refuse('campaign conversion config parent unreadable')
            item['goal_config_level'] = _conversion_enum('GoalConfigLevelEnum', item.get('goal_config_level'))[0]
            ref = item.get('custom_conversion_goal')
            if type(ref) is not str or (ref and ref not in shared):
                _conversion_refuse('custom goal usage unresolved')
            configs[campaign] = item
        if any(rn not in configs for rn, item in campaigns.items() if item['status'] != 'REMOVED'):
            _conversion_refuse('campaign conversion configs incomplete')
        for entity in ('customer_conversion_goal', 'campaign_conversion_goal'):
            seen = set()
            for item in data[entity]:
                cat, catnum = _conversion_enum('ConversionActionCategoryEnum', item.get('category'))
                origin, origin_num = _conversion_enum('ConversionOriginEnum', item.get('origin'))
                parent = item.get('campaign') if entity == 'campaign_conversion_goal' else None
                if entity == 'campaign_conversion_goal' and parent not in campaigns:
                    _conversion_refuse('campaign goal parent missing')
                suffix = f'{catnum}~{origin_num}'
                if parent:
                    suffix = parent.rsplit('/', 1)[1] + '~' + suffix
                kind = 'campaignConversionGoals' if parent else 'customerConversionGoals'
                if item['resource_name'] != f'customers/{cid}/{kind}/{suffix}' or type(item.get('biddable')) is not bool:
                    _conversion_refuse('conversion goal identity/flag unreadable')
                key = (parent, cat, origin)
                if key in seen:
                    _conversion_refuse('duplicate conversion goal pair')
                seen.add(key)
                item.update(category=cat, origin=origin)
            parents = [None] if entity == 'customer_conversion_goal' else [rn for rn, item in campaigns.items() if item['status'] != 'REMOVED']
            required_pairs = {(item['category'], item['origin']) for item in actions if item['status'] != 'REMOVED'}
            for parent in parents:
                observed_pairs = {(item['category'], item['origin']) for item in data[entity]
                                  if item.get('campaign') == parent}
                if not required_pairs <= observed_pairs:
                    _conversion_refuse('existing conversion actions lack complete goal coverage')
                if category is None:
                    continue
                same = [item for item in data[entity] if item.get('campaign') == parent and item['category'] == category]
                matching = [item for item in same if item['origin'] == 'WEBSITE']
                if matching:
                    predictions.append({'customer_id': cid, 'entity': entity, 'effect': 'reuse', 'goal': matching[0]})
                    continue
                biddable = not any(item['biddable'] is False for item in same)
                if parent is None and not biddable:
                    _conversion_refuse('new customer WEBSITE goal default is ambiguous for this category')
                suffix = f'{category_number}~{website_number}'
                kind = 'customerConversionGoals'
                goal = {'category': category, 'origin': 'WEBSITE', 'biddable': biddable}
                if parent:
                    kind = 'campaignConversionGoals'
                    suffix = parent.rsplit('/', 1)[1] + '~' + suffix
                    goal['campaign'] = parent
                goal['resource_name'] = f'customers/{cid}/{kind}/{suffix}'
                predictions.append({'customer_id': cid, 'entity': entity, 'effect': 'auto_create', 'goal': goal})
        accounts[cid] = data
    return {'scope': scope, 'actions': actions, 'accounts': accounts, 'predictions': predictions}


def verify_conversion_creation(check, rn):
    import copy

    from .rails import RailViolation
    descriptor = check['conversion_creation']
    owner = check['customer_id']
    rows = _conversion_rows(owner, 'conversion_action', f" WHERE conversion_action.resource_name = '{rn}'")
    expected = dict(check['expected'], resource_name=rn, owner_customer=f'customers/{owner}', origin='WEBSITE')
    if len(rows) != 1:
        raise RailViolation('saved conversion does not resolve exactly once')
    observed = _conversion_action(rows[0], owner)
    managed = {key: observed[key] for key in ('value_settings', 'attribution_model_settings')}
    if observed != dict(expected, **managed):
        raise RailViolation('saved conversion does not match exact requested configuration')
    fresh = conversion_creation_state(descriptor['selected'], expected['category'])
    before = descriptor['state']
    wanted = copy.deepcopy(before)
    wanted['actions'] = sorted([*wanted['actions'], observed], key=lambda item: item['resource_name'])
    for prediction in before['predictions']:
        if prediction['effect'] == 'auto_create':
            items = wanted['accounts'][prediction['customer_id']][prediction['entity']]
            items.append(prediction['goal'])
            items.sort(key=lambda item: item['resource_name'])
    # Compare the complete observed action again, rather than excluding its identity.
    if any(fresh[key] != wanted[key] for key in ('scope', 'actions', 'accounts')):
        raise RailViolation('conversion control scope, actions, goals or custom usage changed unexpectedly')


def verify_conversion_primary_status(check, rn):
    import copy

    from .rails import RailViolation
    owner = check['customer_id']
    rows = _conversion_rows(owner, 'conversion_action', f" WHERE conversion_action.resource_name = '{rn}'")
    if len(rows) != 1 or _conversion_action(rows[0], owner) != check['expected']:
        raise RailViolation('saved conversion does not match exact approved update')
    descriptor = check['conversion_primary_status']
    wanted = copy.deepcopy(descriptor['state'])
    for action in wanted['actions']:
        if action['resource_name'] == rn:
            action['primary_for_goal'] = check['expected']['primary_for_goal']
    if conversion_inventory_state(descriptor['selected']) != wanted:
        raise RailViolation('conversion action, control scope, goals or custom usage changed unexpectedly')



def discover_keywords(seed_keywords, customer_id, page_token=None) -> dict:
    """One explicitly requested KeywordPlanIdeaService page, with bound continuation."""
    from . import settings
    from .rails import RailViolation

    config = settings.keyword_research()
    geos = config["geo_target_constant_ids"]
    language = config["language_constant_id"]
    network = config["keyword_plan_network"]
    adult = config["include_adult_keywords"]

    def valid_id(value):
        return type(value) in (str, int) and re.fullmatch(r"[1-9][0-9]*", str(value))

    if (type(geos) is not list or len(geos) > 10 or any(not valid_id(g) for g in geos)
            or len({str(g) for g in geos}) != len(geos)
            or (language is not None and not valid_id(language))
            or type(network) is not str
            or network not in {"GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS"}
            or type(adult) is not bool):
        raise RailViolation("invalid effective keyword_research settings", code="BAD_SETTINGS")
    request_metadata = {
        "customer_id": customer_id, "seed_keywords": list(seed_keywords),
        "geo_target_constants": [f"geoTargetConstants/{g}" for g in geos],
        "language": f"languageConstants/{language}" if language is not None else None,
        "keyword_plan_network": network, "include_adult_keywords": adult,
        "page_size": 50, "all_geographies": not geos, "all_languages": language is None,
        "service": "KeywordPlanIdeaService.GenerateKeywordIdeas",
    }
    context = json.dumps(request_metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if page_token is not None and (type(page_token) is not str
            or not re.fullmatch(r"[0-9a-f]{64}:.+", page_token, re.DOTALL)):
        raise RailViolation("page_token must be a nonempty bound discovery token", code="TOKEN_MISMATCH")
    raw_token = _unwrap_token(page_token, context, customer_id)
    c = gads()
    req = c.get_type("GenerateKeywordIdeasRequest")
    req.customer_id = customer_id
    req.keyword_seed.keywords.extend(seed_keywords)
    req.geo_target_constants.extend(request_metadata["geo_target_constants"])
    if language is not None:
        req.language = request_metadata["language"]
    req.keyword_plan_network = network
    req.include_adult_keywords = adult
    req.page_size = 50
    if raw_token is not None:
        req.page_token = raw_token
    pager = c.get_service("KeywordPlanIdeaService").generate_keyword_ideas(request=req)
    page = next(iter(pager.pages))
    rows = list(page.results)
    total, next_raw = page.total_size, page.next_page_token
    if (type(total) is not int or total < len(rows) or len(rows) > 50
            or type(next_raw) is not str or (next_raw and next_raw == raw_token)
            or (not rows and (total != 0 or next_raw))
            or (next_raw and total <= len(rows))
            or (raw_token is None and not next_raw and total != len(rows))):
        raise RailViolation("malformed keyword ideas page or repeated continuation token", code="BAD_RESPONSE")
    return {
        "keyword_ideas": [type(row).to_dict(row) for row in rows],
        "returned_count": len(rows), "total_results_count": total,
        "next_page_token": _wrap_token(next_raw, context, customer_id) if next_raw else None,
        "pages_complete": not next_raw, "request": request_metadata,
        "source": {
            "service": request_metadata["service"],
            "metrics": "Historical estimates, not realized account performance or forecasts. "
                       "Omitted metrics are unavailable.",
            "historical_window": "Provider default past 12 months; monthly rows identify supplied months.",
            "monetary_units": "Micros (1,000,000 per account-currency unit); currency code not fetched.",
            "attribution": "Not applicable to keyword research estimates.",
        },
    }


def keyword_forecasts(keyword_texts, match_type, forecast_start_date, forecast_end_date,
                      max_cpc_bid_micros, customer_id, daily_budget_micros=None):
    """One planless v25 forecast, after explicit targeting and account-local date checks."""
    from datetime import date
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from google.ads.googleads.v25.services.types.keyword_plan_idea_service import (
        GenerateKeywordForecastMetricsResponse,
    )

    from . import settings
    from .rails import RailViolation

    try:
        dates = []
        for value in (forecast_start_date, forecast_end_date):
            if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError()
            dates.append(date.fromisoformat(value))
        start, end = dates
        if start > end:
            raise ValueError()
    except ValueError:
        raise RailViolation("forecast dates must be ordered canonical YYYY-MM-DD dates",
                            code="BAD_INPUT") from None
    config = settings.keyword_research()
    geos, language = config.get("geo_target_constant_ids"), config.get("language_constant_id")

    def valid_id(value):
        return type(value) in (str, int) and re.fullmatch(r"[1-9][0-9]*", str(value))

    if (type(geos) is not list or not 1 <= len(geos) <= 10
            or any(not valid_id(g) for g in geos)
            or len({str(g) for g in geos}) != len(geos) or not valid_id(language)):
        raise RailViolation("forecast requires 1..10 unique locations and a concrete language",
                            code="BAD_SETTINGS")
    rows = gaql_all("SELECT customer.id, customer.currency_code, customer.time_zone FROM customer",
                    customer_id)
    account = rows[0].get("customer") if len(rows) == 1 and isinstance(rows[0], dict) else None
    if (not isinstance(account, dict) or account.get("id") != customer_id
            or type(account.get("currency_code")) is not str
            or not re.fullmatch(r"[A-Z]{3}", account["currency_code"])
            or type(account.get("time_zone")) is not str):
        raise RailViolation("forecast account metadata missing or malformed", code="BAD_RESPONSE")
    try:
        zone = ZoneInfo(account["time_zone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise RailViolation("forecast account timezone is unknown", code="BAD_RESPONSE") from None
    today = _forecast_today(zone)
    try:
        latest = today.replace(year=today.year + 1)
    except ValueError:  # February 29 has no equivalent in the following calendar year.
        latest = today.replace(year=today.year + 1, day=28)
    if start <= today or end > latest:
        raise RailViolation("forecast start must be after account-local today and end within "
                            "one calendar year", code="BAD_INPUT")
    c = gads()
    req = c.get_type("GenerateKeywordForecastMetricsRequest")
    req.customer_id = customer_id
    req.forecast_period.start_date = forecast_start_date
    req.forecast_period.end_date = forecast_end_date
    req.campaign.language_constants.append(f"languageConstants/{language}")
    req.campaign.geo_target_constants.extend(f"geoTargetConstants/{g}" for g in geos)
    req.campaign.ad_groups.append({"keywords": [
        {"text": text, "match_type": match_type} for text in keyword_texts]})
    bid = req.campaign.bidding_strategy.manual_cpc_bidding_strategy
    bid.max_cpc_bid_micros = max_cpc_bid_micros
    if daily_budget_micros is not None:
        bid.daily_budget_micros = daily_budget_micros
    response = c.get_service("KeywordPlanIdeaService").generate_keyword_forecast_metrics(
        request=req, retry=None)
    if not isinstance(response, GenerateKeywordForecastMetricsResponse):
        raise RailViolation("malformed keyword forecast response", code="BAD_RESPONSE")
    raw = type(response).to_dict(response, always_print_fields_with_no_presence=False)
    metrics = raw.get("campaign_forecast_metrics")
    return {
        **raw,
        "metric_availability": "unavailable" if metrics is None else {
            name: "available" if name in metrics else "unavailable"
            for name in type(response.campaign_forecast_metrics).meta.fields},
        "request": {"customer_id": customer_id, "currency_code": account["currency_code"],
                    "time_zone": account["time_zone"], "account_local_today": today.isoformat(),
                    "scenario": type(req).to_dict(req, always_print_fields_with_no_presence=False),
                    "currency_override": False,
                    "discovery_settings_not_applied": {
                        "keyword_plan_network": config.get("keyword_plan_network"),
                        "include_adult_keywords": config.get("include_adult_keywords")}},
        "source": {
            "service": "KeywordPlanIdeaService.GenerateKeywordForecastMetrics", "api_version": "v25",
            "metrics": "Google forecast estimates for the whole proposed campaign under a Manual "
                       "CPC clicks/cost scenario; not guaranteed outcomes or account performance. "
                       "Omitted metrics are unavailable. No locally developed prediction model.",
            "network_and_adult_filtering": "No selectors on this forecast request; discovery "
                                           "settings do not apply. Effective filtering is not verified.",
            "monetary_units": "Micros (1,000,000 per account-currency unit).",
            "attribution": "Conversion definitions and attribution window are not supplied by this response.",
        },
    }


def _forecast_today(zone):
    """Current date in the selected Google Ads account timezone."""
    from datetime import datetime
    return datetime.now(zone).date()


# PMax is a closed, atomic creation graph. Legacy operation admission stays unchanged.
PMAX_AUTOMATIONS = (
    'FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION', 'TEXT_ASSET_AUTOMATION',
    'GENERATE_IMAGE_EXTRACTION', 'GENERATE_IMAGE_ENHANCEMENT',
    'GENERATE_ENHANCED_YOUTUBE_VIDEOS',
)
PMAX_TEXT_ROLES = {'HEADLINE': (3, 15, 30, 15), 'LONG_HEADLINE': (1, 5, 90, 90),
                   'DESCRIPTION': (2, 5, 90, 60), 'BUSINESS_NAME': (1, 1, 25, 25)}
PMAX_IMAGE_ROLES = ('MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE', 'LOGO')
PMAX_ASSET_GROUP_ROLES = (
    'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION',
    'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE',
)
PMAX_ASSET_GROUP_ROLE_LIMITS = {
    **{role: PMAX_TEXT_ROLES[role][:2]
       for role in ('HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION')},
    'MARKETING_IMAGE': (1, 20), 'SQUARE_MARKETING_IMAGE': (1, 20),
}
PMAX_FIELD_NUMBERS = {'HEADLINE': '2', 'LONG_HEADLINE': '17', 'DESCRIPTION': '3',
                      'BUSINESS_NAME': '18', 'MARKETING_IMAGE': '5',
                      'SQUARE_MARKETING_IMAGE': '19', 'LOGO': '21'}
PMAX_KINDS = {**_CREATED_KINDS, 'asset_group': 'assetGroups',
              'asset_group_asset': 'assetGroupAssets'}


def pmax_id(value):
    from .rails import RailViolation
    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]*', value):
        raise RailViolation('PMax IDs must be canonical positive numeric strings')
    return numeric_id(value)


def pmax_texts(role, texts):
    from .rails import RailViolation, check_content
    minimum, maximum, limit, short = PMAX_TEXT_ROLES[role]
    if type(texts) is not list or not minimum <= len(texts) <= maximum:
        raise RailViolation(f'{role} requires {minimum} to {maximum} texts')
    for value in texts:
        rsa_text(value, limit)
    if len(set(texts)) != len(texts):
        raise RailViolation(f'{role} texts must be unique')
    for value in texts:
        try:
            rsa_text(value, short)
            break
        except RailViolation:
            pass
    else:
        raise RailViolation(f'{role} requires at least one text within {short} weighted characters')
    check_content(texts)
    return texts


def pmax_url(value):
    from . import settings
    from .rails import RailViolation
    if not settings.advertiser_domain() or type(value) is not str or not value.startswith('https://'):
        raise RailViolation('PMax requires a configured advertiser_domain and one HTTPS final URL')
    return rsa_url(value)


def _pmax_owned(rn, kind, cid):
    from .rails import RailViolation
    suffix = r'[1-9][0-9]*'
    if kind in {'campaignAssets', 'assetGroupAssets'}:
        suffix += r'~[1-9][0-9]*~[1-9][0-9]*'
    elif kind == 'campaignCriteria':
        suffix += r'~[1-9][0-9]*'
    if type(rn) is not str or not re.fullmatch(rf'customers/{cid}/{kind}/{suffix}', rn):
        raise RailViolation('PMax resource identity or owner is malformed')
    for part in rn.rsplit('/', 1)[1].split('~'):
        pmax_id(part)


def _pmax_integer(value):
    from .rails import RailViolation
    if type(value) is int and value > 0:
        return value
    if type(value) is str and re.fullmatch(r'[1-9][0-9]*', value):
        return int(value)
    raise RailViolation('PMax image metadata requires explicit positive integers')


def pmax_image(state, cid, role, rn=None):
    """Canonical immutable metadata, never image bytes or sparse defaults."""
    from .rails import RailViolation
    if type(state) is not dict:
        raise RailViolation('PMax image metadata unreadable')
    identity = state.get('resource_name')
    _pmax_owned(identity, 'assets', cid)
    if rn is not None and identity != rn:
        raise RailViolation('PMax image metadata identity mismatch')
    if _inventory_enum('AssetTypeEnum', state.get('type_', state.get('type'))) != 'IMAGE':
        raise RailViolation('PMax creative must be an IMAGE asset')
    metadata = state.get('image_asset')
    if type(metadata) is not dict or type(metadata.get('full_size')) is not dict:
        raise RailViolation('PMax image metadata incomplete')
    mime = _inventory_enum('MimeTypeEnum', metadata.get('mime_type'))
    size = _pmax_integer(metadata.get('file_size'))
    width = _pmax_integer(metadata['full_size'].get('width_pixels'))
    height = _pmax_integer(metadata['full_size'].get('height_pixels'))
    if mime not in {'IMAGE_JPEG', 'IMAGE_PNG'} or size > 5_000_000:
        raise RailViolation('PMax image requires JPEG/PNG at most 5000000 bytes')
    if role == 'MARKETING_IMAGE':
        valid = width >= 600 and height >= 314 and (width * 100 == height * 191 or width * 314 == height * 600)
    else:
        valid = width == height and width >= (300 if role == 'SQUARE_MARKETING_IMAGE' else 128)
    if role not in PMAX_IMAGE_ROLES or not valid:
        raise RailViolation('PMax image dimensions fail the conservative role requirements')
    return {'resource_name': identity, 'type_': 'IMAGE', 'image_asset': {
        'mime_type': mime, 'file_size': size,
        'full_size': {'width_pixels': width, 'height_pixels': height}}}


def pmax_image_state(cid, role, rn):
    _pmax_owned(rn, 'assets', cid)
    fields = ['resource_name', 'type', 'image_asset.mime_type', 'image_asset.file_size',
              'image_asset.full_size.width_pixels', 'image_asset.full_size.height_pixels']
    rows = gaql_all('SELECT ' + ', '.join('asset.' + key for key in fields)
                    + f" FROM asset WHERE asset.resource_name = '{rn}'", cid)
    from .rails import RailViolation
    if len(rows) != 1:
        raise RailViolation('PMax image metadata missing or duplicate')
    return pmax_image(dict(_creation_row(rows[0], 'asset')), cid, role, rn)


def pmax_creation_state(cid, name, geo_ids, language_ids, images):
    from .rails import RailViolation
    account = _creation_account(cid, include_manager=True, include_status=True)
    if account['manager'] is not False:
        raise RailViolation('PMax requires an own non-manager client account')
    collisions = (_creation_collisions(cid, 'campaign', name, strict_owner=True)
                  + _creation_collisions(cid, 'campaign_budget', name, strict_owner=True))
    if collisions:
        raise RailViolation('requested campaign or budget name already exists')
    return {'account': account, 'collisions': collisions,
            'locations': _creation_constants(cid, 'geo_target_constant', geo_ids),
            'languages': _creation_constants(cid, 'language_constant', language_ids),
            'images': {role: pmax_image_state(cid, role, rn) for role, rn in images.items()}}


def _pmax_enum_exact(enum_type, value, allowed):
    """Installed enum membership with a caller-owned allowed set, including UNSPECIFIED."""
    from google.ads.googleads.v25 import enums

    from .rails import RailViolation
    enum = getattr(getattr(enums, enum_type), enum_type.removesuffix('Enum'))
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RailViolation('PMax parent enum is unreadable')
    try:
        name = enum[value].name if isinstance(value, str) else enum(value).name
    except (KeyError, ValueError, TypeError):
        raise RailViolation('PMax parent enum is unreadable') from None
    if name not in allowed:
        raise RailViolation('PMax parent enum is outside the supported contract')
    return name


def _pmax_raw_dict(row):
    if isinstance(row, dict):
        return row
    try:
        return type(row).to_dict(row)
    except (AttributeError, TypeError, ValueError):
        from .rails import RailViolation
        raise RailViolation('PMax parent row is unreadable') from None


def _pmax_asset_group_parent_brand_proof(cid, campaign_id):
    """Complete immutable parent and brand proof; raw proto proves setting absence."""
    from .rails import RailViolation, check_content
    cid, campaign_id = pmax_id(cid), pmax_id(campaign_id)
    campaign_rn = campaign_path(cid, campaign_id)
    account = _creation_account(cid, include_manager=True, include_status=True)
    if account['manager'] is not False:
        raise RailViolation('PMax asset groups require an own non-manager client account')
    fields = [
        'resource_name', 'id', 'name', 'status', 'advertising_channel_type',
        'advertising_channel_sub_type', 'bidding_strategy_type', 'bidding_strategy',
        'maximize_conversions.target_cpa_micros', 'brand_guidelines_enabled',
        'asset_automation_settings', 'contains_eu_political_advertising',
        'geo_target_type_setting.positive_geo_target_type',
        'geo_target_type_setting.negative_geo_target_type',
        'shopping_setting.merchant_id', 'shopping_setting.feed_label',
        'shopping_setting.disable_product_feed', 'shopping_setting.use_vehicle_inventory',
        'travel_campaign_settings.travel_account_id', 'hotel_setting.hotel_center_id',
        'hotel_setting.disable_hotel_setting', 'hotel_property_asset_set']
    rows = _scan_rows('SELECT ' + ', '.join('campaign.' + field for field in fields)
                      + f' FROM campaign WHERE campaign.id = {campaign_id}', cid)
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('PMax parent campaign must be one complete raw provider row')
    raw_campaign = rows[0].campaign
    try:
        absent = all(not raw_campaign._pb.HasField(field) for field in (
            'shopping_setting', 'travel_campaign_settings', 'hotel_setting',
            'hotel_property_asset_set'))
    except (AttributeError, ValueError):
        raise RailViolation('PMax retail/travel setting presence is unreadable') from None
    if not absent:
        raise RailViolation('retail, vehicle, travel and hotel PMax parents are unsupported')
    state = dict(_creation_row(_pmax_raw_dict(rows[0]), 'campaign'))
    state['status'] = _pmax_enum_exact('CampaignStatusEnum', state.get('status'), {'PAUSED'})
    state['advertising_channel_type'] = _pmax_enum_exact(
        'AdvertisingChannelTypeEnum', state.get('advertising_channel_type'), {'PERFORMANCE_MAX'})
    state['advertising_channel_sub_type'] = _pmax_enum_exact(
        'AdvertisingChannelSubTypeEnum', state.get('advertising_channel_sub_type'),
        {'UNSPECIFIED'})
    state['bidding_strategy_type'] = _pmax_enum_exact(
        'BiddingStrategyTypeEnum', state.get('bidding_strategy_type'), {'MAXIMIZE_CONVERSIONS'})
    state['contains_eu_political_advertising'] = _pmax_enum_exact(
        'EuPoliticalAdvertisingStatusEnum', state.get('contains_eu_political_advertising'),
        set(POLITICAL_DECLARATIONS.values()))
    geo = state.get('geo_target_type_setting')
    if type(geo) is not dict:
        raise RailViolation('PMax parent geography options are incomplete')
    state['geo_target_type_setting'] = {
        key: _pmax_enum_exact(enum, geo.get(key), {'PRESENCE'}) for key, enum in (
            ('positive_geo_target_type', 'PositiveGeoTargetTypeEnum'),
            ('negative_geo_target_type', 'NegativeGeoTargetTypeEnum'))}
    automations = state.get('asset_automation_settings')
    if type(automations) is not list or len(automations) != len(PMAX_AUTOMATIONS):
        raise RailViolation('PMax parent automation settings are incomplete')
    decoded = []
    for item in automations:
        if type(item) is not dict or set(item) != {'asset_automation_type', 'asset_automation_status'}:
            raise RailViolation('PMax parent automation setting is malformed')
        decoded.append({
            'asset_automation_type': _pmax_enum_exact(
                'AssetAutomationTypeEnum', item['asset_automation_type'], set(PMAX_AUTOMATIONS)),
            'asset_automation_status': _pmax_enum_exact(
                'AssetAutomationStatusEnum', item['asset_automation_status'], {'OPTED_OUT'})})
    by_type = {item['asset_automation_type']: item for item in decoded}
    if set(by_type) != set(PMAX_AUTOMATIONS):
        raise RailViolation('PMax parent automation types are missing or duplicated')
    target = state.get('maximize_conversions')
    if (state.get('resource_name') != campaign_rn or str(state.get('id')) != campaign_id
            or type(state.get('name')) is not str or not state['name'].strip()
            or state.get('brand_guidelines_enabled') is not True
            or state.get('bidding_strategy', '') != '' or type(target) is not dict
            or 'target_cpa_micros' not in target
            or any(key != 'target_cpa_micros' and value not in (0, '0')
                   for key, value in target.items())):
        raise RailViolation('PMax parent identity, branding or bidding is unreadable')
    check_content([creation_name(state['name'])])
    target = {'target_cpa_micros': _pmax_integer(target['target_cpa_micros'])}
    parent = {key: state[key] for key in (
        'resource_name', 'id', 'name', 'status', 'advertising_channel_type',
        'advertising_channel_sub_type', 'bidding_strategy_type')}
    parent.update(bidding_strategy='', maximize_conversions=target,
                  brand_guidelines_enabled=True,
                  asset_automation_settings=[by_type[kind] for kind in PMAX_AUTOMATIONS],
                  contains_eu_political_advertising=state['contains_eu_political_advertising'],
                  geo_target_type_setting=state['geo_target_type_setting'],
                  retail_travel_settings_absent=True)
    brand_fields = ['resource_name', 'campaign', 'asset', 'field_type', 'status']
    brand_rows = _scan_rows(
        'SELECT ' + ', '.join('campaign_asset.' + field for field in brand_fields)
        + ', asset.resource_name, asset.type, asset.text_asset.text, asset.image_asset.mime_type, '
          'asset.image_asset.file_size, asset.image_asset.full_size.width_pixels, '
          'asset.image_asset.full_size.height_pixels FROM campaign_asset '
        + f"WHERE campaign_asset.campaign = '{campaign_rn}' AND campaign_asset.status != 'REMOVED' "
          "AND campaign_asset.field_type IN ('BUSINESS_NAME', 'LOGO')", cid)
    branding, seen = [], set()
    for row in brand_rows:
        data = _pmax_raw_dict(row)
        link, asset = _creation_row(data, 'campaign_asset'), _creation_row(data, 'asset')
        role = _pmax_enum_exact('AssetFieldTypeEnum', link.get('field_type'), {'BUSINESS_NAME', 'LOGO'})
        status = _pmax_enum_exact('AssetLinkStatusEnum', link.get('status'), {'ENABLED', 'PAUSED'})
        if role in seen or link.get('campaign') != campaign_rn:
            raise RailViolation('PMax inherited branding is duplicate or belongs to another parent')
        _pmax_owned(link.get('resource_name'), 'campaignAssets', cid)
        _pmax_owned(link.get('asset'), 'assets', cid)
        if asset.get('resource_name') != link['asset']:
            raise RailViolation('PMax inherited brand asset identity mismatch')
        parts = link['resource_name'].rsplit('/', 1)[1].split('~')
        if parts != [campaign_id, link['asset'].rsplit('/', 1)[1], PMAX_FIELD_NUMBERS[role]]:
            raise RailViolation('PMax inherited brand association identity mismatch')
        if role == 'BUSINESS_NAME':
            if _pmax_enum_exact('AssetTypeEnum', asset.get('type_', asset.get('type')), {'TEXT'}) != 'TEXT':
                raise RailViolation('PMax inherited business name must be text')
            content = asset.get('text_asset', {}).get('text')
            pmax_texts(role, [content])
        else:
            content = pmax_image(asset, cid, 'LOGO', link['asset'])
        branding.append({'resource_name': link['resource_name'], 'campaign': campaign_rn,
                         'asset': link['asset'], 'field_type': role, 'status': status,
                         'content': content})
        seen.add(role)
    if seen != {'BUSINESS_NAME', 'LOGO'}:
        raise RailViolation('PMax parent requires exactly one business name and one logo')
    proof = {'account': account, 'parent': parent,
             'branding': sorted(branding, key=lambda item: item['field_type'])}
    _validate_pmax_parent_brand_proof(proof, cid, campaign_rn)
    return proof


def pmax_asset_group_parent_proof(cid, campaign_id, images):
    """Creation-only parent/brand proof plus its exact two marketing images."""
    proof = _pmax_asset_group_parent_brand_proof(cid, campaign_id)
    if type(images) is not dict or set(images) != {'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'}:
        from .rails import RailViolation
        raise RailViolation('PMax asset-group creation requires exactly two image references')
    proof['images'] = {role: pmax_image_state(cid, role, rn) for role, rn in images.items()}
    _validate_pmax_parent_proof(proof, pmax_id(cid), campaign_path(cid, campaign_id))
    return proof


def pmax_asset_group_state(cid, campaign_id, name, images):
    """Draft fingerprint adds a complete existing-name inventory to immutable parent proof."""
    cid, campaign_id = pmax_id(cid), pmax_id(campaign_id)
    campaign_rn = campaign_path(cid, campaign_id)
    proof = pmax_asset_group_parent_proof(cid, campaign_id, images)
    groups = _pmax_asset_group_inventory(cid, campaign_rn)
    if any(item['name'] == name for item in groups):
        from .rails import RailViolation
        raise RailViolation('requested asset-group name already exists under this campaign')
    return {'parent_proof': proof, 'asset_groups': groups}


def _pmax_asset_group_inventory(cid, campaign_rn):
    """Read and validate every nonremoved group under one exact owned campaign."""
    from .rails import RailViolation
    cid = pmax_id(cid)
    _pmax_owned(campaign_rn, 'campaigns', cid)
    rows = _scan_rows(
        'SELECT asset_group.resource_name, asset_group.id, asset_group.campaign, '
        'asset_group.name, asset_group.status FROM asset_group '
        + f"WHERE asset_group.campaign = '{campaign_rn}' AND asset_group.status != 'REMOVED'", cid)
    groups, seen = [], set()
    for row in rows:
        item = dict(_creation_row(_pmax_raw_dict(row), 'asset_group'))
        rn = item.get('resource_name')
        _pmax_owned(rn, 'assetGroups', cid)
        status = _pmax_enum_exact('AssetGroupStatusEnum', item.get('status'), {'ENABLED', 'PAUSED'})
        if (rn in seen or str(item.get('id')) != rn.rsplit('/', 1)[1]
                or item.get('campaign') != campaign_rn or type(item.get('name')) is not str
                or not item['name'].strip()):
            raise RailViolation('PMax asset-group inventory is malformed, foreign or duplicate')
        groups.append({'resource_name': rn, 'id': str(item['id']), 'campaign': campaign_rn,
                       'name': item['name'], 'status': status})
        seen.add(rn)
    return sorted(groups, key=lambda item: item['resource_name'])


def _pmax_asset_group_target_state(cid, group_id):
    """Read one populated v25 row so empty alternate destinations are observed defaults."""
    from .rails import RailViolation, check_content
    cid, group_id = pmax_id(cid), pmax_id(group_id)
    rn = asset_group_path(cid, group_id)
    fields = ('resource_name', 'id', 'campaign', 'name', 'status', 'final_urls',
              'final_mobile_urls', 'path1', 'path2')
    rows = _scan_rows('SELECT ' + ', '.join('asset_group.' + field for field in fields)
                      + f' FROM asset_group WHERE asset_group.id = {group_id}', cid)
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('PMax target asset group must be one complete raw provider row')
    try:
        raw = rows[0].asset_group
        state = {'resource_name': raw.resource_name, 'id': str(raw.id),
                 'campaign': raw.campaign, 'name': raw.name,
                 'status': _pmax_enum_exact('AssetGroupStatusEnum', raw.status, {'PAUSED'}),
                 'final_urls': list(raw.final_urls),
                 'final_mobile_urls': list(raw.final_mobile_urls),
                 'path1': raw.path1, 'path2': raw.path2}
    except (AttributeError, TypeError, ValueError):
        raise RailViolation('PMax target asset-group fields are unreadable') from None
    _pmax_owned(state['resource_name'], 'assetGroups', cid)
    _pmax_owned(state['campaign'], 'campaigns', cid)
    if (state['resource_name'] != rn or state['id'] != group_id
            or type(state['name']) is not str or not state['name'].strip()
            or type(state['final_urls']) is not list or len(state['final_urls']) != 1
            or state['final_mobile_urls'] != [] or state['path1'] != '' or state['path2'] != ''):
        raise RailViolation('PMax target identity, status or destinations are unsupported')
    check_content([creation_name(state['name'])])
    pmax_url(state['final_urls'][0])
    return state


def pmax_asset_group_update_state(cid, group_id, proposed_name=None):
    """Exact target, parent/brand proof and complete sibling inventory for one update."""
    from .rails import RailViolation, check_content
    cid, group_id = pmax_id(cid), pmax_id(group_id)
    target = _pmax_asset_group_target_state(cid, group_id)
    proof = _pmax_asset_group_parent_brand_proof(
        cid, target['campaign'].rsplit('/', 1)[1])
    groups = _pmax_asset_group_inventory(cid, target['campaign'])
    for item in groups:
        check_content([creation_name(item['name'])])
    target_inventory = next((item for item in groups
                             if item['resource_name'] == target['resource_name']), None)
    expected_inventory = {key: target[key] for key in
                          ('resource_name', 'id', 'campaign', 'name', 'status')}
    if target_inventory != expected_inventory:
        raise RailViolation('PMax target disagrees with complete asset-group inventory')
    if (proposed_name is not None and any(
            item['resource_name'] != target['resource_name'] and item['name'] == proposed_name
            for item in groups)):
        raise RailViolation('requested asset-group name already exists under this campaign')
    return {'parent_proof': proof, 'target': target, 'asset_groups': groups}


def _pmax_asset_group_content(cid, role, asset, rn):
    """Canonical content proof for one selected asset and one supported role."""
    from .rails import RailViolation, check_content
    if type(asset) is not dict or asset.get('resource_name') != rn:
        raise RailViolation('PMax requested or linked asset identity is unreadable')
    _pmax_owned(rn, 'assets', cid)
    expected_type = ('TEXT' if role in {'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION'}
                     else 'IMAGE')
    asset_type = _pmax_enum_exact('AssetTypeEnum', asset.get('type_', asset.get('type')),
                                  {expected_type})
    if asset_type == 'TEXT':
        payload = asset.get('text_asset')
        if type(payload) is not dict or set(payload) != {'text'} or type(payload['text']) is not str:
            raise RailViolation('PMax text asset content is incomplete')
        rsa_text(payload['text'], PMAX_TEXT_ROLES[role][2])
        check_content([payload['text']])
        return payload['text']
    return pmax_image(asset, cid, role, rn)


def pmax_asset_group_role_counts(items):
    return {role: sum(item['field_type'] == role for item in items)
            for role in PMAX_ASSET_GROUP_ROLES}


def _validate_pmax_asset_group_creatives(cid, group_rn, existing, requested,
                                         *, require_complete=True):
    """Validate exact canonical existing/requested proofs and their combined contract."""
    from .rails import RailViolation, check_content
    cid = pmax_id(cid)
    _pmax_owned(group_rn, 'assetGroups', cid)
    if type(existing) is not list or type(requested) is not list:
        raise RailViolation('PMax asset-group creative proof must be lists')
    pairs, link_names = set(), set()
    by_role = {role: [] for role in PMAX_ASSET_GROUP_ROLES}
    existing_fields = {'resource_name', 'asset_group', 'asset', 'field_type', 'status',
                       'content'}
    requested_fields = {'asset', 'field_type', 'content'}
    for is_existing, collection in ((True, existing), (False, requested)):
        for item in collection:
            required = existing_fields if is_existing else requested_fields
            if type(item) is not dict or set(item) != required:
                raise RailViolation('PMax asset-group creative proof is malformed')
            role, asset = item['field_type'], item['asset']
            if role not in PMAX_ASSET_GROUP_ROLES:
                raise RailViolation('PMax asset-group creative role is unsupported')
            _pmax_owned(asset, 'assets', cid)
            pair = (asset, role)
            if pair in pairs:
                raise RailViolation('PMax asset-group asset pair is duplicated or already linked')
            pairs.add(pair)
            if is_existing:
                rn = item['resource_name']
                _pmax_owned(rn, 'assetGroupAssets', cid)
                if (rn in link_names or item['asset_group'] != group_rn
                        or item['status'] != 'PAUSED'
                        or rn.rsplit('/', 1)[1].split('~') != [
                            group_rn.rsplit('/', 1)[1], asset.rsplit('/', 1)[1],
                            PMAX_FIELD_NUMBERS[role]]):
                    raise RailViolation('PMax asset-group link proof is malformed or duplicated')
                link_names.add(rn)
            content = item['content']
            if role in {'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION'}:
                if type(content) is not str:
                    raise RailViolation('PMax text proof is malformed')
                rsa_text(content, PMAX_TEXT_ROLES[role][2])
                check_content([content])
            elif pmax_image(content, cid, role, asset) != content:
                raise RailViolation('PMax image proof is malformed')
            by_role[role].append(content)
    if existing != sorted(existing, key=lambda item: item['resource_name']):
        raise RailViolation('PMax existing asset-group links are not stably sorted')
    if requested != sorted(requested, key=lambda item: (item['asset'], item['field_type'])):
        raise RailViolation('PMax requested asset proofs are not stably sorted')
    for role, values in by_role.items():
        minimum, maximum = PMAX_ASSET_GROUP_ROLE_LIMITS[role]
        if len(values) > maximum or require_complete and len(values) < minimum:
            raise RailViolation(f'combined {role} inventory requires {minimum} to {maximum} assets')
        if role in {'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION'}:
            if len(values) != len(set(values)):
                raise RailViolation(f'combined {role} text content must be unique')
            if require_complete:
                pmax_texts(role, values)
    return True


def _pmax_asset_group_link_inventory(cid, group_rn):
    """Complete scan of every nonremoved link joined to its fully selected asset."""
    from .rails import RailViolation
    fields = ('resource_name', 'asset_group', 'asset', 'field_type', 'status')
    query = ('SELECT ' + ', '.join('asset_group_asset.' + field for field in fields)
             + ', asset.resource_name, asset.type, asset.text_asset.text, '
               'asset.image_asset.mime_type, asset.image_asset.file_size, '
               'asset.image_asset.full_size.width_pixels, '
               'asset.image_asset.full_size.height_pixels FROM asset_group_asset '
             + f"WHERE asset_group_asset.asset_group = '{group_rn}' "
               "AND asset_group_asset.status != 'REMOVED'")
    rows = _scan_rows(query, cid)
    inventory = []
    for row in rows:
        if isinstance(row, dict):
            raise RailViolation('PMax asset-group links require complete raw provider rows')
        data = _pmax_raw_dict(row)
        link, asset = (dict(_creation_row(data, key))
                       for key in ('asset_group_asset', 'asset'))
        role = _pmax_enum_exact('AssetFieldTypeEnum', link.get('field_type'),
                                set(PMAX_ASSET_GROUP_ROLES))
        status = _pmax_enum_exact('AssetLinkStatusEnum', link.get('status'), {'PAUSED'})
        rn, asset_rn = link.get('resource_name'), link.get('asset')
        content = _pmax_asset_group_content(cid, role, asset, asset_rn)
        inventory.append({'resource_name': rn, 'asset_group': link.get('asset_group'),
                          'asset': asset_rn, 'field_type': role, 'status': status,
                          'content': content})
    inventory.sort(key=lambda item: item['resource_name'])
    _validate_pmax_asset_group_creatives(cid, group_rn, inventory, [],
                                         require_complete=False)
    return inventory


def _pmax_requested_asset_proofs(cid, additions):
    """Read each distinct requested owned asset exactly once, then prove every role."""
    from .rails import RailViolation
    if type(additions) is not list:
        raise RailViolation('PMax requested asset proof input is malformed')
    asset_names = sorted({item['asset'] for item in additions})
    for rn in asset_names:
        _pmax_owned(rn, 'assets', cid)
    fields = ('resource_name', 'type', 'text_asset.text', 'image_asset.mime_type',
              'image_asset.file_size', 'image_asset.full_size.width_pixels',
              'image_asset.full_size.height_pixels')
    quoted = ', '.join(repr(rn) for rn in asset_names)
    rows = _scan_rows('SELECT ' + ', '.join('asset.' + field for field in fields)
                      + f' FROM asset WHERE asset.resource_name IN ({quoted})', cid)
    assets = {}
    for row in rows:
        if isinstance(row, dict):
            raise RailViolation('PMax requested assets require complete raw provider rows')
        asset = dict(_creation_row(_pmax_raw_dict(row), 'asset'))
        rn = asset.get('resource_name')
        _pmax_owned(rn, 'assets', cid)
        if rn in assets or rn not in asset_names:
            raise RailViolation('PMax requested asset identities are foreign or duplicated')
        assets[rn] = asset
    if set(assets) != set(asset_names):
        raise RailViolation('PMax requested asset scan is missing an exact identity')
    proofs = [{'asset': item['asset'], 'field_type': item['field_type'],
               'content': _pmax_asset_group_content(
                   cid, item['field_type'], assets[item['asset']], item['asset'])}
              for item in additions]
    return sorted(proofs, key=lambda item: (item['asset'], item['field_type']))


def pmax_asset_group_asset_state(cid, group_id, additions=None):
    """Exact group/parent/link state plus requested existing-asset proofs."""
    cid, group_id = pmax_id(cid), pmax_id(group_id)
    target = _pmax_asset_group_target_state(cid, group_id)
    proof = _pmax_asset_group_parent_brand_proof(
        cid, target['campaign'].rsplit('/', 1)[1])
    existing = _pmax_asset_group_link_inventory(cid, target['resource_name'])
    requested = [] if additions is None else _pmax_requested_asset_proofs(cid, additions)
    _validate_pmax_asset_group_creatives(cid, target['resource_name'], existing, requested,
                                         require_complete=True)
    return {'parent_proof': proof, 'target': target, 'existing_assets': existing,
            'requested_assets': requested}


def pmax_asset_group_asset_path(cid, group_id, asset_id, field_type_number):
    from google.ads.googleads.v25.services.services.asset_group_asset_service import (
        AssetGroupAssetServiceClient,
    )
    return AssetGroupAssetServiceClient.asset_group_asset_path(
        pmax_id(cid), pmax_id(group_id), pmax_id(asset_id), str(field_type_number))


def pmax_temporary_path(cid, kind, negative_id):
    from google.ads.googleads.v25.services.services.asset_group_service import (
        AssetGroupServiceClient,
    )
    from google.ads.googleads.v25.services.services.asset_service import (
        AssetServiceClient,
    )
    helper = {'assetGroups': AssetGroupServiceClient.asset_group_path,
              'assets': AssetServiceClient.asset_path}[kind]
    return helper(pmax_id(cid), str(negative_id))


def pmax_checks(cid, operations, images, *, parent_proof=None, asset_group=False):
    """Exact ordered proof descriptors derived from the same reviewed operations."""
    import copy
    checks = []
    for index, op in enumerate(operations):
        values = copy.deepcopy(op.operation['create'])
        entity = _snake(op.service.removesuffix('Service'))
        expected = {key: value for key, value in values.items() if key != 'resource_name'}
        if entity == 'campaign':
            expected['bidding_strategy_type'] = 'MAXIMIZE_CONVERSIONS'
        if entity == 'campaign_criterion':
            expected['status'] = 'ENABLED'
        if entity == 'asset':
            expected['type_'] = 'TEXT'
        checks.append({'pmax': True, 'result_index': index, 'entity_type': entity,
                       'customer_id': cid, 'definition': values.get('resource_name'),
                       'expected': expected})
    checks[0]['images'] = copy.deepcopy(images)
    if asset_group:
        checks[0]['pmax_asset_group'] = True
        checks[0]['parent_proof'] = copy.deepcopy(parent_proof)
    return checks


def demand_gen_checks(cid, operations, family):
    """Exact ordered descriptors rebuilt from the closed Demand Gen graph."""
    import copy
    checks = []
    for index, op in enumerate(operations):
        values = copy.deepcopy(op.operation['create'])
        entity = _snake(op.service.removesuffix('Service'))
        expected = {key: value for key, value in values.items() if key != 'resource_name'}
        if entity == 'campaign_budget':
            expected.update(name=values.get('name', operations[1].operation['create']['name']),
                            status='ENABLED', total_amount_micros=0,
                            aligned_bidding_strategy_id=0, reference_count=1)
        elif entity == 'campaign':
            expected.update(
                bidding_strategy_type='MAXIMIZE_CONVERSIONS', bidding_strategy='',
                advertising_channel_sub_type='UNSPECIFIED',
                maximize_conversions={'target_cpa_micros': 0,
                                      'cpc_bid_ceiling_micros': 0,
                                      'cpc_bid_floor_micros': 0},
                shopping_setting={'merchant_id': 0, 'feed_label': '',
                                  'advertising_partner_ids': [], 'use_vehicle_inventory': False},
                travel_campaign_settings={'travel_account_id': 0},
                hotel_setting={'hotel_center_id': 0}, hotel_property_asset_set='')
        else:
            expected['status'] = 'ENABLED'
        checks.append({
            'demand_gen': True, 'result_index': index, 'entity_type': entity,
            'customer_id': cid, 'definition': values.get('resource_name'),
            'expected': expected, 'demand_gen_family': copy.deepcopy(family)})
    if len(checks) > 1:
        checks[1]['budget_result_index'] = 0
    for check in checks[2:]:
        check['campaign_result_index'] = 1
    return checks


class _DemandGenContext:
    def __init__(self, plan):
        self.plan = plan


def validate_demand_gen_plan(plan):
    """Validate one independent Demand Gen budget, campaign and target graph."""
    from .rails import EntityMutationPlan, RailViolation, check_content
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True or type(plan.operations) is not list
            or not 4 <= len(plan.operations) <= 202 or type(plan.post_checks) is not list):
        raise RailViolation('invalid Demand Gen plan shape')
    cid, ops = pmax_id(plan.mutate_customer_id), plan.operations
    values = []
    for op in ops:
        if (type(op.operation) is not dict or set(op.operation) != {'create'}
                or type(op.operation['create']) is not dict or op.update_mask is not None):
            raise RailViolation('Demand Gen permits only exact create actions without masks')
        values.append(op.operation['create'])
    budget_rn, campaign_rn = creation_paths(cid)
    if ops[0].service != 'CampaignBudgetService':
        raise RailViolation('Demand Gen must begin with its dedicated budget')
    _validate_entity_create('CampaignBudget', values[0], cid, {budget_rn: 'campaignBudgets'})
    campaign = values[1]
    required = {'resource_name', 'name', 'campaign_budget', 'status',
                'advertising_channel_type', 'maximize_conversions',
                'demand_gen_campaign_settings', 'geo_target_type_setting',
                'contains_eu_political_advertising'}
    if (ops[1].service != 'CampaignService' or set(campaign) != required
            or campaign['resource_name'] != campaign_rn or campaign['campaign_budget'] != budget_rn
            or campaign['status'] != 'PAUSED'
            or campaign['advertising_channel_type'] != 'DEMAND_GEN'
            or campaign['maximize_conversions'] != {}
            or campaign['demand_gen_campaign_settings'] != {'upgraded_targeting': False}
            or campaign['geo_target_type_setting'] != SEARCH_GEO_OPTIONS
            or campaign['contains_eu_political_advertising'] not in POLITICAL_DECLARATIONS.values()):
        raise RailViolation('Demand Gen campaign schema or fixed settings are invalid')
    check_content([creation_name(campaign['name'])])
    targets = {'location': [], 'language': []}
    phase = 'location'
    for op, item in zip(ops[2:], values[2:]):
        if op.service != 'CampaignCriterionService':
            raise RailViolation('Demand Gen graph contains a foreign operation')
        role = 'location' if 'location' in item else 'language' if 'language' in item else None
        if role == 'location' and phase == 'language':
            raise RailViolation('Demand Gen locations must precede languages')
        if role == 'language':
            phase = 'language'
        field, prefix = (('geo_target_constant', 'geoTargetConstants') if role == 'location'
                         else ('language_constant', 'languageConstants')) if role else (None, None)
        if (role is None or set(item) != {'campaign', 'negative', role}
                or item['campaign'] != campaign_rn or item['negative'] is not False
                or type(item[role]) is not dict or set(item[role]) != {field}
                or type(item[role][field]) is not str
                or not re.fullmatch(rf'{prefix}/[1-9][0-9]*', item[role][field])
                or item[role][field] in targets[role]):
            raise RailViolation('Demand Gen targeting criterion is invalid')
        targets[role].append(item[role][field])
    if any(not 1 <= len(items) <= 100 for items in targets.values()):
        raise RailViolation('Demand Gen requires explicit unique locations and languages')
    family = {'strategy': 'MAXIMIZE_CONVERSIONS', 'upgraded_targeting_present': True,
              'upgraded_targeting': False, 'locations': targets['location'],
              'languages': targets['language']}
    if plan.post_checks != demand_gen_checks(cid, ops, family):
        raise RailViolation('Demand Gen result descriptors differ from the exact graph')
    return _DemandGenContext(plan)


class _PMaxContext:
    """A validated whole graph is required to build any PMax operation in isolation."""
    def __init__(self, plan):
        self.plan = plan


class _PMaxAssetGroupContext(_PMaxContext):
    pass


class _PMaxAssetGroupUpdateContext(_PMaxContext):
    pass


class _PMaxAssetGroupAssetAddContext(_PMaxContext):
    pass


class _PMaxAssetGroupAssetRemoveContext(_PMaxContext):
    pass


def _validate_pmax_asset_group_target_proof(target, cid, group_rn, campaign_rn):
    from .rails import RailViolation, check_content
    fields = {'resource_name', 'id', 'campaign', 'name', 'status', 'final_urls',
              'final_mobile_urls', 'path1', 'path2'}
    if (type(target) is not dict or set(target) != fields
            or target['resource_name'] != group_rn
            or target['id'] != group_rn.rsplit('/', 1)[1]
            or target['campaign'] != campaign_rn or target['status'] != 'PAUSED'
            or type(target['final_urls']) is not list or len(target['final_urls']) != 1
            or target['final_mobile_urls'] != [] or target['path1'] != ''
            or target['path2'] != ''):
        raise RailViolation('PMax target asset-group proof is malformed')
    _pmax_owned(group_rn, 'assetGroups', cid)
    _pmax_owned(campaign_rn, 'campaigns', cid)
    check_content([creation_name(target['name'])])
    pmax_url(target['final_urls'][0])


def validate_pmax_asset_group_asset_add_plan(plan):
    """Closed existing-asset link graph for one paused PMax asset group."""
    from .rails import EntityMutationPlan, RailViolation
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True
            or type(plan.operations) is not list or not 1 <= len(plan.operations) <= 65
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('invalid PMax asset-group asset-add plan shape')
    cid, check = pmax_id(plan.mutate_customer_id), plan.post_checks[0]
    required = {'pmax_asset_group_assets', 'customer_id', 'asset_group', 'campaign',
                'target', 'parent_proof', 'existing_assets', 'requested_assets',
                'additions'}
    if (type(check) is not dict or set(check) != required
            or check['pmax_asset_group_assets'] is not True
            or check['customer_id'] != cid):
        raise RailViolation('PMax asset-group asset-add proof descriptor is malformed')
    group_rn, campaign_rn = check['asset_group'], check['campaign']
    _validate_pmax_asset_group_target_proof(
        check['target'], cid, group_rn, campaign_rn)
    _validate_pmax_parent_brand_proof(check['parent_proof'], cid, campaign_rn)
    if type(check['additions']) is not list or len(check['additions']) != len(plan.operations):
        raise RailViolation('PMax asset-group additions do not match operation count')
    proof_by_pair = {}
    for item in check['requested_assets'] if type(check['requested_assets']) is list else ():
        if type(item) is not dict:
            raise RailViolation('PMax requested asset proof is malformed')
        pair = (item.get('asset'), item.get('field_type'))
        if pair in proof_by_pair:
            raise RailViolation('PMax requested asset proof is duplicated')
        proof_by_pair[pair] = item
    pairs = set()
    for op, addition in zip(plan.operations, check['additions']):
        if (type(addition) is not dict or set(addition) != {'asset', 'field_type', 'content'}
                or op.service != 'AssetGroupAssetService'
                or type(op.operation) is not dict or set(op.operation) != {'create'}
                or type(op.operation['create']) is not dict or op.update_mask is not None):
            raise RailViolation('PMax asset-group asset-add operation is malformed')
        values = op.operation['create']
        if (set(values) != {'asset_group', 'asset', 'field_type', 'status'}
                or values['asset_group'] != group_rn or values['status'] != 'PAUSED'
                or values['asset'] != addition['asset']
                or values['field_type'] != addition['field_type']):
            raise RailViolation('PMax asset-group asset-add operation differs from proof')
        pair = (addition['asset'], addition['field_type'])
        if pair in pairs or proof_by_pair.get(pair) != addition:
            raise RailViolation('PMax asset-group addition proof is missing or duplicated')
        pairs.add(pair)
    if pairs != set(proof_by_pair):
        raise RailViolation('PMax requested proof contains an unlinked asset')
    _validate_pmax_asset_group_creatives(
        cid, group_rn, check['existing_assets'], check['requested_assets'],
        require_complete=True)
    return _PMaxAssetGroupAssetAddContext(plan)


def validate_pmax_asset_group_asset_remove_plan(plan):
    """Closed proof for removing one paused link while retaining a valid full inventory."""
    from .rails import EntityMutationPlan, RailViolation
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True
            or type(plan.operations) is not list or len(plan.operations) != 1
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('invalid PMax asset-group asset-remove plan shape')
    cid, op, check = pmax_id(plan.mutate_customer_id), plan.operations[0], plan.post_checks[0]
    required = {'pmax_asset_group_asset_removal', 'customer_id', 'resource_name',
                'asset_group', 'campaign', 'field_type', 'target', 'parent_proof',
                'current_assets', 'target_link', 'selected_asset', 'remaining_assets'}
    if (type(check) is not dict or set(check) != required
            or check['pmax_asset_group_asset_removal'] is not True
            or check['customer_id'] != cid or op.service != 'AssetGroupAssetService'
            or type(op.operation) is not dict or set(op.operation) != {'remove'}
            or op.operation['remove'] != check['resource_name'] or op.update_mask is not None):
        raise RailViolation('PMax asset-group asset-remove descriptor or operation is malformed')
    group_rn, campaign_rn, role = check['asset_group'], check['campaign'], check['field_type']
    if role not in PMAX_ASSET_GROUP_ROLES:
        raise RailViolation('PMax asset-group removal role is unsupported')
    _validate_pmax_asset_group_target_proof(check['target'], cid, group_rn, campaign_rn)
    _validate_pmax_parent_brand_proof(check['parent_proof'], cid, campaign_rn)
    current, target, selected, remaining = (check[key] for key in
                                            ('current_assets', 'target_link',
                                             'selected_asset', 'remaining_assets'))
    _validate_pmax_asset_group_creatives(cid, group_rn, current, [], require_complete=True)
    _validate_pmax_asset_group_creatives(cid, group_rn, remaining, [], require_complete=True)
    if (type(target) is not dict or target.get('resource_name') != check['resource_name']
            or target.get('asset_group') != group_rn or target.get('field_type') != role
            or target.get('status') != 'PAUSED'
            or current.count(target) != 1
            or remaining != [item for item in current if item['resource_name'] != check['resource_name']]
            or type(selected) is not dict
            or selected != {key: target[key] for key in ('asset', 'field_type', 'content')}):
        raise RailViolation('PMax target, bare asset or remaining inventory proof is malformed')
    expected = pmax_asset_group_asset_path(
        cid, group_rn.rsplit('/', 1)[1], target['asset'].rsplit('/', 1)[1],
        PMAX_FIELD_NUMBERS[role])
    if expected != check['resource_name']:
        raise RailViolation('PMax removal compound connection identity is malformed')
    return _PMaxAssetGroupAssetRemoveContext(plan)


def validate_pmax_asset_group_update_plan(plan):
    """Closed one-operation update for one existing paused PMax asset group."""
    from .rails import EntityMutationPlan, RailViolation, check_content
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True
            or type(plan.operations) is not list or len(plan.operations) != 1
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('invalid PMax asset-group update plan shape')
    cid, op, check = pmax_id(plan.mutate_customer_id), plan.operations[0], plan.post_checks[0]
    if (op.service != 'AssetGroupService' or type(op.operation) is not dict
            or set(op.operation) != {'update'} or type(op.operation['update']) is not dict
            or type(op.update_mask) is not list
            or op.update_mask not in (['name'], ['final_urls'], ['name', 'final_urls'])):
        raise RailViolation('PMax asset-group update permits one exact update and mask')
    values = op.operation['update']
    expected_fields = {'resource_name', *op.update_mask}
    if set(values) != expected_fields:
        raise RailViolation('PMax asset-group update fields must exactly match its mask')
    rn = values.get('resource_name')
    _pmax_owned(rn, 'assetGroups', cid)
    if 'name' in values:
        check_content([creation_name(values['name'])])
    if 'final_urls' in values:
        if type(values['final_urls']) is not list or len(values['final_urls']) != 1:
            raise RailViolation('PMax asset-group update requires one final URL')
        pmax_url(values['final_urls'][0])
    required = {'pmax_asset_group_update', 'customer_id', 'resource_name', 'campaign',
                'before', 'expected', 'parent_proof', 'asset_groups'}
    if (type(check) is not dict or set(check) != required
            or check['pmax_asset_group_update'] is not True or check['customer_id'] != cid
            or check['resource_name'] != rn):
        raise RailViolation('PMax asset-group update proof descriptor is malformed')
    before, expected = check['before'], check['expected']
    record_fields = {'resource_name', 'id', 'campaign', 'name', 'status', 'final_urls',
                     'final_mobile_urls', 'path1', 'path2'}
    for item in (before, expected):
        if (type(item) is not dict or set(item) != record_fields
                or item['resource_name'] != rn or item['id'] != rn.rsplit('/', 1)[1]
                or item['campaign'] != check['campaign'] or item['status'] != 'PAUSED'
                or type(item['final_urls']) is not list or len(item['final_urls']) != 1
                or item['final_mobile_urls'] != [] or item['path1'] != '' or item['path2'] != ''):
            raise RailViolation('PMax asset-group before/after proof is malformed')
        check_content([creation_name(item['name'])])
        pmax_url(item['final_urls'][0])
    changed = {key for key in ('name', 'final_urls') if before[key] != expected[key]}
    if changed != set(op.update_mask) or any(expected[key] != before[key]
                                              for key in record_fields - changed):
        raise RailViolation('PMax asset-group proof changes fields outside the exact mask')
    if any(values[key] != expected[key] for key in op.update_mask):
        raise RailViolation('PMax asset-group payload differs from expected saved state')
    _pmax_owned(check['campaign'], 'campaigns', cid)
    _validate_pmax_parent_brand_proof(check['parent_proof'], cid, check['campaign'])
    inventory = check['asset_groups']
    if type(inventory) is not list:
        raise RailViolation('PMax asset-group inventory proof is malformed')
    target = None
    seen = set()
    for item in inventory:
        fields = {'resource_name', 'id', 'campaign', 'name', 'status'}
        if type(item) is not dict or set(item) != fields:
            raise RailViolation('PMax asset-group inventory proof is malformed')
        item_rn = item['resource_name']
        _pmax_owned(item_rn, 'assetGroups', cid)
        if (item_rn in seen or item['id'] != item_rn.rsplit('/', 1)[1]
                or item['campaign'] != check['campaign']
                or item['status'] not in {'ENABLED', 'PAUSED'}):
            raise RailViolation('PMax asset-group inventory proof is malformed')
        check_content([creation_name(item['name'])])
        seen.add(item_rn)
        if item_rn == rn:
            target = item
        elif item['name'] == expected['name']:
            raise RailViolation('requested asset-group name already exists under this campaign')
    if target != {key: before[key] for key in ('resource_name', 'id', 'campaign', 'name', 'status')}:
        raise RailViolation('PMax asset-group target disagrees with complete inventory')
    return _PMaxAssetGroupUpdateContext(plan)


def _validate_pmax_parent_brand_proof(proof, cid, campaign_rn):
    """Validate immutable existing campaign and inherited-brand evidence."""
    from .rails import RailViolation, check_content
    if type(proof) is not dict or set(proof) != {'account', 'parent', 'branding'}:
        raise RailViolation('PMax asset-group parent proof is malformed')
    account, parent, branding = (proof[key] for key in ('account', 'parent', 'branding'))
    if (type(account) is not dict or set(account) != {'id', 'currency_code', 'time_zone', 'status', 'manager'}
            or account['id'] != cid or account['status'] != 'ENABLED' or account['manager'] is not False
            or type(account['currency_code']) is not str
            or not re.fullmatch(r'[A-Z]{3}', account['currency_code'])
            or type(account['time_zone']) is not str or not account['time_zone']):
        raise RailViolation('PMax asset-group account proof is malformed')
    required = {'resource_name', 'id', 'name', 'status', 'advertising_channel_type',
                'advertising_channel_sub_type', 'bidding_strategy_type', 'bidding_strategy',
                'maximize_conversions', 'brand_guidelines_enabled', 'asset_automation_settings',
                'contains_eu_political_advertising', 'geo_target_type_setting',
                'retail_travel_settings_absent'}
    if (type(parent) is not dict or set(parent) != required or parent['resource_name'] != campaign_rn
            or parent['id'] != campaign_rn.rsplit('/', 1)[1] or parent['status'] != 'PAUSED'
            or parent['advertising_channel_type'] != 'PERFORMANCE_MAX'
            or parent['advertising_channel_sub_type'] != 'UNSPECIFIED'
            or parent['bidding_strategy_type'] != 'MAXIMIZE_CONVERSIONS'
            or parent['bidding_strategy'] != '' or parent['brand_guidelines_enabled'] is not True
            or parent['retail_travel_settings_absent'] is not True
            or parent['contains_eu_political_advertising'] not in POLITICAL_DECLARATIONS.values()
            or parent['geo_target_type_setting'] != SEARCH_GEO_OPTIONS
            or type(parent['maximize_conversions']) is not dict
            or set(parent['maximize_conversions']) != {'target_cpa_micros'}
            or parent['asset_automation_settings'] != [
                {'asset_automation_type': kind, 'asset_automation_status': 'OPTED_OUT'}
                for kind in PMAX_AUTOMATIONS]):
        raise RailViolation('PMax asset-group parent campaign proof is malformed')
    check_content([creation_name(parent['name'])])
    _payload_micros(parent['maximize_conversions']['target_cpa_micros'])
    if type(branding) is not list or [item.get('field_type') for item in branding] != ['BUSINESS_NAME', 'LOGO']:
        raise RailViolation('PMax asset-group inherited branding proof is malformed')
    for item in branding:
        if (type(item) is not dict or set(item) != {'resource_name', 'campaign', 'asset', 'field_type', 'status', 'content'}
                or item['campaign'] != campaign_rn or item['status'] not in {'ENABLED', 'PAUSED'}):
            raise RailViolation('PMax asset-group inherited branding proof is malformed')
        _pmax_owned(item['resource_name'], 'campaignAssets', cid)
        _pmax_owned(item['asset'], 'assets', cid)
        parts = item['resource_name'].rsplit('/', 1)[1].split('~')
        if parts != [campaign_rn.rsplit('/', 1)[1], item['asset'].rsplit('/', 1)[1],
                     PMAX_FIELD_NUMBERS[item['field_type']]]:
            raise RailViolation('PMax asset-group inherited branding identity is malformed')
        if item['field_type'] == 'BUSINESS_NAME':
            pmax_texts('BUSINESS_NAME', [item['content']])
        elif pmax_image(item['content'], cid, 'LOGO', item['asset']) != item['content']:
            raise RailViolation('PMax asset-group inherited logo proof is malformed')


def _validate_pmax_parent_proof(proof, cid, campaign_rn):
    """Creation wrapper keeps the exact two-image requirement closed and mandatory."""
    from .rails import RailViolation
    if type(proof) is not dict or set(proof) != {'account', 'parent', 'branding', 'images'}:
        raise RailViolation('PMax asset-group parent proof is malformed')
    base = {key: proof[key] for key in ('account', 'parent', 'branding')}
    _validate_pmax_parent_brand_proof(base, cid, campaign_rn)
    images = proof['images']
    if type(images) is not dict or set(images) != {'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'}:
        raise RailViolation('PMax asset-group image proof is malformed')
    for role, image in images.items():
        if pmax_image(image, cid, role) != image:
            raise RailViolation('PMax asset-group image proof is malformed')


def validate_pmax_asset_group_plan(plan):
    """Closed graph for one group under an existing campaign; never admits parent writes."""
    from .rails import EntityMutationPlan, RailViolation, check_content
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True or type(plan.operations) is not list
            or type(plan.post_checks) is not list or not plan.post_checks):
        raise RailViolation('invalid PMax asset-group plan shape')
    cid, ops = pmax_id(plan.mutate_customer_id), plan.operations
    values = []
    for op in ops:
        if (op.service not in {'AssetGroupService', 'AssetService', 'AssetGroupAssetService'}
                or type(op.operation) is not dict or set(op.operation) != {'create'}
                or type(op.operation['create']) is not dict or op.update_mask is not None):
            raise RailViolation('PMax asset-group graph permits only exact group, text and link creates')
        values.append(op.operation['create'])
    group_rn = pmax_temporary_path(cid, 'assetGroups', -3)
    group = values[0] if values else {}
    if (not ops or ops[0].service != 'AssetGroupService' or set(group) != {
            'resource_name', 'campaign', 'name', 'status', 'final_urls'}
            or group.get('resource_name') != group_rn or group.get('status') != 'PAUSED'
            or type(group.get('final_urls')) is not list or len(group['final_urls']) != 1):
        raise RailViolation('PMax asset-group graph must begin with one paused group')
    _pmax_owned(group.get('campaign'), 'campaigns', cid)
    check_content([creation_name(group['name'])])
    pmax_url(group['final_urls'][0])
    index, definitions, contents = 1, {}, set()
    while index < len(ops) and ops[index].service == 'AssetService':
        item = values[index]
        rn = pmax_temporary_path(cid, 'assets', -4 - len(definitions))
        if (set(item) != {'resource_name', 'text_asset'} or item.get('resource_name') != rn
                or type(item.get('text_asset')) is not dict or set(item['text_asset']) != {'text'}):
            raise RailViolation('PMax asset-group text definitions are malformed')
        text = rsa_text(item['text_asset']['text'], 90)
        if text in contents:
            raise RailViolation('PMax asset-group text definitions must be deduplicated')
        definitions[rn], index = text, index + 1
        contents.add(text)
    roles = {role: [] for role in ('HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION')}
    images, consumed = {}, set()
    for op, item in zip(ops[index:], values[index:]):
        role = item.get('field_type')
        if (op.service != 'AssetGroupAssetService'
                or set(item) != {'asset_group', 'asset', 'field_type', 'status'}
                or item['asset_group'] != group_rn or item['status'] != 'PAUSED'
                or role not in {*roles, 'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'}):
            raise RailViolation('PMax asset-group link is malformed')
        asset = item['asset']
        if role in roles:
            if asset not in definitions or (asset, role) in consumed:
                raise RailViolation('PMax asset-group text link is dangling or duplicated')
            roles[role].append(definitions[asset])
            consumed.add((asset, role))
        else:
            _pmax_owned(asset, 'assets', cid)
            if role in images:
                raise RailViolation('PMax asset-group image role is duplicated')
            images[role] = asset
    for role, texts in roles.items():
        pmax_texts(role, texts)
    if {rn for rn, _ in consumed} != set(definitions) or set(images) != {'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'}:
        raise RailViolation('PMax asset-group graph has unused or missing assets')
    first = plan.post_checks[0]
    proof_images = first.get('images') if type(first) is dict else None
    if type(proof_images) is not dict or set(proof_images) != set(images):
        raise RailViolation('PMax asset-group exact image proof is required')
    for role, image in proof_images.items():
        if pmax_image(image, cid, role, images[role]) != image:
            raise RailViolation('PMax asset-group exact image proof is malformed')
    _validate_pmax_parent_proof(first.get('parent_proof'), cid, group['campaign'])
    if proof_images != first['parent_proof']['images']:
        raise RailViolation('PMax asset-group image proofs disagree')
    if plan.post_checks != pmax_checks(cid, ops, proof_images,
                                      parent_proof=first['parent_proof'], asset_group=True):
        raise RailViolation('PMax asset-group result descriptors differ from the exact operations')
    return _PMaxAssetGroupContext(plan)


def validate_pmax_plan(plan):
    """Closed graph cardinality, schemas, references and descriptors, before dispatch."""
    from .rails import EntityMutationPlan, RailViolation, check_content
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True or type(plan.operations) is not list
            or type(plan.post_checks) is not list or not plan.post_checks):
        raise RailViolation('invalid PMax plan shape')
    cid = pmax_id(plan.mutate_customer_id)
    ops = plan.operations
    if not 3 <= len(ops) <= 258:
        raise RailViolation('PMax graph operation count is outside bounds')
    values = []
    for op in ops:
        if (type(op.operation) is not dict or set(op.operation) != {'create'}
                or type(op.operation['create']) is not dict or op.update_mask is not None):
            raise RailViolation('PMax permits only exact create actions without masks')
        values.append(op.operation['create'])
    budget_rn, campaign_rn = creation_paths(cid)
    group_rn = pmax_temporary_path(cid, 'assetGroups', -3)
    budget = values[0]
    if ops[0].service != 'CampaignBudgetService':
        raise RailViolation('PMax must begin with its dedicated budget')
    _validate_entity_create('CampaignBudget', budget, cid, {budget_rn: 'campaignBudgets'})
    campaign = values[1]
    if (ops[1].service != 'CampaignService' or set(campaign) != {
            'resource_name', 'name', 'campaign_budget', 'status', 'advertising_channel_type',
            'maximize_conversions', 'geo_target_type_setting', 'contains_eu_political_advertising',
            'brand_guidelines_enabled', 'asset_automation_settings'}
            or campaign['resource_name'] != campaign_rn or campaign['campaign_budget'] != budget_rn
            or campaign['status'] != 'PAUSED' or campaign['advertising_channel_type'] != 'PERFORMANCE_MAX'
            or campaign['brand_guidelines_enabled'] is not True
            or campaign['contains_eu_political_advertising'] not in POLITICAL_DECLARATIONS.values()
            or campaign['geo_target_type_setting'] != SEARCH_GEO_OPTIONS
            or type(campaign['maximize_conversions']) is not dict
            or set(campaign['maximize_conversions']) != {'target_cpa_micros'}
            or campaign['asset_automation_settings'] != [
                {'asset_automation_type': kind, 'asset_automation_status': 'OPTED_OUT'} for kind in PMAX_AUTOMATIONS]):
        raise RailViolation('PMax campaign schema/settings are invalid')
    check_content([creation_name(campaign['name'])])
    _payload_micros(campaign['maximize_conversions']['target_cpa_micros'])
    index, targets = 2, {'location': set(), 'language': set()}
    while index < len(ops) and ops[index].service == 'CampaignCriterionService':
        item = values[index]
        role = 'location' if 'location' in item else 'language'
        field, prefix = ('geo_target_constant', 'geoTargetConstants') if role == 'location' else ('language_constant', 'languageConstants')
        if (set(item) != {'campaign', 'negative', role} or item['campaign'] != campaign_rn
                or item['negative'] is not False or type(item[role]) is not dict
                or set(item[role]) != {field} or type(item[role][field]) is not str
                or not re.fullmatch(rf'{prefix}/[1-9][0-9]*', item[role][field])
                or item[role][field] in targets[role]):
            raise RailViolation('PMax criterion graph is invalid')
        targets[role].add(item[role][field])
        index += 1
    if any(not 1 <= len(items) <= 100 for items in targets.values()) or index >= len(ops):
        raise RailViolation('PMax requires explicit unique locations and languages')
    group = values[index]
    if (ops[index].service != 'AssetGroupService' or set(group) != {
            'resource_name', 'campaign', 'name', 'status', 'final_urls'}
            or group['resource_name'] != group_rn or group['campaign'] != campaign_rn
            or group['status'] != 'PAUSED' or type(group['final_urls']) is not list
            or len(group['final_urls']) != 1):
        raise RailViolation('PMax requires exactly one paused asset group')
    check_content([creation_name(group['name'])])
    pmax_url(group['final_urls'][0])
    index += 1
    definitions, contents = {}, set()
    while index < len(ops) and ops[index].service == 'AssetService':
        item = values[index]
        rn = pmax_temporary_path(cid, 'assets', -4 - len(definitions))
        if (set(item) != {'resource_name', 'text_asset'} or item['resource_name'] != rn
                or type(item['text_asset']) is not dict or set(item['text_asset']) != {'text'}):
            raise RailViolation('PMax text definitions must be unique ordered temporary assets')
        text = rsa_text(item['text_asset']['text'], 90)
        if text in contents:
            raise RailViolation('PMax text definitions must be deduplicated across roles')
        contents.add(text)
        definitions[rn] = text
        index += 1
    roles = {role: [] for role in PMAX_TEXT_ROLES}
    image_links, consumed = {}, set()
    for op, item in zip(ops[index:], values[index:]):
        parent = 'campaign' if op.service == 'CampaignAssetService' else 'asset_group'
        role = item.get('field_type')
        allowed = {'BUSINESS_NAME', 'LOGO'} if parent == 'campaign' else {
            'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION', 'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'}
        if (op.service not in {'CampaignAssetService', 'AssetGroupAssetService'}
                or set(item) != {parent, 'asset', 'field_type', 'status'}
                or item[parent] != (campaign_rn if parent == 'campaign' else group_rn)
                or item['status'] != 'PAUSED' or type(role) is not str or role not in allowed
                or type(item['asset']) is not str):
            raise RailViolation('PMax link role, status, fields or parent is invalid')
        asset = item['asset']
        if role in PMAX_TEXT_ROLES:
            if asset not in definitions or (asset, role) in consumed:
                raise RailViolation('PMax text link is dangling or duplicated')
            roles[role].append(definitions[asset])
            consumed.add((asset, role))
        else:
            _pmax_owned(asset, 'assets', cid)
            if role in image_links:
                raise RailViolation('PMax permits exactly one image link per role')
            image_links[role] = asset
    for role, texts in roles.items():
        pmax_texts(role, texts)
    if {rn for rn, role in consumed} != set(definitions) or set(image_links) != set(PMAX_IMAGE_ROLES):
        raise RailViolation('PMax contains unused definitions or incomplete image roles')
    images = plan.post_checks[0].get('images') if type(plan.post_checks[0]) is dict else None
    if type(images) is not dict or set(images) != set(PMAX_IMAGE_ROLES):
        raise RailViolation('PMax exact image proof is required')
    for role, image in images.items():
        if pmax_image(image, cid, role, image_links[role]) != image:
            raise RailViolation('PMax image proof must be canonical immutable metadata')
    if plan.post_checks != pmax_checks(cid, ops, images):
        raise RailViolation('PMax result descriptors differ from the exact operations')
    return _PMaxContext(plan)


_PMAX_READ_FIELDS = {
    'campaign_budget': ['amount_micros', 'explicitly_shared', 'period', 'delivery_method'],
    'campaign': ['name', 'status', 'campaign_budget', 'advertising_channel_type',
                 'bidding_strategy_type', 'bidding_strategy', 'maximize_conversions.target_cpa_micros',
                 'brand_guidelines_enabled', 'asset_automation_settings', 'contains_eu_political_advertising',
                 'geo_target_type_setting.positive_geo_target_type',
                 'geo_target_type_setting.negative_geo_target_type'],
    'campaign_criterion': ['campaign', 'status', 'negative', 'location.geo_target_constant', 'language.language_constant'],
    'asset_group': ['name', 'campaign', 'status', 'final_urls'],
    'asset': ['type', 'text_asset.text'],
    'campaign_asset': ['campaign', 'asset', 'field_type', 'status'],
    'asset_group_asset': ['asset_group', 'asset', 'field_type', 'status'],
}


def pmax_created_state(cid, entity, rn):
    """Read exactly one fully populated saved object, validating enums and ownership."""
    from .rails import RailViolation
    _pmax_owned(rn, PMAX_KINDS[entity], cid)
    fields = ['resource_name', *_PMAX_READ_FIELDS[entity]]
    rows = gaql_all('SELECT ' + ', '.join(entity + '.' + key for key in fields)
                    + f" FROM {entity} WHERE {entity}.resource_name = '{rn}'", cid)
    if len(rows) != 1:
        raise RailViolation('PMax saved resource missing or duplicate')
    state = dict(_creation_row(rows[0], entity))
    if state.get('resource_name') != rn:
        raise RailViolation('PMax saved identity mismatch')
    enums = {**_CREATED_ENUMS, 'asset_group': {'status': 'AssetGroupStatusEnum'},
             'asset_group_asset': {'status': 'AssetLinkStatusEnum', 'field_type': 'AssetFieldTypeEnum'}}
    for field, enum in enums[entity].items():
        state[field] = _inventory_enum(enum, state.get(field, state.get('type') if field == 'type_' else None))
    for parent, kind in [('campaign', 'campaigns'), ('campaign_budget', 'campaignBudgets'),
                         ('asset_group', 'assetGroups'), ('asset', 'assets')]:
        if parent in _PMAX_READ_FIELDS[entity]:
            _pmax_owned(state.get(parent), kind, cid)
    if entity == 'campaign':
        # The explicit target message proves the standard strategy; its oneof excludes
        # a portfolio reference. An omitted target never becomes a default-value proof.
        if 'bidding_strategy' in state and state['bidding_strategy'] != '':
            raise RailViolation('PMax saved standard strategy must not contain a portfolio reference')
        geo = state.get('geo_target_type_setting')
        if type(geo) is not dict:
            raise RailViolation('PMax saved geo options are incomplete')
        state['geo_target_type_setting'] = {
            key: _inventory_enum(enum, geo.get(key)) for key, enum in [
                ('positive_geo_target_type', 'PositiveGeoTargetTypeEnum'),
                ('negative_geo_target_type', 'NegativeGeoTargetTypeEnum')]}
        settings = state.get('asset_automation_settings')
        if type(settings) is not list or len(settings) != len(PMAX_AUTOMATIONS):
            raise RailViolation('PMax saved automation settings are incomplete')
        decoded = []
        for item in settings:
            if type(item) is not dict or set(item) != {'asset_automation_type', 'asset_automation_status'}:
                raise RailViolation('PMax saved automation setting malformed')
            decoded.append({key: _inventory_enum(enum, item.get(key)) for key, enum in [
                ('asset_automation_type', 'AssetAutomationTypeEnum'),
                ('asset_automation_status', 'AssetAutomationStatusEnum')]})
        by_type = {item['asset_automation_type']: item for item in decoded}
        if set(by_type) != set(PMAX_AUTOMATIONS):
            raise RailViolation('PMax saved automation types missing or duplicated')
        state['asset_automation_settings'] = [by_type[kind] for kind in PMAX_AUTOMATIONS]
    return state


def verify_pmax_results(checks, result):
    """Resolve the entire ordered result graph before the first post-write read."""
    import copy

    from .rails import RailViolation
    if (type(result) is not dict or result.get('validate_only') or type(result.get('results')) is not list
            or len(result['results']) != len(checks)):
        raise RailViolation('PMax result count/validation state does not reconcile')
    definitions, resolved, seen = {}, [], set()
    for index, (check, entry) in enumerate(zip(checks, result['results'])):
        cid, entity = pmax_id(check['customer_id']), check['entity_type']
        if (check.get('pmax') is not True or check['result_index'] != index or entity not in _PMAX_READ_FIELDS
                or type(entry) is not dict or entry.get('type') != entity + '_result'):
            raise RailViolation('PMax ordered result kind mismatch')
        rn = entry.get('resource_name')
        _pmax_owned(rn, PMAX_KINDS[entity], cid)
        if rn in seen:
            raise RailViolation('PMax duplicate result identity')
        seen.add(rn)
        resolved.append(rn)
        if check['definition'] is not None:
            definitions[check['definition']] = rn
    expected_states = []
    for check, rn in zip(checks, resolved):
        entity = check['entity_type']
        expected = copy.deepcopy(check['expected'])
        for field in ('campaign_budget', 'campaign', 'asset_group', 'asset'):
            if field in expected:
                expected[field] = definitions.get(expected[field], expected[field])
                kind = {'campaign_budget': 'campaignBudgets', 'campaign': 'campaigns',
                        'asset_group': 'assetGroups', 'asset': 'assets'}[field]
                _pmax_owned(expected[field], kind, check['customer_id'])
        parts = rn.rsplit('/', 1)[1].split('~')
        if entity == 'campaign_criterion' and parts[0] != expected['campaign'].rsplit('/', 1)[1]:
            raise RailViolation('PMax criterion result belongs to the wrong campaign')
        if entity in {'campaign_asset', 'asset_group_asset'}:
            parent = 'campaign' if entity == 'campaign_asset' else 'asset_group'
            if parts != [expected[parent].rsplit('/', 1)[1], expected['asset'].rsplit('/', 1)[1],
                         PMAX_FIELD_NUMBERS[expected['field_type']]]:
                raise RailViolation('PMax association result parent/asset/field type mismatch')
        expected_states.append(expected)
    for check, rn, expected in zip(checks, resolved, expected_states):
        state = pmax_created_state(check['customer_id'], check['entity_type'], rn)
        if not _created_match(state, expected):
            mismatches = [key for key, value in expected.items() if key not in state or not _created_match(state[key], value)]
            raise RailViolation(f'PMax saved {check["entity_type"]} fields do not match: {mismatches}')
    if checks[0].get('pmax_asset_group'):
        group = expected_states[0]
        campaign_id = group['campaign'].rsplit('/', 1)[1]
        image_paths = {role: item['resource_name'] for role, item in checks[0]['images'].items()}
        if pmax_asset_group_parent_proof(checks[0]['customer_id'], campaign_id, image_paths) != checks[0]['parent_proof']:
            raise RailViolation('PMax parent, branding or referenced images changed after mutation')
    else:
        for role, expected in checks[0]['images'].items():
            if pmax_image_state(checks[0]['customer_id'], role, expected['resource_name']) != expected:
                raise RailViolation('PMax referenced image metadata changed after mutation')


_DEMAND_GEN_READ_FIELDS = {
    'campaign_budget': ['name', 'status', 'amount_micros', 'explicitly_shared', 'period',
                        'delivery_method', 'total_amount_micros',
                        'aligned_bidding_strategy_id', 'reference_count'],
    'campaign': [
        'id', 'name', 'status', 'campaign_budget', 'advertising_channel_type',
        'advertising_channel_sub_type', 'bidding_strategy_type', 'bidding_strategy',
        'maximize_conversions.target_cpa_micros',
        'maximize_conversions.cpc_bid_ceiling_micros',
        'maximize_conversions.cpc_bid_floor_micros',
        'demand_gen_campaign_settings.upgraded_targeting',
        'geo_target_type_setting.positive_geo_target_type',
        'geo_target_type_setting.negative_geo_target_type',
        'contains_eu_political_advertising', 'shopping_setting.merchant_id',
        'shopping_setting.feed_label', 'shopping_setting.advertising_partner_ids',
        'shopping_setting.use_vehicle_inventory',
        'travel_campaign_settings.travel_account_id', 'hotel_setting.hotel_center_id',
        'hotel_property_asset_set'],
    'campaign_criterion': ['campaign', 'type', 'status', 'negative',
                           'location.geo_target_constant', 'language.language_constant'],
}


def demand_gen_created_state(cid, entity, rn):
    """Read one raw v25 resource; dictionary fixtures cannot prove selected defaults."""
    from .rails import RailViolation
    cid = pmax_id(cid)
    _pmax_owned(rn, PMAX_KINDS[entity], cid)
    fields = ['resource_name', *_DEMAND_GEN_READ_FIELDS[entity]]
    rows = _scan_rows('SELECT ' + ', '.join(entity + '.' + field for field in fields)
                      + f" FROM {entity} WHERE {entity}.resource_name = '{rn}'", cid)
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('Demand Gen saved proof requires one raw provider row')
    return _demand_gen_row_state(cid, entity, rn, rows[0])


def _demand_gen_row_state(cid, entity, rn, row):
    """Normalize and validate the raw row supplied by its complete owning scan."""
    from .rails import RailViolation
    cid = pmax_id(cid)
    _pmax_owned(rn, PMAX_KINDS[entity], cid)
    if isinstance(row, dict):
        raise RailViolation('Demand Gen saved proof requires a raw provider row')
    try:
        raw_entity = getattr(row, entity)
        state = dict(_creation_row(_pmax_raw_dict(row), entity))
    except (AttributeError, TypeError, ValueError):
        raise RailViolation('Demand Gen saved row is unreadable') from None
    if state.get('resource_name') != rn:
        raise RailViolation('Demand Gen saved identity mismatch')
    if entity == 'campaign_budget':
        state.update(
            status=_pmax_enum_exact('BudgetStatusEnum', state.get('status'), {'ENABLED'}),
            period=_pmax_enum_exact('BudgetPeriodEnum', state.get('period'), {'DAILY'}),
            delivery_method=_pmax_enum_exact(
                'BudgetDeliveryMethodEnum', state.get('delivery_method'), {'STANDARD'}),
            amount_micros=raw_entity.amount_micros,
            explicitly_shared=raw_entity.explicitly_shared,
            total_amount_micros=raw_entity.total_amount_micros,
            aligned_bidding_strategy_id=raw_entity.aligned_bidding_strategy_id,
            reference_count=raw_entity.reference_count)
    elif entity == 'campaign':
        try:
            settings_present = raw_entity._pb.HasField('demand_gen_campaign_settings')
            upgraded_present = (settings_present and
                                raw_entity.demand_gen_campaign_settings._pb.HasField('upgraded_targeting'))
        except (AttributeError, ValueError):
            raise RailViolation('Demand Gen targeting field presence is unreadable') from None
        if not upgraded_present:
            raise RailViolation('Demand Gen saved upgraded_targeting=false is not explicitly present')
        if str(raw_entity.id) != rn.rsplit('/', 1)[1]:
            raise RailViolation('Demand Gen saved campaign id mismatch')
        state.update(
            id=str(raw_entity.id),
            status=_pmax_enum_exact('CampaignStatusEnum', state.get('status'), {'PAUSED'}),
            advertising_channel_type=_pmax_enum_exact(
                'AdvertisingChannelTypeEnum', state.get('advertising_channel_type'), {'DEMAND_GEN'}),
            advertising_channel_sub_type=_pmax_enum_exact(
                'AdvertisingChannelSubTypeEnum', state.get('advertising_channel_sub_type'), {'UNSPECIFIED'}),
            bidding_strategy_type=_pmax_enum_exact(
                'BiddingStrategyTypeEnum', state.get('bidding_strategy_type'), {'MAXIMIZE_CONVERSIONS'}),
            contains_eu_political_advertising=_pmax_enum_exact(
                'EuPoliticalAdvertisingStatusEnum', state.get('contains_eu_political_advertising'),
                set(POLITICAL_DECLARATIONS.values())),
            bidding_strategy=raw_entity.bidding_strategy,
            maximize_conversions={
                'target_cpa_micros': raw_entity.maximize_conversions.target_cpa_micros,
                'cpc_bid_ceiling_micros': raw_entity.maximize_conversions.cpc_bid_ceiling_micros,
                'cpc_bid_floor_micros': raw_entity.maximize_conversions.cpc_bid_floor_micros},
            demand_gen_campaign_settings={'upgraded_targeting':
                                          raw_entity.demand_gen_campaign_settings.upgraded_targeting},
            geo_target_type_setting={
                'positive_geo_target_type': _pmax_enum_exact(
                    'PositiveGeoTargetTypeEnum',
                    state.get('geo_target_type_setting', {}).get('positive_geo_target_type'),
                    {'PRESENCE'}),
                'negative_geo_target_type': _pmax_enum_exact(
                    'NegativeGeoTargetTypeEnum',
                    state.get('geo_target_type_setting', {}).get('negative_geo_target_type'),
                    {'PRESENCE'})},
            shopping_setting={
                'merchant_id': raw_entity.shopping_setting.merchant_id,
                'feed_label': raw_entity.shopping_setting.feed_label,
                'advertising_partner_ids': list(raw_entity.shopping_setting.advertising_partner_ids),
                'use_vehicle_inventory': raw_entity.shopping_setting.use_vehicle_inventory},
            travel_campaign_settings={
                'travel_account_id': raw_entity.travel_campaign_settings.travel_account_id},
            hotel_setting={'hotel_center_id': raw_entity.hotel_setting.hotel_center_id},
            hotel_property_asset_set=raw_entity.hotel_property_asset_set)
    else:
        state.update(
            type_=_pmax_enum_exact('CriterionTypeEnum', state.get('type_', state.get('type')),
                                   {'LOCATION', 'LANGUAGE'}),
            status=_pmax_enum_exact('CampaignCriterionStatusEnum', state.get('status'), {'ENABLED'}),
            negative=raw_entity.negative)
        try:
            has_location = raw_entity._pb.HasField('location')
            has_language = raw_entity._pb.HasField('language')
        except (AttributeError, ValueError):
            raise RailViolation('Demand Gen criterion type presence is unreadable') from None
        if has_location == has_language:
            raise RailViolation('Demand Gen criterion must contain exactly one target type')
        state['location'] = ({'geo_target_constant': raw_entity.location.geo_target_constant}
                             if has_location else {})
        state['language'] = ({'language_constant': raw_entity.language.language_constant}
                             if has_language else {})
    return state


def _demand_gen_population(cid, campaign_rn):
    """Complete type-filtered campaign target scan for the exact positive populations."""
    from .rails import RailViolation
    fields = ['resource_name', *_DEMAND_GEN_READ_FIELDS['campaign_criterion']]
    rows = _scan_rows(
        'SELECT ' + ', '.join('campaign_criterion.' + field for field in fields)
        + " FROM campaign_criterion WHERE campaign_criterion.campaign = '"
        + campaign_rn + "' AND campaign_criterion.type IN (LOCATION, LANGUAGE)", cid)
    if any(isinstance(row, dict) for row in rows):
        raise RailViolation('Demand Gen target population requires raw provider rows')
    items = []
    for row in rows:
        converted = _pmax_raw_dict(row)
        rn = converted.get('campaign_criterion', {}).get('resource_name')
        items.append(_demand_gen_row_state(cid, 'campaign_criterion', rn, row))
    return items


def verify_demand_gen_results(checks, result):
    """Resolve every returned identity before exact raw saved-state verification."""
    import copy

    from .rails import RailViolation
    if (type(checks) is not list or not checks or type(result) is not dict
            or result.get('validate_only') or type(result.get('results')) is not list
            or len(result['results']) != len(checks)):
        raise RailViolation('Demand Gen result count or validation state does not reconcile')
    resolved, definitions, seen = [], {}, set()
    for index, (check, entry) in enumerate(zip(checks, result['results'])):
        entity, cid = check.get('entity_type'), pmax_id(check.get('customer_id'))
        if (check.get('demand_gen') is not True or check.get('result_index') != index
                or entity not in _DEMAND_GEN_READ_FIELDS or type(entry) is not dict
                or entry.get('type') != entity + '_result'):
            raise RailViolation('Demand Gen ordered result kind mismatch')
        rn = entry.get('resource_name')
        _pmax_owned(rn, PMAX_KINDS[entity], cid)
        if rn in seen:
            raise RailViolation('Demand Gen duplicate result identity')
        seen.add(rn)
        resolved.append(rn)
        if check.get('definition'):
            definitions[check['definition']] = rn
    expected_states = []
    for check, rn in zip(checks, resolved):
        expected = copy.deepcopy(check['expected'])
        if 'campaign_budget' in expected:
            expected['campaign_budget'] = definitions.get(expected['campaign_budget'], expected['campaign_budget'])
        if 'campaign' in expected:
            expected['campaign'] = definitions.get(expected['campaign'], expected['campaign'])
        if check['entity_type'] == 'campaign_criterion':
            parent = rn.rsplit('/', 1)[1].split('~')[0]
            if parent != expected['campaign'].rsplit('/', 1)[1]:
                raise RailViolation('Demand Gen criterion result belongs to the wrong campaign')
        expected_states.append(expected)
    for check, rn, expected in zip(checks, resolved, expected_states):
        actual = demand_gen_created_state(check['customer_id'], check['entity_type'], rn)
        if not _created_match(actual, expected):
            raise RailViolation(f'Demand Gen saved {check["entity_type"]} fields do not match')
    campaign_rn = resolved[1]
    population = _demand_gen_population(checks[0]['customer_id'], campaign_rn)
    actual = {'locations': [], 'languages': []}
    identities = set()
    for item in population:
        rn = item['resource_name']
        if rn in identities or item['campaign'] != campaign_rn or item['negative'] is not False:
            raise RailViolation('Demand Gen target population contains duplicate, foreign or negative criteria')
        identities.add(rn)
        role = 'locations' if item['type_'] == 'LOCATION' else 'languages'
        target = (item['location'].get('geo_target_constant') if role == 'locations'
                  else item['language'].get('language_constant'))
        if not target:
            raise RailViolation('Demand Gen target population type and value disagree')
        actual[role].append(target)
    family = checks[0]['demand_gen_family']
    if sorted(actual['locations']) != sorted(family['locations']) or sorted(actual['languages']) != sorted(family['languages']):
        raise RailViolation('Demand Gen saved target population does not reconcile')
    return True


# Retail listing filters deliberately do not reuse or widen the non-retail creative proof.
_LISTING_GROUP_FIELDS = ('resource_name', 'id', 'campaign', 'name', 'status', 'final_urls',
                         'final_mobile_urls', 'path1', 'path2')
_LISTING_ACCOUNT_FIELDS = ('resource_name', 'id', 'status', 'manager', 'currency_code', 'time_zone')
_LISTING_CAMPAIGN_FIELDS = ('resource_name', 'id', 'name', 'status', 'advertising_channel_type',
                           'advertising_channel_sub_type', 'bidding_strategy_type',
                           'bidding_strategy', 'shopping_setting', 'listing_type', 'travel_campaign_settings',
                           'hotel_setting', 'hotel_property_asset_set', 'campaign_budget',
                           'brand_guidelines_enabled', 'asset_automation_settings',
                           'contains_eu_political_advertising', 'geo_target_type_setting')
_LISTING_NODE_FIELDS = ('resource_name', 'id', 'asset_group', 'type_', 'listing_source',
                       'parent_listing_group_filter', 'case_value', 'path')


def listing_item_ids(values):
    import unicodedata

    from .rails import RailViolation
    if type(values) is not list or not 1 <= len(values) <= 20:
        raise RailViolation('product_item_ids requires an actual list of 1 through 20 strings', code='BAD_INPUT')
    for value in values:
        if (type(value) is not str or not value or value != value.strip() or value == 'null'
                or any(unicodedata.category(char).startswith('C') for char in value)):
            raise RailViolation('product item IDs must be exact nonempty strings without controls', code='BAD_INPUT')
        try:
            size = len(value.encode('utf-8'))
        except UnicodeError:
            raise RailViolation('product item ID is not valid UTF-8', code='BAD_INPUT') from None
        if size > 50:
            raise RailViolation('product item ID exceeds the local 50-byte cap', code='BAD_INPUT')
    if len(set(values)) != len(values):
        raise RailViolation('duplicate product item IDs', code='BAD_INPUT')
    return sorted(values)


def listing_filter_path(cid, group_id, filter_id):
    from google.ads.googleads.v25.services.services.asset_group_listing_group_filter_service import (
        AssetGroupListingGroupFilterServiceClient,
    )
    return AssetGroupListingGroupFilterServiceClient.asset_group_listing_group_filter_path(
        pmax_id(cid), pmax_id(group_id), str(filter_id))


def _listing_path(value, cid, group_id):
    from .rails import RailViolation
    if type(value) is not str or not re.fullmatch(
            rf'customers/{cid}/assetGroupListingGroupFilters/{group_id}~[1-9][0-9]*', value):
        raise RailViolation('listing filter must have an exact owned positive compound identity')
    return value


def _listing_message(kind):
    # Message classes only: no provider/client construction or credentials.
    from google.ads.googleads.v25 import resources
    return getattr(resources, kind)()


def _listing_fields(kind):
    if kind == 'Campaign':
        message = _listing_message(kind)
        bidding = tuple(f.name for f in message._pb.DESCRIPTOR.oneofs_by_name['campaign_bidding_strategy'].fields)
        return (*_LISTING_CAMPAIGN_FIELDS, *bidding)
    return {'Customer': _LISTING_ACCOUNT_FIELDS, 'AssetGroup': _LISTING_GROUP_FIELDS,
            'AssetGroupListingGroupFilter': _LISTING_NODE_FIELDS}[kind]


def _listing_selection(kind, prefix):
    """Select every nested descriptor leaf, so checked absence is actually observed."""
    def leaves(descriptor, path):
        if not descriptor.fields:
            yield path
        for field in descriptor.fields:
            full = path + '.' + field.name
            if field.message_type:
                yield from leaves(field.message_type, full)
            else:
                yield full
    descriptor = _listing_message(kind)._pb.DESCRIPTOR
    selected = []
    for name in _listing_fields(kind):
        field = descriptor.fields_by_name[name]
        if field.message_type:
            selected.extend(leaves(field.message_type, prefix + '.' + name))
        else:
            selected.append(prefix + '.' + name)
    return ', '.join(selected).replace('.type_', '.type')


def _listing_snapshot(raw, kind):
    from .rails import RailViolation
    try:
        if raw._pb.DESCRIPTOR.full_name != _listing_message(kind)._pb.DESCRIPTOR.full_name:
            raise ValueError()
        if any(f.name not in _listing_fields(kind) for f, _ in raw._pb.ListFields()):
            raise ValueError()
        return raw._pb.SerializeToString(deterministic=True).hex()
    except (AttributeError, TypeError, ValueError):
        raise RailViolation('listing proof requires a complete readable raw selected message') from None


def _listing_decode(snapshot, kind):
    from .rails import RailViolation
    try:
        if type(snapshot) is not str:
            raise ValueError()
        raw = _listing_message(kind)
        raw._pb.ParseFromString(bytes.fromhex(snapshot))
        clean = _listing_message(kind)
        clean._pb.CopyFrom(raw._pb)
        clean._pb.DiscardUnknownFields()
        if (_listing_snapshot(raw, kind) != snapshot
                or clean._pb.SerializeToString(deterministic=True).hex() != snapshot):
            raise ValueError()
        return raw
    except Exception as exc:
        raise RailViolation('listing snapshot is malformed or contains unsupported fields') from exc


def _listing_read_one(cid, kind, resource, where=''):
    from .rails import RailViolation
    query = 'SELECT ' + _listing_selection(kind, resource) + ' FROM ' + resource + where
    rows = _scan_rows(query, cid)
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('listing proof requires exactly one complete raw row')
    try:
        return _listing_snapshot(getattr(rows[0], resource), kind)
    except AttributeError:
        raise RailViolation('listing proof row is unreadable') from None


def _validate_listing_proof(proof, cid, group_id):
    import math

    from .rails import RailViolation
    if type(proof) is not dict or set(proof) != {'account', 'group', 'campaign'}:
        raise RailViolation('listing proof fields are not closed')
    account = _listing_decode(proof['account'], 'Customer')
    group = _listing_decode(proof['group'], 'AssetGroup')
    campaign = _listing_decode(proof['campaign'], 'Campaign')
    if (str(account.id) != cid or account.resource_name != f'customers/{cid}' or account.manager
            or not re.fullmatch('[A-Z]{3}', account.currency_code) or not account.time_zone):
        raise RailViolation('listing account identity is unsupported')
    _pmax_enum_exact('CustomerStatusEnum', account.status, {'ENABLED'})
    if str(group.id) != group_id or group.resource_name != asset_group_path(cid, group_id):
        raise RailViolation('listing group identity mismatch')
    _pmax_enum_exact('AssetGroupStatusEnum', group.status, {'PAUSED'})
    _pmax_owned(group.campaign, 'campaigns', cid)
    if campaign.resource_name != group.campaign or str(campaign.id) != group.campaign.rsplit('/', 1)[1]:
        raise RailViolation('listing parent identity mismatch')
    _pmax_enum_exact('CampaignStatusEnum', campaign.status, {'PAUSED'})
    _pmax_enum_exact('AdvertisingChannelTypeEnum', campaign.advertising_channel_type, {'PERFORMANCE_MAX'})
    _pmax_enum_exact('AdvertisingChannelSubTypeEnum', campaign.advertising_channel_sub_type, {'UNSPECIFIED'})
    pb = campaign._pb
    if (not pb.HasField('shopping_setting')
            or any(pb.HasField(f) for f in ('travel_campaign_settings', 'hotel_setting', 'hotel_property_asset_set'))
            or (pb.HasField('listing_type') and int(campaign.listing_type) != 0)):
        raise RailViolation('listing filter requires ordinary retail without vehicle, hotel or travel settings')
    shopping = campaign.shopping_setting
    if (not shopping._pb.HasField('merchant_id') or shopping.merchant_id <= 0
            or not re.fullmatch('[A-Z0-9_-]{1,20}', shopping.feed_label)
            or shopping.enable_local or shopping.use_vehicle_inventory or shopping.advertising_partner_ids
            or shopping.disable_product_feed or shopping.ignore_brand_exclusion_in_shopping_ads
            or shopping.campaign_priority != 0):
        raise RailViolation('listing merchant/feed or shopping settings are unsupported')
    if campaign.bidding_strategy:
        raise RailViolation('listing parent requires its own campaign bidding strategy')
    strategy = _pmax_enum_exact('BiddingStrategyTypeEnum', campaign.bidding_strategy_type,
                               {'MAXIMIZE_CONVERSIONS', 'MAXIMIZE_CONVERSION_VALUE'})
    expected = strategy.lower()
    if pb.WhichOneof('campaign_bidding_strategy') != expected:
        raise RailViolation('listing parent requires its own matching supported bidding message')
    bidding = getattr(campaign, expected)
    allowed = 'target_cpa_micros' if expected == 'maximize_conversions' else 'target_roas'
    if any(f.name != allowed for f, _ in bidding._pb.ListFields()):
        raise RailViolation('listing bidding message has unsupported populated fields')
    value = getattr(bidding, allowed)
    if (allowed == 'target_cpa_micros' and (type(value) is not int or value < 0)
            or allowed == 'target_roas' and (not math.isfinite(value) or value < 0)):
        raise RailViolation('listing bidding target is invalid')
    _pmax_owned(campaign.campaign_budget, 'campaignBudgets', cid)
    return group, campaign


def _listing_case(raw):
    from .rails import RailViolation
    if not raw._pb.HasField('case_value'):
        return None
    dimension = raw.case_value
    if dimension._pb.WhichOneof('dimension') != 'product_item_id':
        raise RailViolation('listing case must be a typed product item ID')
    item = dimension.product_item_id
    if not item._pb.HasField('value'):
        return {'product_item_id': {}}
    listing_item_ids([item.value])
    return {'product_item_id': {'value': item.value}}


def _listing_tree(snapshots, cid, group_id, *, saved=False):
    from .rails import RailViolation
    if type(snapshots) is not list or len(snapshots) > 22:
        raise RailViolation('listing inventory exceeds the local tree cap')
    rows, seen = [], set()
    for snapshot in snapshots:
        raw = _listing_decode(snapshot, 'AssetGroupListingGroupFilter')
        rn = _listing_path(raw.resource_name, cid, group_id)
        if (rn in seen or raw.asset_group != asset_group_path(cid, group_id)
                or str(raw.id) != rn.rsplit('~', 1)[1]):
            raise RailViolation('listing inventory has duplicate or mismatched identities')
        seen.add(rn)
        kind = _pmax_enum_exact('ListingGroupFilterTypeEnum', raw.type_,
                                {'SUBDIVISION', 'UNIT_INCLUDED', 'UNIT_EXCLUDED'})
        _pmax_enum_exact('ListingGroupFilterListingSourceEnum', raw.listing_source, {'SHOPPING'})
        parent = raw.parent_listing_group_filter
        if parent:
            _listing_path(parent, cid, group_id)
        case = _listing_case(raw)
        if saved and raw.path.dimensions:
            if not parent or len(raw.path.dimensions) != 1:
                raise RailViolation('saved listing path dimensions mismatch')
            dimension = raw.path.dimensions[0]
            expected = _listing_message('AssetGroupListingGroupFilter')
            expected.case_value = case
            if dimension._pb != expected.case_value._pb:
                raise RailViolation('saved listing semantic path mismatch')
        row = {'resource_name': rn, 'asset_group': raw.asset_group, 'type_': kind,
               'listing_source': 'SHOPPING'}
        if parent:
            row['parent_listing_group_filter'] = parent
        if case is not None:
            row['case_value'] = case
        rows.append(row)
    if not rows:
        return [], []
    roots = [row for row in rows if 'parent_listing_group_filter' not in row]
    if len(roots) != 1 or 'case_value' in roots[0]:
        raise RailViolation('listing inventory requires exactly one case-free root')
    root = roots[0]
    if len(rows) == 1 and root['type_'] == 'UNIT_INCLUDED':
        return rows, None  # all products, distinct from an empty inventory
    children = [row for row in rows if row is not root]
    if root['type_'] != 'SUBDIVISION' or not 2 <= len(children) <= 21:
        raise RailViolation('listing inventory is not the admitted shallow allowlist')
    ids, remainder = [], 0
    for row in children:
        if row.get('parent_listing_group_filter') != root['resource_name'] or 'case_value' not in row:
            raise RailViolation('listing inventory is disconnected, deep or missing a case')
        item = row['case_value']['product_item_id']
        if not item and row['type_'] == 'UNIT_EXCLUDED':
            remainder += 1
        elif item and row['type_'] == 'UNIT_INCLUDED':
            ids.append(item['value'])
        else:
            raise RailViolation('listing inventory has a wrong inclusion/remainder type')
    if remainder != 1:
        raise RailViolation('listing inventory requires one excluded typed remainder')
    ids = listing_item_ids(ids)
    return sorted(children, key=lambda row: row['resource_name']) + [root], ids


def listing_filter_state(cid, group_id):
    cid, group_id = pmax_id(cid), pmax_id(group_id)
    account = _listing_read_one(cid, 'Customer', 'customer')
    group = _listing_read_one(cid, 'AssetGroup', 'asset_group', f' WHERE asset_group.id = {group_id}')
    target = _listing_decode(group, 'AssetGroup')
    _pmax_owned(target.campaign, 'campaigns', cid)
    campaign_id = target.campaign.rsplit('/', 1)[1]
    campaign = _listing_read_one(cid, 'Campaign', 'campaign', f' WHERE campaign.id = {campaign_id}')
    proof = {'account': account, 'group': group, 'campaign': campaign}
    _validate_listing_proof(proof, cid, group_id)
    rn = asset_group_path(cid, group_id)
    query = ('SELECT ' + _listing_selection('AssetGroupListingGroupFilter', 'asset_group_listing_group_filter')
             + f" FROM asset_group_listing_group_filter WHERE asset_group_listing_group_filter.asset_group = '{rn}'")
    snapshots = []
    from .rails import RailViolation
    for row in _scan_rows(query, cid):
        if isinstance(row, dict):
            raise RailViolation('listing inventory requires raw rows')
        try:
            snapshots.append(_listing_snapshot(row.asset_group_listing_group_filter, 'AssetGroupListingGroupFilter'))
        except AttributeError:
            raise RailViolation('listing inventory row is unreadable') from None
    snapshots.sort()
    _listing_tree(snapshots, cid, group_id)
    return {'proof': proof, 'tree': snapshots}


def listing_filter_operations(cid, group_id, old, ids):
    from .rails import MutationOp
    operations = [MutationOp('AssetGroupListingGroupFilterService', {'remove': row['resource_name']}, None)
                  for row in old]
    root = listing_filter_path(cid, group_id, -1)
    base = {'asset_group': asset_group_path(cid, group_id), 'listing_source': 'SHOPPING'}
    values = [dict(base, resource_name=root, type_='SUBDIVISION')]
    for index, item in enumerate(ids, 2):
        values.append(dict(base, resource_name=listing_filter_path(cid, group_id, -index),
                           type_='UNIT_INCLUDED', parent_listing_group_filter=root,
                           case_value={'product_item_id': {'value': item}}))
    values.append(dict(base, resource_name=listing_filter_path(cid, group_id, -len(ids)-2),
                       type_='UNIT_EXCLUDED', parent_listing_group_filter=root,
                       case_value={'product_item_id': {}}))
    return operations + [MutationOp('AssetGroupListingGroupFilterService', {'create': value}, None) for value in values]


class _ListingFilterContext:
    def __init__(self, plan):
        import copy
        self.plan = copy.deepcopy(plan)


def validate_listing_filter_plan(plan):
    from .rails import EntityMutationPlan, RailViolation
    if (type(plan) is not EntityMutationPlan or plan.kind != 'entity'
            or plan.validate_only_supported is not True or type(plan.operations) is not list
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('listing filter requires one closed atomic entity plan')
    cid, check = pmax_id(plan.mutate_customer_id), plan.post_checks[0]
    if (type(check) is not dict or set(check) != {'listing_filter', 'customer_id', 'group_id', 'before', 'item_ids'}
            or check['listing_filter'] is not True or check['customer_id'] != cid
            or type(check['before']) is not dict or set(check['before']) != {'proof', 'tree'}):
        raise RailViolation('listing filter check fields are not closed')
    group_id = pmax_id(check['group_id'])
    _validate_listing_proof(check['before']['proof'], cid, group_id)
    old, current = _listing_tree(check['before']['tree'], cid, group_id)
    ids = listing_item_ids(check['item_ids'])
    if check['item_ids'] != ids or current == ids:
        raise RailViolation('listing requested set is noncanonical or unchanged')
    expected = listing_filter_operations(cid, group_id, old, ids)
    if plan.operations != expected:
        raise RailViolation('listing operations differ from the exact closed remove/create graph')
    return _ListingFilterContext(plan)


def verify_listing_filter_result(checks, result):
    from .rails import EntityMutationPlan, RailViolation
    check = checks[0]
    cid, group_id = check['customer_id'], check['group_id']
    old, _ = _listing_tree(check['before']['tree'], cid, group_id)
    operations = listing_filter_operations(cid, group_id, old, check['item_ids'])
    validate_listing_filter_plan(EntityMutationPlan(cid, operations, True, post_checks=checks))
    results = result.get('results') if type(result) is dict else None
    if (type(results) is not list or len(results) != len(operations)
            or result.get('validate_only')):
        raise RailViolation('listing response count or apply-mode mismatch')
    removed = {row['resource_name'] for row in old}
    mapping, seen = {}, set()
    for op, entry in zip(operations, results):
        if (type(entry) is not dict or set(entry) != {'type', 'resource_name'}
                or entry['type'] != 'asset_group_listing_group_filter_result'):
            raise RailViolation('listing response kind/fields mismatch')
        rn = _listing_path(entry['resource_name'], cid, group_id)
        if rn in seen:
            raise RailViolation('listing response duplicates an identity')
        seen.add(rn)
        if 'remove' in op.operation:
            if rn != op.operation['remove']:
                raise RailViolation('listing removal result is out of order')
        else:
            if rn in removed:
                raise RailViolation('listing created identity reuses a removed node')
            mapping[op.operation['create']['resource_name']] = rn
    fresh = listing_filter_state(cid, group_id)
    if fresh['proof'] != check['before']['proof']:
        raise RailViolation('listing group, account or retail parent changed')
    actual, _ = _listing_tree(fresh['tree'], cid, group_id, saved=True)
    expected = []
    for op in operations:
        if 'create' not in op.operation:
            continue
        row = dict(op.operation['create'])
        row['resource_name'] = mapping[row['resource_name']]
        if 'parent_listing_group_filter' in row:
            row['parent_listing_group_filter'] = mapping[row['parent_listing_group_filter']]
        expected.append(row)
    if (sorted(actual, key=lambda x: x['resource_name']) != sorted(expected, key=lambda x: x['resource_name'])
            or removed & {row['resource_name'] for row in actual}):
        raise RailViolation('saved listing tree differs from exact ordered result mapping')


# Single-image Demand Gen ads have their own closed family; RSA admission is unchanged.
DEMAND_GEN_AD_INPUTS = ('ad_group_id', 'headline', 'description', 'business_name',
                      'square_marketing_image_asset_id', 'logo_image_asset_id', 'final_url')
_DG_AD_FIELDS = {
    'ad_group': ('resource_name', 'id', 'campaign', 'status', 'type'),
    'campaign': ('resource_name', 'id', 'status', 'advertising_channel_type',
                 'advertising_channel_sub_type', 'campaign_budget', 'bidding_strategy_type',
                 'bidding_strategy'),
    'campaign_budget': ('resource_name', 'amount_micros', 'explicitly_shared', 'period'),
    'asset': ('resource_name', 'id', 'type', 'image_asset.mime_type', 'image_asset.file_size',
              'image_asset.full_size.width_pixels', 'image_asset.full_size.height_pixels'),
    'ad_group_ad': ('resource_name', 'ad_group', 'status', 'ad.resource_name', 'ad.id', 'ad.type',
                    'ad.final_urls', 'ad.final_mobile_urls', 'ad.tracking_url_template',
                    'ad.final_url_suffix', 'ad.url_custom_parameters', 'ad.demand_gen_multi_asset_ad'),
}


class _DemandGenAdContext:
    def __init__(self, plan):
        self.plan = plan


def demand_gen_ad_operation(cid, inputs):
    from .rails import RailViolation, check_content, safe_create_operation
    cid = demand_gen_ad_id(cid)
    if type(inputs) is not dict or set(inputs) != set(DEMAND_GEN_AD_INPUTS):
        raise RailViolation('Demand Gen ad input fields are not closed')
    gid = demand_gen_ad_id(inputs['ad_group_id'])
    square = demand_gen_ad_id(inputs['square_marketing_image_asset_id'])
    logo = demand_gen_ad_id(inputs['logo_image_asset_id'])
    for key, limit in (('headline', 30), ('description', 90), ('business_name', 25)):
        rsa_text(inputs[key], limit)
    pmax_url(inputs['final_url'])
    check_content([inputs[key] for key in ('headline', 'description', 'business_name', 'final_url')])
    return safe_create_operation('AdGroupAdService', {
        'ad_group': ad_group_path(cid, gid), 'ad': {
            'final_urls': [inputs['final_url']], 'demand_gen_multi_asset_ad': {
                'square_marketing_images': [{'asset': asset_path(cid, square)}],
                'logo_images': [{'asset': asset_path(cid, logo)}],
                'headlines': [{'text': inputs['headline']}],
                'descriptions': [{'text': inputs['description']}],
                'business_name': inputs['business_name']}}})


def _dg_ad_read(cid, kind, rn):
    """An actual selected raw row is required to prove scalar defaults and oneofs."""
    from .rails import RailViolation
    fields = ', '.join(kind + '.' + field for field in _DG_AD_FIELDS[kind])
    rows = _scan_rows(f"SELECT {fields} FROM {kind} WHERE {kind}.resource_name = '{rn}'", cid)
    if len(rows) != 1 or isinstance(rows[0], dict):
        raise RailViolation('Demand Gen ad proof requires exactly one complete raw row')
    try:
        raw = getattr(rows[0], kind)
        message_kind = ''.join(part.title() for part in kind.split('_'))
        if raw._pb.DESCRIPTOR.full_name != _listing_message(message_kind)._pb.DESCRIPTOR.full_name:
            raise RailViolation('Demand Gen ad proof requires the selected schema message')
        if raw.resource_name != rn:
            raise RailViolation('Demand Gen ad selected identity mismatch')
        return raw
    except AttributeError:
        raise RailViolation('Demand Gen ad row is unreadable') from None


def _dg_ad_scalar(raw, name):
    from .rails import RailViolation
    # Presence is required for optional scalar fields; nonoptional raw defaults are observed.
    descriptor = raw._pb.DESCRIPTOR.fields_by_name[name]
    if descriptor.has_presence and not raw._pb.HasField(name):
        raise RailViolation('Demand Gen ad selected optional field is absent: ' + name)
    return getattr(raw, name)


def _dg_ad_known_enum(kind, value):
    from google.ads.googleads.v25 import enums
    enum = getattr(getattr(enums, kind), kind.removesuffix('Enum'))
    return _pmax_enum_exact(kind, value, {item.name for item in enum if item.name != 'UNKNOWN'})


def _dg_ad_image(raw, cid, rn):
    from .rails import RailViolation
    if str(_dg_ad_scalar(raw, 'id')) != rn.rsplit('/', 1)[1]:
        raise RailViolation('Demand Gen asset id mismatch')
    image = raw.image_asset
    state = {'resource_name': rn, 'id': str(raw.id),
             'type': _pmax_enum_exact('AssetTypeEnum', raw.type_, {'IMAGE'}),
             'mime_type': _pmax_enum_exact('MimeTypeEnum', image.mime_type, {'IMAGE_PNG', 'IMAGE_JPEG'}),
             'file_size': _dg_ad_scalar(image, 'file_size'),
             'width': _dg_ad_scalar(image.full_size, 'width_pixels'),
             'height': _dg_ad_scalar(image.full_size, 'height_pixels')}
    return state


def demand_gen_ad_state(cid, gid, inputs):
    cid, gid = demand_gen_ad_id(cid), demand_gen_ad_id(gid)
    account = demand_gen_account_state(cid)
    group = _dg_ad_read(cid, 'ad_group', ad_group_path(cid, gid))
    group_state = {'resource_name': group.resource_name, 'id': str(_dg_ad_scalar(group, 'id')),
                   'campaign': _dg_ad_scalar(group, 'campaign'),
                   'status': _pmax_enum_exact('AdGroupStatusEnum', group.status, {'PAUSED'}),
                   'type': _dg_ad_known_enum('AdGroupTypeEnum', group.type_)}
    _dg_ad_owned(group_state['campaign'], 'campaigns', cid)
    campaign = _dg_ad_read(cid, 'campaign', group_state['campaign'])
    campaign_state = {key: _dg_ad_scalar(campaign, key) for key in (
        'resource_name', 'campaign_budget', 'bidding_strategy')}
    campaign_state.update(id=str(_dg_ad_scalar(campaign, 'id')),
        status=_pmax_enum_exact('CampaignStatusEnum', campaign.status, {'PAUSED'}),
        advertising_channel_type=_pmax_enum_exact('AdvertisingChannelTypeEnum', campaign.advertising_channel_type, {'DEMAND_GEN'}),
        advertising_channel_sub_type=_dg_ad_known_enum('AdvertisingChannelSubTypeEnum', campaign.advertising_channel_sub_type),
        bidding_strategy_type=_dg_ad_known_enum('BiddingStrategyTypeEnum', campaign.bidding_strategy_type))
    _dg_ad_owned(campaign_state['campaign_budget'], 'campaignBudgets', cid)
    budget = _dg_ad_read(cid, 'campaign_budget', campaign_state['campaign_budget'])
    budget_state = {key: _dg_ad_scalar(budget, key) for key in (
        'resource_name', 'amount_micros', 'explicitly_shared')}
    budget_state['period'] = _dg_ad_known_enum('BudgetPeriodEnum', budget.period)
    roles = {'square_marketing_images': asset_path(cid, demand_gen_ad_id(inputs['square_marketing_image_asset_id'])),
             'logo_images': asset_path(cid, demand_gen_ad_id(inputs['logo_image_asset_id']))}
    assets = [_dg_ad_image(_dg_ad_read(cid, 'asset', rn), cid, rn) for rn in sorted(set(roles.values()))]
    proof = {'account': account, 'group': group_state, 'campaign': campaign_state,
             'budget': budget_state, 'assets': assets, 'roles': roles}
    _validate_dg_ad_proof(cid, inputs, proof)
    return proof


def _validate_dg_ad_proof(cid, inputs, proof):
    """Validate proof independently of submitted operations; no agreeing forgery shortcuts."""
    from .rails import RailViolation

    def keys(item, expected):
        if type(item) is not dict or set(item) != set(expected.split()):
            raise RailViolation('Demand Gen ad proof fields are not closed')

    keys(proof, 'account group campaign budget assets roles')
    account, group, campaign, budget = (proof[key] for key in ('account', 'group', 'campaign', 'budget'))
    keys(account, 'resource_name id descriptive_name currency_code time_zone manager status')
    if (account['resource_name'] != f'customers/{cid}' or account['id'] != cid
            or account['manager'] is not False or account['status'] != 'ENABLED'
            or type(account['currency_code']) is not str or not re.fullmatch('[A-Z]{3}', account['currency_code'])
            or any(type(account[key]) is not str or not account[key].strip()
                   for key in ('descriptive_name', 'time_zone'))):
        raise RailViolation('Demand Gen ad account proof invalid')
    keys(group, 'resource_name id campaign status type')
    if group['resource_name'] != ad_group_path(cid, inputs['ad_group_id']) or group['id'] != inputs['ad_group_id'] or group['status'] != 'PAUSED':
        raise RailViolation('Demand Gen ad group proof invalid')
    _dg_ad_known_enum('AdGroupTypeEnum', group['type'])
    _dg_ad_owned(group['campaign'], 'campaigns', cid)
    keys(campaign, 'resource_name id status advertising_channel_type advertising_channel_sub_type campaign_budget bidding_strategy_type bidding_strategy')
    if (campaign['resource_name'] != group['campaign'] or campaign['id'] != group['campaign'].rsplit('/', 1)[1]
            or campaign['status'] != 'PAUSED' or campaign['advertising_channel_type'] != 'DEMAND_GEN'):
        raise RailViolation('Demand Gen ad campaign proof invalid')
    _dg_ad_known_enum('AdvertisingChannelSubTypeEnum', campaign['advertising_channel_sub_type'])
    _dg_ad_known_enum('BiddingStrategyTypeEnum', campaign['bidding_strategy_type'])
    if type(campaign['bidding_strategy']) is not str:
        raise RailViolation('Demand Gen ad portfolio proof unreadable')
    if campaign['bidding_strategy']:
        _dg_ad_owned(campaign['bidding_strategy'], 'biddingStrategies', cid)
    _dg_ad_owned(campaign['campaign_budget'], 'campaignBudgets', cid)
    keys(budget, 'resource_name amount_micros explicitly_shared period')
    if (budget['resource_name'] != campaign['campaign_budget'] or type(budget['amount_micros']) is not int
            or budget['amount_micros'] <= 0 or type(budget['explicitly_shared']) is not bool):
        raise RailViolation('Demand Gen ad budget proof invalid')
    _dg_ad_known_enum('BudgetPeriodEnum', budget['period'])
    roles = {'square_marketing_images': asset_path(cid, inputs['square_marketing_image_asset_id']),
             'logo_images': asset_path(cid, inputs['logo_image_asset_id'])}
    if proof['roles'] != roles or type(proof['assets']) is not list:
        raise RailViolation('Demand Gen ad role proof invalid')
    identities = []
    for asset in proof['assets']:
        keys(asset, 'resource_name id type mime_type file_size width height')
        rn = asset['resource_name']
        _dg_ad_owned(rn, 'assets', cid)
        if (asset['id'] != rn.rsplit('/', 1)[1] or asset['type'] != 'IMAGE'
                or asset['mime_type'] not in {'IMAGE_PNG', 'IMAGE_JPEG'}
                or any(type(asset[key]) is not int or asset[key] <= 0 for key in ('file_size', 'width', 'height'))
                or asset['file_size'] > 5_000_000 or asset['width'] != asset['height']
                or asset['width'] < (300 if roles['square_marketing_images'] == rn else 128)):
            raise RailViolation('Demand Gen ad image proof invalid')
        identities.append(rn)
    if identities != sorted(set(roles.values())):
        raise RailViolation('Demand Gen ad asset population mismatch')


def demand_gen_ad_checks(cid, inputs, proof):
    import copy
    expected = demand_gen_ad_operation(cid, inputs).operation['create']
    return [{'demand_gen_ad': True, 'result_index': 0, 'entity_type': 'ad_group_ad',
             'customer_id': cid, 'ad_group': expected['ad_group'],
             'campaign': proof['group']['campaign'], 'inputs': copy.deepcopy(inputs),
             'expected': expected, 'proof': copy.deepcopy(proof)}]


def validate_demand_gen_ad_plan(plan):
    from .rails import RailViolation
    cid = demand_gen_ad_id(plan.mutate_customer_id)
    if (plan.validate_only_supported is not True or len(plan.operations) != 1
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1
            or type(plan.post_checks[0]) is not dict):
        raise RailViolation('Demand Gen ad must have exactly one operation and closed check')
    check = plan.post_checks[0]
    if set(check) != {'demand_gen_ad', 'result_index', 'entity_type', 'customer_id',
                       'ad_group', 'campaign', 'inputs', 'expected', 'proof'}:
        raise RailViolation('Demand Gen ad check fields are not closed')
    expected_op = demand_gen_ad_operation(cid, check['inputs'])
    _validate_dg_ad_proof(cid, check['inputs'], check['proof'])
    if (check['demand_gen_ad'] is not True or type(check['result_index']) is not int
            or plan.operations != [expected_op]
            or plan.post_checks != demand_gen_ad_checks(cid, check['inputs'], check['proof'])):
        raise RailViolation('Demand Gen ad graph differs from validated intent and proof')
    return _DemandGenAdContext(plan)


def verify_demand_gen_ad_results(checks, result):
    from .rails import EntityMutationPlan, RailViolation
    if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
            or type(result) is not dict or result.get('validate_only')
            or type(result.get('results')) is not list or len(result['results']) != 1):
        raise RailViolation('Demand Gen ad result count or validation state mismatch')
    check = checks[0]
    cid = demand_gen_ad_id(check.get('customer_id'))
    operation = demand_gen_ad_operation(cid, check.get('inputs'))
    validate_demand_gen_ad_plan(EntityMutationPlan(cid, [operation], True, post_checks=checks))
    entry = result['results'][0]
    gid = check['inputs']['ad_group_id']
    if type(entry) is not dict or entry.get('type') != 'ad_group_ad_result':
        raise RailViolation('Demand Gen ad result type mismatch')
    rn = entry.get('resource_name')
    if type(rn) is not str or not re.fullmatch(rf'customers/{cid}/adGroupAds/{gid}~[1-9][0-9]*', rn):
        raise RailViolation('Demand Gen ad compound result identity mismatch')
    aid = demand_gen_ad_id(rn.rsplit('~', 1)[1])
    raw = _dg_ad_read(cid, 'ad_group_ad', rn)
    ad = raw.ad
    _pmax_enum_exact('AdGroupAdStatusEnum', raw.status, {'PAUSED'})
    _pmax_enum_exact('AdTypeEnum', ad.type_, {'DEMAND_GEN_MULTI_ASSET_AD'})
    if (raw.ad_group != check['ad_group'] or ad.resource_name != f'customers/{cid}/ads/{aid}'
            or str(ad.id) != aid or ad._pb.WhichOneof('ad_data') != 'demand_gen_multi_asset_ad'):
        raise RailViolation('Demand Gen saved ad identity or family mismatch')
    expected = check['expected']['ad']
    if (list(ad.final_urls) != expected['final_urls'] or ad.final_mobile_urls
            or _dg_ad_scalar(ad, 'tracking_url_template') or _dg_ad_scalar(ad, 'final_url_suffix')
            or ad.url_custom_parameters):
        raise RailViolation('Demand Gen saved URL or unrequested tracking mismatch')
    creative = ad.demand_gen_multi_asset_ad
    wanted = expected['demand_gen_multi_asset_ad']
    for role in ('square_marketing_images', 'logo_images'):
        if [{'asset': item.asset} for item in getattr(creative, role)] != wanted[role]:
            raise RailViolation('Demand Gen saved image role mismatch')
    for field in ('headlines', 'descriptions'):
        actual = getattr(creative, field)
        if [{'text': item.text} for item in actual] != wanted[field]:
            raise RailViolation('Demand Gen saved text mismatch')
        for item in actual:
            _pmax_enum_exact('ServedAssetFieldTypeEnum', item.pinned_field, {'UNSPECIFIED'})
    if (creative.business_name != wanted['business_name'] or creative.call_to_action_text
            or any(getattr(creative, field) for field in (
                'marketing_images', 'portrait_marketing_images', 'tall_portrait_marketing_images',
                'classic_display_images'))):
        raise RailViolation('Demand Gen saved unrequested creative mismatch')
    if demand_gen_ad_state(cid, gid, check['inputs']) != check['proof']:
        raise RailViolation('Demand Gen saved parent or asset proof drift')
    return [rn]



def demand_gen_ad_id(value):
    pmax_id(value)
    positive_int64(value)
    return value


def _dg_ad_owned(rn, kind, cid):
    _pmax_owned(rn, kind, cid)
    demand_gen_ad_id(rn.rsplit('/', 1)[1])


_SHARED_NEGATIVE_SERVICES = {'SharedSetService', 'SharedCriterionService', 'CampaignSharedSetService'}
_SHARED_SET_FIELDS = ('resource_name', 'id', 'name', 'type', 'status', 'member_count', 'reference_count')


class _SharedNegativeContext:
    def __init__(self, plan):
        self.plan = plan


def shared_set_path(cid, set_id):
    from google.ads.googleads.v25.services.services.shared_set_service import (
        SharedSetServiceClient,
    )
    return SharedSetServiceClient.shared_set_path(demand_gen_ad_id(cid), demand_gen_ad_id(set_id))


def _shared_keys(value, keys):
    from .rails import RailViolation
    if type(value) is not dict or set(value) != set(keys.split()):
        raise RailViolation('shared negative proof fields are not closed')


def _validate_shared_set_metadata(cid, item, counts=False):
    from .rails import RailViolation
    keys = 'resource_name id name type status' + (' member_count reference_count' if counts else '')
    _shared_keys(item, keys)
    if (item['resource_name'] != shared_set_path(cid, item['id'])
            or item['type'] != 'NEGATIVE_KEYWORDS' or item['status'] != 'ENABLED'):
        raise RailViolation('shared set identity, type or status invalid')
    creation_name(item['name'])
    if counts and any(type(item[key]) is not int or not 0 <= item[key] <= _INT64_MAX
                      for key in ('member_count', 'reference_count')):
        raise RailViolation('shared set counts require present nonnegative int64 values')


def _shared_set_row(cid, row, counts=False):
    from .rails import RailViolation
    try:
        raw = row.shared_set
        if raw._pb.DESCRIPTOR.full_name != _listing_message('SharedSet')._pb.DESCRIPTOR.full_name:
            raise RailViolation('shared set requires raw selected schema message')
        item = {key: _dg_ad_scalar(raw, key) for key in ('resource_name', 'id', 'name')}
        item['id'] = str(item['id'])
        item['type'] = _pmax_enum_exact('SharedSetTypeEnum', raw.type_, {'NEGATIVE_KEYWORDS'})
        item['status'] = _pmax_enum_exact('SharedSetStatusEnum', raw.status, {'ENABLED'})
        if counts:
            item.update({key: _dg_ad_scalar(raw, key) for key in ('member_count', 'reference_count')})
    except (AttributeError, KeyError):
        raise RailViolation('shared set requires raw selected schema message') from None
    _validate_shared_set_metadata(cid, item, counts)
    return item


def shared_negative_name_inventory(cid):
    fields = ', '.join('shared_set.' + key for key in _SHARED_SET_FIELDS)
    rows = _scan_rows(f"SELECT {fields} FROM shared_set WHERE shared_set.type = 'NEGATIVE_KEYWORDS' "
                      "AND shared_set.status != 'REMOVED'", cid)
    items = [_shared_set_row(cid, row) for row in rows]
    from .rails import RailViolation
    if len({item['resource_name'] for item in items}) != len(items):
        raise RailViolation('duplicate shared set inventory identity')
    return sorted(items, key=lambda item: item['resource_name'])


def _validate_shared_creation_proof(cid, name, proof):
    from .rails import RailViolation
    _shared_keys(proof, 'account sets')
    account = proof['account']
    _shared_keys(account, 'resource_name id descriptive_name currency_code time_zone manager status')
    if (account['resource_name'] != f'customers/{cid}' or account['id'] != cid
            or account['manager'] is not False or account['status'] != 'ENABLED'
            or type(account['currency_code']) is not str or not re.fullmatch('[A-Z]{3}', account['currency_code'])
            or any(type(account[key]) is not str or not account[key].strip()
                   for key in ('descriptive_name', 'time_zone'))):
        raise RailViolation('shared negative account proof invalid')
    if type(proof['sets']) is not list:
        raise RailViolation('shared negative inventory must be a list')
    for item in proof['sets']:
        _validate_shared_set_metadata(cid, item)
    identities = [item['resource_name'] for item in proof['sets']]
    names = [item['name'] for item in proof['sets']]
    if identities != sorted(set(identities)) or len(names) != len(set(names)) or name in names:
        raise RailViolation('shared negative name collision or ambiguous inventory')


def shared_negative_creation_state(cid, name):
    proof = {'account': demand_gen_account_state(cid), 'sets': shared_negative_name_inventory(cid)}
    _validate_shared_creation_proof(cid, name, proof)
    return proof


def shared_negative_create_operation(name):
    from .rails import RailViolation, check_content, safe_create_operation
    if type(name) is not str:
        raise RailViolation('shared set name must be an original string')
    name = creation_name(name)
    check_content([name])
    return safe_create_operation('SharedSetService', {'name': name, 'type_': 'NEGATIVE_KEYWORDS'})


def shared_negative_create_checks(cid, name, proof):
    import copy
    return [{'shared_negative_create': True, 'result_index': 0, 'entity_type': 'shared_set',
             'customer_id': cid, 'name': name, 'proof': copy.deepcopy(proof)}]


def validate_shared_negative_create_plan(plan):
    from .rails import RailViolation
    cid = demand_gen_ad_id(plan.mutate_customer_id)
    if (set(vars(plan)) != {'mutate_customer_id', 'operations', 'validate_only_supported', 'kind', 'post_checks'}
            or plan.kind != 'entity' or type(plan.operations) is not list
            or plan.validate_only_supported is not True or len(plan.operations) != 1
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('shared negative creation requires one operation and closed saved check')
    check = plan.post_checks[0]
    _shared_keys(check, 'shared_negative_create result_index entity_type customer_id name proof')
    operation = shared_negative_create_operation(check['name'])
    _validate_shared_creation_proof(cid, check['name'], check['proof'])
    if (check['shared_negative_create'] is not True or type(check['result_index']) is not int
            or plan.operations != [operation]
            or plan.post_checks != shared_negative_create_checks(cid, check['name'], check['proof'])):
        raise RailViolation('shared negative graph differs from closed intent and proof')
    return _SharedNegativeContext(plan)


def verify_shared_negative_create_results(checks, result):
    from .rails import EntityMutationPlan, RailViolation
    if (type(checks) is not list or len(checks) != 1 or type(checks[0]) is not dict
            or type(result) is not dict or result.get('validate_only')
            or type(result.get('results')) is not list or len(result['results']) != 1):
        raise RailViolation('shared negative result count or validation state mismatch')
    check = checks[0]
    cid = demand_gen_ad_id(check.get('customer_id'))
    operation = shared_negative_create_operation(check.get('name'))
    validate_shared_negative_create_plan(EntityMutationPlan(cid, [operation], True, post_checks=checks))
    entry = result['results'][0]
    if type(entry) is not dict or entry.get('type') != 'shared_set_result':
        raise RailViolation('shared negative result type mismatch')
    rn = entry.get('resource_name')
    if type(rn) is not str or not re.fullmatch(rf'customers/{cid}/sharedSets/[1-9][0-9]*', rn):
        raise RailViolation('shared negative result ownership or identity mismatch')
    set_id = demand_gen_ad_id(rn.rsplit('/', 1)[1])
    if rn != shared_set_path(cid, set_id):
        raise RailViolation('shared negative canonical identity mismatch')
    fields = ', '.join('shared_set.' + key for key in _SHARED_SET_FIELDS)
    rows = _scan_rows(f"SELECT {fields} FROM shared_set WHERE shared_set.resource_name = '{rn}'", cid)
    if len(rows) != 1:
        raise RailViolation('saved shared set requires exactly one row')
    saved = _shared_set_row(cid, rows[0], counts=True)
    if (saved['resource_name'] != rn or saved['name'] != check['name']
            or saved['member_count'] != 0 or saved['reference_count'] != 0):
        raise RailViolation('saved shared set differs from empty unattached intent')
    for kind, selected in (
        ('shared_criterion', ('resource_name', 'shared_set', 'criterion_id', 'type', 'negative',
                              'keyword.text', 'keyword.match_type')),
        ('campaign_shared_set', ('resource_name', 'campaign', 'shared_set', 'status')),
    ):
        fields = ', '.join(kind + '.' + key for key in selected)
        if _scan_rows(f"SELECT {fields} FROM {kind} WHERE {kind}.shared_set = '{rn}'", cid):
            raise RailViolation('new shared set has unexpected members, links or tombstones')
    expected = dict(saved)
    del expected['member_count'], expected['reference_count']
    inventory = shared_negative_name_inventory(cid)
    if (demand_gen_account_state(cid) != check['proof']['account']
            or inventory != sorted(check['proof']['sets'] + [expected], key=lambda item: item['resource_name'])):
        raise RailViolation('saved shared negative account or name inventory drift')
    return [rn]


def shared_negative_keywords(values):
    from .rails import RailViolation, _keywords
    if (type(values) is not list or any(type(item) is not dict or
            set(item) != {'text', 'match_type'} or
            any(type(value) is not str for value in item.values()) for item in values)):
        raise RailViolation('shared keywords require exact list, dictionaries and strings')
    return _keywords(values)


def shared_criterion_path(cid, sid, criterion_id):
    from google.ads.googleads.v25.services.services.shared_criterion_service import (
        SharedCriterionServiceClient,
    )
    return SharedCriterionServiceClient.shared_criterion_path(
        demand_gen_ad_id(cid), demand_gen_ad_id(sid), demand_gen_ad_id(criterion_id))


def _shared_link_path(cid, campaign_id, sid):
    from google.ads.googleads.v25.services.services.campaign_shared_set_service import (
        CampaignSharedSetServiceClient,
    )
    return CampaignSharedSetServiceClient.campaign_shared_set_path(
        demand_gen_ad_id(cid), demand_gen_ad_id(campaign_id), demand_gen_ad_id(sid))


def _shared_raw(row, kind, message):
    from .rails import RailViolation
    raw = getattr(row, kind, None)
    if (not hasattr(raw, '_pb') or
            raw._pb.DESCRIPTOR.full_name != _listing_message(message)._pb.DESCRIPTOR.full_name):
        raise RailViolation('shared proof requires raw selected schema message')
    return raw


def _shared_scan(cid, kind, fields, condition):
    selected = ', '.join(kind + '.' + field for field in fields.split())
    return _scan_rows(f'SELECT {selected} FROM {kind} WHERE {condition}', cid)


def _validate_shared_population(cid, sid, proof):
    from .rails import RailViolation
    _shared_keys(proof, 'account set members links campaigns')
    _validate_shared_creation_proof(cid, '', {'account': proof['account'], 'sets': []})
    metadata = dict(proof['set']) if type(proof['set']) is dict else {}
    if 'vertical_ads_item_vertical_type' not in metadata or metadata.pop('vertical_ads_item_vertical_type') is not None:
        raise RailViolation('shared set requires proved absence of vertical type')
    _validate_shared_set_metadata(cid, metadata, counts=True)
    rn = shared_set_path(cid, sid)
    if proof['set']['resource_name'] != rn:
        raise RailViolation('selected shared set differs from requested identity')
    for key in ('members', 'links', 'campaigns'):
        items = proof[key]
        if type(items) is not list:
            raise RailViolation('shared population requires lists')
        identities = []
        for item in items:
            if type(item) is not dict:
                raise RailViolation('shared population requires dictionaries')
            identities.append(item.get('resource_name'))
        if any(type(value) is not str for value in identities) or identities != sorted(set(identities)):
            raise RailViolation('shared population identities must be unique and canonical sorted')
    keywords = []
    for member in proof['members']:
        _shared_keys(member, 'resource_name shared_set criterion_id type negative keyword')
        if (member['resource_name'] != shared_criterion_path(cid, sid, member['criterion_id'])
                or member['shared_set'] != rn or member['type'] != 'KEYWORD' or member['negative'] is not True):
            raise RailViolation('shared member identity or negative keyword type invalid')
        keywords.append(member['keyword'])
    if keywords:
        shared_negative_keywords(keywords)
    active = []
    for link in proof['links']:
        _shared_keys(link, 'resource_name campaign shared_set status')
        campaign = link['campaign']
        if type(campaign) is not str or not re.fullmatch(rf'customers/{cid}/campaigns/[1-9][0-9]*', campaign):
            raise RailViolation('shared link campaign ownership invalid')
        campaign_id = demand_gen_ad_id(campaign.rsplit('/', 1)[1])
        if (link['resource_name'] != _shared_link_path(cid, campaign_id, sid)
                or link['shared_set'] != rn or link['status'] not in ('ENABLED', 'REMOVED')):
            raise RailViolation('shared link identity or status invalid')
        if link['status'] == 'ENABLED':
            active.append(campaign)
    for campaign in proof['campaigns']:
        _validate_shared_campaign(cid, campaign)
    if (sorted(active) != [item['resource_name'] for item in proof['campaigns']]
            or len(active) != proof['set']['reference_count']
            or len(proof['members']) != proof['set']['member_count']):
        raise RailViolation('shared provider counts or complete campaign population mismatch')


def _validate_shared_campaign(cid, campaign):
    from .rails import RailViolation
    _shared_keys(campaign, 'resource_name id name status advertising_channel_type advertising_channel_sub_type')
    if (campaign['resource_name'] != campaign_path(cid, demand_gen_ad_id(campaign['id']))
            or type(campaign['name']) is not str or not campaign['name'].strip()
            or campaign['status'] != 'PAUSED' or campaign['advertising_channel_type'] != 'SEARCH'
            or campaign['advertising_channel_sub_type'] != 'UNSPECIFIED'):
        raise RailViolation('every attached campaign must be PAUSED standard Search')


def shared_negative_population(cid, sid):
    from .rails import RailViolation
    rn = shared_set_path(cid, sid)
    proof = {'account': demand_gen_account_state(cid), 'members': [], 'links': [], 'campaigns': []}
    rows = _shared_scan(cid, 'shared_set', ' '.join((*_SHARED_SET_FIELDS, 'vertical_ads_item_vertical_type')), f"shared_set.resource_name = '{rn}'")
    if len(rows) != 1:
        raise RailViolation('selected shared set requires exactly one row')
    proof['set'] = _shared_set_row(cid, rows[0], counts=True)
    if rows[0].shared_set._pb.HasField('vertical_ads_item_vertical_type'):
        raise RailViolation('negative keyword shared set has inapplicable vertical type')
    proof['set']['vertical_ads_item_vertical_type'] = None
    for row in _shared_scan(cid, 'shared_criterion',
            'resource_name shared_set criterion_id type negative keyword.text keyword.match_type',
            f"shared_criterion.shared_set = '{rn}'"):
        raw = _shared_raw(row, 'shared_criterion', 'SharedCriterion')
        if raw._pb.WhichOneof('criterion') != 'keyword':
            raise RailViolation('shared members must have keyword oneof')
        item = {key: _dg_ad_scalar(raw, key) for key in ('resource_name', 'shared_set', 'negative')}
        item.update(criterion_id=str(_dg_ad_scalar(raw, 'criterion_id')),
            type=_pmax_enum_exact('CriterionTypeEnum', raw.type_, {'KEYWORD'}),
            keyword={'text': _dg_ad_scalar(raw.keyword, 'text'),
                     'match_type': _pmax_enum_exact('KeywordMatchTypeEnum', raw.keyword.match_type, {'EXACT', 'PHRASE', 'BROAD'})})
        proof['members'].append(item)
    for row in _shared_scan(cid, 'campaign_shared_set', 'resource_name campaign shared_set status',
            f"campaign_shared_set.shared_set = '{rn}'"):
        raw = _shared_raw(row, 'campaign_shared_set', 'CampaignSharedSet')
        item = {key: _dg_ad_scalar(raw, key) for key in ('resource_name', 'campaign', 'shared_set')}
        item['status'] = _pmax_enum_exact('CampaignSharedSetStatusEnum', raw.status, {'ENABLED', 'REMOVED'})
        proof['links'].append(item)
    for link in proof['links']:
        if link['status'] != 'ENABLED':
            continue
        campaign = link['campaign']
        if type(campaign) is not str or not re.fullmatch(rf'customers/{cid}/campaigns/[1-9][0-9]*', campaign):
            raise RailViolation('shared link campaign ownership invalid')
        proof['campaigns'].append(shared_negative_campaign(cid, campaign))
    for key in ('members', 'links', 'campaigns'):
        proof[key].sort(key=lambda item: item['resource_name'])
    _validate_shared_population(cid, sid, proof)
    return proof


def shared_negative_campaign(cid, campaign):
    from .rails import RailViolation
    rows = _shared_scan(cid, 'campaign',
        'resource_name id name status advertising_channel_type advertising_channel_sub_type',
        f"campaign.resource_name = '{campaign}'")
    if len(rows) != 1:
        raise RailViolation('attached campaign requires exactly one row')
    raw = _shared_raw(rows[0], 'campaign', 'Campaign')
    item = {key: _dg_ad_scalar(raw, key) for key in ('resource_name', 'name')}
    item.update(id=str(_dg_ad_scalar(raw, 'id')),
        status=_pmax_enum_exact('CampaignStatusEnum', raw.status, {'PAUSED'}),
        advertising_channel_type=_pmax_enum_exact('AdvertisingChannelTypeEnum', raw.advertising_channel_type, {'SEARCH'}),
        advertising_channel_sub_type=_pmax_enum_exact('AdvertisingChannelSubTypeEnum', raw.advertising_channel_sub_type, {'UNSPECIFIED'}))
    _validate_shared_campaign(cid, item)
    if item['resource_name'] != campaign:
        raise RailViolation('requested campaign differs from selected campaign')
    return item


def shared_negative_add_operations(cid, sid, keywords):
    from .rails import safe_create_operation
    rn = shared_set_path(cid, sid)
    return [safe_create_operation('SharedCriterionService', {'shared_set': rn, 'negative': True,
             'keyword': item}) for item in shared_negative_keywords(keywords)]


def shared_negative_add_checks(cid, sid, keywords, proof):
    import copy
    return [{'shared_negative_add': True, 'customer_id': cid, 'shared_set_id': sid,
             'keywords': copy.deepcopy(keywords), 'proof': copy.deepcopy(proof)}]


def validate_shared_negative_plan(plan):
    if (type(plan.post_checks) is list and len(plan.post_checks) == 1
            and type(plan.post_checks[0]) is dict and 'shared_negative_attach' in plan.post_checks[0]):
        return validate_shared_negative_attach_plan(plan)
    if (type(plan.post_checks) is list and len(plan.post_checks) == 1
            and type(plan.post_checks[0]) is dict and 'shared_negative_add' in plan.post_checks[0]):
        return validate_shared_negative_add_plan(plan)
    return validate_shared_negative_create_plan(plan)


def validate_shared_negative_add_plan(plan):
    from .rails import RailViolation
    cid = demand_gen_ad_id(plan.mutate_customer_id)
    if (set(vars(plan)) != {'mutate_customer_id', 'operations', 'validate_only_supported', 'kind', 'post_checks'}
            or plan.kind != 'entity' or type(plan.operations) is not list
            or plan.validate_only_supported is not True
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('shared additions require closed operations and saved proof')
    check = plan.post_checks[0]
    _shared_keys(check, 'shared_negative_add customer_id shared_set_id keywords proof')
    sid = demand_gen_ad_id(check['shared_set_id'])
    keywords = shared_negative_keywords(check['keywords'])
    _validate_shared_population(cid, sid, check['proof'])
    existing = {(item['keyword']['text'].strip().casefold(), item['keyword']['match_type'])
                for item in check['proof']['members']}
    if any((item['text'].casefold(), item['match_type']) in existing for item in keywords):
        raise RailViolation('addition collides with existing shared keyword')
    if (check['shared_negative_add'] is not True or check['customer_id'] != cid
            or keywords != check['keywords']
            or any(type(op.operation) is not dict or type(op.operation.get('create')) is not dict
                   or op.operation['create'].get('negative') is not True for op in plan.operations)
            or plan.operations != shared_negative_add_operations(cid, sid, keywords)):
        raise RailViolation('shared additions differ from closed intent and proof')
    return _SharedNegativeContext(plan)


def verify_shared_negative_add_results(checks, result):
    import copy

    from .rails import EntityMutationPlan, RailViolation
    check = checks[0]
    cid, sid = check['customer_id'], check['shared_set_id']
    operations = shared_negative_add_operations(cid, sid, check['keywords'])
    validate_shared_negative_add_plan(EntityMutationPlan(cid, operations, True, post_checks=checks))
    if (type(result) is not dict or result.get('validate_only') or
            type(result.get('results')) is not list or len(result['results']) != len(operations)):
        raise RailViolation('shared addition result count mismatch')
    expected = copy.deepcopy(check['proof'])
    identities = {item['resource_name'] for item in expected['members']}
    returned = []
    for entry, keyword in zip(result['results'], check['keywords']):
        if (type(entry) is not dict or entry.get('type') != 'shared_criterion_result'
                or type(entry.get('resource_name')) is not str
                or not re.fullmatch(rf'customers/{cid}/sharedCriteria/{sid}~[1-9][0-9]*', entry['resource_name'])):
            raise RailViolation('shared criterion result identity or type invalid')
        rn = entry['resource_name']
        criterion_id = demand_gen_ad_id(rn.rsplit('~', 1)[1])
        if rn != shared_criterion_path(cid, sid, criterion_id) or rn in identities:
            raise RailViolation('shared criterion result duplicate or noncanonical')
        identities.add(rn)
        returned.append(rn)
        expected['members'].append({'resource_name': rn, 'shared_set': shared_set_path(cid, sid),
            'criterion_id': criterion_id, 'type': 'KEYWORD', 'negative': True, 'keyword': keyword})
    expected['members'].sort(key=lambda item: item['resource_name'])
    expected['set']['member_count'] += len(operations)
    if shared_negative_population(cid, sid) != expected:
        raise RailViolation('saved shared population differs from exact old plus ordered additions')
    return returned


def shared_negative_attach_operation(cid, sid, campaign_id):
    from .rails import safe_create_operation
    return safe_create_operation('CampaignSharedSetService', {
        'campaign': campaign_path(cid, demand_gen_ad_id(campaign_id)), 'shared_set': shared_set_path(cid, sid)})


def shared_negative_attach_checks(cid, sid, campaign_id, proof, target):
    import copy
    return [{'shared_negative_attach': True, 'customer_id': cid, 'shared_set_id': sid,
             'campaign_id': campaign_id, 'proof': copy.deepcopy(proof), 'target': copy.deepcopy(target)}]


def validate_shared_negative_attach_plan(plan):
    from .rails import RailViolation
    cid = demand_gen_ad_id(plan.mutate_customer_id)
    if (set(vars(plan)) != {'mutate_customer_id', 'operations', 'validate_only_supported', 'kind', 'post_checks'}
            or plan.kind != 'entity' or type(plan.operations) is not list
            or plan.validate_only_supported is not True
            or type(plan.post_checks) is not list or len(plan.post_checks) != 1):
        raise RailViolation('shared attachment requires closed operation and saved proof')
    check = plan.post_checks[0]
    _shared_keys(check, 'shared_negative_attach customer_id shared_set_id campaign_id proof target')
    sid, campaign_id = demand_gen_ad_id(check['shared_set_id']), demand_gen_ad_id(check['campaign_id'])
    _validate_shared_population(cid, sid, check['proof'])
    _validate_shared_campaign(cid, check['target'])
    if (check['target']['resource_name'] != campaign_path(cid, campaign_id)
            or any(link['campaign'] == check['target']['resource_name'] for link in check['proof']['links'])):
        raise RailViolation('target differs from requested campaign or already has active/removed link')
    if (check['shared_negative_attach'] is not True or check['customer_id'] != cid
            or plan.operations != [shared_negative_attach_operation(cid, sid, campaign_id)]):
        raise RailViolation('shared attachment differs from closed intent and independent proof')
    return _SharedNegativeContext(plan)


def verify_shared_negative_attach_results(checks, result):
    import copy

    from .rails import EntityMutationPlan, RailViolation
    check = checks[0]
    cid, sid, campaign_id = check['customer_id'], check['shared_set_id'], check['campaign_id']
    operation = shared_negative_attach_operation(cid, sid, campaign_id)
    validate_shared_negative_attach_plan(EntityMutationPlan(cid, [operation], True, post_checks=checks))
    rn = _shared_link_path(cid, campaign_id, sid)
    if (type(result) is not dict or result.get('validate_only')
            or type(result.get('results')) is not list or len(result['results']) != 1
            or type(result['results'][0]) is not dict
            or result['results'][0].get('type') != 'campaign_shared_set_result'
            or result['results'][0].get('resource_name') != rn):
        raise RailViolation('shared attachment result must equal exact requested compound identity')
    expected = copy.deepcopy(check['proof'])
    expected['links'].append({'resource_name': rn, 'campaign': campaign_path(cid, campaign_id),
                             'shared_set': shared_set_path(cid, sid), 'status': 'ENABLED'})
    expected['campaigns'].append(copy.deepcopy(check['target']))
    for key in ('links', 'campaigns'):
        expected[key].sort(key=lambda item: item['resource_name'])
    expected['set']['reference_count'] += 1
    if shared_negative_population(cid, sid) != expected:
        raise RailViolation('saved shared population differs from exact old plus target attachment')
    return [rn]
