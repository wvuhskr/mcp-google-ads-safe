"""Offline retail listing filter contract, with actual v25 messages and no credentials."""

import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, tools
from tests.conftest import make_type

CID, GID = "1234567890", "90"
GROUP = f"customers/{CID}/assetGroups/{GID}"
CAMPAIGN = f"customers/{CID}/campaigns/88"


def node(n, kind="UNIT_INCLUDED", parent="", case=None):
    row = make_type("GoogleAdsRow")
    values = dict(
        resource_name=f"customers/{CID}/assetGroupListingGroupFilters/90~{n}",
        asset_group=GROUP,
        id=n,
        type_=kind,
        listing_source="SHOPPING",
    )
    if parent:
        values["parent_listing_group_filter"] = (
            f"customers/{CID}/assetGroupListingGroupFilters/90~{parent}"
        )
    if case is not None:
        values["case_value"] = case
    row.asset_group_listing_group_filter = values
    return row


@pytest.fixture
def retail(monkeypatch):
    customer, campaign, group = [make_type("GoogleAdsRow") for _ in range(3)]
    customer.customer = dict(
        resource_name=f"customers/{CID}",
        id=int(CID),
        status="ENABLED",
        manager=False,
        currency_code="USD",
        time_zone="America/New_York",
    )
    campaign.campaign = dict(
        resource_name=CAMPAIGN,
        id=88,
        name="Retail",
        status="PAUSED",
        advertising_channel_type="PERFORMANCE_MAX",
        advertising_channel_sub_type="UNSPECIFIED",
        bidding_strategy_type="MAXIMIZE_CONVERSION_VALUE",
        maximize_conversion_value={"target_roas": 2.5},
        shopping_setting={"merchant_id": 123, "feed_label": "US"},
        campaign_budget=f"customers/{CID}/campaignBudgets/555",
    )
    group.asset_group = dict(
        resource_name=GROUP,
        id=90,
        campaign=CAMPAIGN,
        status="PAUSED",
        name="Retail Group",
        final_urls=["https://example.com"],
    )
    data = {
        "customer": [customer],
        "campaign": [campaign],
        "asset_group": [group],
        "asset_group_listing_group_filter": [],
    }
    queries = []

    def scan(query, cid):
        assert cid == CID
        queries.append(query)
        resource = query.split(" FROM ")[1].split()[0]
        return copy.deepcopy(data[resource])

    monkeypatch.setattr(client, "_scan_rows", scan)
    monkeypatch.setattr(client, "gads", lambda: pytest.fail("unexpected provider"))
    return data, queries


def draft(ids=None, **kwargs):
    return tools.set_listing_group_filter(
        GID, ["b", "A"] if ids is None else ids, **kwargs
    )


def plan(ids=None):
    return rails._DRAFTS[draft(ids)["draft_id"]].plan


def test_empty_initialization_closed_graph(retail):
    result = draft()
    pending = rails._DRAFTS[result["draft_id"]].plan
    context = client.validate_mutation_plan(pending)
    assert len(pending.operations) == 4
    operations = [
        client._build_mutate_operation(TypeClient(), op, context)
        for op in pending.operations
    ]
    root = operations[0].asset_group_listing_group_filter_operation.create
    remainder = operations[-1].asset_group_listing_group_filter_operation.create
    assert root.resource_name.endswith("90~-1")
    assert not root._pb.HasField("case_value")
    assert remainder.case_value._pb.WhichOneof("dimension") == "product_item_id"
    assert not remainder.case_value.product_item_id._pb.HasField("value")
    assert remainder.parent_listing_group_filter == root.resource_name
    assert [
        op.asset_group_listing_group_filter_operation.create.case_value.product_item_id.value
        for op in operations[1:-1]
    ] == ["A", "b"]


class TypeClient:
    def get_type(self, name):
        return make_type(name)


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["x"] * 2,
        ["null"],
        [" x"],
        ["x "],
        ["x\n"],
        [True],
        [1],
        "x",
        ("x",),
        ["é" * 26],
        ["x" * 51],
        [str(i) for i in range(21)],
    ],
)
def test_bad_item_inputs_refuse(retail, ids):
    with pytest.raises(rails.RailViolation):
        draft(ids)


def tree(ids=("old",), start=10):
    rows = [node(start, "SUBDIVISION")]
    rows += [
        node(start + i, parent=start, case={"product_item_id": {"value": value}})
        for i, value in enumerate(ids, 1)
    ]
    rows += [
        node(
            start + len(ids) + 1,
            "UNIT_EXCLUDED",
            parent=start,
            case={"product_item_id": {}},
        )
    ]
    return rows


