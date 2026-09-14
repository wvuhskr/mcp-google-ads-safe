import copy
from contextlib import nullcontext

import pytest

from mcp_google_ads_safe import app, client, rails, settings, tools
from tests.conftest import make_row


@pytest.fixture(autouse=True)
def deny_real_provider_client(monkeypatch):
    monkeypatch.setattr(client, 'gads', lambda *a, **k: pytest.fail('real provider client forbidden'))


CID = '1234567890'
ARGS = dict(ad_group_id='22', headline='Cooling Help', description='Book cooling service',
            business_name='Cooling Company', square_marketing_image_asset_id='33',
            logo_image_asset_id='44', final_url='https://example.com/cooling', customer_id=CID)


def test_minimum_ad_is_one_paused_create(monkeypatch):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, '_search_one_page', raw_page)
    result = tools.draft_demand_gen_ad(**ARGS)
    plan = rails._DRAFTS[result['draft_id']].plan
    assert len(plan.operations) == 1
    assert plan.operations[0].service == 'AdGroupAdService'
    assert plan.operations[0].operation['create']['status'] == 'PAUSED'


def raw_rows():
    return {
        'customer': make_row(**{'customer.resource_name': f'customers/{CID}', 'customer.id': int(CID),
            'customer.descriptive_name': 'Test', 'customer.currency_code': 'USD',
            'customer.time_zone': 'America/New_York', 'customer.manager': False, 'customer.status': 'ENABLED'}),
        'ad_group': make_row(**{'ad_group.resource_name': f'customers/{CID}/adGroups/22',
            'ad_group.id': 22, 'ad_group.status': 'PAUSED', 'ad_group.type_': 'UNSPECIFIED',
            'ad_group.campaign': f'customers/{CID}/campaigns/11'}),
        'campaign': make_row(**{'campaign.resource_name': f'customers/{CID}/campaigns/11',
            'campaign.id': 11, 'campaign.status': 'PAUSED', 'campaign.advertising_channel_type': 'DEMAND_GEN',
            'campaign.advertising_channel_sub_type': 'UNSPECIFIED', 'campaign.bidding_strategy_type': 'MAXIMIZE_CONVERSIONS',
            'campaign.bidding_strategy': '', 'campaign.campaign_budget': f'customers/{CID}/campaignBudgets/55'}),
        'campaign_budget': make_row(**{'campaign_budget.resource_name': f'customers/{CID}/campaignBudgets/55',
            'campaign_budget.amount_micros': 25000000, 'campaign_budget.explicitly_shared': False,
            'campaign_budget.period': 'DAILY'}),
        **{f'asset/{aid}': make_row(**{'asset.resource_name': f'customers/{CID}/assets/{aid}',
            'asset.id': aid, 'asset.type_': 'IMAGE', 'asset.image_asset.mime_type': 'IMAGE_PNG',
            'asset.image_asset.file_size': 1000, 'asset.image_asset.full_size.width_pixels': 300,
            'asset.image_asset.full_size.height_pixels': 300}) for aid in (33, 44)},
    }


def raw_page(query, cid, token):
    kind = query.split(' FROM ')[1].split()[0]
    key = 'asset/' + query.split("assets/")[1].split("'")[0] if kind == 'asset' else kind
    return [raw_rows()[key]], None, 1


@pytest.fixture
def dg(monkeypatch):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    rows = raw_rows()
    reads = []

    def page(query, cid, token):
        reads.append(query)
        kind = query.split(' FROM ')[1].split()[0]
        key = 'asset/' + query.split("assets/")[1].split("'")[0] if kind == 'asset' else kind
        value = rows[key]
        if isinstance(value, Exception):
            raise value
        values = value if isinstance(value, list) else [value]
        return values, None, len(values)

    monkeypatch.setattr(client, '_search_one_page', page)
    return rows, reads


def draft(**changes):
    return tools.draft_demand_gen_ad(**dict(ARGS, **changes))


def plan_for(**changes):
    return rails.compile(rails.DraftDemandGenAdIntent(**dict(ARGS, **changes))).plan


def saved_row(plan):
    from google.ads.googleads.v25.resources.types.ad_group_ad import AdGroupAd
    value = copy.deepcopy(plan.operations[0].operation['create'])
    value['resource_name'] = f'customers/{CID}/adGroupAds/22~66'
    value['ad'].update(resource_name=f'customers/{CID}/ads/66', id=66,
                       type_='DEMAND_GEN_MULTI_ASSET_AD', tracking_url_template='', final_url_suffix='')
    row = make_row()
    row.ad_group_ad = AdGroupAd(**value)
    return row


