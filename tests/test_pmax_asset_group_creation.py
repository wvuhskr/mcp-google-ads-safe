"""Offline proof for adding one paused asset group to an existing PMax campaign."""
import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, settings, tools

CID, CAMPAIGN_ID = '1234567890', '88'
ARGS = dict(campaign_id=CAMPAIGN_ID, asset_group_name='Cooling Services',
            headlines=['Cool Today', 'Local Cooling', 'Book Service'],
            long_headlines=['Cooling service for your home'],
            descriptions=['Book cooling service today', 'Your local cooling specialists'],
            final_url='https://example.com/service', landscape_image_asset_id='70',
            square_image_asset_id='71', customer_id=CID)


def image_state(identity, width, height):
    return {'resource_name': f'customers/{CID}/assets/{identity}', 'type_': 'IMAGE',
            'image_asset': {'mime_type': 'IMAGE_JPEG', 'file_size': 5000000,
                            'full_size': {'width_pixels': width, 'height_pixels': height}}}


def parent_proof():
    campaign = f'customers/{CID}/campaigns/{CAMPAIGN_ID}'
    return {'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York',
                        'status': 'ENABLED', 'manager': False},
            'parent': {'resource_name': campaign, 'id': CAMPAIGN_ID, 'name': 'Existing PMax',
                       'status': 'PAUSED', 'advertising_channel_type': 'PERFORMANCE_MAX',
                       'advertising_channel_sub_type': 'UNSPECIFIED',
                       'bidding_strategy_type': 'MAXIMIZE_CONVERSIONS', 'bidding_strategy': '',
                       'maximize_conversions': {'target_cpa_micros': 10000000},
                       'brand_guidelines_enabled': True,
                       'asset_automation_settings': [
                           {'asset_automation_type': kind, 'asset_automation_status': 'OPTED_OUT'}
                           for kind in client.PMAX_AUTOMATIONS],
                       'contains_eu_political_advertising': 'DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING',
                       'geo_target_type_setting': dict(client.SEARCH_GEO_OPTIONS),
                       'retail_travel_settings_absent': True},
            'branding': [
                {'resource_name': f'customers/{CID}/campaignAssets/{CAMPAIGN_ID}~72~18',
                 'campaign': campaign, 'asset': f'customers/{CID}/assets/72',
                 'field_type': 'BUSINESS_NAME', 'status': 'PAUSED', 'content': 'Cooling Co'},
                {'resource_name': f'customers/{CID}/campaignAssets/{CAMPAIGN_ID}~73~21',
                 'campaign': campaign, 'asset': f'customers/{CID}/assets/73',
                 'field_type': 'LOGO', 'status': 'ENABLED', 'content': image_state('73', 128, 128)}],
            'images': {'MARKETING_IMAGE': image_state('70', 600, 314),
                       'SQUARE_MARKETING_IMAGE': image_state('71', 300, 300)}}


@pytest.fixture
def asset_group(monkeypatch, fake_client):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    proof = parent_proof()
    state = {'parent_proof': proof, 'asset_groups': []}
    monkeypatch.setattr(client, 'pmax_asset_group_state', lambda *a: copy.deepcopy(state))
    monkeypatch.setattr(client, 'pmax_asset_group_parent_proof', lambda *a: copy.deepcopy(proof))
    return state, fake_client


def draft(**changes):
    return tools.create_asset_group(**dict(ARGS, **changes))


def plan_for(**changes):
    return rails._DRAFTS[draft(**changes)['draft_id']].plan


def saved_graph(plan):
    resolved, rows, results = {}, {}, []
    for index, check in enumerate(plan.post_checks):
        entity, state = check['entity_type'], copy.deepcopy(check['expected'])
        for key in ('campaign', 'asset_group', 'asset'):
            if key in state:
                state[key] = resolved.get(state[key], state[key])
        identity = str(100 + index)
        if entity == 'asset_group_asset':
            identity = '~'.join((state['asset_group'].rsplit('/', 1)[1],
                                 state['asset'].rsplit('/', 1)[1],
                                 client.PMAX_FIELD_NUMBERS[state['field_type']]))
        rn = f'customers/{CID}/{client.PMAX_KINDS[entity]}/{identity}'
        if check['definition']:
            resolved[check['definition']] = rn
        state['resource_name'] = rn
        rows[rn] = state
        results.append({'type': entity + '_result', 'resource_name': rn})
    return {'results': results, 'request_id': None}, rows