@pytest.mark.parametrize(
    "old", [[], [node(1)], tree(), tree(tuple(str(i) for i in range(20)))]
)
def test_accepted_trees_atomic_order_and_maximal_request(retail, old):
    retail[0]["asset_group_listing_group_filter"] = copy.deepcopy(old)
    pending = plan([f"ID{i}" for i in range(20)])
    context = client.validate_mutation_plan(pending)
    assert len(pending.operations) == len(old) + 22 <= 44
    remove = [
        op.operation["remove"] for op in pending.operations if "remove" in op.operation
    ]
    if old:
        assert remove[-1] == old[0].asset_group_listing_group_filter.resource_name
        assert remove[:-1] == sorted(remove[:-1])
    for op in pending.operations:
        built = client._build_mutate_operation(TypeClient(), op, context)
        sub = built.asset_group_listing_group_filter_operation
        assert not sub._pb.HasField("update_mask")
        if sub._pb.WhichOneof("operation") == "create":
            assert sub.create.id == 0
            assert "id" not in {f.name for f, _ in sub.create._pb.ListFields()}
            assert not sub.create._pb.HasField("path")
            assert "status" not in sub.create._pb.DESCRIPTOR.fields_by_name
    assert all("LIMIT" not in q for q in retail[1])
    assert "status" not in retail[1][-1]


def test_unchanged_exact_set_and_case_preservation(retail):
    retail[0]["asset_group_listing_group_filter"] = tree(("b", "A"))
    with pytest.raises(rails.RailViolation) as error:
        draft(["A", "b"])
    assert error.value.code == "NO_CHANGES"
    pending = draft(["a", "B", "é" * 25])
    assert pending["preview"]["new_item_ids"] == ["B", "a", "é" * 25]


@pytest.mark.parametrize("mode", ["off", "read", "write"])
def test_gates_before_any_scan_direct_and_actual_mcp(monkeypatch, mode):
    from tests.test_protocol_errors import boundary, payload

    if mode == "off":
        monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "false")
    else:
        monkeypatch.setenv("GOOGLE_ADS_" + mode.upper() + "_CUSTOMER_IDS", "999")
    monkeypatch.setattr(client, "_scan_rows", lambda *a: pytest.fail("scan reached"))
    monkeypatch.setattr(client, "gads", lambda: pytest.fail("provider reached"))
    with pytest.raises(rails.RailViolation):
        draft()
    assert payload(
        boundary(
            app.mcp,
            "set_listing_group_filter",
            {"asset_group_id": GID, "product_item_ids": ["A"]},
        )
    )["code"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("customer_id", None),
        ("customer_id", 123),
        ("customer_id", "null"),
        ("asset_group_id", True),
        ("asset_group_id", 90),
        ("product_item_ids", [123]),
        ("product_item_ids", "A"),
    ],
)
def test_mcp_original_types_no_null_coercion(retail, field, value):
    from tests.test_protocol_errors import boundary, payload

    args = {"asset_group_id": GID, "product_item_ids": ["A"], field: value}
    assert (
        payload(boundary(app.mcp, "set_listing_group_filter", args))["code"]
        == "BAD_INPUT"
    )


@pytest.mark.parametrize(
    "value", [True, 90, "090", " 90", "90 ", "-90", "0", "９０", "null", GROUP]
)
def test_strict_group_ids(retail, value):
    with pytest.raises(rails.RailViolation):
        tools.set_listing_group_filter(value, ["A"])
    assert not retail[1]


def test_direct_none_default_and_public_inventory(retail):
    assert draft(customer_id=None)["preview"]["customer_id"] == CID

    async def check():
        item = next(
            x
            for x in await app.mcp.list_tools()
            if x.name == "set_listing_group_filter"
        )
        assert set(item.input_schema["properties"]) == {
            "customer_id",
            "asset_group_id",
            "product_item_ids",
        }
        assert set(item.input_schema["required"]) == {
            "asset_group_id",
            "product_item_ids",
        }

    asyncio.run(check())


