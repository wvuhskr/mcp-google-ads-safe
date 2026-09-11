"""Offline complete-control-scan and original secondary-create checks."""

import copy

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

ROOT = "999"
OWNER = CID
RESULT = f"customers/{OWNER}/conversionActions/789"
CAM = f"customers/{CID}/campaigns/123"
# Synthetic observed provider settings; the create operation does not request these.
MANAGED = {
    "value_settings": {
        "default_value": 7.5,
        "default_currency_code": "USD",
        "always_use_default_value": False,
    },
    "attribution_model_settings": {
        "attribution_model": "GOOGLE_ADS_LAST_CLICK",
        "data_driven_model_status": "UNSPECIFIED",
    },
}


def customer(cid, owner=None, manager=False):
    return {
        "resource_name": f"customers/{cid}",
        "id": cid,
        "manager": manager,
        "status": "ENABLED",
        "conversion_tracking_setting": {
            "google_ads_conversion_customer": f"customers/{owner or cid}"
        },
    }


def child(parent, cid, manager=False):
    return {
        "resource_name": f"customers/{parent}/customerClients/{cid}",
        "id": cid,
        "client_customer": f"customers/{cid}",
        "manager": manager,
        "level": 1,
        "status": "ENABLED",
        "hidden": False,
    }


def goal(cid=CID, campaign=None, category="DEFAULT", origin="WEBSITE", biddable=True):
    catnum = client._conversion_enum("ConversionActionCategoryEnum", category)[1]
    orgnum = client._conversion_enum("ConversionOriginEnum", origin)[1]
    kind = "campaignConversionGoals" if campaign else "customerConversionGoals"
    suffix = (
        campaign.rsplit("/", 1)[1] + "~" if campaign else ""
    ) + f"{catnum}~{orgnum}"
    item = {
        "resource_name": f"customers/{cid}/{kind}/{suffix}",
        "category": category,
        "origin": origin,
        "biddable": biddable,
    }
    if campaign:
        item["campaign"] = campaign
    return item


@pytest.fixture
def conversion(monkeypatch, fake_gads):
    monkeypatch.setenv("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT", "true")
    fake_gads.login_customer_id = ROOT
    data = {
        (ROOT, "customer"): [customer(ROOT, manager=True)],
        (CID, "customer"): [customer(CID)],
        (ROOT, "customer_client"): [child(ROOT, CID)],
        (CID, "campaign"): [
            {
                "resource_name": CAM,
                "id": "123",
                "name": "Search",
                "status": "PAUSED",
                "advertising_channel_type": "SEARCH",
            }
        ],
        (CID, "conversion_goal_campaign_config"): [
            {
                "resource_name": f"customers/{CID}/conversionGoalCampaignConfigs/123",
                "campaign": CAM,
                "custom_conversion_goal": "",
                "goal_config_level": "CUSTOMER",
            }
        ],
    }
    state = {
        "data": data,
        "reads": [],
        "after": None,
        "exact": None,
        "category": "DEFAULT",
    }

    def page(query, cid, token):
        assert token is None and "LIMIT" not in query
        entity = query.split(" FROM ")[1].split()[0]
        state["reads"].append((cid, entity, query))
        rows = copy.deepcopy(data.get((cid, entity), []))
        if entity == "conversion_action":
            for row in rows:
                for key, value in MANAGED.items():
                    row.setdefault(key, copy.deepcopy(value))
        if fake_gads.mutate_calls:
            if entity == "conversion_action":
                rows.append(
                    dict(
                        client.CONVERSION_FIXED,
                        name="Website lead",
                        category=state["category"],
                        resource_name=RESULT,
                        owner_customer=f"customers/{OWNER}",
                        origin="WEBSITE",
                        **copy.deepcopy(MANAGED),
                    )
                )
                if ".resource_name =" in query and state["exact"]:
                    state["exact"](rows)
            if entity in {"customer_conversion_goal", "campaign_conversion_goal"}:
                parent = CAM if entity == "campaign_conversion_goal" else None
                if not any(
                    item["category"] == state["category"]
                    and item["origin"] == "WEBSITE"
                    for item in rows
                ):
                    rows.append(
                        goal(
                            cid,
                            parent,
                            category=state["category"],
                            biddable=not any(
                                item["category"] == state["category"]
                                and item["biddable"] is False
                                for item in rows
                            ),
                        )
                    )
            if state["after"]:
                state["after"](cid, entity, rows, query)
        return [{entity: item} for item in rows], None, len(rows)

    monkeypatch.setattr(client, "_search_one_page", page)
    fake_gads.mutate_response = make_type("MutateGoogleAdsResponse")
    fake_gads.mutate_response.mutate_operation_responses.append(
        {"conversion_action_result": {"resource_name": RESULT}}
    )
    return state, fake_gads


