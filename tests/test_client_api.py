"""client.py Google-Ads v25 layer — offline, against the fake GoogleAdsClient (conftest).

No network: `fake_gads` monkeypatches client.gads to return a FakeGoogleAdsClient whose
get_type yields REAL v25 proto-plus messages, so client.py's real request-building and
response-parsing run. Every guard test is discriminating (the "reversed" case is asserted
alongside the guard).
"""
import json

import pytest
from google.ads.googleads.v25.enums.types.ad_group_status import AdGroupStatusEnum
from google.ads.googleads.v25.enums.types.bidding_strategy_type import (
    BiddingStrategyTypeEnum,
)
from google.ads.googleads.v25.enums.types.budget_period import BudgetPeriodEnum
from google.ads.googleads.v25.enums.types.campaign_status import CampaignStatusEnum

from mcp_google_ads_safe import audit, client, rails
from tests.conftest import make_row, make_search_response, make_type

CID = "1234567890"
_BST = BiddingStrategyTypeEnum.BiddingStrategyType
_PERIOD = BudgetPeriodEnum.BudgetPeriod
_CSTATUS = CampaignStatusEnum.CampaignStatus
_AGSTATUS = AdGroupStatusEnum.AdGroupStatus


# --- gaql envelope ---------------------------------------------------------------------

def test_gaql_envelope_normalizes_rows_and_surfaces_counts(fake_gads):
    row = make_row(**{"campaign_budget.amount_micros": 50_000_000,
                      "campaign_budget.resource_name": f"customers/{CID}/campaignBudgets/555"})
    fake_gads.search_responses[(CID, "")] = make_search_response([row], next_token="", total=1)
    env = client.gaql("SELECT campaign_budget.amount_micros FROM campaign_budget", CID)
    assert env["returned_count"] == 1
    assert env["total_results_count"] == 1
    assert env["pages_complete"] is True
    assert env["next_page_token"] is None
    assert env["query_limited"] is False
    # proto-plus to_dict -> snake_case dict
    assert env["rows"][0]["campaign_budget"]["amount_micros"] == "50000000"


