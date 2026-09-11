"""Rails spine: env parsers, allowlist, caps, shared budget, content, and the
Intent->compile->draft->apply lifecycle over an in-memory fake client."""
import json

import pytest

from mcp_google_ads_safe import audit, client, rails, settings

# --- numeric env parsers ---------------------------------------------------------------

@pytest.mark.parametrize("env_var,accessor", [
    ("GOOGLE_ADS_MAX_DAILY_BUDGET", rails.max_daily_budget),
    ("GOOGLE_ADS_MAX_CPC", rails.max_cpc),
    ("GOOGLE_ADS_DRAFT_TTL_SECONDS", rails.draft_ttl_seconds),
])
@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-5", "0"])
def test_numeric_rail_rejects_bad(monkeypatch, env_var, accessor, bad):
    monkeypatch.setenv(env_var, bad)
    with pytest.raises(rails.RailViolation, match=env_var):
        accessor()


@pytest.mark.parametrize("env_var,accessor,default", [
    ("GOOGLE_ADS_MAX_DAILY_BUDGET", rails.max_daily_budget, 1000),
    ("GOOGLE_ADS_MAX_CPC", rails.max_cpc, 50),
    ("GOOGLE_ADS_DRAFT_TTL_SECONDS", rails.draft_ttl_seconds, 3600),
])
def test_numeric_rail_default(monkeypatch, env_var, accessor, default):
    monkeypatch.delenv(env_var, raising=False)
    assert accessor() == default


def test_parse_bool_env_tokens(monkeypatch):
    for token in ["true", "TRUE", "1", "  true  "]:
        monkeypatch.setenv("GOOGLE_ADS_SOME_FLAG", token)
        assert rails.parse_bool_env("GOOGLE_ADS_SOME_FLAG") is True
    for token in ["false", "FALSE", "0", "  0  "]:
        monkeypatch.setenv("GOOGLE_ADS_SOME_FLAG", token)
        assert rails.parse_bool_env("GOOGLE_ADS_SOME_FLAG") is False
    for bad in ["yes", "on", "", "nope"]:
        monkeypatch.setenv("GOOGLE_ADS_SOME_FLAG", bad)
        with pytest.raises(rails.RailViolation, match="GOOGLE_ADS_SOME_FLAG"):
            rails.parse_bool_env("GOOGLE_ADS_SOME_FLAG")


def test_deferred_flag_parsers_default_false(monkeypatch):
    for var, accessor in [
        ("GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT", rails.allow_portfolio_edit),
        ("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT", rails.allow_conversion_goal_edit),
    ]:
        monkeypatch.delenv(var, raising=False)
        assert accessor() is False


# --- budget / bid caps -----------------------------------------------------------------

def test_budget_cap(monkeypatch):
    rails.check_budget(999)
    with pytest.raises(rails.RailViolation, match="exceeds cap") as exc:
        rails.check_budget(1001)
    assert exc.value.code == "CAP_EXCEEDED"
    monkeypatch.setenv("GOOGLE_ADS_MAX_DAILY_BUDGET", "300")
    with pytest.raises(rails.RailViolation):
        rails.check_budget(301)


def test_bid_cap():
    rails.check_bid(50)
    with pytest.raises(rails.RailViolation, match="exceeds cap") as exc:
        rails.check_bid(50.01)
    assert exc.value.code == "CAP_EXCEEDED"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 0, True])
def test_check_budget_rejects_bad_amounts(bad):
    with pytest.raises(rails.RailViolation):
        rails.check_budget(bad)


def test_check_budget_accepts_decimal():
    import decimal
    rails.check_budget(decimal.Decimal("999.99"))
    with pytest.raises(rails.RailViolation, match="exceeds cap"):
        rails.check_budget(decimal.Decimal("1000.01"))


# --- shared budget ---------------------------------------------------------------------

def test_shared_budget_refused_and_allowed(monkeypatch):
    shared = {"explicitly_shared": True}
    with pytest.raises(rails.RailViolation, match="shared") as exc:
        rails.check_shared_budget(shared)
    assert exc.value.code == "SHARED_BUDGET"
    # non-shared passes
    rails.check_shared_budget({"explicitly_shared": False})
    # override lets it through
    monkeypatch.setenv("GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT", "true")
    rails.check_shared_budget(shared)