def test_closed_serialized_graph_has_only_paused_group_text_and_links(asset_group, fake_gads):
    plan = plan_for()
    context = client.validate_mutation_plan(plan)
    built = [client._build_mutate_operation(fake_gads, op, context) for op in plan.operations]
    assert {op.service for op in plan.operations} == {
        'AssetGroupService', 'AssetService', 'AssetGroupAssetService'}
    assert built[0].asset_group_operation.create.status.name == 'PAUSED'
    assert built[0].asset_group_operation.create.campaign == f'customers/{CID}/campaigns/{CAMPAIGN_ID}'
    links = [built[index].asset_group_asset_operation.create
             for index, op in enumerate(plan.operations) if op.service == 'AssetGroupAssetService']
    assert len(links) == 8 and all(link.status.name == 'PAUSED' for link in links)


def test_actual_atomic_validate_only_request(asset_group, fake_gads):
    from tests.conftest import make_type
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan_for(), True)
    assert result['validate_only'] is True and len(fake_gads.mutate_calls) == 1
    sent = fake_gads.mutate_calls[0]
    assert sent['customer_id'] == CID and sent['partial_failure'] is False
    assert sent['validate_only'] is True


def test_shared_text_uses_one_definition_for_multiple_roles(asset_group):
    plan = plan_for(headlines=['One', 'Two', 'Three'], long_headlines=['One'],
                    descriptions=['One', 'Two'])
    assert len([op for op in plan.operations if op.service == 'AssetService']) == 3
    assert len([op for op in plan.operations if op.service == 'AssetGroupAssetService']) == 8


@pytest.mark.parametrize(('field', 'value'), [
    ('customer_id', 123), ('campaign_id', '088'),
    ('campaign_id', True), ('headlines', '["One","Two","Three"]'),
    ('headlines', ['One', 'Two', 3]), ('asset_group_name', ''),
    ('landscape_image_asset_id', 70)])
def test_direct_input_strictness_precedes_reads(monkeypatch, field, value):
    monkeypatch.setattr(client, 'pmax_asset_group_state', lambda *a: pytest.fail('read reached'))
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


def test_actual_mcp_boundary_rejects_coerced_originals(asset_group):
    from tests.test_protocol_errors import boundary, payload
    for field, value in [('campaign_id', 88), ('headlines', '["One"]'),
                         ('headlines', ['One', 2, 'Three']), ('customer_id', 'null')]:
        assert payload(boundary(app.mcp, 'create_asset_group', dict(ARGS, **{field: value})))['code'] == 'BAD_INPUT'
    assert not boundary(app.mcp, 'create_asset_group', ARGS).is_error


def test_default_off_refuses_direct_and_actual_mcp_before_reads_or_provider(monkeypatch):
    from tests.test_protocol_errors import boundary, payload
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    monkeypatch.setattr(client, 'pmax_asset_group_state', lambda *a: pytest.fail('safety read reached'))
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider construction reached'))
    for call in (lambda: draft(),
                 lambda: payload(boundary(app.mcp, 'create_asset_group', ARGS))):
        try:
            result = call()
        except rails.RailViolation as exc:
            assert exc.code == 'WRITES_DISABLED'
        else:
            assert result['code'] == 'WRITES_DISABLED'


@pytest.mark.parametrize('gate', ['writes', 'write', 'read'])
def test_all_gates_repeat_before_confirm(asset_group, monkeypatch, gate):
    _, fake = asset_group
    pending = draft()
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'write':
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_asset_group_state', lambda *a: pytest.fail('gate must precede read'))
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize('part', ['parent_proof', 'asset_groups'])
def test_confirmation_drift_refuses_without_consuming(asset_group, part):
    state, fake = asset_group
    pending = draft()
    if part == 'parent_proof':
        state[part]['account']['currency_code'] = 'EUR'
    else:
        state[part] = [{'changed': True}]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert pending['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize('damage', ['service', 'field', 'status', 'parent', 'descriptor',
                                     'proof', 'image_proof'])