@pytest.mark.parametrize(
    ("resource", "path", "value"),
    [
        ("customer", "manager", True),
        ("customer", "status", "CANCELED"),
        ("customer", "id", 9),
        ("asset_group", "status", "ENABLED"),
        ("asset_group", "id", 91),
        ("asset_group", "campaign", "customers/999/campaigns/88"),
        ("campaign", "status", "ENABLED"),
        ("campaign", "id", 89),
        ("campaign", "advertising_channel_type", "SEARCH"),
        ("campaign", "advertising_channel_sub_type", "SHOPPING_SMART_ADS"),
        ("campaign", "shopping_setting.merchant_id", 0),
        ("campaign", "shopping_setting.feed_label", ""),
        ("campaign", "shopping_setting.feed_label", "lowercase"),
        ("campaign", "shopping_setting.feed_label", "A" * 21),
        ("campaign", "shopping_setting.enable_local", True),
        ("campaign", "shopping_setting.use_vehicle_inventory", True),
        ("campaign", "shopping_setting.advertising_partner_ids", [22]),
        ("campaign", "shopping_setting.disable_product_feed", True),
        ("campaign", "shopping_setting.ignore_brand_exclusion_in_shopping_ads", True),
        ("campaign", "shopping_setting.campaign_priority", 1),
        ("campaign", "listing_type", "VEHICLES"),
        ("campaign", "listing_type", "UNKNOWN"),
        ("campaign", "hotel_property_asset_set", "customers/123/assetSets/1"),
        ("campaign", "travel_campaign_settings", {}),
        ("campaign", "hotel_setting", {}),
        ("campaign", "bidding_strategy", f"customers/{CID}/biddingStrategies/1"),
        ("campaign", "bidding_strategy_type", "MANUAL_CPC"),
        ("campaign", "maximize_conversion_value.target_roas", -1),
        ("campaign", "maximize_conversion_value.target_roas", float("nan")),
        ("campaign", "maximize_conversion_value.target_roas", float("inf")),
        ("campaign", "maximize_conversion_value.cpc_bid_ceiling_micros", 1),
        (
            "campaign",
            "maximize_conversion_value.target_roas_tolerance_percent_millis",
            0,
        ),
        ("campaign", "manual_cpc", {}),
    ],
)
def test_raw_retail_refusals(retail, resource, path, value):
    raw = getattr(retail[0][resource][0], resource)
    parts = path.split(".")
    for part in parts[:-1]:
        raw = getattr(raw, part)
    setattr(raw, parts[-1], value)
    with pytest.raises(rails.RailViolation):
        draft()


def test_full_shopping_bidding_selection_and_untouched_snapshot(retail):
    campaign = retail[0]["campaign"][0].campaign
    campaign.maximize_conversions = {"target_cpa_micros": 0}
    campaign.bidding_strategy_type = "MAXIMIZE_CONVERSIONS"
    campaign.listing_type = "UNSPECIFIED"
    campaign.shopping_setting.enable_local = False
    group = retail[0]["asset_group"][0].asset_group
    group.final_urls = []
    group.final_mobile_urls = ["http://untouched.example/path"]
    group.path1 = "existing"
    group.name = ""
    pending = plan()
    assert (
        client._listing_decode(
            pending.post_checks[0]["before"]["proof"]["group"], "AssetGroup"
        )
        == group
    )
    selected = next(q for q in retail[1] if " FROM campaign " in q)
    for name in client._listing_fields("Campaign"):
        assert "campaign." + name in selected or name in (
            "manual_cpa",
            "manual_cpm",
            "manual_cpv",
        )
    assert (
        "campaign.shopping_setting.ignore_brand_exclusion_in_shopping_ads" in selected
    )
    assert (
        "campaign.maximize_conversion_value.target_roas_tolerance_percent_millis"
        in selected
    )