# --- content blocklist -----------------------------------------------------------------

def test_content_blocklist(tmp_path, monkeypatch):
    rails.check_content(["ac repair", "sewage cleanup", None])  # empty blocklist -> nothing blocked
    path = tmp_path / "advertiser.yaml"
    path.write_text("blocked_terms:\n  - Backup\n")
    monkeypatch.setenv(settings.ENV_VAR, str(path))
    settings._reset()
    with pytest.raises(rails.RailViolation, match="backup"):
        rails.check_content(["emergency Backup generator"])


# --- customer allowlist ----------------------------------------------------------------

def test_allowlist_default_single_account(monkeypatch):
    # conftest sets GOOGLE_ADS_CUSTOMER_ID = 1234567890, lists unset
    rails.check_customer_allowlisted("1234567890", "read")
    rails.check_customer_allowlisted("1234567890", "write")
    with pytest.raises(rails.RailViolation, match="not allowlisted") as exc:
        rails.check_customer_allowlisted("9999999999", "read")
    assert exc.value.code == "NOT_ALLOWLISTED"
    with pytest.raises(rails.RailViolation, match="not allowlisted"):
        rails.check_customer_allowlisted("9999999999", "write")


def test_allowlist_dash_normalization(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "123-456-7890")
    rails.check_customer_allowlisted("1234567890", "write")
    rails.check_customer_allowlisted("123-456-7890", "write")


def test_allowlist_write_must_be_subset_of_read(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1111111111,2222222222")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "3333333333")  # not a read id
    with pytest.raises(rails.RailViolation, match="config error"):
        rails.check_customer_allowlisted("1111111111", "read")


def test_allowlist_write_subset_ok(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "1111111111,2222222222")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "1111111111")
    rails.check_customer_allowlisted("1111111111", "write")
    # a read-only id cannot be written
    with pytest.raises(rails.RailViolation, match="not allowlisted"):
        rails.check_customer_allowlisted("2222222222", "write")
    rails.check_customer_allowlisted("2222222222", "read")


# --- compile(UpdateCampaignBudgetIntent) -----------------------------------------------

def test_compile_builds_entity_mutation_plan(fake_client):
    compiled = rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))
    plan = compiled.plan
    assert isinstance(plan, rails.EntityMutationPlan)
    assert plan.kind == "entity"
    assert plan.mutate_customer_id == "1234567890"
    assert plan.validate_only_supported is True
    assert len(plan.operations) == 1
    op = plan.operations[0]
    assert op.service == "CampaignBudgetService"
    assert op.update_mask == ["amount_micros"]
    assert op.operation == {
        "update": {"resource_name": "customers/1234567890/campaignBudgets/555",
                   "amount_micros": 75_000_000}}
    # preview carries a digest equal to the plan digest, and it is stable across compiles.
    assert compiled.preview["digest"] == rails.plan_digest(plan)
    again = rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))
    assert again.preview["digest"] == compiled.preview["digest"]
    # currency omitted when unknown (B2 campaign_budget has no currency)
    assert "currency" not in compiled.preview


def test_compile_cross_customer_rejected(fake_client):
    fake_client.budget_info["budget_resource_name"] = "customers/9999999999/campaignBudgets/1"
    with pytest.raises(rails.RailViolation, match="cross-customer") as exc:
        rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))
    assert exc.value.code == "CROSS_CUSTOMER"


def test_compile_non_daily_and_aligned_refused(fake_client):
    fake_client.budget_info["period"] = "MONTHLY"
    with pytest.raises(rails.RailViolation, match="not DAILY") as exc:
        rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))
    assert exc.value.code == "NON_DAILY_BUDGET"

    fake_client.budget_info["period"] = "DAILY"
    fake_client.budget_info["aligned_bidding_strategy_id"] = "987"
    with pytest.raises(rails.RailViolation, match="aligned") as exc:
        rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))
    assert exc.value.code == "ALIGNED_BUDGET"


