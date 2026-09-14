"""Advertiser settings: one optional YAML file, loaded once at server startup.

Path resolution: explicit arg -> GOOGLE_ADS_ADVERTISER_CONFIG env var -> default
~/.mcp-google-ads-safe/advertiser.yaml (~ expanded). Missing file, or a file that
yaml loads as None (empty), means all defaults. Any other shape problem is a
loud ValueError naming the offending key and the file path -- this fails at
server startup, not mid-tool-call.

keyword_research uses the Google Keyword Planner shape (geo_target_constant_ids /
language_constant_id / keyword_plan_network / include_adult_keywords).
"""
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
_ALLOWED_KEYS = frozenset({"blocked_terms", "advertiser_domain", "keyword_research"})
_ALLOWED_KEYWORD_RESEARCH_KEYS = frozenset(_KEYWORD_RESEARCH_DEFAULTS)

_cache: dict | None = None


def _defaults() -> dict:
    return {
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


def _validate(raw: dict, path: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: root must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {sorted(unknown)}")

    result = _defaults()

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


def blocked_terms() -> tuple[str, ...]:
    if _cache is None:
        load()
    return tuple(_cache["blocked_terms"])


def advertiser_domain() -> str:
    if _cache is None:
        load()
    return _cache["advertiser_domain"]


def keyword_research() -> dict:
    if _cache is None:
        load()
    return dict(_cache["keyword_research"])


def config_path(path: str | None = None) -> str:
    """Resolved settings file path (same resolution as load()) -- for error messages
    naming where to fix a setting, whether or not the file exists."""
    return os.path.expanduser(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