def draft(**kwargs):
    return tools.create_conversion_action(
        kwargs.pop("name", "Website lead"), kwargs.pop("category", "DEFAULT"), **kwargs
    )


@pytest.mark.parametrize("category", sorted(client.CONVERSION_CATEGORIES))
def test_real_original_secondary_request(conversion, category):
    state, fake = conversion
    state["category"] = category
    d = draft(category=category)
    assert len(d["preview"]["goal_effects"]) == 2
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and out["verified"]
    assert (
        len(fake.mutate_calls) == 1 and fake.mutate_calls[0]["partial_failure"] is False
    )
    assert fake.mutate_calls[0]["customer_id"] == OWNER
    op = fake.mutate_calls[0]["operations"][0]
    obj = (
        type(op).deserialize(type(op).serialize(op)).conversion_action_operation.create
    )
    assert obj.type_.name == "WEBPAGE" and obj.status.name == "ENABLED"
    assert obj.primary_for_goal is False and obj._pb.HasField("primary_for_goal")
    assert obj.counting_type.name == "ONE_PER_CLICK"
    assert (
        obj.click_through_lookback_window_days == 30
        and obj.view_through_lookback_window_days == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("category", "UNKNOWN"),
        ("category", "PAGE_VIEW"),
        ("category", False),
        ("name", ""),
        ("name", None),
        ("name", 1),
        ("customer_id", ""),
        ("customer_id", False),
    ],
)
def test_bad_input_before_scan(conversion, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})
    assert not conversion[0]["reads"]


@pytest.mark.parametrize(
    "env,value",
    [
        ("GOOGLE_ADS_ENABLE_WRITES", "false"),
        ("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT", "false"),
        ("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT", "maybe"),
        ("GOOGLE_ADS_WRITE_CUSTOMER_IDS", "2"),
        ("GOOGLE_ADS_READ_CUSTOMER_IDS", "2"),
    ],
)
def test_gates_before_scan(conversion, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(rails.RailViolation):
        draft()
    assert not conversion[0]["reads"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("manager", 1),
        ("id", "2"),
        ("resource_name", "customers/2"),
        ("status", "CANCELED"),
        ("conversion_tracking_setting", {}),
        (
            "conversion_tracking_setting",
            {"google_ads_conversion_customer": "customers/2"},
        ),
    ],
)
def test_selected_identity_refuses(conversion, field, value):
    conversion[0]["data"][CID, "customer"][0][field] = value
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize(
    "field,value",
    [
        ("hidden", True),
        ("manager", 1),
        ("id", "4"),
        ("level", 2),
        ("status", "CANCELED"),
        ("resource_name", "customers/2/customerClients/3"),
    ],
)
def test_discovery_refuses(conversion, field, value):
    conversion[0]["data"][ROOT, "customer_client"][0][field] = value
    with pytest.raises(rails.RailViolation):
        draft()


def test_unallowlisted_tracking_sibling(conversion):
    data = conversion[0]["data"]
    data[ROOT, "customer_client"].append(child(ROOT, "777"))
    data["777", "customer"] = [customer("777", CID)]
    with pytest.raises(rails.RailViolation, match="every tracking") as exc:
        draft()
    assert exc.value.code == "CONVERSION_SCOPE"


def test_unreadable_unrelated_branch(conversion):
    conversion[0]["data"][ROOT, "customer_client"].append(child(ROOT, "777"))
    with pytest.raises(rails.RailViolation):
        draft()