def test_compile_cap_and_shared_refusals(fake_client, monkeypatch):
    with pytest.raises(rails.RailViolation, match="exceeds cap") as exc:
        rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "1001"))
    assert exc.value.code == "CAP_EXCEEDED"

    fake_client.budget_info["explicitly_shared"] = True
    with pytest.raises(rails.RailViolation, match="shared") as exc:
        rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))
    assert exc.value.code == "SHARED_BUDGET"
    monkeypatch.setenv("GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT", "true")
    monkeypatch.setattr(client, "gaql_all", lambda *args: [{"campaign": {
        "id": "77", "resource_name": "customers/1234567890/campaigns/77",
        "name": "Synthetic campaign", "status": "PAUSED",
        "campaign_budget": fake_client.budget_info["budget_resource_name"],
    }}])
    rails.compile(rails.UpdateCampaignBudgetIntent("1234567890", "77", "75"))  # now passes


# --- draft cap refusal: no draft created + audited refused -----------------------------

def test_cap_refusal_creates_no_draft_and_audits_refused(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    before = set(rails._DRAFTS)
    with pytest.raises(rails.RailViolation) as exc:
        rails.update_campaign_budget_draft("1234567890", "77", "5000")
    assert exc.value.code == "CAP_EXCEEDED"
    assert set(rails._DRAFTS) == before  # NOT created
    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    refused = [e for e in events if e["phase"] == "refused"]
    assert len(refused) == 1
    assert refused[0]["code"] == "CAP_EXCEEDED"
    assert "cap" in refused[0]["reason"].lower()


# --- happy-path draft -> apply dispatches the plan exactly once -------------------------

def test_draft_then_apply_dispatches_plan_once(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    assert out["dry_run"] is True
    draft_id = out["draft_id"]
    plan = rails._DRAFTS[draft_id].plan

    apply_out = rails.apply_draft(draft_id)
    assert apply_out["applied"] is True
    assert fake_client.dispatch_calls == [plan]  # exactly once, with the plan
    # single-use
    with pytest.raises(rails.RailViolation, match="unknown or already-applied"):
        rails.apply_draft(draft_id)


# --- monotonic TTL expiry: expired, not dispatched -------------------------------------

def test_expired_draft_not_dispatched(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]
    rails._DRAFTS[draft_id].created_at -= (rails.draft_ttl_seconds() + 1)

    with pytest.raises(rails.RailViolation, match="expired") as exc:
        rails.apply_draft(draft_id)
    assert "unknown or already-applied" not in str(exc.value)
    assert fake_client.dispatch_calls == []  # never reached _dispatch
    assert draft_id not in rails._DRAFTS


def test_draft_ttl_override_honoured(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("GOOGLE_ADS_DRAFT_TTL_SECONDS", "5")
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]
    rails._DRAFTS[draft_id].created_at -= 10  # older than 5s
    with pytest.raises(rails.RailViolation, match="TTL is 5s"):
        rails.apply_draft(draft_id)


# --- validate_fn drift: refuses, does not dispatch, does not consume --------------------

def test_validate_fn_drift_refuses_without_dispatch(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]
    # account state drifts underneath the draft: current budget changes
    fake_client.budget_info["amount"] = "90"

    with pytest.raises(rails.RailViolation, match="drifted"):
        rails.apply_draft(draft_id)
    assert fake_client.dispatch_calls == []  # NOT dispatched
    assert draft_id in rails._DRAFTS  # NOT consumed by the refusal


# --- UNKNOWN_WRITE_OUTCOME on a transport error ----------------------------------------

class _FakeTransportError(Exception):
    """Mimics a GoogleAdsException: carries the structured provider error (request_id +
    failure) so rails classifies it as a transport-boundary failure."""

    def __init__(self):
        super().__init__("RPC failed at the wire")
        self.request_id = "req-err-999"
        self.failure = {"errors": ["MUTATE_FAILED_MYSTERIOUSLY"]}


def test_unknown_write_outcome_on_transport_error(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]
    fake_client.dispatch_error = _FakeTransportError()

    with pytest.raises(rails.UnknownWriteOutcome) as exc:
        rails.apply_draft(draft_id)
    # structured data preserved (not flattened to a string)
    assert exc.value.request_id == "req-err-999"
    assert exc.value.failure == {"errors": ["MUTATE_FAILED_MYSTERIOUSLY"]}

    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    unknown = [e for e in events if e["phase"] == "unknown"]
    assert len(unknown) == 1
    assert unknown[0]["request_id"] == "req-err-999"


def test_non_transport_dispatch_error_is_error_phase(fake_client, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]
    fake_client.dispatch_error = ValueError("plan rejected before RPC")

    with pytest.raises(ValueError, match="plan rejected"):
        rails.apply_draft(draft_id)
    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    assert [e for e in events if e["phase"] == "error"]
    assert not [e for e in events if e["phase"] == "unknown"]


# --- writes-gate belt-and-suspenders ---------------------------------------------------

def test_apply_refuses_when_writes_disabled_after_draft(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]

    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "false")
    with pytest.raises(rails.RailViolation, match="GOOGLE_ADS_ENABLE_WRITES") as exc:
        rails.apply_draft(draft_id)
    assert exc.value.code == "WRITES_DISABLED"
    assert draft_id in rails._DRAFTS  # not consumed
    assert fake_client.dispatch_calls == []

    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "true")
    assert rails.apply_draft(draft_id)["applied"] is True