def result_for():
    return {'results': [{'type': 'ad_group_ad_result',
                         'resource_name': f'customers/{CID}/adGroupAds/22~66'}], 'request_id': 'offline-request'}


@pytest.mark.parametrize('field', tuple(ARGS))
@pytest.mark.parametrize('value', [None, 1, True, 1.5, [], {}, ['text']])
def test_mcp_original_scalars(dg, field, value):
    from tests.test_protocol_errors import boundary, payload
    assert payload(boundary(app.mcp, 'draft_demand_gen_ad', dict(ARGS, **{field: value})))['code'] == 'BAD_INPUT'
    assert dg[1] == []


def test_mcp_unknown_and_customer_omission(dg):
    from tests.test_protocol_errors import boundary, payload
    assert payload(boundary(app.mcp, 'draft_demand_gen_ad', dict(ARGS, other='x')))['code'] == 'BAD_INPUT'
    args = dict(ARGS)
    args.pop('customer_id')
    assert not boundary(app.mcp, 'draft_demand_gen_ad', args).is_error


@pytest.mark.parametrize('field', ['ad_group_id', 'square_marketing_image_asset_id', 'logo_image_asset_id', 'customer_id'])
@pytest.mark.parametrize('value', ['null', '0', '01', '-1', '+1', '1 ', ' 1', '١', str(2**63), 'customers/2/assets/33'])
def test_strict_ids_before_reads(dg, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})
    assert dg[1] == []


@pytest.mark.parametrize('field,limit', [('headline', 30), ('description', 90), ('business_name', 25)])
@pytest.mark.parametrize('edge', ['blank', 'space', 'control', 'format', 'brace', 'long', 'wide'])
def test_plain_text_edges(dg, field, limit, edge):
    value = {'blank': '', 'space': ' x', 'control': 'x\n', 'format': 'x\u200d',
             'brace': '{x}', 'long': 'x' * (limit + 1), 'wide': '界' * (limit // 2 + 1)}[edge]
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})
    assert dg[1] == []


