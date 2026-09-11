"""Advertiser settings — Google keyword_research shape, strict validation."""
import re

import pytest

from mcp_google_ads_safe import settings

_DEFAULT_KEYWORD_RESEARCH = {
    "geo_target_constant_ids": [],
    "language_constant_id": None,
    "keyword_plan_network": "GOOGLE_SEARCH",
    "include_adult_keywords": False,
}


@pytest.fixture(autouse=True)
def reset_settings_cache():
    settings._reset()
    yield
    settings._reset()


def _write(tmp_path, content, name="advertiser.yaml"):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# --- missing / empty -> defaults -------------------------------------------------------

def test_missing_file_gives_defaults(tmp_path):
    result = settings.load(str(tmp_path / "does-not-exist.yaml"))
    assert result == {
        "blocked_terms": [],
        "advertiser_domain": "",
        "keyword_research": _DEFAULT_KEYWORD_RESEARCH,
    }


def test_empty_file_gives_defaults(tmp_path):
    result = settings.load(_write(tmp_path, ""))
    assert result["keyword_research"] == _DEFAULT_KEYWORD_RESEARCH


def test_require_pmax_target_cpa_is_dropped(tmp_path):
    """The Microsoft-only key must no longer exist as a top-level default, and setting it
    must be rejected as unknown."""
    result = settings.load(_write(tmp_path, ""))
    assert "require_pmax_target_cpa" not in result
    with pytest.raises(ValueError, match="require_pmax_target_cpa"):
        settings.load(_write(tmp_path, "require_pmax_target_cpa: true\n"))


# --- malformed root / unknown keys -----------------------------------------------------

def test_malformed_root_not_a_mapping(tmp_path):
    path = _write(tmp_path, "- one\n- two\n")
    with pytest.raises(ValueError, match=re.escape(path)):
        settings.load(path)


def test_unknown_top_level_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="totally_unknown_key"):
        settings.load(_write(tmp_path, "totally_unknown_key: 1\n"))


def test_keyword_research_unknown_subkey_rejected(tmp_path):
    with pytest.raises(ValueError, match="bogus"):
        settings.load(_write(tmp_path, "keyword_research:\n  bogus: 1\n"))


# --- blocked_terms (lower-cased) -------------------------------------------------------

def test_blocked_terms_lowercased(tmp_path):
    result = settings.load(_write(tmp_path, "blocked_terms:\n  - Sewage\n  - BACKUP\n"))
    assert result["blocked_terms"] == ["sewage", "backup"]


def test_blocked_terms_must_be_list(tmp_path):
    with pytest.raises(ValueError, match="blocked_terms"):
        settings.load(_write(tmp_path, "blocked_terms: not-a-list\n"))


def test_blocked_terms_rejects_blank_entry(tmp_path):
    with pytest.raises(ValueError, match="blocked_terms"):
        settings.load(_write(tmp_path, 'blocked_terms:\n  - sewage\n  - "   "\n'))


# --- geo_target_constant_ids -----------------------------------------------------------

def test_geo_targets_must_be_list(tmp_path):
    with pytest.raises(ValueError, match="geo_target_constant_ids"):
        settings.load(_write(tmp_path, "keyword_research:\n  geo_target_constant_ids: 5\n"))


def test_geo_targets_max_ten(tmp_path):
    ids = "\n".join(f"    - {i}" for i in range(1, 12))  # 11 ids
    with pytest.raises(ValueError, match="at most 10"):
        settings.load(_write(tmp_path, f"keyword_research:\n  geo_target_constant_ids:\n{ids}\n"))


def test_geo_targets_ten_allowed(tmp_path):
    ids = "\n".join(f"    - {i}" for i in range(1, 11))  # 10 ids
    result = settings.load(_write(tmp_path, f"keyword_research:\n  geo_target_constant_ids:\n{ids}\n"))
    assert result["keyword_research"]["geo_target_constant_ids"] == list(range(1, 11))


def test_geo_targets_reject_non_positive_int(tmp_path):
    with pytest.raises(ValueError, match="geo_target_constant_ids"):
        settings.load(_write(tmp_path, "keyword_research:\n  geo_target_constant_ids:\n    - -3\n"))


def test_geo_targets_reject_bool(tmp_path):
    with pytest.raises(ValueError, match="geo_target_constant_ids"):
        settings.load(_write(tmp_path, "keyword_research:\n  geo_target_constant_ids:\n    - true\n"))


# --- language_constant_id (str | int | None) -------------------------------------------

