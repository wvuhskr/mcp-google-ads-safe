"""Audit log — append-only JSONL, 0700/0600, Google path/env, per-phase schema with digest."""
import json
import os
import stat
import subprocess
import sys

from mcp_google_ads_safe import audit, rails


def test_log_event_appends_jsonl(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    audit.log_event("update_campaign", "draft", {"campaign_id": 1}, path=p)
    audit.log_event("update_campaign", "apply", {"campaign_id": 1}, path=p)
    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["tool"] == "update_campaign"
    assert lines[0]["phase"] == "draft"
    assert lines[1]["phase"] == "apply"
    assert "ts" in lines[0]


def test_log_event_serializes_non_json(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    audit.log_event("t", "apply", {"obj": object()}, path=p)  # must not raise
    assert json.loads((tmp_path / "audit.jsonl").read_text())["obj"]


def test_env_var_selects_audit_path(monkeypatch, tmp_path):
    p = tmp_path / "sub" / "audit.jsonl"
    monkeypatch.setenv("GOOGLE_ADS_AUDIT_PATH", str(p))
    audit.log_event("t", "draft", {"x": 1})
    assert p.exists()


def test_explicit_path_beats_env(monkeypatch, tmp_path):
    wrong = tmp_path / "wrong.jsonl"
    real = tmp_path / "real.jsonl"
    monkeypatch.setenv("GOOGLE_ADS_AUDIT_PATH", str(wrong))
    audit.log_event("t", "draft", {"x": 1}, path=str(real))
    assert real.exists() and not wrong.exists()


def test_audit_default_path_literal():
    """The documented default (used when GOOGLE_ADS_AUDIT_PATH is unset) must stay
    ~/.mcp-google-ads-safe/audit.jsonl. The autouse _sandbox_audit_path fixture patches
    audit.AUDIT_PATH for every in-process test, so the only honest check is an unpatched
    import in a subprocess."""
    out = subprocess.run(
        [sys.executable, "-c", "from mcp_google_ads_safe import audit; print(audit.AUDIT_PATH)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == os.path.expanduser("~/.mcp-google-ads-safe/audit.jsonl")


def test_fresh_install_creates_dir_0700_and_file_0600(monkeypatch, tmp_path):
    home = tmp_path / "home"
    audit_file = home / ".mcp-google-ads-safe" / "audit.jsonl"
    monkeypatch.setenv("GOOGLE_ADS_AUDIT_PATH", str(audit_file))
    assert not audit_file.parent.exists()

    audit.log_event("update_campaign", "draft", {"campaign_id": 1})

    assert stat.S_IMODE(os.stat(audit_file.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(audit_file).st_mode) == 0o600


def test_existing_file_mode_untouched(tmp_path):
    p = tmp_path / "audit.jsonl"
    p.write_text("")
    os.chmod(p, 0o644)
    audit.log_event("t", "draft", {"x": 1}, path=str(p))
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644


def test_per_phase_schema_carries_digest_and_google_fields(tmp_path, monkeypatch, fake_client):
    """Driving a real draft->apply through rails must write a "draft" and an "apply" event,
    each carrying the plan digest, the mutate customer id, and operation_count."""
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))

    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    apply_out = rails.apply_draft(out["draft_id"])

    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    by_phase = {e["phase"]: e for e in events}
    assert set(by_phase) == {"draft", "apply"}
    digest = apply_out["digest"]
    for phase in ("draft", "apply"):
        e = by_phase[phase]
        assert e["digest"] == digest
        assert e["customer_id"] == "1234567890"
        assert e["operation_count"] == 1
        assert e["tool"] == "update_campaign"
    # REAL contract: the success-path client result carries request_id=None (the provider
    # request_id lives in trailing metadata, captured only by a logging interceptor — a
    # documented deferral), so the applied-write "apply" event carries NO request_id. The
    # "unknown" phase DOES carry one from the exception — see
    # test_unknown_write_outcome_on_transport_error in test_rails.py.
    assert "request_id" not in by_phase["apply"]