@pytest.mark.parametrize('field,limit', [('headline', 30), ('description', 90), ('business_name', 25)])
def test_text_exact_width_and_no_normalization(dg, field, limit):
    value = '界' * (limit // 2) + ('a' if limit % 2 else '')
    plan = plan_for(**{field: value})
    assert plan.post_checks[0]['inputs'][field] == value


@pytest.mark.parametrize('url', ['http://example.com', 'https://other.test', 'https://example.com.evil.test',
    'https://user@example.com', 'https://example.com:0', 'https://example.com:',
    'https://example.com/{x}', 'https://example.com/ x', 'https://example.com/' + 'x' * 2048])
def test_url_rules(dg, url):
    with pytest.raises(rails.RailViolation):
        draft(final_url=url)
    assert dg[1] == []


def test_domain_mandatory_and_subdomain_allowed(dg, monkeypatch):
    assert plan_for(final_url='https://sub.example.com/a?x=1').post_checks[0]['inputs']['final_url'] == 'https://sub.example.com/a?x=1'
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: '')
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('field', ['headline', 'description', 'business_name', 'final_url'])
def test_current_blocked_policy(dg, monkeypatch, field):
    original = draft()
    monkeypatch.setattr(settings, 'blocked_terms', lambda: [ARGS[field].lower().split()[0]])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert original['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('gate', ['writes', 'read', 'write'])
def test_gates_before_reads_and_at_confirmation(dg, monkeypatch, gate):
    original = draft()
    dg[1].clear()
    env = {'writes': 'GOOGLE_ADS_ENABLE_WRITES', 'read': 'GOOGLE_ADS_READ_CUSTOMER_IDS',
           'write': 'GOOGLE_ADS_WRITE_CUSTOMER_IDS'}[gate]
    monkeypatch.setenv(env, 'false' if gate == 'writes' else '999')
    with pytest.raises(rails.RailViolation):
        draft()
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert dg[1] == [] and original['draft_id'] in rails._DRAFTS


def test_same_asset_deduplicates_read_not_roles(dg):
    plan = plan_for(logo_image_asset_id='33')
    assert len(plan.post_checks[0]['proof']['assets']) == 1
    assert sum(' FROM asset ' in q for q in dg[1]) == 1
    creative = plan.operations[0].operation['create']['ad']['demand_gen_multi_asset_ad']
    assert creative['square_marketing_images'] == creative['logo_images']


@pytest.mark.parametrize('key', ['customer', 'ad_group', 'campaign', 'campaign_budget', 'asset/33', 'asset/44'])
@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'foreign', 'sparse'])
def test_complete_owned_raw_proof(dg, key, failure):
    row = dg[0][key]
    kind = 'asset' if key.startswith('asset/') else key
    if failure == 'missing':
        dg[0][key] = []
    elif failure == 'duplicate':
        dg[0][key] = [row, row]
    elif failure == 'foreign':
        entity = getattr(row, kind)
        entity.resource_name = entity.resource_name.replace(CID, '999')
    else:
        dg[0][key] = type(row).to_dict(row)
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('path,value', [
    ('customer.id', 3), ('customer.manager', True), ('customer.status', 'CANCELED'),
    ('customer.status', 999), ('customer.currency_code', ''), ('customer.time_zone', ''),
    ('ad_group.id', 3), ('ad_group.status', 'ENABLED'), ('ad_group.type_', 'UNKNOWN'), ('ad_group.type_', 999),
    ('ad_group.campaign', 'customers/999/campaigns/11'),
    ('campaign.id', 3), ('campaign.status', 'ENABLED'), ('campaign.advertising_channel_type', 'SEARCH'),
    ('campaign.advertising_channel_sub_type', 'UNKNOWN'), ('campaign.bidding_strategy_type', 999),
    ('campaign.bidding_strategy', 'customers/999/biddingStrategies/2'),
    ('campaign.campaign_budget', 'customers/999/campaignBudgets/55'),
    ('campaign_budget.amount_micros', 0), ('campaign_budget.period', 'UNKNOWN'),
    ('asset.id', 3), ('asset.type_', 'TEXT'), ('asset.image_asset.mime_type', 'IMAGE_GIF'),
    ('asset.image_asset.file_size', 0), ('asset.image_asset.file_size', 5000001),
    ('asset.image_asset.full_size.width_pixels', 299), ('asset.image_asset.full_size.height_pixels', 299),
])
def test_parent_inventory_metadata_failures(dg, path, value):
    kind, *parts = path.split('.')
    raw = getattr(dg[0]['asset/33' if kind == 'asset' else kind], kind)
    for part in parts[:-1]:
        raw = getattr(raw, part)
    setattr(raw, parts[-1], value)
    warning = {'ad_group.type_': 'AdGroupType', 'campaign.bidding_strategy_type': 'BiddingStrategyType'}.get(path)
    expected_warning = (pytest.warns(UserWarning, match=f'Unrecognized {warning} enum value: 999')
                        if value == 999 and warning else nullcontext())
    with expected_warning, pytest.raises(rails.RailViolation):
        draft()


def test_zero_type_false_shared_and_explicit_empty_portfolio(dg):
    proof = plan_for().post_checks[0]['proof']
    assert proof['group']['type'] == 'UNSPECIFIED'
    assert proof['budget']['explicitly_shared'] is False
    assert proof['campaign']['bidding_strategy'] == ''


def test_externally_existing_strategy_subtype_and_shared_budget(dg):
    dg[0]['ad_group'].ad_group.type_ = 'SEARCH_STANDARD'
    dg[0]['campaign'].campaign.bidding_strategy_type = 'TARGET_CPA'
    dg[0]['campaign'].campaign.bidding_strategy = f'customers/{CID}/biddingStrategies/88'
    dg[0]['campaign_budget'].campaign_budget.explicitly_shared = True
    assert plan_for().post_checks[0]['proof']['budget']['explicitly_shared'] is True


@pytest.mark.parametrize('role,minimum', [('33', 300), ('44', 128)])
@pytest.mark.parametrize('offset', [-1, 0])
def test_role_minima(dg, role, minimum, offset):
    raw = dg[0]['asset/' + role].asset.image_asset.full_size
    raw.width_pixels = raw.height_pixels = minimum + offset
    if offset < 0:
        with pytest.raises(rails.RailViolation):
            draft()
    else:
        assert draft()['dry_run']


