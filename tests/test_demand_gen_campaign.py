import asyncio
import copy
import json

import pytest

from mcp_google_ads_safe import app, audit, client, rails, tools

CID = "1234567890"
ARGS = {
    "campaign_name": "Demand Gen One",
    "daily_budget": "25",
    "geo_target_ids": ["2840"],
    "language_ids": ["1000"],
    "contains_eu_political_advertising": False,
    "customer_id": CID,
}


@pytest.fixture
def demand_gen(monkeypatch, fake_client):
    monkeypatch.setenv("GOOGLE_ADS_ENABLE_WRITES", "true")
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CID)
    monkeypatch.setenv("GOOGLE_ADS_READ_CUSTOMER_IDS", CID)
    monkeypatch.setenv("GOOGLE_ADS_WRITE_CUSTOMER_IDS", CID)
    state = {
        "account": {"resource_name": f"customers/{CID}", "id": CID,
                    "descriptive_name": "Test", "currency_code": "USD",
                    "time_zone": "America/New_York", "manager": False, "status": "ENABLED"},
        "collisions": [],
        "locations": [{"resource_name": "geoTargetConstants/2840", "name": "United States", "status": "ENABLED"}],
        "languages": [{"resource_name": "languageConstants/1000", "name": "English", "targetable": True}],
    }
    monkeypatch.setattr(client, "demand_gen_creation_state", lambda *a: copy.deepcopy(state))
    return state, fake_client


def draft(**changes):
    return tools.create_demand_gen_campaign(**dict(ARGS, **changes))


def plan_for(**changes):
    return rails.compile(rails.CreateDemandGenCampaignIntent(**dict(
        customer_id=CID, campaign_name="Demand Gen One", daily_budget="25",
        geo_target_ids=["2840"], language_ids=["1000"],
        contains_eu_political_advertising=False, **changes))).plan


def test_minimum_graph_is_closed_and_paused(demand_gen, fake_gads):
    result = draft()
    assert any('not reversible' in warning and 'does not automatically undo creation' in warning
               for warning in result['preview']['warnings'])
    plan = rails._DRAFTS[result["draft_id"]].plan
    assert [op.service for op in plan.operations] == [
        "CampaignBudgetService", "CampaignService", "CampaignCriterionService",
        "CampaignCriterionService"]
    campaign = plan.operations[1].operation["create"]
    assert campaign == {
        "resource_name": f"customers/{CID}/campaigns/-2",
        "name": "Demand Gen One",
        "campaign_budget": f"customers/{CID}/campaignBudgets/-1",
        "status": "PAUSED",
        "advertising_channel_type": "DEMAND_GEN",
        "maximize_conversions": {},
        "demand_gen_campaign_settings": {"upgraded_targeting": False},
        "geo_target_type_setting": {"positive_geo_target_type": "PRESENCE", "negative_geo_target_type": "PRESENCE"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }
    message = client._build_mutate_operation(fake_gads, plan.operations[1], client.validate_demand_gen_plan(plan))
    saved = message.campaign_operation.create
    assert saved.demand_gen_campaign_settings._pb.HasField("upgraded_targeting")
    assert saved.demand_gen_campaign_settings.upgraded_targeting is False
    assert saved._pb.WhichOneof('campaign_bidding_strategy') == 'maximize_conversions'
    assert saved.maximize_conversions._pb.ListFields() == []


@pytest.mark.parametrize(("field", "value"), [
    ("campaign_name", 1), ("geo_target_ids", "[2840]"),
    ("geo_target_ids", [2840]), ("language_ids", [True]),
    ("contains_eu_political_advertising", "false"), ("daily_budget", True),
    ("customer_id", False), ("customer_id", "null"),
])
def test_actual_boundary_refuses_coercion(demand_gen, field, value):
    from tests.test_protocol_errors import boundary, payload
    result = payload(boundary(app.mcp, "create_demand_gen_campaign", dict(ARGS, **{field: value})))
    assert result["code"] == "BAD_INPUT"


def test_boundary_rejects_explicit_null_unknown_and_accepts_omission(demand_gen):
    from tests.test_protocol_errors import boundary, payload
    assert payload(boundary(app.mcp, "create_demand_gen_campaign", dict(ARGS, customer_id=None)))["code"] == "BAD_INPUT"
    assert payload(boundary(app.mcp, "create_demand_gen_campaign", dict(ARGS, surprise=True)))["code"] == "BAD_INPUT"
    omitted = dict(ARGS)
    omitted.pop("customer_id")
    assert not boundary(app.mcp, "create_demand_gen_campaign", omitted).is_error


@pytest.mark.parametrize(("field", "value"), [
    ("campaign_name", ""), ("campaign_name", "x" * 129),
    ("daily_budget", "0"), ("daily_budget", "0.0000001"),
    ("geo_target_ids", []), ("geo_target_ids", ["2840", "2840"]),
    ("geo_target_ids", ["02840"]), ("geo_target_ids", ("2840",)),
    ("language_ids", ["0"]),
])
def test_strict_inputs_refuse_before_dispatch(demand_gen, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})
    assert demand_gen[1].dispatch_calls == []