def test_gaql_query_limited_detects_limit_token_not_substring(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([], next_token="", total=0)
    assert client.gaql("SELECT campaign.id FROM campaign LIMIT 10", CID)["query_limited"] is True
    assert client.gaql("SELECT campaign.id FROM campaign", CID)["query_limited"] is False
    # 'LIMITED' must NOT trip the \bLIMIT\b token
    assert client.gaql("SELECT campaign.name FROM campaign WHERE campaign.name = 'LIMITED'",
                       CID)["query_limited"] is False


def test_gaql_row_cap_truncates_on_page_boundary(fake_gads, monkeypatch):
    # default GOOGLE_ADS_MAX_PAGES=1: fetch ONE page, surface a resume token, not complete.
    r0 = make_row(**{"campaign.id": 1})
    fake_gads.search_responses[(CID, "")] = make_search_response([r0], next_token="RAW1", total=2)
    env = client.gaql("SELECT campaign.id FROM campaign", CID)
    assert env["returned_count"] == 1
    assert env["pages_complete"] is False
    assert env["next_page_token"] is not None
    # discriminating: raising the page cap pages further and completes
    monkeypatch.setenv("GOOGLE_ADS_MAX_PAGES", "2")
    r1 = make_row(**{"campaign.id": 2})
    fake_gads.search_responses[(CID, "RAW1")] = make_search_response([r1], next_token="", total=2)
    env2 = client.gaql("SELECT campaign.id FROM campaign", CID)
    assert env2["returned_count"] == 2 and env2["pages_complete"] is True


def test_gaql_resume_token_is_bound_to_query_and_customer(fake_gads):
    r0 = make_row(**{"campaign.id": 1})
    query = "SELECT campaign.id FROM campaign"
    fake_gads.search_responses[(CID, "")] = make_search_response([r0], next_token="RAW1", total=2)
    token = client.gaql(query, CID)["next_page_token"]
    # same (query, customer) resumes: fake serves the RAW1 page
    fake_gads.search_responses[(CID, "RAW1")] = make_search_response(
        [make_row(**{"campaign.id": 2})], next_token="", total=2)
    resumed = client.gaql(query, CID, page_token=token)
    assert resumed["returned_count"] == 1
    # a DIFFERENT query with the same token digest-mismatches -> refused
    with pytest.raises(rails.RailViolation) as exc:
        client.gaql("SELECT campaign.name FROM campaign", CID, page_token=token)
    assert exc.value.code == "TOKEN_MISMATCH"


# --- gaql_all (fail-closed complete scan) ----------------------------------------------

def test_gaql_all_pages_to_completion(fake_gads):
    query = "SELECT campaign.id FROM campaign"
    fake_gads.search_responses[(CID, "")] = make_search_response(
        [make_row(**{"campaign.id": 1})], next_token="P1", total=2)
    fake_gads.search_responses[(CID, "P1")] = make_search_response(
        [make_row(**{"campaign.id": 2})], next_token="", total=2)
    rows = client.gaql_all(query, CID)
    assert [r["campaign"]["id"] for r in rows] == ["1", "2"]


def test_gaql_all_refuses_a_limit_query(fake_gads):
    with pytest.raises(rails.RailViolation) as exc:
        client.gaql_all("SELECT campaign.id FROM campaign LIMIT 5", CID)
    assert exc.value.code == "SCAN_INCOMPLETE"


def test_gaql_all_scan_incomplete_on_count_mismatch(fake_gads):
    # one complete page of 2 rows, but total says 3 -> a page is missing
    fake_gads.search_responses[(CID, "")] = make_search_response(
        [make_row(**{"campaign.id": 1}), make_row(**{"campaign.id": 2})], next_token="", total=3)
    with pytest.raises(rails.RailViolation) as exc:
        client.gaql_all("SELECT campaign.id FROM campaign", CID)
    assert exc.value.code == "SCAN_INCOMPLETE"


def test_gaql_all_ok_when_count_reconciles(fake_gads):
    # discriminating counterpart to the mismatch test
    fake_gads.search_responses[(CID, "")] = make_search_response(
        [make_row(**{"campaign.id": 1}), make_row(**{"campaign.id": 2})], next_token="", total=2)
    assert len(client.gaql_all("SELECT campaign.id FROM campaign", CID)) == 2


# --- _dispatch: entity -----------------------------------------------------------------

def _budget_plan(customer_id=CID, budget_customer=CID, micros=7_000_000):
    op = rails.MutationOp(
        service="CampaignBudgetService",
        operation={"update": {
            "resource_name": f"customers/{budget_customer}/campaignBudgets/555",
            "amount_micros": micros}},
        update_mask=["amount_micros"])
    return rails.EntityMutationPlan(
        mutate_customer_id=customer_id, operations=[op], validate_only_supported=True)


def _ok_mutate_response():
    resp = make_type("MutateGoogleAdsResponse")
    opr = make_type("MutateOperationResponse")
    opr.campaign_budget_result.resource_name = f"customers/{CID}/campaignBudgets/555"
    resp.mutate_operation_responses.append(opr)
    return resp


def test_dispatch_entity_one_mutate_no_partial_failure(fake_gads):
    fake_gads.mutate_response = _ok_mutate_response()
    result = client._dispatch(_budget_plan())
    assert len(fake_gads.mutate_calls) == 1
    call = fake_gads.mutate_calls[0]
    assert call["customer_id"] == CID
    assert call["partial_failure"] is False
    assert call["validate_only"] is False
    # exactly one MutateOperation, correct sub-op, entity value + field mask
    ops = call["operations"]
    assert len(ops) == 1
    mo = ops[0]
    assert mo._pb.WhichOneof("operation") == "campaign_budget_operation"
    assert mo.campaign_budget_operation.update.amount_micros == 7_000_000
    assert list(mo.campaign_budget_operation.update_mask.paths) == ["amount_micros"]
    assert result["resource_names"] == [f"customers/{CID}/campaignBudgets/555"]


def test_dispatch_entity_validate_only_passthrough(fake_gads):
    fake_gads.mutate_response = _ok_mutate_response()
    client._dispatch(_budget_plan(), validate_only=True)
    assert fake_gads.mutate_calls[0]["validate_only"] is True


def _status_plan(service, resource_name, status):
    op = rails.MutationOp(
        service=service,
        operation={"update": {"resource_name": resource_name, "status": status}},
        update_mask=["status"])
    return rails.EntityMutationPlan(
        mutate_customer_id=CID, operations=[op], validate_only_supported=True)


def _ok_status_mutate_response(result_field, rn):
    resp = make_type("MutateGoogleAdsResponse")
    opr = make_type("MutateOperationResponse")
    setattr(getattr(opr, result_field), "resource_name", rn)
    resp.mutate_operation_responses.append(opr)
    return resp


def test_dispatch_campaign_status_update_through_real_protos(fake_gads):
    # Proves the "PAUSED" enum NAME string survives setattr into a real v25 Campaign proto,
    # and that _build_mutate_operation routes CampaignService to campaign_operation.
    rn = f"customers/{CID}/campaigns/42"
    fake_gads.mutate_response = _ok_status_mutate_response("campaign_result", rn)
    result = client._dispatch(_status_plan("CampaignService", rn, "PAUSED"))
    mo = fake_gads.mutate_calls[0]["operations"][0]
    assert mo._pb.WhichOneof("operation") == "campaign_operation"
    assert mo.campaign_operation.update.status == _CSTATUS.PAUSED
    assert list(mo.campaign_operation.update_mask.paths) == ["status"]
    assert result["resource_names"] == [rn]


def test_dispatch_ad_group_status_update_through_real_protos(fake_gads):
    rn = f"customers/{CID}/adGroups/99"
    fake_gads.mutate_response = _ok_status_mutate_response("ad_group_result", rn)
    result = client._dispatch(_status_plan("AdGroupService", rn, "ENABLED"))
    mo = fake_gads.mutate_calls[0]["operations"][0]
    assert mo._pb.WhichOneof("operation") == "ad_group_operation"
    assert mo.ad_group_operation.update.status == _AGSTATUS.ENABLED
    assert list(mo.ad_group_operation.update_mask.paths) == ["status"]
    assert result["resource_names"] == [rn]


def test_dispatch_cross_customer_op_blocked_at_boundary_non_transport(fake_gads):
    fake_gads.mutate_response = _ok_mutate_response()
    plan = _budget_plan(customer_id=CID, budget_customer="9999999999")  # op belongs to another cid
    with pytest.raises(rails.RailViolation) as exc:
        client._dispatch(plan)
    assert exc.value.code == "CROSS_CUSTOMER"
    assert fake_gads.mutate_calls == []  # never reached the wire
    # the raised error must NOT look like a transport error (rails would then mislabel it)
    assert rails._is_transport_error(exc.value) is False
    # discriminating: a matching-customer op DOES dispatch
    client._dispatch(_budget_plan(customer_id=CID, budget_customer=CID))
    assert len(fake_gads.mutate_calls) == 1


def test_dispatch_unknown_kind_raises(fake_gads):
    class Weird:
        kind = "portfolio"
        mutate_customer_id = CID
    with pytest.raises(rails.RailViolation):
        client._dispatch(Weird())
    assert fake_gads.mutate_calls == []


# --- _dispatch: after-dispatch transport error keeps the UNKNOWN shape -----------------

class _FakeTransportError(Exception):
    """Mimics a GoogleAdsException at the shape rails._is_transport_error keys on."""

    def __init__(self):
        super().__init__("mutate failed on the wire")
        self.request_id = "req-err-1"
        self.failure = {"errors": [{"error_code": {"mutate_error": 8}}]}


def test_dispatch_transport_error_is_recognized_and_drives_apply_to_unknown(fake_gads):
    fake_gads.mutate_error = _FakeTransportError()
    # 1) _dispatch lets the transport-shaped error propagate unflattened
    with pytest.raises(_FakeTransportError) as exc:
        client._dispatch(_budget_plan())
    assert rails._is_transport_error(exc.value) is True

    # 2) end-to-end: rails.apply_draft classifies it as UNKNOWN (write MAY have landed),
    #    preserving the structured provider detail — NOT a plain failure.
    plan = _budget_plan()
    # digest must match the plan: apply_draft recomputes plan_digest and refuses a mismatch
    # (PLAN_TAMPERED). This is a legal apply that reaches the wire, then fails there.
    draft = rails.create_draft(tool="update_campaign",
                               preview={"digest": rails.plan_digest(plan)}, plan=plan,
                               fingerprint={})
    with pytest.raises(rails.UnknownWriteOutcome) as unknown:
        rails.apply_draft(draft["draft_id"])
    assert unknown.value.request_id == "req-err-1"
    assert unknown.value.failure == {"errors": [{"error_code": {"mutate_error": 8}}]}


# --- _dispatch: parse failure AFTER a successful mutate is UNKNOWN, not "error" ---------

def _raise_parse(_response):
    raise ValueError("response could not be parsed")


def test_dispatch_entity_parse_failure_after_mutate_is_unknown(fake_gads, monkeypatch):
    # the mutate SUCCEEDS (the write landed) but parsing its response blows up.
    fake_gads.mutate_response = _ok_mutate_response()
    monkeypatch.setattr(client, "_mutate_result", _raise_parse)
    with pytest.raises(rails.UnknownWriteOutcome) as exc:
        client._dispatch(_budget_plan())
    assert len(fake_gads.mutate_calls) == 1              # the mutate DID happen (write landed)
    assert isinstance(exc.value.cause, ValueError)        # the parse exc is preserved
    # discriminating: removing the wrap makes _dispatch raise the raw ValueError instead,
    # which _is_transport_error would NOT recognize -> apply would log "error" (nothing landed)
    assert rails._is_transport_error(exc.value) is True   # UnknownWriteOutcome routes to unknown


def test_apply_parse_failure_after_mutate_audits_unknown_not_error(fake_gads, tmp_path, monkeypatch):
    audit_file = tmp_path / "a.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_file))
    fake_gads.mutate_response = _ok_mutate_response()
    monkeypatch.setattr(client, "_mutate_result", _raise_parse)
    plan = _budget_plan()
    draft = rails.create_draft(tool="update_campaign",
                               preview={"digest": rails.plan_digest(plan)}, plan=plan,
                               fingerprint={})
    with pytest.raises(rails.UnknownWriteOutcome) as exc:
        rails.apply_draft(draft["draft_id"])
    # re-raised UNCHANGED, not re-wrapped: the parse message + the original ValueError cause
    # survive (a re-wrap would swap in the transport-boundary message and an inner
    # UnknownWriteOutcome cause).
    assert "could not be parsed" in str(exc.value)
    assert isinstance(exc.value.cause, ValueError)
    assert "verify account state" in str(exc.value)             # tells the caller to reconcile
    events = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
    assert [e for e in events if e["phase"] == "unknown"]       # audited "unknown"
    assert not [e for e in events if e["phase"] == "error"]     # NOT "error"
    assert draft["draft_id"] not in rails._DRAFTS               # draft consumed


# --- _max_pages fails closed on bad GOOGLE_ADS_MAX_PAGES --------------------------------

@pytest.mark.parametrize("bad", ["abc", "0", "-1"])
def test_max_pages_fails_closed_on_bad_values(monkeypatch, bad):
    monkeypatch.setenv("GOOGLE_ADS_MAX_PAGES", bad)
    with pytest.raises(ValueError, match="GOOGLE_ADS_MAX_PAGES"):
        client._max_pages()


def test_max_pages_valid_and_default(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MAX_PAGES", "3")
    assert client._max_pages() == 3
    monkeypatch.delenv("GOOGLE_ADS_MAX_PAGES", raising=False)
    assert client._max_pages() == 1


# --- _dispatch: recommendation ---------------------------------------------------------

def test_dispatch_recommendation_apply_and_dismiss(fake_gads):
    rn = f"customers/{CID}/recommendations/77"
    resp = make_type("ApplyRecommendationResponse")
    resp.results.append({"resource_name": rn})
    fake_gads.reco_response = resp

    apply_plan = rails.RecommendationActionPlan(
        mutate_customer_id=CID, rpc="apply", recommendation_resource_name=rn,
        new_budget_amount_micros=25000000, post_checks=[recommendation_postcheck()])
    out = client._dispatch(apply_plan)
    assert out["resource_names"] == [rn]
    assert fake_gads.reco_calls[-1][0] == "apply"
    assert fake_gads.reco_calls[-1][1].customer_id == CID

    dismiss_plan = rails.RecommendationActionPlan(
        mutate_customer_id=CID, rpc="dismiss", recommendation_resource_name=rn,
        post_checks=[{'recommendation_dismiss': True, 'customer_id': CID, 'resource_name': rn,
                      'expected': {'resource_name': rn, 'type_': 'KEYWORD', 'dismissed': True,
                                   'campaign_budget': '', 'campaign': '', 'ad_group': ''}}])
    client._dispatch(dismiss_plan)
    assert fake_gads.reco_calls[-1][0] == "dismiss"


# --- effective_strategy ----------------------------------------------------------------

def test_effective_strategy_non_portfolio(fake_gads):
    row = make_row(**{"campaign.id": 42, "campaign.bidding_strategy_type": _BST.MANUAL_CPC})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    got = client.effective_strategy(CID, 42)
    assert got == {"type": "MANUAL_CPC", "portfolio_resource_name": None,
                   "owner_customer_id": None}


def test_effective_strategy_portfolio_parses_owner(fake_gads):
    row = make_row(**{"campaign.id": 42, "campaign.bidding_strategy_type": _BST.TARGET_CPA,
                      "campaign.bidding_strategy": f"customers/{CID}/biddingStrategies/77",
                      "accessible_bidding_strategy.id": 77,
                      "accessible_bidding_strategy.owner_customer_id": 999})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    got = client.effective_strategy(CID, 42)
    assert got["type"] == "TARGET_CPA"
    assert got["portfolio_resource_name"] == f"customers/{CID}/biddingStrategies/77"
    assert got["owner_customer_id"] == "999"


# --- campaign_budget -------------------------------------------------------------------

def _budget_row(**over):
    fields = {
        "campaign.id": 42,
        "campaign_budget.resource_name": f"customers/{CID}/campaignBudgets/555",
        "campaign_budget.amount_micros": 50_000_000,
        "campaign_budget.explicitly_shared": False,
        "campaign_budget.reference_count": 1,
        "campaign_budget.period": _PERIOD.DAILY,
    }
    fields.update(over)
    return make_row(**fields)


def test_campaign_budget_basic_daily(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([_budget_row()])
    got = client.campaign_budget(CID, 42)
    assert got["budget_resource_name"] == f"customers/{CID}/campaignBudgets/555"
    assert got["amount"] == "50"  # decimal str via from_micros
    assert got["explicitly_shared"] is False
    assert got["reference_count"] == 1
    assert got["period"] == "DAILY"
    assert got["total_amount_micros"] is None      # unset optional -> None
    assert got["aligned_bidding_strategy_id"] is None  # 0 -> None


def test_campaign_budget_shared_aligned_and_total(fake_gads):
    row = _budget_row(**{
        "campaign_budget.explicitly_shared": True,
        "campaign_budget.aligned_bidding_strategy_id": 88,
        "campaign_budget.total_amount_micros": 900_000_000,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    got = client.campaign_budget(CID, 42)
    assert got["explicitly_shared"] is True
    assert got["aligned_bidding_strategy_id"] == "88"
    assert got["total_amount_micros"] == 900_000_000


# --- entity_status (real v25 protos: the fake that MUST match the real call) -----------

def test_entity_status_campaign_reads_real_proto(fake_gads):
    rn = f"customers/{CID}/campaigns/424242424"
    row = make_row(**{"campaign.id": 424242424, "campaign.status": _CSTATUS.PAUSED,
                      "campaign.resource_name": rn})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    assert client.entity_status(CID, "campaign", "424242424") == {
        "exists": True, "resource_name": rn, "status": "PAUSED"}


def test_entity_status_ad_group_reads_real_proto(fake_gads):
    rn = f"customers/{CID}/adGroups/9988776655"
    row = make_row(**{"ad_group.id": 9988776655, "ad_group.status": _AGSTATUS.ENABLED,
                      "ad_group.resource_name": rn})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    assert client.entity_status(CID, "ad_group", "9988776655") == {
        "exists": True, "resource_name": rn, "status": "ENABLED"}


def test_entity_status_empty_rows_means_not_exists(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([])
    assert client.entity_status(CID, "campaign", "1") == {
        "exists": False, "resource_name": None, "status": None}


def test_entity_status_unsupported_entity_type_raises(fake_gads):
    with pytest.raises(rails.RailViolation) as exc:
        client.entity_status(CID, "keyword", "1")
    assert exc.value.code == "UNSUPPORTED_ENTITY"


# --- preflight / login-manager ancestry ------------------------------------------------

def _fake_tree(monkeypatch, tree, login="1234567890"):
    """tree: {manager_id: [(child_id, is_manager), ...]}. Drives the walk directly."""
    monkeypatch.setattr(client, "gads",
                        lambda profile=None: type("C", (), {"login_customer_id": login})())
    monkeypatch.setattr(client, "_customer_client_children",
                        lambda mid: [{"id": c, "manager": m} for c, m in tree.get(mid, [])])


def test_preflight_all_descendants_present_passes(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "111,333")
    _fake_tree(monkeypatch, {
        "1234567890": [("111", False), ("222", True)],
        "222": [("333", False)],
    })
    client.preflight()  # no raise


def test_preflight_missing_id_fails_loud(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "111,999")  # 999 not in tree
    _fake_tree(monkeypatch, {"1234567890": [("111", False)]})
    with pytest.raises(rails.RailViolation) as exc:
        client.preflight()
    assert exc.value.code == "ANCESTRY_NOT_DESCENDANT"
    assert "999" in str(exc.value)


def test_preflight_truncated_depth_fails_loud_not_guess(monkeypatch):
    # required id sits below the depth cap -> we can't confirm it -> refuse as truncated,
    # NOT as "not a descendant".
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "9001")
    _fake_tree(monkeypatch, {
        "1234567890": [("2001", True)],
        "2001": [("2002", True)],
        "2002": [("2003", True)],   # 2003 sits at depth 3 == cap: not expanded
        "2003": [("9001", False)],  # the required id, never queried
    })
    with pytest.raises(rails.RailViolation) as exc:
        client.preflight()
    assert exc.value.code == "ANCESTRY_TRUNCATED"


def test_preflight_count_cap_fails_loud(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "111")
    big = [(str(i), False) for i in range(600)]  # > 500 count cap
    _fake_tree(monkeypatch, {"1234567890": big})
    with pytest.raises(rails.RailViolation) as exc:
        client.preflight()
    assert exc.value.code == "ANCESTRY_TRUNCATED"


def test_customer_client_children_parses_real_rows(fake_gads):
    # proves the provider path (query -> _scan_rows -> row.customer_client) works on real protos
    row = make_row(**{"customer_client.id": 222, "customer_client.manager": True,
                      "customer_client.level": 1})
    fake_gads.search_responses[("777", "")] = make_search_response([row])
    kids = client._customer_client_children("777")
    assert kids == [{"id": "222", "manager": True}]


# --- gads() factory --------------------------------------------------------------------

@pytest.mark.parametrize("auth_mode", ["service_account", "installed_user"])
def test_google_ads_32_selects_tokenless_and_legacy_auth(monkeypatch, auth_mode):
    from google.ads.googleads import config, oauth2
    from google.ads.googleads.client import GoogleAdsClient

    calls = []
    service_credentials = object()
    user_credentials = object()

    def fake_service(path, subject, http_proxy=None):
        calls.append(("service_account", path, subject, http_proxy))
        return service_credentials

    def fake_user(client_id, client_secret, refresh_token, http_proxy=None):
        calls.append(("installed_user", client_id, client_secret, refresh_token, http_proxy))
        return user_credentials

    monkeypatch.setattr(oauth2, "get_service_account_credentials", fake_service)
    monkeypatch.setattr(oauth2, "get_installed_app_credentials", fake_user)
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("credential files must not be read")
        ),
    )

    if auth_mode == "service_account":
        raw = {
            "json_key_file_path": "/synthetic/private-key.json",
            "impersonated_email": "synthetic-user@example.test",
            "login_customer_id": "1112223333",
            "use_proto_plus": True,
        }
        expected_credentials = service_credentials
        expected_call = (
            "service_account",
            "/synthetic/private-key.json",
            "synthetic-user@example.test",
            None,
        )
        expected_token = None
    else:
        raw = {
            "client_id": "synthetic-client-id",
            "client_secret": "synthetic-client-secret",
            "refresh_token": "synthetic-refresh-token",
            "developer_token": "synthetic-developer-token",
            "login_customer_id": "1112223333",
            "use_proto_plus": True,
        }
        expected_credentials = user_credentials
        expected_call = (
            "installed_user",
            "synthetic-client-id",
            "synthetic-client-secret",
            "synthetic-refresh-token",
            None,
        )
        expected_token = "synthetic-developer-token"

    kwargs = GoogleAdsClient._get_client_kwargs(config.load_from_dict(raw))

    assert calls == [expected_call]
    assert kwargs["credentials"] is expected_credentials
    assert kwargs["developer_token"] == expected_token
    assert kwargs["login_customer_id"] == "1112223333"
    assert kwargs["use_proto_plus"] is True


@pytest.mark.parametrize(
    "raw",
    [
        {"use_proto_plus": True, "impersonated_email": "secret-sentinel@example.test"},
        {"use_proto_plus": True, "client_id": "secret-sentinel"},
    ],
)
def test_google_ads_32_refuses_incomplete_auth_without_echoing_values(monkeypatch, raw):
    from google.ads.googleads import config, oauth2
    from google.ads.googleads.client import GoogleAdsClient

    def refuse(*args, **kwargs):
        raise AssertionError("credential factories must not run")

    monkeypatch.setattr(oauth2, "get_service_account_credentials", refuse)
    monkeypatch.setattr(oauth2, "get_installed_app_credentials", refuse)
    monkeypatch.setattr("builtins.open", refuse)

    with pytest.raises(ValueError) as exc:
        GoogleAdsClient._get_client_kwargs(config.load_from_dict(raw))

    assert "secret-sentinel" not in str(exc.value)

def test_gads_explicit_path_wins_expands_home_and_caches(monkeypatch, tmp_path):
    from tests.conftest import OFFLINE_GADS_FACTORY
    monkeypatch.setattr(client, 'gads', OFFLINE_GADS_FACTORY)
    calls = []

    def fake_load(config_dict, version=None):
        calls.append((dict(config_dict), version))
        return type("C", (), {"login_customer_id": "1"})()

    monkeypatch.setattr(client.GoogleAdsClient, "load_from_dict", staticmethod(fake_load))
    client._CLIENT_CACHE.clear()
    yaml_path = tmp_path / "g.yaml"
    yaml_path.write_text("developer_token: X\nuse_proto_plus: false\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_ADS_YAML", str(tmp_path / "wrong.yaml"))
    expanded = []
    monkeypatch.setattr(
        client.os.path, "expanduser",
        lambda path: expanded.append(path) or str(yaml_path))

    c1 = client.gads("~/g.yaml")
    c2 = client.gads("~/g.yaml")
    assert c1 is c2                       # cached per resolved path
    assert expanded == ["~/g.yaml", "~/g.yaml"]
    assert len(calls) == 1
    cfg, version = calls[0]
    assert version == "v25"               # always explicit
    assert cfg["use_proto_plus"] is True  # forced on even though the yaml said false
    client._CLIENT_CACHE.clear()


def test_gads_environment_path_expands_home(monkeypatch, tmp_path):
    from tests.conftest import OFFLINE_GADS_FACTORY
    monkeypatch.setattr(client, "gads", OFFLINE_GADS_FACTORY)
    loaded = []

    def fake_load(config_dict, version=None):
        loaded.append((dict(config_dict), version))
        return object()

    monkeypatch.setattr(client.GoogleAdsClient, "load_from_dict", staticmethod(fake_load))
    yaml_path = tmp_path / "env.yaml"
    yaml_path.write_text("developer_token: ENV\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_ADS_YAML", "~/env.yaml")
    expanded = []
    monkeypatch.setattr(
        client.os.path, "expanduser",
        lambda path: expanded.append(path) or str(yaml_path))

    client.gads()

    assert expanded == ["~/env.yaml"]
    assert loaded == [({"developer_token": "ENV", "use_proto_plus": True}, "v25")]


@pytest.mark.parametrize("case", ["missing", "blank_environment", "blank_profile"])
def test_gads_missing_or_blank_path_fails_before_file_or_loader(
        monkeypatch, case):
    from tests.conftest import OFFLINE_GADS_FACTORY
    monkeypatch.setattr(client, "gads", OFFLINE_GADS_FACTORY)

    def refuse(*args, **kwargs):
        raise AssertionError("file or Google client loader must not run")

    monkeypatch.setattr("builtins.open", refuse)
    monkeypatch.setattr(client.GoogleAdsClient, "load_from_dict", staticmethod(refuse))
    monkeypatch.setattr(client.GoogleAdsClient, "load_from_storage", staticmethod(refuse))
    if case == "missing":
        monkeypatch.delenv("GOOGLE_ADS_YAML", raising=False)
        profile = None
    elif case == "blank_environment":
        monkeypatch.setenv("GOOGLE_ADS_YAML", "  \t")
        profile = None
    else:
        monkeypatch.setenv("GOOGLE_ADS_YAML", "/must/not/fall/through.yaml")
        profile = "  \t"

    with pytest.raises(ValueError, match="set GOOGLE_ADS_YAML"):
        client.gads(profile)


# --- list_accounts ---------------------------------------------------------------------

def test_list_accounts_returns_read_allowlisted_with_name_and_currency(fake_gads, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", "111,222")
    fake_gads.search_responses[("111", "")] = make_search_response([make_row(**{
        "customer.id": 111, "customer.descriptive_name": "Acct One",
        "customer.currency_code": "USD"})])
    fake_gads.search_responses[("222", "")] = make_search_response([make_row(**{
        "customer.id": 222, "customer.descriptive_name": "Acct Two",
        "customer.currency_code": "EUR"})])
    by_id = {a["customer_id"]: a for a in client.list_accounts()}
    assert by_id["111"]["descriptive_name"] == "Acct One"
    assert by_id["111"]["currency_code"] == "USD"
    assert by_id["222"]["currency_code"] == "EUR"


# ==========================================================================================
# M1-READS: 9 read tools (client-layer). Every function below is a canned-GAQL wrapper that
# MUST go through client.gaql (never gaql_all/_scan_rows) -- get_search_terms's LIMIT 200 is
# the concrete proof (gaql_all hard-refuses any LIMIT). Money/enum policy: no decoding here,
# just assert the envelope carries proto-plus's raw dict rows through unchanged.
# ==========================================================================================

# --- _date_clause (pure, no fake_gads needed) --------------------------------------------

def test_date_clause_default_last_30_days_and_explicit_between():
    assert client._date_clause(None, None) == "segments.date DURING LAST_30_DAYS"
    assert client._date_clause("", "") == "segments.date DURING LAST_30_DAYS"
    # discriminating: only ONE side given still defaults (BETWEEN needs both)
    assert client._date_clause("2026-01-01", None) == "segments.date DURING LAST_30_DAYS"
    assert client._date_clause(None, "2026-01-31") == "segments.date DURING LAST_30_DAYS"
    assert client._date_clause("2026-01-01", "2026-01-31") == (
        "segments.date BETWEEN '2026-01-01' AND '2026-01-31'")


# --- page_token plumbing: one test covers ALL 8 multi-row reads (get_account_info has none)

def test_page_token_threads_through_every_multi_row_read(monkeypatch):
    calls = []

    def fake_gaql(q, cid, page_token=None):
        calls.append((q, cid, page_token))
        return {"rows": []}

    monkeypatch.setattr(client, "gaql", fake_gaql)
    sentinel = "TOKEN123"
    client.campaign_performance(CID, None, None, sentinel)
    client.ad_performance(CID, None, None, sentinel)
    client.keyword_performance(CID, None, None, sentinel)
    client.search_terms(CID, None, None, sentinel)
    client.geo_performance(CID, None, None, sentinel)
    client.negative_keywords(CID, sentinel)
    client.geo_targets(CID, "Orlando", sentinel)
    client.entities(CID, "campaign", page_token=sentinel)
    assert len(calls) == 8
    assert all(c[1] == CID for c in calls)
    assert all(c[2] == sentinel for c in calls)


# --- 1: get_account_info -> client.account_info -------------------------------------------

def test_account_info_query_and_row_roundtrip(fake_gads):
    row = make_row(**{
        "customer.id": 1234567890,
        "customer.descriptive_name": "Synthetic Test Advertiser",
        "customer.currency_code": "USD",
        "customer.time_zone": "America/New_York",
        "customer.auto_tagging_enabled": True,
        "customer.manager": False,
        "customer.status": 2,  # raw enum int -- NOT decoded (money/enum policy)
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.account_info(CID)
    assert fake_gads.search_requests[-1].query == (
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
        "customer.time_zone, customer.auto_tagging_enabled, customer.manager, "
        "customer.status FROM customer LIMIT 1"
    )
    cust = env["rows"][0]["customer"]
    assert cust["id"] == "1234567890"
    assert cust["descriptive_name"] == "Synthetic Test Advertiser"
    assert cust["currency_code"] == "USD"
    assert cust["auto_tagging_enabled"] is True
    assert cust["manager"] is False
    assert cust["status"] == 2


def test_account_info_has_no_page_token_param():
    import inspect
    assert "page_token" not in inspect.signature(client.account_info).parameters


# --- 2: get_campaign_performance -> client.campaign_performance ---------------------------

def test_campaign_performance_default_date_range_query_and_row(fake_gads):
    row = make_row(**{
        "campaign.id": 42, "campaign.name": "z. Remarketing",
        "metrics.cost_micros": 25_000_000, "metrics.impressions": 1000,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.campaign_performance(CID, None, None, None)
    assert fake_gads.search_requests[-1].query == (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        "FROM campaign "
        "WHERE campaign.status != 'REMOVED' AND segments.date DURING LAST_30_DAYS "
        "ORDER BY metrics.cost_micros DESC"
    )
    got = env["rows"][0]
    assert got["campaign"]["id"] == "42"
    assert got["campaign"]["name"] == "z. Remarketing"
    assert got["metrics"]["cost_micros"] == "25000000"
    assert got["metrics"]["impressions"] == "1000"


def test_campaign_performance_explicit_date_range(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([])
    client.campaign_performance(CID, "2026-01-01", "2026-01-31", None)
    sent = fake_gads.search_requests[-1].query
    assert "WHERE campaign.status != 'REMOVED' AND segments.date BETWEEN '2026-01-01' AND '2026-01-31' " in sent
    assert "LAST_30_DAYS" not in sent


def test_campaign_performance_page_token_passthrough(fake_gads):
    fake_gads.search_responses[(CID, "RAW1")] = make_search_response([])
    client.campaign_performance(CID, None, None, "RAW1")
    assert fake_gads.search_requests[-1].page_token == "RAW1"


# --- 3: get_ad_performance -> client.ad_performance ----------------------------------------

def test_ad_performance_query_and_row_roundtrip(fake_gads):
    row = make_row(**{
        "campaign.name": "GQ - A/C", "ad_group.name": "Broad",
        "ad_group_ad.ad.id": 555, "ad_group_ad.status": 2,
        "metrics.clicks": 10,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.ad_performance(CID, None, None, None)
    assert fake_gads.search_requests[-1].query == (
        "SELECT campaign.name, campaign.id, ad_group.name, ad_group.id, "
        "ad_group_ad.ad.id, ad_group_ad.ad.type, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.final_urls, "
        "ad_group_ad.status, metrics.impressions, metrics.clicks, metrics.ctr, "
        "metrics.conversions, metrics.cost_micros "
        "FROM ad_group_ad "
        "WHERE ad_group_ad.status != 'REMOVED' AND segments.date DURING LAST_30_DAYS "
        "ORDER BY metrics.cost_micros DESC"
    )
    got = env["rows"][0]
    assert got["campaign"]["name"] == "GQ - A/C"
    assert got["ad_group"]["name"] == "Broad"
    assert got["ad_group_ad"]["ad"]["id"] == "555"
    assert got["ad_group_ad"]["status"] == 2
    assert got["metrics"]["clicks"] == "10"


# --- 4: get_keyword_performance -> client.keyword_performance ------------------------------

def test_keyword_performance_query_and_row_roundtrip(fake_gads):
    row = make_row(**{
        "ad_group_criterion.keyword.text": "ac repair",
        "ad_group_criterion.keyword.match_type": 2,
        "ad_group_criterion.quality_info.quality_score": 7,
        "metrics.cost_micros": 12_000_000,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.keyword_performance(CID, None, None, None)
    assert fake_gads.search_requests[-1].query == (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, "
        "ad_group_criterion.quality_info.quality_score, metrics.impressions, "
        "metrics.clicks, metrics.ctr, metrics.average_cpc, metrics.cost_micros, "
        "metrics.conversions "
        "FROM keyword_view "
        "WHERE ad_group_criterion.status != 'REMOVED' AND segments.date DURING LAST_30_DAYS "
        "ORDER BY metrics.cost_micros DESC"
    )
    got = env["rows"][0]["ad_group_criterion"]
    assert got["keyword"]["text"] == "ac repair"
    assert got["keyword"]["match_type"] == 2
    assert got["quality_info"]["quality_score"] == 7
    assert env["rows"][0]["metrics"]["cost_micros"] == "12000000"


# --- 5: get_search_terms -> client.search_terms (LIMIT 200, no leading AND) ----------------

def test_search_terms_query_has_no_leading_and_and_limit_200(fake_gads):
    row = make_row(**{
        "search_term_view.search_term": "ac repair near me",
        "campaign.name": "GQ - A/C", "metrics.clicks": 3,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.search_terms(CID, None, None, None)
    sent = fake_gads.search_requests[-1].query
    assert sent == (
        "SELECT search_term_view.search_term, campaign.name, ad_group.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions "
        "FROM search_term_view "
        "WHERE segments.date DURING LAST_30_DAYS "
        "ORDER BY metrics.clicks DESC "
        "LIMIT 200"
    )
    # the date clause is the ONLY where condition -- no "REMOVED' AND" style leading filter
    assert "WHERE segments.date" in sent and "AND segments.date" not in sent
    assert sent.endswith("LIMIT 200")
    got = env["rows"][0]
    assert got["search_term_view"]["search_term"] == "ac repair near me"
    assert got["metrics"]["clicks"] == "3"


# --- 6: get_geo_performance -> client.geo_performance (no leading AND) ---------------------

def test_geo_performance_query_and_row_roundtrip(fake_gads):
    row = make_row(**{
        "geographic_view.country_criterion_id": 2840,
        "geographic_view.location_type": 1,
        "metrics.cost_micros": 5_000_000,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.geo_performance(CID, None, None, None)
    sent = fake_gads.search_requests[-1].query
    assert sent == (
        "SELECT campaign.name, geographic_view.country_criterion_id, "
        "geographic_view.location_type, metrics.impressions, metrics.clicks, "
        "metrics.cost_micros, metrics.conversions "
        "FROM geographic_view "
        "WHERE segments.date DURING LAST_30_DAYS "
        "ORDER BY metrics.cost_micros DESC"
    )
    assert "AND segments.date" not in sent
    got = env["rows"][0]["geographic_view"]
    assert got["country_criterion_id"] == "2840"
    assert got["location_type"] == 1
    assert env["rows"][0]["metrics"]["cost_micros"] == "5000000"


# --- 7: get_negative_keywords -> client.negative_keywords (no date range) -----------------

def test_negative_keywords_query_and_row_roundtrip(fake_gads):
    row = make_row(**{
        "campaign.id": 42, "campaign.name": "GQ - A/C",
        "campaign_criterion.keyword.text": "free",
        "campaign_criterion.keyword.match_type": 3,
        "campaign_criterion.negative": True,
        "campaign_criterion.criterion_id": 999,
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.negative_keywords(CID, None)
    assert fake_gads.search_requests[-1].query == (
        "SELECT campaign.id, campaign.name, campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type, campaign_criterion.negative, "
        "campaign_criterion.criterion_id "
        "FROM campaign_criterion "
        "WHERE campaign_criterion.negative = TRUE AND campaign_criterion.status != 'REMOVED'"
    )
    got = env["rows"][0]["campaign_criterion"]
    assert got["keyword"]["text"] == "free"
    assert got["negative"] is True
    assert got["criterion_id"] == "999"


# --- 8: search_geo_targets -> client.geo_targets (single-quote escaping is load-bearing) ---

def test_geo_targets_query_and_row_roundtrip(fake_gads):
    row = make_row(**{
        "geo_target_constant.id": 1014221, "geo_target_constant.name": "Orlando",
        "geo_target_constant.canonical_name": "Orlando, FL, United States",
    })
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.geo_targets(CID, "Orlando", None)
    assert fake_gads.search_requests[-1].query == (
        "SELECT geo_target_constant.id, geo_target_constant.name, "
        "geo_target_constant.canonical_name, geo_target_constant.country_code, "
        "geo_target_constant.target_type "
        "FROM geo_target_constant "
        "WHERE geo_target_constant.name LIKE '%Orlando%'"
    )
    got = env["rows"][0]["geo_target_constant"]
    assert got["id"] == "1014221"
    assert got["name"] == "Orlando"
    assert got["canonical_name"] == "Orlando, FL, United States"


def test_geo_targets_escapes_single_quote_in_query(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([])
    client.geo_targets(CID, "Coeur d'Alene", None)
    sent = fake_gads.search_requests[-1].query
    assert "LIKE '%Coeur d\\'Alene%'" in sent
    assert "LIKE '%Coeur d'Alene%'" not in sent  # unescaped form would break the GAQL string


# --- 9: get_entities -> client.entities (new tool, no production precedent) ---------------

def test_entities_campaign_basic_query_and_row(fake_gads):
    row = make_row(**{"campaign.id": 42, "campaign.name": "z. Remarketing",
                      "campaign.status": 3})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.entities(CID, "campaign")
    assert fake_gads.search_requests[-1].query == (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type "
        "FROM campaign WHERE campaign.status != 'REMOVED'"
    )
    got = env["rows"][0]["campaign"]
    assert got["id"] == "42"
    assert got["name"] == "z. Remarketing"
    assert got["status"] == 3


def test_entities_campaign_with_ids_filter(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([])
    client.entities(CID, "campaign", ids=["111", "222"])
    sent = fake_gads.search_requests[-1].query
    assert sent.endswith("WHERE campaign.status != 'REMOVED' AND campaign.id IN (111, 222)")


def test_entities_ad_group_basic_query_and_row(fake_gads):
    row = make_row(**{"ad_group.id": 777, "ad_group.name": "Broad", "ad_group.status": 2,
                      "campaign.id": 42, "campaign.name": "z. Remarketing"})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.entities(CID, "ad_group")
    assert fake_gads.search_requests[-1].query == (
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, "
        "campaign.id, campaign.name "
        "FROM ad_group WHERE ad_group.status != 'REMOVED'"
    )
    got = env["rows"][0]
    assert got["ad_group"]["id"] == "777"
    assert got["campaign"]["id"] == "42"


def test_entities_ad_group_with_parent_id_filter(fake_gads):
    fake_gads.search_responses[(CID, "")] = make_search_response([])
    client.entities(CID, "ad_group", parent_id="999")
    sent = fake_gads.search_requests[-1].query
    assert sent.endswith("WHERE ad_group.status != 'REMOVED' AND campaign.id = 999")


def test_entities_keyword_basic_query_and_row(fake_gads):
    row = make_row(**{"ad_group_criterion.criterion_id": 55,
                      "ad_group_criterion.keyword.text": "ac repair",
                      "ad_group.id": 777})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.entities(CID, "keyword")
    assert fake_gads.search_requests[-1].query == (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name "
        "FROM ad_group_criterion "
        "WHERE ad_group_criterion.status != 'REMOVED' AND ad_group_criterion.type = 'KEYWORD'"
    )
    got = env["rows"][0]
    assert got["ad_group_criterion"]["criterion_id"] == "55"
    assert got["ad_group_criterion"]["keyword"]["text"] == "ac repair"


def test_entities_ad_basic_query_and_row(fake_gads):
    row = make_row(**{"ad_group_ad.ad.id": 12345, "ad_group_ad.status": 2,
                      "ad_group.id": 777})
    fake_gads.search_responses[(CID, "")] = make_search_response([row])
    env = client.entities(CID, "ad")
    assert fake_gads.search_requests[-1].query == (
        "SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name "
        "FROM ad_group_ad WHERE ad_group_ad.status != 'REMOVED'"
    )
    got = env["rows"][0]
    assert got["ad_group_ad"]["ad"]["id"] == "12345"
    assert got["ad_group_ad"]["status"] == 2


def test_entities_unsupported_entity_type_raises(fake_gads):
    with pytest.raises(rails.RailViolation) as exc:
        client.entities(CID, "bogus")
    assert exc.value.code == "UNSUPPORTED_ENTITY"


def test_entities_campaign_with_parent_id_raises(fake_gads):
    with pytest.raises(rails.RailViolation) as exc:
        client.entities(CID, "campaign", parent_id="123")
    assert "parent_id" in str(exc.value)
    assert exc.value.code is None


@pytest.mark.parametrize('rows', [[_budget_row(), _budget_row()], [_budget_row(**{'campaign.id': 99})]])
def test_campaign_budget_parent_must_resolve_exactly(fake_gads, rows):
    fake_gads.search_responses[(CID, '')] = make_search_response(rows)
    with pytest.raises(rails.RailViolation):
        client.campaign_budget(CID, 42)


def recommendation_postcheck():
    budget = f'customers/{CID}/campaignBudgets/42'
    return {'recommendation_budget': True, 'customer_id': CID, 'budget_resource_name': budget,
            'expected': {'budget': {'resource_name': budget, 'amount_micros': 25000000,
                'period': 'DAILY', 'currency': 'USD', 'explicitly_shared': False,
                'reference_count': 1, 'aligned_bidding_strategy_id': '0'},
                'attachments': [{'id': '123', 'resource_name': f'customers/{CID}/campaigns/123',
                    'customer_id': CID, 'campaign_budget': budget, 'status': 'PAUSED'}]}}