@pytest.mark.parametrize("field", ["shopping_setting", "maximize_conversion_value"])
def test_missing_required_parent_message(retail, field):
    retail[0]["campaign"][0].campaign._pb.ClearField(field)
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize("resource", ["customer", "asset_group", "campaign"])
@pytest.mark.parametrize("bad", ["empty", "duplicate", "dictionary"])
def test_unreadable_or_nonunique_raw_proof(retail, resource, bad):
    rows = retail[0][resource]
    retail[0][resource] = (
        [] if bad == "empty" else rows * 2 if bad == "duplicate" else [{}]
    )
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate_root",
        "duplicate_child",
        "missing_root",
        "missing_remainder",
        "included_remainder",
        "empty_value",
        "absent_case",
        "nonitem",
        "retail",
        "webpage",
        "unknown_type",
        "root_excluded",
        "deep",
        "cross_group",
        "cycle",
        "extra",
        "dict",
    ],
)
def test_unsupported_existing_tree(retail, damage):
    rows = tree()
    child = rows[1].asset_group_listing_group_filter
    if damage == "duplicate_root":
        rows.append(copy.deepcopy(rows[0]))
    elif damage == "duplicate_child":
        rows.append(copy.deepcopy(rows[1]))
    elif damage == "missing_root":
        rows.pop(0)
    elif damage == "missing_remainder":
        rows.pop()
    elif damage == "included_remainder":
        rows[-1].asset_group_listing_group_filter.type_ = "UNIT_INCLUDED"
    elif damage == "empty_value":
        child.case_value.product_item_id.value = ""
    elif damage == "absent_case":
        child._pb.ClearField("case_value")
    elif damage == "nonitem":
        child.case_value = {"product_brand": {"value": "Brand"}}
    elif damage == "retail":
        child.listing_source = "RETAIL"
    elif damage == "webpage":
        child.listing_source = "WEBPAGE"
    elif damage == "unknown_type":
        child.type_ = "UNSPECIFIED"
    elif damage == "root_excluded":
        rows = [node(1, "UNIT_EXCLUDED")]
    elif damage == "deep":
        child.parent_listing_group_filter = rows[
            -1
        ].asset_group_listing_group_filter.resource_name
    elif damage == "cross_group":
        child.asset_group = f"customers/{CID}/assetGroups/91"
    elif damage == "cycle":
        rows[
            0
        ].asset_group_listing_group_filter.parent_listing_group_filter = (
            child.resource_name
        )
    elif damage == "extra":
        rows = tree(tuple(str(i) for i in range(21)))
    else:
        rows = [{}]
    retail[0]["asset_group_listing_group_filter"] = rows
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize(
    "damage",
    [
        "field",
        "service",
        "mask",
        "order",
        "extra",
        "missing",
        "parent",
        "check",
        "proof",
        "bare",
        "remove",
        "remove_order",
    ],
)
def test_forged_closed_graph_refused_before_provider(retail, damage):
    retail[0]["asset_group_listing_group_filter"] = tree()
    pending = plan()
    op = pending.operations[-1]
    if damage == "field":
        op.operation["create"]["status"] = "PAUSED"
    elif damage == "service":
        object.__setattr__(op, "service", "AssetGroupService")
    elif damage == "mask":
        object.__setattr__(op, "update_mask", [])
    elif damage == "order":
        pending.operations.reverse()
    elif damage == "extra":
        pending.operations.append(op)
    elif damage == "missing":
        pending.operations.pop()
    elif damage == "parent":
        op.operation["create"]["parent_listing_group_filter"] = (
            "customers/123/assetGroups/90"
        )
    elif damage == "check":
        pending.post_checks[0]["extra"] = True
    elif damage == "proof":
        pending.post_checks[0]["before"]["proof"]["campaign"] = ""
    elif damage == "bare":
        pending.post_checks.clear()
    elif damage == "remove":
        pending.operations[0].operation["remove"] = client.listing_filter_path(
            CID, GID, 99
        )
    else:
        pending.operations[0], pending.operations[1] = (
            pending.operations[1],
            pending.operations[0],
        )
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(pending, False)


@pytest.mark.parametrize(
    "change", ["merchant", "feed", "bidding", "status", "group", "tree", "presence"]
)
def test_fresh_proof_drift_aborts_before_dispatch(retail, change):
    result = draft()
    raw = retail[0]["campaign"][0].campaign
    if change == "merchant":
        raw.shopping_setting.merchant_id = 124
    elif change == "feed":
        raw.shopping_setting.feed_label = "GB"
    elif change == "bidding":
        raw.maximize_conversion_value.target_roas = 3
    elif change == "status":
        raw.status = "ENABLED"
    elif change == "group":
        retail[0]["asset_group"][0].asset_group.name = "changed"
    elif change == "presence":
        raw.shopping_setting.enable_local = False
    else:
        retail[0]["asset_group_listing_group_filter"] = [node(10)]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result["draft_id"])


