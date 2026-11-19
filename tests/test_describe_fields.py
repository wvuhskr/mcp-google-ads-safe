"""Field catalog: which resources exist, and which fields, metrics and segments each takes."""
import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import make_type


@pytest.fixture
def catalog(monkeypatch):
    queries = []

    def rows(query):
        queries.append(query)
        if "name = 'campaign'" in query:
            return [{"name": "campaign", "category": "RESOURCE",
                     "metrics": ["metrics.clicks", "metrics.cost_micros"],
                     "segments": ["segments.date", "segments.device"],
                     "attribute_resources": ["customer", "campaign_budget"]}]
        if "name LIKE 'campaign.%'" in query:
            return [
                {"name": "campaign.name", "category": "ATTRIBUTE", "data_type": "STRING",
                 "selectable": True, "filterable": True, "sortable": True, "is_repeated": False},
                {"name": "campaign.status", "category": "ATTRIBUTE", "data_type": "ENUM",
                 "selectable": True, "filterable": True, "sortable": True, "is_repeated": False},
                {"name": "campaign.internal_only", "category": "ATTRIBUTE", "data_type": "STRING",
                 "selectable": False, "filterable": False, "sortable": False, "is_repeated": False},
            ]
        return []

    monkeypatch.setattr(client, "_field_rows", rows)
    monkeypatch.setattr(client, "_FIELD_CACHE", {})
    return queries


def test_no_resource_lists_common_report_resources_without_asking_google(catalog):
    out = tools.describe_fields()
    names = {r["resource"] for r in out["resources"]}
    assert {"campaign", "ad_group", "ad_group_ad", "keyword_view", "search_term_view",
            "geographic_view", "asset_group", "customer"} <= names
    assert all(r["description"] for r in out["resources"])
    assert catalog == []


def test_resource_lists_selectable_attributes_and_counts(catalog):
    out = tools.describe_fields("campaign")
    assert out["resource"] == "campaign"
    assert out["attributes"] == [
        {"name": "campaign.name", "data_type": "STRING", "filterable": True, "sortable": True},
        {"name": "campaign.status", "data_type": "ENUM", "filterable": True, "sortable": True}]
    assert out["metrics_count"] == 2 and out["segments_count"] == 2
    assert "metrics" not in out and "category='metrics'" in out["next"]
    assert out["related_resources"] == ["campaign_budget", "customer"]


def test_category_metrics_and_segments(catalog):
    assert tools.describe_fields("campaign", "metrics")["metrics"] == [
        "metrics.clicks", "metrics.cost_micros"]
    assert tools.describe_fields("campaign", "segments")["segments"] == [
        "segments.date", "segments.device"]
    assert "attributes" not in tools.describe_fields("campaign", "metrics")


def test_name_contains_narrows_any_list(catalog):
    assert tools.describe_fields("campaign", "metrics", "COST")["metrics"] == ["metrics.cost_micros"]
    assert [a["name"] for a in tools.describe_fields("campaign", name_contains="stat")["attributes"]] == [
        "campaign.status"]


def test_catalog_is_cached_per_resource(catalog):
    tools.describe_fields("campaign")
    tools.describe_fields("campaign", "metrics")
    assert len(catalog) == 2  # one resource row + one attribute list, asked once


@pytest.mark.parametrize("resource", ["Campaign", "campaign'; --", "campaign.name", "", "a" * 80])
def test_bad_resource_name_is_refused_before_any_request(catalog, resource):
    with pytest.raises(rails.RailViolation) as exc:
        client.describe_fields(resource)
    assert exc.value.code == "BAD_INPUT" and catalog == []


def test_unknown_resource_and_category_are_refused(catalog):
    with pytest.raises(rails.RailViolation) as exc:
        tools.describe_fields("no_such_thing")
    assert exc.value.code == "UNKNOWN_RESOURCE"
    with pytest.raises(rails.RailViolation) as exc:
        tools.describe_fields("campaign", "everything")
    assert exc.value.code == "BAD_INPUT"


def test_field_rows_reads_real_field_messages(fake_gads):
    field = make_type("GoogleAdsField")
    field.name = "campaign.status"
    field.category = "ATTRIBUTE"
    field.data_type = "ENUM"
    field.selectable = True
    fake_gads.field_responses = [field]
    (row,) = client._field_rows("SELECT name WHERE name = 'campaign.status'")
    assert row["name"] == "campaign.status" and row["category"] == "ATTRIBUTE"
    assert row["data_type"] == "ENUM" and row["selectable"] is True
    assert fake_gads.field_requests[0].query == "SELECT name WHERE name = 'campaign.status'"
