"""Advertiser settings: one optional YAML file, loaded once at server startup.

Path resolution: explicit arg -> GOOGLE_ADS_ADVERTISER_CONFIG env var -> default
~/.mcp-google-ads-safe/advertiser.yaml (~ expanded). Missing file, or a file that
yaml loads as None (empty), means all defaults. Any other shape problem is a
loud ValueError naming the offending key and the file path -- this fails at
server startup, not mid-tool-call.

keyword_research uses the Google Keyword Planner shape (geo_target_constant_ids /
language_constant_id / keyword_plan_network / include_adult_keywords).

Agencies: an optional `accounts:` map keyed by customer id gives one account its own
advertiser_domain, blocked_terms (added to the top-level list, never replacing it),
keyword_research and spend caps (max_daily_budget, max_cpc, max_target_cpa). Top-level
values are the default for every account not listed. Which account's rules apply is set
per tool call with set_account(); when an accounts map exists and no account is set, every
getter refuses rather than fall back to the wrong client's rules.
"""
import contextvars
import math
import os

import yaml

ENV_VAR = "GOOGLE_ADS_ADVERTISER_CONFIG"
DEFAULT_PATH = "~/.mcp-google-ads-safe/advertiser.yaml"

_MAX_GEO_TARGETS = 10
_KEYWORD_RESEARCH_DEFAULTS = {
    "geo_target_constant_ids": [],
    "language_constant_id": None,
    "keyword_plan_network": "GOOGLE_SEARCH",
    "include_adult_keywords": False,
}
_RULE_KEYS = frozenset({"blocked_terms", "advertiser_domain", "keyword_research"})
_CAP_KEYS = ("max_daily_budget", "max_cpc", "max_target_cpa")
_ALLOWED_KEYS = _RULE_KEYS | {"accounts"}
_ALLOWED_ACCOUNT_KEYS = _RULE_KEYS | set(_CAP_KEYS)
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("active_account", default=None)
_ALLOWED_KEYWORD_RESEARCH_KEYS = frozenset(_KEYWORD_RESEARCH_DEFAULTS)

_cache: dict | None = None


def _defaults() -> dict:
    return {
        "accounts": {},
        "blocked_terms": [],
        "advertiser_domain": "",
        "keyword_research": {k: (list(v) if isinstance(v, list) else v)
                             for k, v in _KEYWORD_RESEARCH_DEFAULTS.items()},
    }


def _validate_keyword_research(kr, path: str, result: dict) -> None:
    if not isinstance(kr, dict):
        raise ValueError(f"{path}: 'keyword_research' must be a mapping")
    unknown_kr = set(kr) - _ALLOWED_KEYWORD_RESEARCH_KEYS
    if unknown_kr:
        raise ValueError(f"{path}: unknown key(s) in 'keyword_research': {sorted(unknown_kr)}")

    if "geo_target_constant_ids" in kr:
        geos = kr["geo_target_constant_ids"]
        if not isinstance(geos, list):
            raise ValueError(f"{path}: 'keyword_research.geo_target_constant_ids' must be a list")
        if len(geos) > _MAX_GEO_TARGETS:
            raise ValueError(
                f"{path}: 'keyword_research.geo_target_constant_ids' allows at most "
                f"{_MAX_GEO_TARGETS} ids (got {len(geos)})")
        for g in geos:
            if isinstance(g, bool) or not isinstance(g, int) or g <= 0:
                raise ValueError(
                    f"{path}: 'keyword_research.geo_target_constant_ids' entries must be positive ints")
        result["keyword_research"]["geo_target_constant_ids"] = list(geos)

    if "language_constant_id" in kr:
        lang = kr["language_constant_id"]
        if lang is None:
            result["keyword_research"]["language_constant_id"] = None
        elif isinstance(lang, bool):
            raise ValueError(
                f"{path}: 'keyword_research.language_constant_id' must be a string, int, or null")
        elif isinstance(lang, int):
            if lang <= 0:
                raise ValueError(
                    f"{path}: 'keyword_research.language_constant_id' int must be positive")
            result["keyword_research"]["language_constant_id"] = lang
        elif isinstance(lang, str):
            if not lang.strip():
                raise ValueError(
                    f"{path}: 'keyword_research.language_constant_id' string must be non-empty")
            result["keyword_research"]["language_constant_id"] = lang
        else:
            raise ValueError(
                f"{path}: 'keyword_research.language_constant_id' must be a string, int, or null")

    if "keyword_plan_network" in kr:
        network = kr["keyword_plan_network"]
        if not isinstance(network, str) or not network.strip():
            raise ValueError(
                f"{path}: 'keyword_research.keyword_plan_network' must be a non-empty string")
        result["keyword_research"]["keyword_plan_network"] = network

    if "include_adult_keywords" in kr:
        adult = kr["include_adult_keywords"]
        if not isinstance(adult, bool):
            raise ValueError(
                f"{path}: 'keyword_research.include_adult_keywords' must be a boolean")
        result["keyword_research"]["include_adult_keywords"] = adult