def test_deep_large_graph_and_dag(conversion):
    data = conversion[0]["data"]
    parent = ROOT
    for number in range(1000, 1605):
        cid = str(number)
        data.setdefault((parent, "customer_client"), []).append(
            child(parent, cid, True)
        )
        data[cid, "customer"] = [customer(cid, manager=True)]
        parent = cid
    data[parent, "customer_client"] = [child(parent, CID)]
    d = draft()
    assert len(d["preview"]["control_scope"]["nodes"]) == 607


def test_cycle(conversion):
    data = conversion[0]["data"]
    data[ROOT, "customer_client"].append(child(ROOT, ROOT, True))
    with pytest.raises(rails.RailViolation, match="cycle"):
        draft()


@pytest.mark.parametrize(
    "failure", ["repeat", "count", "duplicate", "missing", "error"]
)
def test_paging_failures(conversion, monkeypatch, failure):
    calls = []

    def page(q, cid, token):
        calls.append(token)
        if failure == "error":
            raise RuntimeError("permission")
        row = {"customer": customer(CID)}
        if failure == "repeat":
            return [], "same", 0
        if failure == "count":
            return [], "next" if token is None else None, 0 if token is None else 1
        if failure == "duplicate":
            return [row, row], None, 2
        return [row], None, 2

    monkeypatch.setattr(client, "_search_one_page", page)
    with pytest.raises(rails.RailViolation):
        client._conversion_rows(CID, "customer")
    assert len(calls) <= 2


def test_uncapped_pages(conversion, monkeypatch):
    monkeypatch.setattr(client, "_SCAN_PAGE_CAP", 1)
    monkeypatch.setenv("GOOGLE_ADS_MAX_PAGES", "1")

    def page(q, cid, token):
        number = int(token or "0")
        return (
            [{"customer": customer(str(number + 1))}],
            str(number + 1) if number < 1001 else None,
            1002,
        )

    monkeypatch.setattr(client, "_search_one_page", page)
    assert len(client._conversion_rows(CID, "customer")) == 1002


@pytest.mark.parametrize("entity", ["conversion_goal_campaign_config", "campaign"])
def test_missing_references(conversion, entity):
    conversion[0]["data"][CID, entity] = []
    with pytest.raises(rails.RailViolation):
        draft()


def test_ambiguous_customer_default(conversion):
    conversion[0]["data"][CID, "customer_conversion_goal"] = [
        goal(origin="CALL_FROM_ADS", biddable=False)
    ]
    with pytest.raises(rails.RailViolation, match="ambiguous"):
        draft()


def test_existing_goals_and_campaign_false_default(conversion):
    data = conversion[0]["data"]
    data[CID, "customer_conversion_goal"] = [goal(biddable=False)]
    data[CID, "campaign_conversion_goal"] = [
        goal(campaign=CAM, origin="CALL_FROM_ADS", biddable=False)
    ]
    d = draft()
    assert d["preview"]["goal_effects"][0]["effect"] == "reuse"
    assert d["preview"]["goal_effects"][1]["goal"]["biddable"] is False
    assert rails.apply_draft(d["draft_id"])["verified"]