def test_page_count_mismatch(dg, monkeypatch):
    monkeypatch.setattr(client, '_search_one_page', lambda *a: ([dg[0]['customer']], None, 2))
    with pytest.raises(rails.RailViolation, match='count'):
        draft()


def test_sdk_roundtrip_and_atomic_validate_only(dg, fake_gads):
    from tests.conftest import make_type
    plan = plan_for()
    context = client.validate_demand_gen_ad_plan(plan)
    message = client._build_mutate_operation(fake_gads, plan.operations[0], context)
    restored = type(message).deserialize(type(message).serialize(message))
    ad = restored.ad_group_ad_operation.create
    assert ad.status.name == 'PAUSED'
    assert ad.ad._pb.WhichOneof('ad_data') == 'demand_gen_multi_asset_ad'
    assert len(ad.ad.demand_gen_multi_asset_ad.square_marketing_images) == 1
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan, True)
    assert result['validate_only'] is True
    assert len(fake_gads.mutate_calls) == 1
    request = fake_gads.mutate_calls[0]
    assert request['partial_failure'] is False and request['validate_only'] is True
    assert len(request['operations']) == 1


@pytest.mark.parametrize('tamper', ['marker_removed', 'checks_removed', 'generic_checks', 'extra_check',
    'status', 'extra_operation', 'update_mask', 'parent', 'foreign_asset', 'other_family',
    'tracking', 'ad_identity', 'alias', 'extra_text_key', 'extra_image_key', 'two_texts',
    'two_images', 'no_logo', 'cta', 'forged_proof', 'result_index_bool'])
def test_closed_family_guard_before_client(dg, monkeypatch, tamper):
    from dataclasses import replace
    plan = copy.deepcopy(plan_for())
    create = plan.operations[0].operation['create']
    ad = create['ad']
    creative = ad['demand_gen_multi_asset_ad']
    if tamper == 'marker_removed':
        del plan.post_checks[0]['demand_gen_ad']
    elif tamper == 'checks_removed':
        plan.post_checks.clear()
    elif tamper == 'generic_checks':
        plan.post_checks[:] = [{'result_index': 0, 'entity_type': 'ad_group_ad', 'customer_id': CID, 'expected': copy.deepcopy(create)}]
    elif tamper == 'extra_check':
        plan.post_checks.append(copy.deepcopy(plan.post_checks[0]))
    elif tamper == 'status':
        create['status'] = 'ENABLED'
    elif tamper == 'extra_operation':
        plan.operations.append(copy.deepcopy(plan.operations[0]))
    elif tamper == 'update_mask':
        plan.operations[0] = replace(plan.operations[0], update_mask=['ad'])
    elif tamper == 'parent':
        create['ad_group'] = f'customers/{CID}/adGroups/99'
    elif tamper == 'foreign_asset':
        creative['logo_images'][0]['asset'] = 'customers/999/assets/44'
    elif tamper == 'other_family':
        ad['responsive_search_ad'] = {'headlines': [{'text': 'hi'}]}
    elif tamper == 'tracking':
        ad['tracking_url_template'] = ''
    elif tamper == 'ad_identity':
        ad['resource_name'] = f'customers/{CID}/ads/66'
    elif tamper == 'alias':
        create['adGroup'] = create.pop('ad_group')
    elif tamper == 'extra_text_key':
        creative['headlines'][0]['pinned_field'] = 'HEADLINE_1'
    elif tamper == 'extra_image_key':
        creative['logo_images'][0]['asset_performance_label'] = 'BEST'
    elif tamper == 'two_texts':
        creative['headlines'] *= 2
    elif tamper == 'two_images':
        creative['square_marketing_images'] *= 2
    elif tamper == 'no_logo':
        creative['logo_images'] = []
    elif tamper == 'cta':
        creative['call_to_action_text'] = 'Book now'
    elif tamper == 'forged_proof':
        plan.post_checks[0]['proof']['group']['status'] = 'ENABLED'
    else:
        plan.post_checks[0]['result_index'] = False
    # Even a check changed to agree with forged content must not authorize it.
    if plan.post_checks and 'expected' in plan.post_checks[0]:
        plan.post_checks[0]['expected'] = copy.deepcopy(create)
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('provider client constructed'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)


def test_saved_success_actual_readers(dg):
    plan = plan_for()
    dg[0]['ad_group_ad'] = saved_row(plan)
    assert client.verify_demand_gen_ad_results(plan.post_checks, result_for()) == [f'customers/{CID}/adGroupAds/22~66']