def test_create_draft_refuses_when_writes_disabled(fake_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "false")
    before = set(rails._DRAFTS)
    with pytest.raises(rails.RailViolation, match="GOOGLE_ADS_ENABLE_WRITES"):
        rails.update_campaign_budget_draft("1234567890", "77", "75")
    assert set(rails._DRAFTS) == before


# --- apply-audit failure must not mask a landed write ----------------------------------

def test_apply_audit_failure_does_not_mask_landed_write(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    real_log = rails.audit.log_event

    def flaky(tool, phase, data, path=None):
        if phase == "apply":
            raise OSError("disk full")
        return real_log(tool, phase, data, path)

    monkeypatch.setattr(rails.audit, "log_event", flaky)
    apply_out = rails.apply_draft(out["draft_id"])
    assert apply_out["applied"] is True
    assert "disk full" in apply_out["audit_error"]


# --- Fix 1: apply-time WRITE allowlist is STRUCTURAL, not riding on validate_fn ----------

def _budget_plan_for(customer_id):
    op = rails.MutationOp(
        service="CampaignBudgetService",
        operation={"update": {
            "resource_name": f"customers/{customer_id}/campaignBudgets/1",
            "amount_micros": 5_000_000}},
        update_mask=["amount_micros"])
    return rails.EntityMutationPlan(
        mutate_customer_id=customer_id, operations=[op], validate_only_supported=True)


def test_apply_time_write_allowlist_is_structural(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    # a draft with NO validate_fn whose plan targets a customer that is NOT write-allowlisted
    # (conftest allowlists only 1234567890). Without the structural check this would dispatch.
    plan = _budget_plan_for("9999999999")
    draft = rails.create_draft(tool="update_campaign",
                               preview={"digest": rails.plan_digest(plan)}, plan=plan,
                               fingerprint={}, validate_fn=None)
    with pytest.raises(rails.RailViolation, match="not allowlisted") as exc:
        rails.apply_draft(draft["draft_id"])
    assert exc.value.code == "NOT_ALLOWLISTED"
    assert fake_client.dispatch_calls == []         # never dispatched
    assert draft["draft_id"] in rails._DRAFTS        # refusal did NOT consume the draft


# --- Fix 2: recompute the plan digest at apply (preview<->executed invariant) -----------

def test_apply_refuses_when_plan_digest_changed(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "a.jsonl"))
    out = rails.update_campaign_budget_draft("1234567890", "77", "75")
    draft_id = out["draft_id"]
    # MutationOp.operation is a mutable dict -> tamper with the in-memory plan after draft time
    rails._DRAFTS[draft_id].plan.operations[0].operation["update"]["amount_micros"] = 999_000_000
    with pytest.raises(rails.RailViolation, match="digest changed") as exc:
        rails.apply_draft(draft_id)
    assert exc.value.code == "PLAN_TAMPERED"
    assert fake_client.dispatch_calls == []          # tampered plan never dispatched


# --- Fix 4: create_draft discards the draft if the draft-audit write fails --------------

def test_create_draft_discards_draft_when_draft_audit_fails(fake_client, monkeypatch):
    real_log = rails.audit.log_event

    def flaky(tool, phase, data, path=None):
        if phase == "draft":
            raise OSError("disk full")
        return real_log(tool, phase, data, path)

    monkeypatch.setattr(rails.audit, "log_event", flaky)
    before = set(rails._DRAFTS)
    with pytest.raises(OSError, match="disk full"):
        rails.update_campaign_budget_draft("1234567890", "77", "75")
    assert set(rails._DRAFTS) == before   # the draft was discarded, none left behind