@pytest.mark.parametrize(
    "entity,field,value",
    [
        ("conversion_action", "primary_for_goal", True),
        ("conversion_action", "name", "Raced"),
        ("customer_conversion_goal", "biddable", False),
        ("campaign_conversion_goal", "biddable", False),
        ("conversion_goal_campaign_config", "goal_config_level", "CAMPAIGN"),
        ("campaign", "status", "ENABLED"),
    ],
)
def test_postwrite_content_races_consumed(conversion, entity, field, value):
    state, fake = conversion
    d = draft()

    def after(cid, resource, rows, query):
        if resource == entity and ".resource_name =" not in query:
            rows[0][field] = value

    state["after"] = after
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d["draft_id"])
    assert len(fake.mutate_calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "wrong"),
        ("primary_for_goal", True),
        ("origin", "CALL_FROM_ADS"),
        ("owner_customer", "customers/2"),
        ("type_", "UPLOAD_CLICKS"),
        ("status", "HIDDEN"),
        ("counting_type", "MANY_PER_CLICK"),
        ("click_through_lookback_window_days", 90),
        ("view_through_lookback_window_days", 30),
    ],
)
def test_exact_postcheck_mismatch(conversion, field, value):
    state, _ = conversion
    d = draft()
    state["exact"] = lambda rows: rows[0].update({field: value})
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"remove": RESULT},
        {"update": dict(client.CONVERSION_FIXED)},
        {
            "create": dict(
                client.CONVERSION_FIXED,
                name="x",
                category="DEFAULT",
                primary_for_goal=True,
            )
        },
        {
            "create": dict(
                client.CONVERSION_FIXED,
                name="x",
                category="DEFAULT",
                resource_name=RESULT,
            )
        },
    ],
)
def test_dispatch_closed_before_provider(conversion, monkeypatch, bad):
    monkeypatch.setattr(client, "gads", lambda: pytest.fail("provider reached"))
    plan = rails.EntityMutationPlan(
        CID, [rails.MutationOp("ConversionActionService", bad, None)], True
    )
    with pytest.raises(rails.RailViolation):
        client._dispatch(plan)


def test_validate_only_no_saved_reads(conversion):
    state, fake = conversion
    d = draft()
    out = client._dispatch_entity(rails._DRAFTS[d["draft_id"]].plan, True)
    assert not out.get("applied")
    assert not any(".resource_name =" in q for _, _, q in state["reads"])
    assert len(fake.mutate_calls) == 1


def test_scope_drift(conversion):
    state, fake = conversion
    d = draft()
    state["data"][ROOT, "customer_client"].append(child(ROOT, "777"))
    state["data"]["777", "customer"] = [customer("777")]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d["draft_id"])
    assert not fake.mutate_calls


def test_real_row_roundtrip(conversion, monkeypatch):
    page = client._search_one_page

    def proto_page(query, cid, token):
        rows, nxt, total = page(query, cid, token)
        return [make_row(row) for row in rows], nxt, total

    monkeypatch.setattr(client, "_search_one_page", proto_page)
    d = draft()
    assert rails.apply_draft(d["draft_id"])["verified"]


def make_row(row):
    obj = make_type("GoogleAdsRow")
    for field, value in row.items():
        setattr(obj, field, value)
    return obj


def test_owner_routing(conversion, monkeypatch):
    state, fake = conversion
    selected = "444"
    state["data"][ROOT, "customer_client"].append(child(ROOT, selected))
    state["data"][selected, "customer"] = [customer(selected, CID)]
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},{selected}")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", f"{CID},{selected}")
    d = draft(customer_id=selected)
    assert d["preview"]["mutate_customer_id"] == CID
    assert d["preview"]["control_scope"]["affected"] == sorted([CID, selected])
    # Request routing is independent of the fake's single-account goal side effects.
    client._dispatch_entity(rails._DRAFTS[d["draft_id"]].plan, True)
    assert fake.mutate_calls[0]["customer_id"] == CID


def test_duplicate_name(conversion):
    conversion[0]["data"][CID, "conversion_action"] = [
        dict(
            client.CONVERSION_FIXED,
            resource_name=RESULT,
            owner_customer=f"customers/{CID}",
            name="WEBSITE LEAD",
            category="DEFAULT",
            origin="WEBSITE",
        )
    ]
    conversion[0]["data"][CID, "conversion_action"][0]["status"] = "REMOVED"
    with pytest.raises(rails.RailViolation, match="name already"):
        draft()