@pytest.mark.parametrize('rn', ['customers/999/adGroupAds/22~66', f'customers/{CID}/adGroupAds/99~66',
    f'customers/{CID}/adGroupAds/22~0', f'customers/{CID}/adGroupAds/22~066',
    f'customers/{CID}/adGroupAds/22~{2**63}', f'customers/{CID}/ads/66', None])
def test_compound_result_refused_before_saved_reads(dg, rn):
    plan = plan_for()
    dg[1].clear()
    result = result_for()
    result['results'][0]['resource_name'] = rn
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_ad_results(plan.post_checks, result)
    assert dg[1] == []


@pytest.mark.parametrize('field,value', [
    ('status', 'ENABLED'), ('ad_group', f'customers/{CID}/adGroups/99'),
    ('ad.id', 77), ('ad.resource_name', f'customers/{CID}/ads/77'), ('ad.type_', 'RESPONSIVE_SEARCH_AD'),
    ('ad.final_urls', ['https://example.com/other']), ('ad.final_mobile_urls', ['https://example.com']),
    ('ad.tracking_url_template', 'https://tracker.example.com'), ('ad.final_url_suffix', 'x=y'),
    ('ad.url_custom_parameters', [{'key': 'x', 'value': 'y'}]),
    ('ad.demand_gen_multi_asset_ad.business_name', 'Other'),
    ('ad.demand_gen_multi_asset_ad.call_to_action_text', 'Book'),
    ('ad.demand_gen_multi_asset_ad.headlines', [{'text': 'Other'}]),
    ('ad.demand_gen_multi_asset_ad.descriptions', []),
    ('ad.demand_gen_multi_asset_ad.logo_images', []),
    ('ad.demand_gen_multi_asset_ad.square_marketing_images', [{'asset': f'customers/{CID}/assets/44'}]),
    *[(f'ad.demand_gen_multi_asset_ad.{role}', [{'asset': f'customers/{CID}/assets/33'}]) for role in
      ('marketing_images', 'portrait_marketing_images', 'tall_portrait_marketing_images', 'classic_display_images')],
    ('ad.demand_gen_multi_asset_ad.headlines', [{'text': ARGS['headline'], 'pinned_field': 'HEADLINE_1'}]),
])
def test_saved_mismatch(dg, field, value):
    plan = plan_for()
    row = saved_row(plan)
    dg[0]['ad_group_ad'] = row
    obj = row.ad_group_ad
    parts = field.split('.')
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_ad_results(plan.post_checks, result_for())


@pytest.mark.parametrize('key,path,value', [
    ('customer', 'customer.descriptive_name', 'Changed'),
    ('customer', 'customer.currency_code', 'EUR'), ('customer', 'customer.time_zone', 'UTC'),
    ('ad_group', 'ad_group.type_', 'SEARCH_STANDARD'),
    ('campaign', 'campaign.bidding_strategy_type', 'TARGET_CPA'),
    ('campaign', 'campaign.bidding_strategy', f'customers/{CID}/biddingStrategies/88'),
    ('campaign', 'campaign.advertising_channel_sub_type', 'DISPLAY_MOBILE_APP'),
    ('campaign_budget', 'campaign_budget.amount_micros', 30000000),
    ('campaign_budget', 'campaign_budget.explicitly_shared', True),
    ('campaign_budget', 'campaign_budget.period', 'CUSTOM_PERIOD'),
    ('asset/33', 'asset.image_asset.file_size', 2000),
    ('asset/44', 'asset.image_asset.mime_type', 'IMAGE_JPEG'),
])
def test_each_readable_fingerprint_change_refuses_without_consuming(dg, monkeypatch, key, path, value):
    original = draft()
    raw = dg[0][key]
    before = copy.deepcopy(raw)
    parts = path.split('.')
    for part in parts[:-1]:
        raw = getattr(raw, part)
    setattr(raw, parts[-1], value)
    dispatches = []
    monkeypatch.setattr(client, '_dispatch', lambda plan: dispatches.append(plan) or result_for())
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert original['draft_id'] in rails._DRAFTS and dispatches == []
    dg[0][key] = before
    plan = rails._DRAFTS[original['draft_id']].plan
    dg[0]['ad_group_ad'] = saved_row(plan)
    assert rails.apply_draft(original['draft_id'])['verified'] is True
    assert len(dispatches) == 1