def test_language_constant_id_accepts_string(tmp_path):
    result = settings.load(_write(tmp_path, "keyword_research:\n  language_constant_id: '1000'\n"))
    assert result["keyword_research"]["language_constant_id"] == "1000"


def test_language_constant_id_accepts_int(tmp_path):
    result = settings.load(_write(tmp_path, "keyword_research:\n  language_constant_id: 1000\n"))
    assert result["keyword_research"]["language_constant_id"] == 1000


def test_language_constant_id_accepts_null(tmp_path):
    result = settings.load(_write(tmp_path, "keyword_research:\n  language_constant_id: null\n"))
    assert result["keyword_research"]["language_constant_id"] is None


def test_language_constant_id_rejects_empty_string(tmp_path):
    with pytest.raises(ValueError, match="language_constant_id"):
        settings.load(_write(tmp_path, "keyword_research:\n  language_constant_id: ''\n"))


def test_language_constant_id_rejects_bool(tmp_path):
    with pytest.raises(ValueError, match="language_constant_id"):
        settings.load(_write(tmp_path, "keyword_research:\n  language_constant_id: true\n"))


# --- keyword_plan_network / include_adult_keywords -------------------------------------

def test_keyword_plan_network_must_be_non_empty_string(tmp_path):
    with pytest.raises(ValueError, match="keyword_plan_network"):
        settings.load(_write(tmp_path, "keyword_research:\n  keyword_plan_network: 7\n"))


def test_include_adult_keywords_must_be_bool(tmp_path):
    with pytest.raises(ValueError, match="include_adult_keywords"):
        settings.load(_write(tmp_path, "keyword_research:\n  include_adult_keywords: 'yes'\n"))


def test_keyword_research_partial_override_keeps_other_defaults(tmp_path):
    result = settings.load(_write(tmp_path, "keyword_research:\n  keyword_plan_network: GOOGLE_SEARCH_AND_PARTNERS\n"))
    assert result["keyword_research"] == {
        "geo_target_constant_ids": [],
        "language_constant_id": None,
        "keyword_plan_network": "GOOGLE_SEARCH_AND_PARTNERS",
        "include_adult_keywords": False,
    }


# --- fully populated -------------------------------------------------------------------

def test_populated_file_all_fields(tmp_path):
    result = settings.load(_write(tmp_path, """
blocked_terms:
  - sewage
  - backup
advertiser_domain: example.com
keyword_research:
  geo_target_constant_ids:
    - 2840
    - 21167
  language_constant_id: 1000
  keyword_plan_network: GOOGLE_SEARCH_AND_PARTNERS
  include_adult_keywords: true
"""))
    assert result == {
        "blocked_terms": ["sewage", "backup"],
        "advertiser_domain": "example.com",
        "keyword_research": {
            "geo_target_constant_ids": [2840, 21167],
            "language_constant_id": 1000,
            "keyword_plan_network": "GOOGLE_SEARCH_AND_PARTNERS",
            "include_adult_keywords": True,
        },
    }


# --- accessors / path resolution -------------------------------------------------------

def test_env_var_used_when_no_explicit_path(tmp_path, monkeypatch):
    path = _write(tmp_path, "advertiser_domain: fromenv.test\n")
    monkeypatch.setenv("GOOGLE_ADS_ADVERTISER_CONFIG", path)
    assert settings.load()["advertiser_domain"] == "fromenv.test"


def test_env_var_name_is_google(tmp_path, monkeypatch):
    """The env var must be the Google one; the old Microsoft var must have no effect."""
    monkeypatch.setenv("MS_ADS_ADVERTISER_CONFIG", _write(tmp_path, "advertiser_domain: ms.test\n", name="ms.yaml"))
    monkeypatch.delenv("GOOGLE_ADS_ADVERTISER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # default path -> no file -> defaults
    assert settings.advertiser_domain() == ""


def test_accessors_lazy_load(tmp_path, monkeypatch):
    path = _write(tmp_path, "advertiser_domain: lazy.test\nblocked_terms:\n  - foo\n")
    monkeypatch.setenv("GOOGLE_ADS_ADVERTISER_CONFIG", path)
    assert settings.blocked_terms() == ("foo",)
    assert settings.advertiser_domain() == "lazy.test"
    assert settings.keyword_research() == _DEFAULT_KEYWORD_RESEARCH


def test_config_path_default_when_unset(monkeypatch):
    import os
    monkeypatch.delenv("GOOGLE_ADS_ADVERTISER_CONFIG", raising=False)
    assert settings.config_path() == os.path.expanduser(settings.DEFAULT_PATH)
