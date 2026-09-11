"""Bidding classification (5 classes) + enum-exhaustiveness pin + pct->multiplier."""
import pytest

from mcp_google_ads_safe import rails

# --- per-class classification ----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(rails.SMART_BIDDING))
def test_classify_smart(name):
    assert rails.classify_bidding_strategy(name) == ("SMART", "BID_SMART")


@pytest.mark.parametrize("name", sorted(rails.SUPPORTED_MANUAL))
def test_classify_supported_manual(name):
    assert rails.classify_bidding_strategy(name) == ("SUPPORTED_MANUAL", None)


@pytest.mark.parametrize("name", sorted(rails.POLICY_DENIED_BIDDING))
def test_classify_policy_denied(name):
    assert rails.classify_bidding_strategy(name) == ("POLICY_DENIED", "BID_POLICY")


def test_enhanced_cpc_is_policy_denied_not_manual():
    """Deliberate divergence from the Microsoft allowlist (which permitted EnhancedCpc):
    Google retired eCPC in 2025, so it fails closed here."""
    assert rails.classify_bidding_strategy("ENHANCED_CPC") == ("POLICY_DENIED", "BID_POLICY")


@pytest.mark.parametrize("name", ["UNKNOWN", "UNSPECIFIED", "INVALID", "SOME_FUTURE_STRATEGY"])
def test_classify_unrecognized(name):
    assert rails.classify_bidding_strategy(name) == ("UNRECOGNIZED", "BID_UNRECOGNIZED")


@pytest.mark.parametrize("name", [None, ""])
def test_classify_unreadable(name):
    assert rails.classify_bidding_strategy(name) == ("UNREADABLE", "BID_UNREADABLE")


# --- exhaustiveness pin against the installed google-ads 31.4.0 -------------------------

def test_bidding_strategy_enum_exhaustively_classified():
    """Every BiddingStrategyType enum NAME in the pinned library must land in exactly one
    of SMART / SUPPORTED_MANUAL / POLICY_DENIED, OR be a known non-strategy sentinel
    (UNKNOWN / UNSPECIFIED / INVALID). A brand-new enum value the library adds later falls
    through to neither and FAILS this test, forcing a human to triage it."""
    from google.ads.googleads.v25.enums.types.bidding_strategy_type import (
        BiddingStrategyTypeEnum,
    )

    names = {f.name for f in BiddingStrategyTypeEnum.BiddingStrategyType}
    assert names, "no enum members found — wrong import path?"

    classified = rails.SMART_BIDDING | rails.SUPPORTED_MANUAL | rails.POLICY_DENIED_BIDDING
    for name in names:
        covered = name in classified or name in rails.UNRECOGNIZED_SENTINELS
        assert covered, f"{name} is not classified and not a known sentinel — triage it"

    # No overlap between the three real-strategy classes.
    assert not (rails.SMART_BIDDING & rails.SUPPORTED_MANUAL)
    assert not (rails.SMART_BIDDING & rails.POLICY_DENIED_BIDDING)
    assert not (rails.SUPPORTED_MANUAL & rails.POLICY_DENIED_BIDDING)


# --- check_bid_write_allowed -----------------------------------------------------------

def test_check_bid_write_allowed_passes_supported_manual():
    for name in rails.SUPPORTED_MANUAL:
        rails.check_bid_write_allowed(name, "keyword bid change")  # no raise


@pytest.mark.parametrize("name,code,match", [
    ("MAXIMIZE_CONVERSIONS", "BID_SMART", "Smart Bidding"),
    ("ENHANCED_CPC", "BID_POLICY", "not an owner-allowed"),
    ("SOME_FUTURE_STRATEGY", "BID_UNRECOGNIZED", "not a recognized"),
    (None, "BID_UNREADABLE", "could not be determined"),
])
def test_check_bid_write_allowed_refuses(name, code, match):
    with pytest.raises(rails.RailViolation, match=match) as exc:
        rails.check_bid_write_allowed(name, "keyword bid change")
    assert exc.value.code == code


# --- bid pct -> API multiplier round-trip ----------------------------------------------

@pytest.mark.parametrize("pct,mult", [(-90, 0.1), (0, 1.0), (100, 2.0), (900, 10.0)])
def test_bid_adjustment_pct_to_multiplier(pct, mult):
    assert rails.bid_adjustment_pct_to_multiplier(pct) == mult


@pytest.mark.parametrize("pct", [-91, 901, 1000])
def test_bid_adjustment_pct_out_of_range(pct):
    with pytest.raises(rails.RailViolation, match="out of range"):
        rails.bid_adjustment_pct_to_multiplier(pct)


def test_bid_adjustment_pct_rejects_bool():
    with pytest.raises(rails.RailViolation):
        rails.bid_adjustment_pct_to_multiplier(True)