def _digits(value) -> str:
    return "".join(ch for ch in str(value) if ch in "0123456789")


def _validate_accounts(accounts, path: str) -> dict:
    if not isinstance(accounts, dict):
        raise ValueError(f"{path}: 'accounts' must be a mapping of customer id to rules")
    out = {}
    for key, rules in accounts.items():
        cid = _digits(key)
        if isinstance(key, bool) or len(cid) != 10 or str(key).strip("0123456789- ") != "":
            raise ValueError(f"{path}: accounts key {key!r} must be a 10-digit customer id")
        if cid in out:
            raise ValueError(f"{path}: account {cid} is listed more than once in 'accounts'")
        where = f"{path}: accounts[{cid}]"
        if not isinstance(rules, dict):
            raise ValueError(f"{where} must be a mapping")
        unknown = set(rules) - _ALLOWED_ACCOUNT_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}")
        checked = _validate({k: v for k, v in rules.items() if k in _RULE_KEYS}, where,
                            allowed=_RULE_KEYS)
        entry = {k: checked[k] for k in _RULE_KEYS if k in rules}
        for cap in _CAP_KEYS:
            if cap in rules:
                value = rules[cap]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError(f"{where}: '{cap}' must be a positive number")
                entry[cap] = value
        out[cid] = entry
    return out


def _validate(raw: dict, path: str, allowed=_ALLOWED_KEYS) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: root must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {sorted(unknown)}")

    result = _defaults()
    result["accounts"] = _validate_accounts(raw["accounts"], path) if "accounts" in raw else {}

    if "blocked_terms" in raw:
        terms = raw["blocked_terms"]
        if not isinstance(terms, list):
            raise ValueError(f"{path}: 'blocked_terms' must be a list")
        cleaned = []
        for term in terms:
            if not isinstance(term, str) or not term.strip():
                raise ValueError(f"{path}: 'blocked_terms' entries must be non-empty strings")
            # rails.check_content lowers only the text side of the comparison; a
            # configured term must be lowered here or a capitalized entry (as an
            # advertiser would naturally type "Backup" in advertiser.yaml) never matches.
            cleaned.append(term.strip().lower())
        result["blocked_terms"] = cleaned

    if "advertiser_domain" in raw:
        domain = raw["advertiser_domain"]
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError(f"{path}: 'advertiser_domain' must be a non-empty string")
        result["advertiser_domain"] = domain

    if "keyword_research" in raw:
        _validate_keyword_research(raw["keyword_research"], path, result)

    return result


def load(path: str | None = None) -> dict:
    """Resolve, parse, validate, cache, and return the advertiser settings dict.

    Always re-reads from disk (no short-circuit on an existing cache) so tests
    and repeated startups see the current file; see _reset() for full cache
    isolation between tests."""
    global _cache
    resolved = path or os.environ.get(ENV_VAR) or DEFAULT_PATH
    full_path = os.path.expanduser(resolved)

    try:
        with open(full_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raw = None

    _cache = _defaults() if raw is None else _validate(raw, full_path)
    return _cache


def _reset() -> None:
    """Test-only: clear the cache so tests don't leak state into each other."""
    global _cache
    _cache = None
    _ACTIVE.set(None)


def set_account(customer_id) -> None:
    """Say which account's rules apply from here on in this tool call (None clears it)."""
    _ACTIVE.set(_digits(customer_id) or None if customer_id is not None else None)


def active_account() -> str | None:
    return _ACTIVE.get()


def _rules() -> tuple[dict, dict]:
    """(top-level defaults, the active account's overrides). With an accounts map and no
    active account this refuses: guessing would apply one client's rules to another."""
    if _cache is None:
        load()
    accounts = _cache["accounts"]
    if not accounts:
        return _cache, {}
    cid = _ACTIVE.get()
    if cid is None:
        from .rails import RailViolation
        raise RailViolation(
            "advertiser.yaml has per-account rules, and this step did not say which account "
            "it is for — refusing rather than apply the wrong account's rules",
            code="NO_ACCOUNT_CONTEXT")
    return _cache, accounts.get(cid, {})


def blocked_terms() -> tuple[str, ...]:
    base, own = _rules()
    return tuple(dict.fromkeys([*base["blocked_terms"], *own.get("blocked_terms", [])]))


def advertiser_domain() -> str:
    base, own = _rules()
    return own.get("advertiser_domain", base["advertiser_domain"])


def keyword_research() -> dict:
    base, own = _rules()
    return dict(own.get("keyword_research", base["keyword_research"]))


def cap(name: str):
    """The active account's own cap (max_daily_budget | max_cpc | max_target_cpa), or None
    when it has none and the environment default applies."""
    return _rules()[1].get(name)


def caps_source() -> str:
    own = _rules()[1]
    if any(name in own for name in _CAP_KEYS):
        return f"advertiser.yaml accounts section for {_ACTIVE.get()}"
    return "environment defaults"


def config_path(path: str | None = None) -> str:
    """Resolved settings file path (same resolution as load()) -- for error messages
    naming where to fix a setting, whether or not the file exists."""
    return os.path.expanduser(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