def test_custom_membership_race(conversion):
    state, fake = conversion
    custom = {
        "resource_name": f"customers/{CID}/customConversionGoals/1",
        "id": "1",
        "name": "Custom",
        "status": "ENABLED",
        "conversion_actions": [],
    }
    state["data"][CID, "custom_conversion_goal"] = [custom]
    state["data"][CID, "conversion_goal_campaign_config"][0][
        "custom_conversion_goal"
    ] = custom["resource_name"]
    d = draft()
    state["after"] = lambda cid, entity, rows, q: (
        rows[0]["conversion_actions"].append(RESULT)
        if entity == "custom_conversion_goal"
        else None
    )
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"] and len(fake.mutate_calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "missing_action",
        "wrong_owner",
        "bad_flag",
        "missing_custom",
        "bad_config",
        "wrong_goal_id",
    ],
)
def test_goal_reference_refusals(conversion, change):
    data = conversion[0]["data"]
    custom = {
        "resource_name": f"customers/{CID}/customConversionGoals/1",
        "id": "1",
        "name": "Custom",
        "status": "ENABLED",
        "conversion_actions": [],
    }
    data[CID, "custom_conversion_goal"] = [custom]
    data[CID, "customer_conversion_goal"] = [goal()]
    if change == "missing_action":
        custom["conversion_actions"] = [RESULT]
    if change == "wrong_owner":
        custom["resource_name"] = "customers/2/customConversionGoals/1"
    if change == "bad_flag":
        data[CID, "customer_conversion_goal"][0]["biddable"] = 1
    if change == "missing_custom":
        data[CID, "conversion_goal_campaign_config"][0]["custom_conversion_goal"] = (
            "customers/123/customConversionGoals/999"
        )
    if change == "bad_config":
        data[CID, "conversion_goal_campaign_config"][0]["goal_config_level"] = "UNKNOWN"
    if change == "wrong_goal_id":
        data[CID, "customer_conversion_goal"][0]["resource_name"] = (
            "customers/2/customerConversionGoals/2~2"
        )
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize(
    "rn",
    [
        "customers/2/conversionActions/789",
        f"customers/{CID}/conversionActions/0",
        f"customers/{CID}/campaigns/1",
    ],
)
def test_bad_result_before_reads(conversion, rn):
    state, fake = conversion
    d = draft()
    checks = rails._DRAFTS[d["draft_id"]].plan.post_checks
    state["reads"].clear()
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(
            checks,
            {"results": [{"type": "conversion_action_result", "resource_name": rn}]},
        )
    assert not state["reads"] and not fake.mutate_calls


@pytest.mark.parametrize(
    "result",
    [
        {"results": []},
        {"results": [{"type": "campaign_result", "resource_name": RESULT}]},
        {
            "results": [{"type": "conversion_action_result", "resource_name": RESULT}]
            * 2
        },
        {"validate_only": True},
    ],
)
def test_bad_result_shape_before_reads(conversion, result):
    state, _ = conversion
    d = draft()
    state["reads"].clear()
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(
            rails._DRAFTS[d["draft_id"]].plan.post_checks, result
        )
    assert not state["reads"]


@pytest.mark.parametrize("change", ["input", "goal", "digest", "expiry", "gate"])
def test_confirm_refuses_drift(conversion, monkeypatch, change):
    state, fake = conversion
    d = draft()
    saved = rails._DRAFTS[d["draft_id"]]
    if change == "input":
        saved.plan.operations[0].operation["create"]["name"] = "changed"
    if change == "goal":
        state["data"][CID, "customer_conversion_goal"] = [goal()]
    if change == "digest":
        saved.digest = "wrong"
    if change == "expiry":
        saved.created_at -= 4000
    if change == "gate":
        monkeypatch.setenv("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT", "false")
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d["draft_id"])
    assert not fake.mutate_calls


@pytest.mark.parametrize(
    "change", ["extra_goal", "missing_goal", "extra_action", "owner", "tree"]
)
def test_postwrite_inventory_changes(conversion, change):
    state, _ = conversion
    d = draft()

    def after(cid, entity, rows, query):
        if change == "extra_goal" and entity == "customer_conversion_goal":
            rows.append(goal(category="PURCHASE"))
        if change == "missing_goal" and entity == "campaign_conversion_goal":
            rows.clear()
        if (
            change == "extra_action"
            and entity == "conversion_action"
            and ".resource_name =" not in query
        ):
            rows.append(
                dict(rows[0], resource_name=f"customers/{CID}/conversionActions/790")
            )
        if change == "owner" and entity == "customer" and cid == CID:
            rows[0]["conversion_tracking_setting"]["google_ads_conversion_customer"] = (
                "customers/2"
            )
        if change == "tree" and entity == "customer_client":
            rows.clear()

    state["after"] = after
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]