def test_input_lists_are_copied_and_maximum_is_202(demand_gen):
    geo = [str(i) for i in range(1, 101)]
    lang = [str(i) for i in range(101, 201)]
    result = draft(geo_target_ids=geo, language_ids=lang)
    geo.append("999")
    assert len(rails._DRAFTS[result["draft_id"]].plan.operations) == 202


@pytest.mark.parametrize("change", ["channel", "status", "strategy", "setting", "criterion", "order", "descriptor", "extra"])
def test_tampered_graph_refused_before_client(demand_gen, monkeypatch, change):
    plan = copy.deepcopy(plan_for())
    if change == "channel":
        plan.operations[1].operation["create"]["advertising_channel_type"] = "SEARCH"
    elif change == "status":
        plan.operations[1].operation["create"]["status"] = "ENABLED"
    elif change == "strategy":
        plan.operations[1].operation["create"]["maximize_conversions"] = {"target_cpa_micros": 1}
    elif change == "setting":
        plan.operations[1].operation["create"]["demand_gen_campaign_settings"] = {}
    elif change == "criterion":
        plan.operations[2].operation["create"]["negative"] = True
    elif change == "order":
        plan.operations[2], plan.operations[3] = plan.operations[3], plan.operations[2]
    elif change == "descriptor":
        plan.post_checks[1]["demand_gen_family"]["strategy"] = "MANUAL_CPC"
    else:
        plan.operations.append(copy.deepcopy(plan.operations[-1]))
    monkeypatch.setattr(client, "gads", lambda: pytest.fail("client constructed for invalid graph"))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


def test_default_off_precedes_reads(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_ENABLE_WRITES", raising=False)
    monkeypatch.setattr(client, "demand_gen_creation_state", lambda *a: pytest.fail("read before write gate"))
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == "WRITES_DISABLED"


def test_drift_keeps_draft_unconsumed(demand_gen):
    state, fake = demand_gen
    result = draft()
    state["account"]["currency_code"] = "EUR"
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result["draft_id"])
    assert result["draft_id"] in rails._DRAFTS and fake.dispatch_calls == []


def test_validate_only_never_reads_saved_state(demand_gen, monkeypatch):
    plan = plan_for()
    monkeypatch.setattr(client, "gaql_all", lambda *a: pytest.fail("saved read on validation only"))
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_results(plan.post_checks, {"validate_only": True, "results": []})


def test_actual_tool_is_registered():
    names = {tool.name for tool in asyncio.run(app.mcp.list_tools())}
    assert "create_demand_gen_campaign" in names