@pytest.mark.parametrize('key,path', [
    ('ad_group', 'ad_group.id'), ('ad_group', 'ad_group.campaign'),
    ('campaign', 'campaign.id'), ('campaign', 'campaign.campaign_budget'),
    ('campaign', 'campaign.bidding_strategy'),
    ('campaign_budget', 'campaign_budget.amount_micros'),
    ('campaign_budget', 'campaign_budget.explicitly_shared'),
    ('asset/33', 'asset.id'), ('asset/33', 'asset.image_asset.file_size'),
    ('asset/33', 'asset.image_asset.full_size.width_pixels'),
])
def test_missing_optional_raw_metadata_refuses(dg, key, path):
    raw = dg[0][key]
    parts = path.split('.')
    for part in parts[:-1]:
        raw = getattr(raw, part)
    raw._pb.ClearField(parts[-1])
    with pytest.raises(rails.RailViolation):
        draft()


def test_two_page_scan_reconciles_requested_population(dg, monkeypatch):
    pages = []

    def page(query, cid, token):
        pages.append((query, token))
        if ' FROM customer' in query:
            return ([], 'next', 1) if token is None else ([dg[0]['customer']], None, 1)
        return raw_page(query, cid, token)

    monkeypatch.setattr(client, '_search_one_page', page)
    assert draft()['dry_run']
    assert [token for query, token in pages if ' FROM customer' in query] == [None, 'next']
    assert all('LIMIT' not in query for query, token in pages)


def test_saved_asset_performance_output_is_ignored(dg):
    plan = plan_for()
    row = saved_row(plan)
    for field in ('headlines', 'descriptions'):
        getattr(row.ad_group_ad.ad.demand_gen_multi_asset_ad, field)[0].asset_performance_label = 'BEST'
    dg[0]['ad_group_ad'] = row
    assert client.verify_demand_gen_ad_results(plan.post_checks, result_for())


@pytest.mark.parametrize('mode', ['missing', 'query_failure', 'bad_result', 'sparse', 'other_oneof', 'parent_drift', 'asset_drift'])
def test_applied_unverified_consumes_once(dg, monkeypatch, mode):
    result = draft()
    plan = rails._DRAFTS[result['draft_id']].plan
    dg[0]['ad_group_ad'] = saved_row(plan)
    calls = []

    def dispatch(plan):
        calls.append(plan)
        if mode == 'missing':
            dg[0]['ad_group_ad'] = []
        elif mode == 'query_failure':
            dg[0]['ad_group_ad'] = RuntimeError('offline query failure')
        elif mode == 'bad_result':
            return {'results': []}
        elif mode == 'sparse':
            dg[0]['ad_group_ad'] = type(dg[0]['ad_group_ad']).to_dict(dg[0]['ad_group_ad'])
        elif mode == 'other_oneof':
            dg[0]['ad_group_ad'].ad_group_ad.ad.responsive_search_ad = {'headlines': [{'text': 'other'}]}
        elif mode == 'parent_drift':
            dg[0]['ad_group'].ad_group.status = 'ENABLED'
        else:
            dg[0]['asset/33'].asset.image_asset.file_size = 2000
        return result_for()

    monkeypatch.setattr(client, '_dispatch', dispatch)
    applied = rails.apply_draft(result['draft_id'])
    assert applied['applied'] is True and applied['verified'] is False
    assert applied['code'] == 'POST_WRITE_VERIFICATION_FAILED'
    assert result['draft_id'] not in rails._DRAFTS
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result['draft_id'])
    assert len(calls) == 1