def test_mixed_and_mask_dispatch_refusal(conversion, monkeypatch):
    values = dict(client.CONVERSION_FIXED, name="x", category="DEFAULT")
    op = rails.safe_create_operation("ConversionActionService", values)
    monkeypatch.setattr(client, "gads", lambda: pytest.fail("provider reached"))
    for ops in [[op, op], [rails.MutationOp(op.service, op.operation, ["name"])]]:
        with pytest.raises(rails.RailViolation):
            client._dispatch(rails.EntityMutationPlan(CID, ops, True))


def test_unknown_consumed_and_audited(conversion, monkeypatch):
    from pathlib import Path

    from mcp_google_ads_safe import audit

    d = draft()
    error = rails.UnknownWriteOutcome(
        "unknown", request_id="request", failure={"code": "X"}
    )
    monkeypatch.setattr(client, "_dispatch", lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(rails.UnknownWriteOutcome) as exc:
        rails.apply_draft(d["draft_id"])
    assert exc.value.request_id == "request" and exc.value.failure == {"code": "X"}
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d["draft_id"])
    assert "unknown" in Path(audit.AUDIT_PATH).read_text()


def test_refusal_audited(conversion, monkeypatch):
    from pathlib import Path

    from mcp_google_ads_safe import audit

    monkeypatch.delenv("GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT")
    with pytest.raises(rails.RailViolation):
        draft()
    assert "refused" in Path(audit.AUDIT_PATH).read_text()
    assert not conversion[0]["reads"]


def test_postwrite_read_error_consumed(conversion, monkeypatch):
    state, fake = conversion
    d = draft()
    page = client._search_one_page

    def read(*args):
        if fake.mutate_calls:
            raise RuntimeError("unreadable")
        return page(*args)

    monkeypatch.setattr(client, "_search_one_page", read)
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d["draft_id"])
    assert len(fake.mutate_calls) == 1


def test_shared_custom_content_conflict(conversion, monkeypatch):
    data = conversion[0]["data"]
    data[ROOT, "customer_client"].append(child(ROOT, "444"))
    data["444", "customer"] = [customer("444", CID)]
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},444")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", f"{CID},444")
    custom = {
        "resource_name": f"customers/{CID}/customConversionGoals/1",
        "id": "1",
        "name": "Custom",
        "status": "ENABLED",
        "conversion_actions": [],
    }
    data[CID, "custom_conversion_goal"] = [custom]
    data["444", "custom_conversion_goal"] = [dict(custom, name="Changed")]
    with pytest.raises(rails.RailViolation, match="conflicts"):
        draft()


def test_removed_campaign_no_predicted_creation(conversion):
    data = conversion[0]["data"]
    data[CID, "campaign"][0]["status"] = "REMOVED"
    data[CID, "conversion_goal_campaign_config"] = []
    d = draft()
    assert len(d["preview"]["goal_effects"]) == 1


def test_owner_routing_inconsistent(conversion, monkeypatch):
    data = conversion[0]["data"]
    data[CID, "customer"][0]["conversion_tracking_setting"][
        "google_ads_conversion_customer"
    ] = "customers/444"
    data[ROOT, "customer_client"].append(child(ROOT, "444"))
    data["444", "customer"] = [customer("444", CID)]
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", f"{CID},444")
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", f"{CID},444")
    with pytest.raises(rails.RailViolation, match="consistent conversion owner"):
        draft()


def test_missing_login_and_nonmanager_root(conversion):
    _, fake = conversion
    fake.login_customer_id = ""
    with pytest.raises(rails.RailViolation):
        draft()
    fake.login_customer_id = ROOT
    conversion[0]["data"][ROOT, "customer"][0]["manager"] = False
    with pytest.raises(rails.RailViolation, match="must be a manager"):
        draft()