def saved_graph(plan):
    from tests.conftest import make_row
    resolved, rows, results = {}, {}, []
    for index, check in enumerate(plan.post_checks):
        entity = check['entity_type']
        expected = copy.deepcopy(check['expected'])
        for field in ('campaign_budget', 'campaign'):
            if field in expected:
                expected[field] = resolved.get(expected[field], expected[field])
        identity = str(100 + index)
        if entity == 'campaign_criterion':
            identity = expected['campaign'].rsplit('/', 1)[1] + '~' + identity
        rn = f"customers/{CID}/{client.PMAX_KINDS[entity]}/{identity}"
        if check['definition']:
            resolved[check['definition']] = rn
        paths = {f'{entity}.resource_name': rn}
        if entity == 'campaign_budget':
            for key, value in expected.items():
                paths[f'{entity}.{key}'] = value
        elif entity == 'campaign':
            expected['id'] = identity
            for key in ('id', 'name', 'status', 'campaign_budget', 'advertising_channel_type',
                        'advertising_channel_sub_type', 'bidding_strategy_type',
                        'bidding_strategy', 'contains_eu_political_advertising',
                        'hotel_property_asset_set'):
                paths[f'{entity}.{key}'] = expected[key]
            for prefix in ('maximize_conversions', 'demand_gen_campaign_settings',
                           'geo_target_type_setting', 'shopping_setting',
                           'travel_campaign_settings', 'hotel_setting'):
                for key, value in expected[prefix].items():
                    paths[f'{entity}.{prefix}.{key}'] = value
        else:
            role = 'location' if 'location' in expected else 'language'
            paths.update({f'{entity}.campaign': expected['campaign'],
                          f'{entity}.type': role.upper(), f'{entity}.status': 'ENABLED',
                          f'{entity}.negative': False})
            field = 'geo_target_constant' if role == 'location' else 'language_constant'
            paths[f'{entity}.{role}.{field}'] = expected[role][field]
        rows[rn] = make_row(**paths)
        results.append({'type': entity + '_result', 'resource_name': rn})
    return {'results': results, 'request_id': None}, rows


def use_raw_saved(monkeypatch, rows):
    def scan(query, cid):
        assert cid == CID and ' LIMIT ' not in query.upper()
        if 'type IN (LOCATION, LANGUAGE)' in query:
            return [copy.deepcopy(row) for rn, row in rows.items()
                    if '/campaignCriteria/' in rn]
        rn = query.split("resource_name = '")[1].split("'")[0]
        return [copy.deepcopy(rows[rn])] if rn in rows else []
    monkeypatch.setattr(client, '_scan_rows', scan)


def test_actual_raw_saved_success_consumes_once(demand_gen, monkeypatch):
    _, fake = demand_gen
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    use_raw_saved(monkeypatch, rows)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is True
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])


@pytest.mark.parametrize(('entity', 'field', 'value'), [
    ('campaign_budget', 'reference_count', 2),
    ('campaign_budget', 'explicitly_shared', True),
    ('campaign', 'advertising_channel_type', 'SEARCH'),
    ('campaign', 'bidding_strategy', f'customers/{CID}/biddingStrategies/9'),
    ('campaign', 'hotel_property_asset_set', f'customers/{CID}/assetSets/9'),
    ('campaign_criterion', 'negative', True),
])
def test_saved_mismatch_is_applied_unverified_and_consumed(demand_gen, monkeypatch, entity, field, value):
    _, fake = demand_gen
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    row = next(row for rn, row in rows.items() if f'/{client.PMAX_KINDS[entity]}/' in rn)
    setattr(getattr(row, entity), field, value)
    use_raw_saved(monkeypatch, rows)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1


def test_missing_explicit_false_and_dict_rows_refuse(demand_gen, monkeypatch):
    plan = plan_for()
    result, rows = saved_graph(plan)
    campaign = next(row.campaign for rn, row in rows.items() if '/campaigns/' in rn)
    campaign._pb.ClearField('demand_gen_campaign_settings')
    use_raw_saved(monkeypatch, rows)
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_results(plan.post_checks, result)
    monkeypatch.setattr(client, '_scan_rows', lambda *a: [{}])
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_results(plan.post_checks, result)


@pytest.mark.parametrize('change', ['kind', 'owner', 'duplicate', 'criterion_parent'])
def test_bad_ordered_results_refuse_before_reads(demand_gen, monkeypatch, change):
    plan = plan_for()
    result, _ = saved_graph(plan)
    if change == 'kind':
        result['results'][0]['type'] = 'campaign_result'
    elif change == 'owner':
        result['results'][0]['resource_name'] = 'customers/999/campaignBudgets/100'
    elif change == 'duplicate':
        result['results'][-1]['resource_name'] = result['results'][-2]['resource_name']
    else:
        result['results'][2]['resource_name'] = f'customers/{CID}/campaignCriteria/999~102'
    monkeypatch.setattr(client, '_scan_rows', lambda *a: pytest.fail('read before result validation'))
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_results(plan.post_checks, result)


