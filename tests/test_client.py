"""client.py seam — pure money helpers have real bodies; API functions are B3 stubs."""
import decimal

import pytest

from mcp_google_ads_safe import client

# --- to_micros (real) ------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("50", 50_000_000),
    ("12.5", 12_500_000),
    (75, 75_000_000),
    (decimal.Decimal("1000.01"), 1_000_010_000),
    ("0.000001", 1),
])
def test_to_micros_valid(value, expected):
    assert client.to_micros(value) == expected


def test_to_micros_avoids_binary_float_noise():
    # 12.10 as a raw float * 1e6 would drift; parsing via Decimal(str(...)) keeps it exact.
    assert client.to_micros(12.10) == 12_100_000


@pytest.mark.parametrize("bad", [True, False, "nan", "inf", "-5", "0", "0.0", "abc", None])
def test_to_micros_rejects(bad):
    with pytest.raises(ValueError):
        client.to_micros(bad)


def test_to_micros_rejects_sub_micro_precision():
    with pytest.raises(ValueError):
        client.to_micros("1.0000005")


def test_to_micros_rejects_over_int64():
    with pytest.raises(ValueError):
        client.to_micros(str(2**63))  # * 1e6 far exceeds int64


# --- from_micros (real) ----------------------------------------------------------------

@pytest.mark.parametrize("micros,expected", [
    (12_500_000, "12.5"),
    (1_000_000, "1"),
    (1, "0.000001"),
    (12_505_000, "12.505"),
])
def test_from_micros(micros, expected):
    assert client.from_micros(micros) == expected


# --- seam invariant (B3 implemented the API layer; full coverage in test_client_api.py) -

def test_client_does_not_import_rails():
    """The seam is one-directional: rails imports client, never the reverse. A rails import
    at client load time would be a circular-import trap for B3. Namespace check (not string
    matching) so the docstring's mention of `from .rails` in the gaql_all contract is fine."""
    assert not hasattr(client, "rails")
    assert not hasattr(client, "RailViolation")