def test_provider_primary_presence_required(conversion):
    state, _ = conversion
    d = draft()
    state["exact"] = lambda rows: rows[0].pop("primary_for_goal")
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]


@pytest.mark.parametrize(
    "entity", ["customer_conversion_goal", "campaign_conversion_goal"]
)
def test_existing_actions_require_goal_coverage(conversion, entity):
    data = conversion[0]["data"]
    data[CID, "conversion_action"] = [
        dict(
            client.CONVERSION_FIXED,
            name="Old",
            category="PURCHASE",
            resource_name=RESULT,
            owner_customer=f"customers/{CID}",
            origin="WEBSITE",
        )
    ]
    data[CID, "customer_conversion_goal"] = [goal(category="PURCHASE")]
    data[CID, "campaign_conversion_goal"] = [goal(category="PURCHASE", campaign=CAM)]
    data[CID, entity] = []
    with pytest.raises(rails.RailViolation, match="goal coverage"):
        draft()


def test_audit_failure_does_not_save_draft(conversion, monkeypatch):
    from mcp_google_ads_safe import audit

    before = set(rails._DRAFTS)
    monkeypatch.setattr(
        audit, "log_event", lambda *args: (_ for _ in ()).throw(OSError("audit"))
    )
    with pytest.raises(OSError):
        draft()
    assert set(rails._DRAFTS) == before and not conversion[1].mutate_calls


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("value_settings", "default_value", 23.0),
        (
            "attribution_model_settings",
            "attribution_model",
            "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        ),
    ],
)
def test_existing_provider_settings_drift(conversion, group, field, value):
    state, fake = conversion
    existing = dict(
        client.CONVERSION_FIXED,
        name="Historical",
        category="DEFAULT",
        status="REMOVED",
        resource_name=f"customers/{CID}/conversionActions/700",
        owner_customer=f"customers/{CID}",
        origin="WEBSITE",
        **copy.deepcopy(MANAGED),
    )
    state["data"][CID, "conversion_action"] = [existing]
    d = draft()
    existing[group][field] = value
    with pytest.raises(rails.RailViolation, match="changed since preview"):
        rails.apply_draft(d["draft_id"])
    assert not fake.mutate_calls


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("value_settings", "default_value", 23.0),
        (
            "attribution_model_settings",
            "attribution_model",
            "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        ),
    ],
)
def test_later_new_provider_settings_race(conversion, group, field, value):
    state, fake = conversion
    d = draft()

    def race(cid, entity, rows, query):
        if entity == "conversion_action" and ".resource_name =" not in query:
            rows[0][group][field] = value

    state["after"] = race
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d["draft_id"])
    assert len(fake.mutate_calls) == 1


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("value_settings", "default_value", float("nan")),
        ("value_settings", "default_value", True),
        ("value_settings", "always_use_default_value", 1),
        ("value_settings", "default_currency_code", None),
        ("attribution_model_settings", "attribution_model", "UNKNOWN"),
        ("attribution_model_settings", "data_driven_model_status", "made_up"),
    ],
)
def test_unreadable_provider_settings(conversion, group, field, value):
    state, _ = conversion
    d = draft()
    state["exact"] = lambda rows: rows[0][group].update({field: value})
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]


@pytest.mark.parametrize("group", ["value_settings", "attribution_model_settings"])
def test_missing_provider_settings(conversion, group):
    state, _ = conversion
    d = draft()
    state["exact"] = lambda rows: rows[0].pop(group)
    out = rails.apply_draft(d["draft_id"])
    assert out["applied"] and not out["verified"]


def test_provider_settings_observed_not_requested(conversion):
    state, fake = conversion
    d = draft()
    out = rails.apply_draft(d["draft_id"])
    assert out["verified"]
    queries = [q for _, entity, q in state["reads"] if entity == "conversion_action"]
    assert all(
        "value_settings.default_value" in q
        and "attribution_model_settings.attribution_model" in q
        for q in queries
    )
    operation = fake.mutate_calls[0]["operations"][0].conversion_action_operation.create
    assert not operation._pb.HasField("value_settings")
    assert not operation._pb.HasField("attribution_model_settings")