def test_atomic_validate_only_dispatch(demand_gen, fake_gads):
    from tests.conftest import make_type
    plan = plan_for()
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan, True)
    request = fake_gads.mutate_calls[0]
    assert result['validate_only'] is True
    assert request['partial_failure'] is False and request['validate_only'] is True
    assert len(request['operations']) == 4


@pytest.mark.parametrize('change', ['missing', 'extra', 'duplicate'])
def test_complete_target_population_mismatch_refuses(demand_gen, monkeypatch, change):
    plan = plan_for()
    result, rows = saved_graph(plan)
    criteria = [rn for rn in rows if '/campaignCriteria/' in rn]
    if change == 'missing':
        rows.pop(criteria[-1])
    elif change == 'duplicate':
        duplicate = copy.deepcopy(rows[criteria[-1]])
        duplicate.campaign_criterion.resource_name = criteria[0]
        rows[f'customers/{CID}/campaignCriteria/101~998'] = duplicate
    else:
        from tests.conftest import make_row
        campaign = result['results'][1]['resource_name']
        rn = f'customers/{CID}/campaignCriteria/{campaign.rsplit("/", 1)[1]}~999'
        rows[rn] = make_row(**{
            'campaign_criterion.resource_name': rn,
            'campaign_criterion.campaign': campaign,
            'campaign_criterion.type': 'LOCATION',
            'campaign_criterion.status': 'ENABLED',
            'campaign_criterion.negative': False,
            'campaign_criterion.location.geo_target_constant': 'geoTargetConstants/999'})
    use_raw_saved(monkeypatch, rows)
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_results(plan.post_checks, result)


@pytest.fixture
def population(monkeypatch):
    account = {'rows': [{'customer': {
        'id': CID, 'descriptive_name': 'Test account', 'currency_code': 'USD',
        'time_zone': 'America/New_York', 'manager': False, 'status': 'ENABLED'}}],
        'pages_complete': True, 'returned_count': 1, 'total_results_count': 1}
    data = {
        'campaign': [{'campaign': {'resource_name': f'customers/{CID}/campaigns/8',
                                   'name': 'Other', 'status': 'PAUSED'}}],
        'campaign_budget': [{'campaign_budget': {
            'resource_name': f'customers/{CID}/campaignBudgets/9',
            'name': 'Other budget', 'status': 'ENABLED'}}],
        'geo_target_constant': [{'geo_target_constant': {
            'resource_name': 'geoTargetConstants/2840', 'name': 'United States',
            'status': 'ENABLED'}}],
        'language_constant': [{'language_constant': {
            'resource_name': 'languageConstants/1000', 'name': 'English',
            'targetable': True}}],
    }
    calls = []
    monkeypatch.setattr(client, 'demand_gen_account_state', lambda cid: {
        'resource_name': f'customers/{CID}', 'id': CID,
        **copy.deepcopy(account['rows'][0]['customer'])})
    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        calls.append(query)
        return copy.deepcopy(data[query.split(' FROM ')[1].split()[0]])
    monkeypatch.setattr(client, 'gaql_all', read)
    return account, data, calls


def test_real_safety_readers_are_complete_and_retain_account(population):
    _, _, calls = population
    result = draft()
    assert len(calls) == 4
    assert result['preview']['account']['descriptive_name'] == 'Test account'
    assert all('LIMIT' not in query for query in calls)


@pytest.mark.parametrize(('target', 'field', 'value'), [
    ('campaign', 'resource_name', 'customers/999/campaigns/8'),
    ('campaign_budget', 'status', 'PAUSED'),
    ('geo_target_constant', 'status', 'REMOVED'),
    ('language_constant', 'targetable', False),
])
def test_real_safety_readers_refuse_unowned_or_ineligible(population, target, field, value):
    account, data, _ = population
    item = data[target][0][target]
    item[field] = value
    with pytest.raises(rails.RailViolation):
        draft()


def test_name_collision_and_incomplete_scan_refuse(population):
    _, data, _ = population
    data['campaign'][0]['campaign']['name'] = ARGS['campaign_name']
    with pytest.raises(rails.RailViolation):
        draft()