def test_unknown_transport_consumes_and_retains_request_id(dg, monkeypatch):
    import json

    from mcp_google_ads_safe import audit
    original = draft()
    calls = []

    def dispatch(plan):
        calls.append(plan)
        raise rails.UnknownWriteOutcome('offline ambiguous result', request_id='offline-request',
                                        failure={'errors': [{'index': 0, 'code': 'UNAVAILABLE'}]})

    monkeypatch.setattr(client, '_dispatch', dispatch)
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(original['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert len(calls) == 1
    events = [json.loads(line) for line in open(audit.AUDIT_PATH)]
    # the second apply attempt is audited "refused"; the unknown outcome precedes it
    assert events[-1]['phase'] == 'refused'
    assert events[-2]['phase'] == 'unknown'
    assert events[-2]['request_id'] == 'offline-request'


def test_validate_only_consumes_without_saved_read(dg, monkeypatch):
    original = draft()
    dg[1].clear()
    monkeypatch.setattr(client, '_dispatch', lambda plan: dict(result_for(), validate_only=True))
    result = rails.apply_draft(original['draft_id'])
    assert result['verified'] is False and result['result']['validate_only'] is True
    assert not any(' FROM ad_group_ad ' in query for query in dg[1])
    assert original['draft_id'] not in rails._DRAFTS


def test_draft_audit_failure_discards(dg, monkeypatch):
    from mcp_google_ads_safe import audit
    before = set(rails._DRAFTS)

    def fail(*a):
        raise OSError('offline audit persistence failure')

    monkeypatch.setattr(audit, 'log_event', fail)
    with pytest.raises(OSError):
        draft()
    assert set(rails._DRAFTS) == before


def test_shared_create_helper_supplies_pause(dg, monkeypatch):
    calls = []
    original = rails.safe_create_operation

    def create(service, fields):
        assert 'status' not in fields
        calls.append(service)
        return original(service, fields)

    monkeypatch.setattr(rails, 'safe_create_operation', create)
    assert draft()['dry_run']
    assert calls and set(calls) == {'AdGroupAdService'}


def test_copied_intent_and_duplicate_creatives_allowed(dg):
    from dataclasses import FrozenInstanceError
    intent = rails.DraftDemandGenAdIntent(**ARGS)
    original = rails.creation_draft(intent)
    with pytest.raises(FrozenInstanceError):
        intent.headline = 'Changed'
    object.__setattr__(intent, 'headline', 'Changed')
    rails._DRAFTS[original['draft_id']].validate_fn()
    other = draft()
    assert original['draft_id'] != other['draft_id']


def test_offline_guards_reject_client_and_socket(dg):
    import socket
    with pytest.raises(BaseException, match='real provider client forbidden'):
        client.gads()
    with pytest.raises(AssertionError, match='forbids network'):
        socket.getaddrinfo('invalid.example', 443)
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match='forbids network'):
            connection.connect(('127.0.0.1', 443))


@pytest.mark.parametrize('field', ['ad_group_id', 'customer_id', 'square_marketing_image_asset_id', 'logo_image_asset_id'])
def test_empty_id_does_not_resolve_default(dg, field):
    with pytest.raises(rails.RailViolation):
        draft(**{field: ''})
    assert dg[1] == []


def test_image_exact_file_ceiling_and_square_drift(dg, monkeypatch):
    dg[0]['asset/33'].asset.image_asset.file_size = 5_000_000
    original = draft()
    size = dg[0]['asset/33'].asset.image_asset.full_size
    size.width_pixels = size.height_pixels = 301
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert original['draft_id'] in rails._DRAFTS


def test_current_domain_change_refuses(dg, monkeypatch):
    original = draft()
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'other.test')
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert original['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('field', ['id', 'type_', 'campaign'])
def test_group_identity_enum_and_linkage_drift_preserves_draft(dg, field):
    original = draft()
    values = {'id': 23, 'type_': 999, 'campaign': f'customers/{CID}/campaigns/12'}
    setattr(dg[0]['ad_group'].ad_group, field, values[field])
    warning = (pytest.warns(UserWarning, match='Unrecognized AdGroupType enum value: 999')
               if field == 'type_' else nullcontext())
    with warning, pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    assert original['draft_id'] in rails._DRAFTS


@pytest.mark.parametrize('kind', ['customer', 'ad_group', 'campaign', 'campaign_budget', 'asset/33'])
def test_post_dispatch_proof_component_drift(dg, kind):
    plan = plan_for()
    dg[0]['ad_group_ad'] = saved_row(plan)
    modifications = {'customer': ('customer', 'time_zone', 'UTC'),
                     'ad_group': ('ad_group', 'type_', 'SEARCH_STANDARD'),
                     'campaign': ('campaign', 'bidding_strategy_type', 'TARGET_CPA'),
                     'campaign_budget': ('campaign_budget', 'amount_micros', 30000000),
                     'asset/33': ('asset', 'id', 999)}
    entity, field, value = modifications[kind]
    setattr(getattr(dg[0][kind], entity), field, value)
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_ad_results(plan.post_checks, result_for())


@pytest.mark.parametrize('role', ['square_marketing_images', 'logo_images', 'headlines', 'descriptions'])
def test_saved_extra_cardinality_refuses(dg, role):
    plan = plan_for()
    row = saved_row(plan)
    sequence = getattr(row.ad_group_ad.ad.demand_gen_multi_asset_ad, role)
    sequence.append(copy.deepcopy(sequence[0]))
    dg[0]['ad_group_ad'] = row
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_ad_results(plan.post_checks, result_for())


@pytest.mark.parametrize('field', ['tracking_url_template', 'final_url_suffix'])
def test_unreadable_optional_saved_absence_is_unverified(dg, field):
    plan = plan_for()
    row = saved_row(plan)
    row.ad_group_ad.ad._pb.ClearField(field)
    dg[0]['ad_group_ad'] = row
    with pytest.raises(rails.RailViolation):
        client.verify_demand_gen_ad_results(plan.post_checks, result_for())


def test_audit_draft_refused_apply_and_error_fields(dg, monkeypatch):
    import json
    from pathlib import Path

    from mcp_google_ads_safe import audit
    original = draft()
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(original['draft_id'])
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'true')
    plan = rails._DRAFTS[original['draft_id']].plan
    dg[0]['ad_group_ad'] = saved_row(plan)
    monkeypatch.setattr(client, '_dispatch', lambda plan: result_for())
    assert rails.apply_draft(original['draft_id'])['verified']
    error_draft = draft()

    def fail(plan):
        raise RuntimeError('offline indexed provider rejection')

    monkeypatch.setattr(client, '_dispatch', fail)
    with pytest.raises(RuntimeError):
        rails.apply_draft(error_draft['draft_id'])
    events = [json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines()]
    assert [item['phase'] for item in events] == ['draft', 'refused', 'apply', 'draft', 'error']
    for event in events:
        assert event['customer_id'] == CID and event['operation_count'] == 1
    assert events[0]['digest'] == events[2]['digest']
    assert error_draft['draft_id'] not in rails._DRAFTS