def saved(pending):
    entries, rows = [], []
    mapping = {}
    for op in pending.operations:
        if "remove" in op.operation:
            rn = op.operation["remove"]
        else:
            values = copy.deepcopy(op.operation["create"])
            number = 100 + len(rows)
            rn = client.listing_filter_path(CID, GID, number)
            mapping[values["resource_name"]] = rn
            values["resource_name"], values["id"] = rn, number
            if "parent_listing_group_filter" in values:
                values["parent_listing_group_filter"] = mapping[
                    values["parent_listing_group_filter"]
                ]
            row = make_type("GoogleAdsRow")
            row.asset_group_listing_group_filter = values
            rows.append(row)
        entries.append(
            {"type": "asset_group_listing_group_filter_result", "resource_name": rn}
        )
    return {"results": entries}, rows


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "missing_result",
        "extra_result",
        "duplicate_result",
        "kind",
        "foreign_result",
        "swapped_creates",
        "swapped_removes",
        "old_remains",
        "missing_row",
        "extra_row",
        "parent",
        "source",
        "type",
        "case",
        "path",
        "group_drift",
        "parent_drift",
        "read_failure",
    ],
)
def test_result_binding_and_exact_readback(retail, monkeypatch, damage):
    retail[0]["asset_group_listing_group_filter"] = tree()
    pending = plan()
    result, rows = saved(pending)
    count = 3
    if damage == "missing_result":
        result["results"].pop()
    elif damage == "extra_result":
        result["results"].append(result["results"][-1])
    elif damage == "duplicate_result":
        result["results"][-1] = result["results"][-2]
    elif damage == "kind":
        result["results"][-1]["type"] = "asset_group_result"
    elif damage == "foreign_result":
        result["results"][-1]["resource_name"] = (
            "customers/999/assetGroupListingGroupFilters/90~200"
        )
    elif damage == "swapped_creates":
        result["results"][count + 1], result["results"][count + 2] = (
            result["results"][count + 2],
            result["results"][count + 1],
        )
    elif damage == "swapped_removes":
        result["results"][0], result["results"][1] = (
            result["results"][1],
            result["results"][0],
        )
    elif damage == "old_remains":
        rows.extend(tree())
    elif damage == "missing_row":
        rows.pop()
    elif damage == "extra_row":
        rows.append(node(999))
    elif damage == "parent":
        rows[
            1
        ].asset_group_listing_group_filter.parent_listing_group_filter = (
            client.listing_filter_path(CID, GID, 999)
        )
    elif damage == "source":
        rows[1].asset_group_listing_group_filter.listing_source = "RETAIL"
    elif damage == "type":
        rows[1].asset_group_listing_group_filter.type_ = "UNIT_EXCLUDED"
    elif damage == "case":
        rows[
            1
        ].asset_group_listing_group_filter.case_value.product_item_id.value = "wrong"
    elif damage == "path":
        rows[1].asset_group_listing_group_filter.path = {
            "dimensions": [{"product_item_id": {"value": "wrong"}}]
        }
    elif damage == "group_drift":
        retail[0]["asset_group"][0].asset_group.path1 = "changed"
    elif damage == "parent_drift":
        retail[0]["campaign"][0].campaign.shopping_setting.feed_label = "CA"
    elif damage == "read_failure":
        monkeypatch.setattr(
            client,
            "_scan_rows",
            lambda *a: (_ for _ in ()).throw(RuntimeError("read failed")),
        )
    retail[0]["asset_group_listing_group_filter"] = rows
    if damage == "none":
        client.verify_listing_filter_result(pending.post_checks, result)
    else:
        with pytest.raises((rails.RailViolation, RuntimeError)):
            client.verify_listing_filter_result(pending.post_checks, result)


def test_apply_verification_failure_consumes_without_retry(retail, monkeypatch):
    result = draft()
    pending = rails._DRAFTS[result["draft_id"]].plan
    response, rows = saved(pending)
    calls = []

    def dispatch(plan):
        calls.append(plan)
        retail[0]["asset_group_listing_group_filter"] = rows[:-1]
        return response

    monkeypatch.setattr(client, "_dispatch", dispatch)
    applied = rails.apply_draft(result["draft_id"])
    assert (
        applied["applied"] is True
        and applied["verified"] is False
        and applied["code"] == "POST_WRITE_VERIFICATION_FAILED"
    )
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result["draft_id"])
    assert len(calls) == 1


def test_real_request_atomic_flags_and_validate_only(retail, monkeypatch):
    pending = plan()

    class Provider(TypeClient):
        def get_service(self, name):
            assert name == "GoogleAdsService"
            return self

        def mutate(self, *, request):
            assert request.partial_failure is False and request.validate_only is True
            assert request.customer_id == CID and len(request.mutate_operations) == 4
            return make_type("MutateGoogleAdsResponse")

    monkeypatch.setattr(client, "gads", Provider)
    assert client._dispatch_entity(pending, True)["validate_only"] is True