def test_selected_graph_guard_disabled_sensitivity(demand_gen, monkeypatch):
    import inspect
    plan = plan_for()
    campaign = plan.operations[1].operation['create']
    campaign['advertising_channel_type'] = 'SEARCH'
    family = plan.post_checks[0]['demand_gen_family']
    object.__setattr__(plan, 'post_checks', client.demand_gen_checks(CID, plan.operations, family))
    source = inspect.getsource(client.validate_demand_gen_plan)
    before = "or campaign['advertising_channel_type'] != 'DEMAND_GEN'"
    assert source.count(before) == 1
    namespace = dict(client.__dict__)
    exec(source.replace(before, 'or False'), namespace)
    monkeypatch.setattr(client, 'validate_demand_gen_plan', namespace['validate_demand_gen_plan'])
    assert client.validate_demand_gen_plan(plan)


def test_ambiguous_transport_consumes_and_never_redispatches(demand_gen):
    from tests.test_final_fixes import mapped_error
    _, fake = demand_gen
    pending = draft()
    fake.dispatch_error = mapped_error('remapped')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(pending['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_dedicated_raw_account_reader_is_complete_and_owner_exact(monkeypatch):
    from tests.conftest import make_row
    rn = f'customers/{CID}'
    row = make_row(**{
        'customer.resource_name': rn, 'customer.id': int(CID),
        'customer.descriptive_name': 'Test account', 'customer.currency_code': 'USD',
        'customer.time_zone': 'America/New_York', 'customer.manager': False,
        'customer.status': 'ENABLED'})
    queries = []
    monkeypatch.setattr(client, '_scan_rows', lambda query, cid: queries.append((query, cid)) or [row])
    assert client.demand_gen_account_state(CID) == {
        'resource_name': rn, 'id': CID, 'descriptive_name': 'Test account',
        'currency_code': 'USD', 'time_zone': 'America/New_York',
        'manager': False, 'status': 'ENABLED'}
    assert len(queries) == 1 and queries[0][1] == CID
    assert 'customer.resource_name' in queries[0][0] and 'LIMIT' not in queries[0][0]


@pytest.mark.parametrize('resource_name', [None, 'customers/999'])
def test_dedicated_raw_account_reader_refuses_missing_or_foreign_identity(monkeypatch, resource_name):
    from tests.conftest import make_row
    paths = {
        'customer.id': int(CID), 'customer.descriptive_name': 'Test account',
        'customer.currency_code': 'USD', 'customer.time_zone': 'America/New_York',
        'customer.manager': False, 'customer.status': 'ENABLED'}
    if resource_name is not None:
        paths['customer.resource_name'] = resource_name
    row = make_row(**paths)
    monkeypatch.setattr(client, '_scan_rows', lambda *a: [row])
    with pytest.raises(rails.RailViolation):
        client.demand_gen_account_state(CID)


@pytest.mark.parametrize(('field', 'value'), [
    ('manager', True),
    ('descriptive_name', ''),
    ('status', 'CANCELED'),
])
def test_dedicated_raw_account_reader_refuses_ineligible_account(monkeypatch, field, value):
    from tests.conftest import make_row
    paths = {
        'customer.resource_name': f'customers/{CID}', 'customer.id': int(CID),
        'customer.descriptive_name': 'Test account', 'customer.currency_code': 'USD',
        'customer.time_zone': 'America/New_York', 'customer.manager': False,
        'customer.status': 'ENABLED'}
    paths[f'customer.{field}'] = value
    monkeypatch.setattr(client, '_scan_rows', lambda *a: [make_row(**paths)])
    with pytest.raises(rails.RailViolation):
        client.demand_gen_account_state(CID)


def test_dedicated_account_scan_refuses_incomplete_total(fake_gads):
    from tests.conftest import make_search_response
    fake_gads.search_responses[(CID, '')] = make_search_response([], total=1)
    with pytest.raises(rails.RailViolation) as exc:
        client.demand_gen_account_state(CID)
    assert exc.value.code == 'SCAN_INCOMPLETE'


@pytest.mark.parametrize('malformation', ['negative', 'wrong_parent', 'wrong_type'])
def test_population_validates_the_actual_scan_rows(demand_gen, monkeypatch, malformation):
    plan = plan_for()
    result, rows = saved_graph(plan)
    def scan(query, cid):
        if 'type IN (LOCATION, LANGUAGE)' in query:
            population = [copy.deepcopy(row) for rn, row in rows.items()
                          if '/campaignCriteria/' in rn]
            target = population[0].campaign_criterion
            if malformation == 'negative':
                target.negative = True
            elif malformation == 'wrong_parent':
                target.campaign = f'customers/{CID}/campaigns/999'
            else:
                target.type_ = 'LANGUAGE'
            return population
        rn = query.split("resource_name = '")[1].split("'")[0]
        return [copy.deepcopy(rows[rn])]
    monkeypatch.setattr(client, '_scan_rows', scan)
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_results(plan.post_checks, result)


def _set_saved(rows, entity, path, value):
    candidates = (row for rn, row in rows.items()
                  if f'/{client.PMAX_KINDS[entity]}/' in rn)
    if path == 'language.language_constant':
        row = next(row for row in candidates
                   if row.campaign_criterion.type_.name == 'LANGUAGE')
    else:
        row = next(candidates)
    target = getattr(row, entity)
    for part in path.split('.')[:-1]:
        target = getattr(target, part)
    field = path.split('.')[-1]
    if field == 'advertising_partner_ids':
        getattr(target, field).append(value)
    else:
        setattr(target, field, value)


@pytest.mark.parametrize(('entity', 'path', 'value'), [
    ('campaign_budget', 'resource_name', f'customers/{CID}/campaignBudgets/999'),
    ('campaign_budget', 'name', 'Wrong'), ('campaign_budget', 'status', 'REMOVED'),
    ('campaign_budget', 'amount_micros', 1), ('campaign_budget', 'explicitly_shared', True),
    ('campaign_budget', 'period', 'CUSTOM_PERIOD'),
    ('campaign_budget', 'delivery_method', 'ACCELERATED'),
    ('campaign_budget', 'total_amount_micros', 1),
    ('campaign_budget', 'aligned_bidding_strategy_id', 1),
    ('campaign_budget', 'reference_count', 2),
    ('campaign', 'resource_name', f'customers/{CID}/campaigns/999'),
    ('campaign', 'id', 999), ('campaign', 'name', 'Wrong'),
    ('campaign', 'status', 'ENABLED'),
    ('campaign', 'campaign_budget', f'customers/{CID}/campaignBudgets/999'),
    ('campaign', 'advertising_channel_type', 'SEARCH'),
    ('campaign', 'advertising_channel_sub_type', 'VIDEO_ACTION'),
    ('campaign', 'bidding_strategy_type', 'TARGET_CPA'),
    ('campaign', 'bidding_strategy', f'customers/{CID}/biddingStrategies/9'),
    ('campaign', 'maximize_conversions.target_cpa_micros', 1),
    ('campaign', 'maximize_conversions.cpc_bid_ceiling_micros', 1),
    ('campaign', 'maximize_conversions.cpc_bid_floor_micros', 1),
    ('campaign', 'demand_gen_campaign_settings.upgraded_targeting', True),
    ('campaign', 'geo_target_type_setting.positive_geo_target_type', 'PRESENCE_OR_INTEREST'),
    ('campaign', 'geo_target_type_setting.negative_geo_target_type', 'PRESENCE_OR_INTEREST'),
    ('campaign', 'contains_eu_political_advertising', 'CONTAINS_EU_POLITICAL_ADVERTISING'),
    ('campaign', 'shopping_setting.merchant_id', 1),
    ('campaign', 'shopping_setting.feed_label', 'feed'),
    ('campaign', 'shopping_setting.advertising_partner_ids', 1),
    ('campaign', 'shopping_setting.use_vehicle_inventory', True),
    ('campaign', 'travel_campaign_settings.travel_account_id', 1),
    ('campaign', 'hotel_setting.hotel_center_id', 1),
    ('campaign', 'hotel_property_asset_set', f'customers/{CID}/assetSets/9'),
    ('campaign_criterion', 'resource_name', f'customers/{CID}/campaignCriteria/999~999'),
    ('campaign_criterion', 'campaign', f'customers/{CID}/campaigns/999'),
    ('campaign_criterion', 'type_', 'LANGUAGE'),
    ('campaign_criterion', 'status', 'REMOVED'),
    ('campaign_criterion', 'negative', True),
    ('campaign_criterion', 'location.geo_target_constant', 'geoTargetConstants/999'),
    ('campaign_criterion', 'language.language_constant', 'languageConstants/999'),
])
def test_every_saved_proof_field_mismatch_consumes_once(
        demand_gen, monkeypatch, entity, path, value):
    _, fake = demand_gen
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    _set_saved(rows, entity, path, value)
    use_raw_saved(monkeypatch, rows)
    outcome = rails.apply_draft(pending['draft_id'])
    assert outcome['applied'] is True and outcome['verified'] is False
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('entity', ['campaign_budget', 'campaign', 'campaign_criterion'])
def test_missing_saved_record_consumes_once(demand_gen, monkeypatch, entity):
    _, fake = demand_gen
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    rn = next(rn for rn in rows if f'/{client.PMAX_KINDS[entity]}/' in rn)
    rows.pop(rn)
    use_raw_saved(monkeypatch, rows)
    outcome = rails.apply_draft(pending['draft_id'])
    assert outcome['applied'] is True and outcome['verified'] is False
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('field', ['account', 'collisions', 'locations', 'languages'])
def test_confirmation_drift_categories_remain_unconsumed(demand_gen, field):
    state, fake = demand_gen
    pending = draft()
    if field == 'account':
        state[field]['time_zone'] = 'UTC'
    else:
        state[field].append({'changed': True})
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize('gate', ['writes', 'read', 'write', 'cap'])
def test_confirmation_rechecks_gates_and_current_budget_cap(demand_gen, monkeypatch, gate):
    _, fake = demand_gen
    pending = draft()
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'read':
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    elif gate == 'write':
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_MAX_DAILY_BUDGET', '24')
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize(('field', 'value'), [
    ('daily_budget', '1000.000001'),
    ('geo_target_ids', [str(i) for i in range(1, 102)]),
    ('language_ids', [str(i) for i in range(1, 102)]),
])
def test_current_caps_refuse_at_draft(demand_gen, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


@pytest.mark.parametrize('change', ['missing_family', 'budget_parent', 'criterion_parent', 'no_context'])
def test_family_parent_and_context_tampering_refuses_before_client(
        demand_gen, fake_gads, monkeypatch, change):
    plan = copy.deepcopy(plan_for())
    if change == 'missing_family':
        plan.post_checks[0].pop('demand_gen_family')
    elif change == 'budget_parent':
        plan.operations[1].operation['create']['campaign_budget'] = f'customers/{CID}/campaignBudgets/99'
        family = plan.post_checks[0]['demand_gen_family']
        object.__setattr__(plan, 'post_checks', client.demand_gen_checks(CID, plan.operations, family))
    elif change == 'criterion_parent':
        plan.operations[2].operation['create']['campaign'] = f'customers/{CID}/campaigns/99'
        family = plan.post_checks[0]['demand_gen_family']
        object.__setattr__(plan, 'post_checks', client.demand_gen_checks(CID, plan.operations, family))
    else:
        with pytest.raises(rails.RailViolation):
            client._build_mutate_operation(fake_gads, plan.operations[1], None)
        return
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('client constructed for invalid graph'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


def _audit_phases():
    with open(audit.AUDIT_PATH, encoding='utf-8') as handle:
        return [json.loads(line)['phase'] for line in handle]


def test_demand_gen_audit_refusal_success_and_error(demand_gen, monkeypatch):
    _, fake = demand_gen
    with pytest.raises(rails.RailViolation):
        draft(campaign_name='')
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    use_raw_saved(monkeypatch, rows)
    assert rails.apply_draft(pending['draft_id'])['verified'] is True
    pending = draft()
    fake.dispatch_error = RuntimeError('local dispatcher failure')
    with pytest.raises(RuntimeError):
        rails.apply_draft(pending['draft_id'])
    phases = _audit_phases()
    assert 'refused' in phases and 'draft' in phases and 'apply' in phases and 'error' in phases


def test_demand_gen_audit_unknown(demand_gen):
    from tests.test_final_fixes import mapped_error
    _, fake = demand_gen
    pending = draft()
    fake.dispatch_error = mapped_error('remapped')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(pending['draft_id'])
    assert 'unknown' in _audit_phases()