def test_closed_graph_tampering_refused_before_provider(asset_group, fake_gads, damage):
    plan = copy.deepcopy(plan_for())
    if damage == 'service':
        object.__setattr__(plan.operations[0], 'service', 'CampaignService')
    elif damage == 'field':
        plan.operations[0].operation['create']['path1'] = 'hidden'
    elif damage == 'status':
        plan.operations[-1].operation['create']['status'] = 'ENABLED'
    elif damage == 'parent':
        plan.operations[0].operation['create']['campaign'] = f'customers/{CID}/campaigns/99'
    elif damage == 'descriptor':
        plan.post_checks[-1]['expected']['field_type'] = 'LOGO'
    elif damage == 'image_proof':
        plan.post_checks[0]['images']['MARKETING_IMAGE']['image_asset']['file_size'] = 1
    else:
        plan.post_checks[0]['parent_proof']['parent']['brand_guidelines_enabled'] = False
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert fake_gads.mutate_calls == []


@pytest.mark.parametrize('damage', ['kind', 'owner', 'relationship'])
def test_ordered_result_identity_refused_before_saved_reads(asset_group, monkeypatch, damage):
    plan = plan_for()
    result, _ = saved_graph(plan)
    if damage == 'kind':
        result['results'][0]['type'] = 'campaign_result'
    elif damage == 'owner':
        result['results'][0]['resource_name'] = 'customers/999/assetGroups/100'
    else:
        link = next(item for item in result['results'] if item['type'] == 'asset_group_asset_result')
        link['resource_name'] = link['resource_name'].replace('/100~', '/999~')
    monkeypatch.setattr(client, 'pmax_created_state', lambda *a: pytest.fail('saved read reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


def test_exact_saved_success_and_parent_recheck(asset_group, monkeypatch):
    _, fake = asset_group
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    monkeypatch.setattr(client, 'pmax_created_state', lambda cid, entity, rn: copy.deepcopy(rows[rn]))
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is True
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1


def test_saved_mismatch_consumes_without_retry(asset_group, monkeypatch):
    _, fake = asset_group
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    rows[next(iter(rows))]['status'] = 'ENABLED'
    monkeypatch.setattr(client, 'pmax_created_state', lambda cid, entity, rn: copy.deepcopy(rows[rn]))
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', ['text_content', 'link_role', 'link_status', 'link_parent'])
def test_saved_text_and_link_mismatch_consumes_without_retry(asset_group, monkeypatch, damage):
    _, fake = asset_group
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    if damage == 'text_content':
        row = next(item for item in rows.values() if 'text_asset' in item)
        row['text_asset']['text'] = 'Different text'
    else:
        row = next(item for item in rows.values() if 'field_type' in item)
        if damage == 'link_role':
            row['field_type'] = 'LOGO'
        elif damage == 'link_status':
            row['status'] = 'ENABLED'
        else:
            row['asset_group'] = f'customers/{CID}/assetGroups/999'
    monkeypatch.setattr(client, 'pmax_created_state', lambda cid, entity, rn: copy.deepcopy(rows[rn]))
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


@pytest.mark.parametrize('damage', ['parent', 'branding', 'images'])
def test_postwrite_parent_proof_drift_consumes_without_retry(asset_group, monkeypatch, damage):
    state, fake = asset_group
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    fresh = copy.deepcopy(state['parent_proof'])
    if damage == 'parent':
        fresh['parent']['name'] = 'Changed Parent'
    elif damage == 'branding':
        fresh['branding'][0]['content'] = 'Changed Brand'
    else:
        fresh['images']['MARKETING_IMAGE']['image_asset']['file_size'] = 4999999
    monkeypatch.setattr(client, 'pmax_created_state', lambda cid, entity, rn: copy.deepcopy(rows[rn]))
    monkeypatch.setattr(client, 'pmax_asset_group_parent_proof', lambda *a: copy.deepcopy(fresh))
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_transport_ambiguity_consumes_without_retry(asset_group):
    from tests.test_final_fixes import mapped_error
    _, fake = asset_group
    pending = draft()
    fake.dispatch_error = mapped_error('remapped')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(pending['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_validation_only_cannot_claim_saved_creation(asset_group, monkeypatch):
    plan = plan_for()
    monkeypatch.setattr(client, 'pmax_created_state', lambda *a: pytest.fail('saved read reached'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, {'validate_only': True, 'results': []})


def _raw_reader_rows(setting=None):
    from tests.conftest import make_type
    campaign_rn = f'customers/{CID}/campaigns/{CAMPAIGN_ID}'
    campaign = make_type('GoogleAdsRow')
    campaign.campaign = {'resource_name': campaign_rn, 'id': CAMPAIGN_ID, 'name': 'Existing PMax',
        'status': 'PAUSED', 'advertising_channel_type': 'PERFORMANCE_MAX',
        'bidding_strategy_type': 'MAXIMIZE_CONVERSIONS',
        'maximize_conversions': {'target_cpa_micros': 10000000}, 'brand_guidelines_enabled': True,
        'contains_eu_political_advertising': 'DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING',
        'geo_target_type_setting': dict(client.SEARCH_GEO_OPTIONS),
        'asset_automation_settings': [{'asset_automation_type': kind, 'asset_automation_status': 'OPTED_OUT'}
                                      for kind in client.PMAX_AUTOMATIONS]}
    if setting == 'shopping':
        campaign.campaign.shopping_setting.merchant_id = 123
    elif setting == 'travel':
        campaign.campaign.travel_campaign_settings.travel_account_id = 123
    elif setting == 'hotel':
        campaign.campaign.hotel_setting.hotel_center_id = 123
    elif setting == 'hotel_property':
        campaign.campaign.hotel_property_asset_set = f'customers/{CID}/assetSets/123'
    brands = []
    for asset_id, role, status, asset in [
        ('72', 'BUSINESS_NAME', 'PAUSED', {'resource_name': f'customers/{CID}/assets/72',
                                           'type_': 'TEXT', 'text_asset': {'text': 'Cooling Co'}}),
        ('73', 'LOGO', 'ENABLED', image_state('73', 128, 128))]:
        row = make_type('GoogleAdsRow')
        row.campaign_asset = {'resource_name': f'customers/{CID}/campaignAssets/{CAMPAIGN_ID}~{asset_id}~{client.PMAX_FIELD_NUMBERS[role]}',
                              'campaign': campaign_rn, 'asset': f'customers/{CID}/assets/{asset_id}',
                              'field_type': role, 'status': status}
        row.asset = asset
        brands.append(row)
    return campaign, brands


def _reader_call(monkeypatch, campaign, brands, groups=()):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, '_creation_account', lambda *a, **kw: parent_proof()['account'])
    monkeypatch.setattr(client, '_scan_rows', lambda query, cid: brands if 'FROM campaign_asset' in query
                        else list(groups) if 'FROM asset_group' in query else [campaign])
    monkeypatch.setattr(client, 'pmax_image_state',
                        lambda cid, role, rn: parent_proof()['images'][role])
    return lambda: client.pmax_asset_group_state(CID, CAMPAIGN_ID, 'New Group', {
        role: item['resource_name'] for role, item in parent_proof()['images'].items()})


@pytest.mark.parametrize(('setting', 'accepted'), [
    (None, True), ('shopping', False), ('travel', False), ('hotel', False),
    ('hotel_property', False)])
def test_populated_v25_parent_presence_is_required(monkeypatch, setting, accepted):
    campaign, brands = _raw_reader_rows(setting)
    call = _reader_call(monkeypatch, campaign, brands)
    if accepted:
        assert call()['asset_groups'] == []
    else:
        with pytest.raises(rails.RailViolation, match='retail, vehicle, travel and hotel'):
            call()


@pytest.mark.parametrize('damage', ['enabled_parent', 'search_parent', 'zero_target',
                                     'missing_enum', 'unknown_enum', 'portfolio', 'brand_off',
                                     'automation_missing', 'automation_unknown', 'political_unknown',
                                     'geo_wrong', 'missing_brand', 'duplicate_brand', 'foreign_brand',
                                     'brand_type', 'brand_status', 'brand_content', 'brand_identity'])
def test_populated_parent_and_brand_reader_fail_closed(monkeypatch, damage):
    campaign, brands = _raw_reader_rows()
    if damage == 'enabled_parent':
        campaign.campaign.status = 'ENABLED'
    elif damage == 'search_parent':
        campaign.campaign.advertising_channel_type = 'SEARCH'
    elif damage == 'zero_target':
        campaign.campaign.maximize_conversions.target_cpa_micros = 0
    elif damage == 'missing_enum':
        campaign.campaign._pb.ClearField('status')
    elif damage == 'unknown_enum':
        campaign.campaign.status = 'UNKNOWN'
    elif damage == 'portfolio':
        campaign.campaign.bidding_strategy = f'customers/{CID}/biddingStrategies/5'
    elif damage == 'brand_off':
        campaign.campaign.brand_guidelines_enabled = False
    elif damage == 'automation_missing':
        campaign.campaign.asset_automation_settings.pop()
    elif damage == 'automation_unknown':
        campaign.campaign.asset_automation_settings[0].asset_automation_status = 'UNKNOWN'
    elif damage == 'political_unknown':
        campaign.campaign.contains_eu_political_advertising = 'UNKNOWN'
    elif damage == 'geo_wrong':
        campaign.campaign.geo_target_type_setting.negative_geo_target_type = 'PRESENCE_OR_INTEREST'
    elif damage == 'missing_brand':
        brands.pop()
    elif damage == 'duplicate_brand':
        brands.append(copy.deepcopy(brands[0]))
    elif damage == 'foreign_brand':
        brands[0].campaign_asset.asset = 'customers/999/assets/72'
    elif damage == 'brand_type':
        brands[0].asset.type_ = 'IMAGE'
    elif damage == 'brand_status':
        brands[0].campaign_asset.status = 'UNKNOWN'
    elif damage == 'brand_content':
        brands[0].asset.text_asset.text = ' bad '
    else:
        brands[0].campaign_asset.resource_name = (
            f'customers/{CID}/campaignAssets/{CAMPAIGN_ID}~72~21')
    with pytest.raises(rails.RailViolation):
        _reader_call(monkeypatch, campaign, brands)()


def test_complete_group_inventory_rejects_name_collision(monkeypatch):
    from tests.conftest import make_type
    campaign, brands = _raw_reader_rows()
    group = make_type('GoogleAdsRow')
    group.asset_group = {'resource_name': f'customers/{CID}/assetGroups/99', 'id': '99',
                         'campaign': f'customers/{CID}/campaigns/{CAMPAIGN_ID}',
                         'name': 'New Group', 'status': 'PAUSED'}
    with pytest.raises(rails.RailViolation, match='name already exists'):
        _reader_call(monkeypatch, campaign, brands, [group])()


@pytest.mark.parametrize('damage', ['manager', 'disabled'])
def test_parent_account_must_be_enabled_non_manager(monkeypatch, damage):
    campaign, brands = _raw_reader_rows()
    call = _reader_call(monkeypatch, campaign, brands)
    account = parent_proof()['account']
    account['manager' if damage == 'manager' else 'status'] = True if damage == 'manager' else 'CANCELED'
    monkeypatch.setattr(client, '_creation_account', lambda *a, **kw: copy.deepcopy(account))
    with pytest.raises(rails.RailViolation):
        call()


@pytest.mark.parametrize('damage', ['foreign_parent', 'blank_name', 'unknown_status', 'duplicate'])
def test_malformed_group_inventory_fails_closed(monkeypatch, damage):
    from tests.conftest import make_type
    campaign, brands = _raw_reader_rows()
    group = make_type('GoogleAdsRow')
    group.asset_group = {'resource_name': f'customers/{CID}/assetGroups/99', 'id': '99',
                         'campaign': f'customers/{CID}/campaigns/{CAMPAIGN_ID}',
                         'name': 'Existing Group', 'status': 'PAUSED'}
    groups = [group]
    if damage == 'foreign_parent':
        group.asset_group.campaign = f'customers/{CID}/campaigns/999'
    elif damage == 'blank_name':
        group.asset_group.name = ''
    elif damage == 'unknown_status':
        group.asset_group.status = 'UNKNOWN'
    else:
        groups.append(copy.deepcopy(group))
    with pytest.raises(rails.RailViolation):
        _reader_call(monkeypatch, campaign, brands, groups)()


def test_sparse_dictionary_cannot_prove_setting_absence(monkeypatch):
    campaign, brands = _raw_reader_rows()
    sparse = type(campaign).to_dict(campaign)
    with pytest.raises(rails.RailViolation, match='complete raw provider row'):
        _reader_call(monkeypatch, sparse, brands)()


def test_literal_null_default_refused_at_mcp_boundary(asset_group):
    with pytest.raises(Exception):
        asyncio.run(app.mcp.call_tool('create_asset_group', dict(ARGS, customer_id='null')))