def test_expiry_precedes_reads_and_consumes(dg, monkeypatch):
    original = draft()
    rails._DRAFTS[original['draft_id']].created_at -= 3601
    dg[1].clear()
    with pytest.raises(rails.RailViolation, match='expired'):
        rails.apply_draft(original['draft_id'])
    assert dg[1] == [] and original['draft_id'] not in rails._DRAFTS


def test_tampered_digest_consumes_without_dispatch(dg):
    original = draft()
    stored = rails._DRAFTS[original['draft_id']]
    stored.plan.operations[0].operation['create']['ad']['final_urls'] = ['https://example.com/other']
    with pytest.raises(rails.RailViolation, match='digest'):
        rails.apply_draft(original['draft_id'])
    assert original['draft_id'] not in rails._DRAFTS


def test_result_entry_count_and_type_refuse_before_reads(dg):
    plan = plan_for()
    dg[1].clear()
    for results in ([], result_for()['results'] * 2, [{'type': 'ad_result', 'resource_name': f'customers/{CID}/ads/66'}]):
        with pytest.raises(rails.RailViolation):
            client.verify_demand_gen_ad_results(plan.post_checks, {'results': results})
    assert dg[1] == []



def test_provider_guard_precedes_any_credential_file_read(dg, monkeypatch):
    import builtins
    reads = []

    def no_open(*args, **kwargs):
        reads.append(args)
        raise AssertionError('credential read reached')

    monkeypatch.setattr(builtins, 'open', no_open)
    with pytest.raises(BaseException, match='real provider client forbidden'):
        client.gads('/must-not-read-credentials.yaml')
    assert reads == []


def test_exact_operation_matches_contract(dg):
    assert plan_for().operations == [rails.MutationOp('AdGroupAdService', {'create': {
        'ad_group': f'customers/{CID}/adGroups/22', 'status': 'PAUSED',
        'ad': {'final_urls': ['https://example.com/cooling'], 'demand_gen_multi_asset_ad': {
            'square_marketing_images': [{'asset': f'customers/{CID}/assets/33'}],
            'logo_images': [{'asset': f'customers/{CID}/assets/44'}],
            'headlines': [{'text': 'Cooling Help'}],
            'descriptions': [{'text': 'Book cooling service'}],
            'business_name': 'Cooling Company'}}}}, None)]