def test_incomplete_scan_refuses_before_draft(retail, monkeypatch):
    # Exercise the shared complete raw scanner, rather than a fake exception.
    monkeypatch.undo()
    monkeypatch.setattr(client, "_search_one_page", lambda *a: ([], None, 1))
    with pytest.raises(rails.RailViolation) as error:
        client.listing_filter_state(CID, GID)
    assert error.value.code == "SCAN_INCOMPLETE"


def test_generic_node_and_forged_context_refuse(retail):
    pending = plan()
    op = pending.operations[0]
    with pytest.raises(rails.RailViolation):
        client._build_mutate_operation(TypeClient(), op)
    context = client.validate_mutation_plan(pending)
    context.plan.operations[0].operation["create"]["status"] = "PAUSED"
    with pytest.raises(rails.RailViolation):
        client._build_mutate_operation(TypeClient(), op, context)


def test_saved_semantic_paths_accept_typed_empty_remainder(retail):
    pending = plan()
    result, rows = saved(pending)
    for row in rows[1:]:
        raw = row.asset_group_listing_group_filter
        raw.path.dimensions.append(raw.case_value)
    retail[0]["asset_group_listing_group_filter"] = rows
    client.verify_listing_filter_result(pending.post_checks, result)


def test_transport_ambiguity_consumes_without_retry(retail, monkeypatch):
    from tests.test_final_fixes import mapped_error

    result = draft()
    calls = []

    def dispatch(plan):
        calls.append(plan)
        raise mapped_error("remapped")

    monkeypatch.setattr(client, "_dispatch", dispatch)
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(result["draft_id"])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result["draft_id"])
    assert len(calls) == 1


def test_legacy_nonretail_proof_still_refuses_retail(retail, monkeypatch):
    monkeypatch.setattr(client, "_creation_account", lambda *a, **k: {"manager": False})
    with pytest.raises(rails.RailViolation, match="retail"):
        client._pmax_asset_group_parent_brand_proof(CID, "88")


def test_validate_only_result_cannot_prove_saved_state(retail):
    pending = plan()
    response, rows = saved(pending)
    response["validate_only"] = True
    retail[0]["asset_group_listing_group_filter"] = rows
    with pytest.raises(rails.RailViolation):
        client.verify_listing_filter_result(pending.post_checks, response)


def test_other_context_cannot_build_listing_node(retail):
    pending = plan()
    with pytest.raises(rails.RailViolation):
        client._build_mutate_operation(
            TypeClient(), pending.operations[0], client._PMaxContext(pending)
        )


def test_successful_apply_consumes_and_verifies_exact_state(retail, monkeypatch):
    result = draft()
    response, rows = saved(rails._DRAFTS[result["draft_id"]].plan)

    def dispatch(pending):
        retail[0]["asset_group_listing_group_filter"] = rows
        return response

    monkeypatch.setattr(client, "_dispatch", dispatch)
    assert rails.apply_draft(result["draft_id"])["verified"] is True
    assert result["draft_id"] not in rails._DRAFTS


@pytest.mark.parametrize("guard", ["vehicle", "graph", "saved_mapping"])
def test_selected_guard_disabled_sensitivity(retail, monkeypatch, guard):
    import inspect

    # Disable one guard in an isolated function namespace; source files stay unchanged.
    if guard == "vehicle":
        function = client._validate_listing_proof
        before, after = "or shopping.use_vehicle_inventory", "or False"
    elif guard == "graph":
        function = client.validate_listing_filter_plan
        before, after = "if plan.operations != expected:", "if False:"
    else:
        function = client.verify_listing_filter_result
        before = "sorted(actual, key=lambda x: x['resource_name']) != sorted(expected, key=lambda x: x['resource_name'])"
        after = "False"
    source = inspect.getsource(function)
    assert source.count(before) == 1
    namespace = dict(client.__dict__)
    exec(source.replace(before, after), namespace)
    monkeypatch.setattr(client, function.__name__, namespace[function.__name__])
    if guard == "vehicle":
        retail[0]["campaign"][0].campaign.shopping_setting.use_vehicle_inventory = True
        assert draft()[
            "draft_id"
        ]  # The rejected vehicle fixture is now wrongly admitted.
    elif guard == "graph":
        pending = plan()
        pending.operations[0].operation["create"]["status"] = "PAUSED"
        assert client.validate_listing_filter_plan(pending)
    else:
        pending = plan()
        response, rows = saved(pending)
        response["results"][1], response["results"][2] = (
            response["results"][2],
            response["results"][1],
        )
        retail[0]["asset_group_listing_group_filter"] = rows
        client.verify_listing_filter_result(pending.post_checks, response)
