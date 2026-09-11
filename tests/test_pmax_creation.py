"""Offline PMax graph, original-input, populated-reader and dispatch safety proof."""
import asyncio
import copy

import pytest

from mcp_google_ads_safe import app, client, rails, settings, tools

CID = '1234567890'
ARGS = dict(campaign_name='PMax One', asset_group_name='Group One', daily_budget='25',
            target_cpa='10', geo_target_ids=['2840'], language_ids=['1000'],
            headlines=['Cool Today', 'Local Cooling', 'Book Service'],
            long_headlines=['Cooling service for your home'],
            descriptions=['Book cooling service today', 'Your local cooling specialists'],
            business_name='Cooling Co', final_url='https://example.com/service',
            landscape_image_asset_id='70', square_image_asset_id='71', logo_asset_id='72',
            contains_eu_political_advertising=False)


def image_state(identity, width, height):
    return {'resource_name': f'customers/{CID}/assets/{identity}', 'type_': 'IMAGE',
            'image_asset': {'mime_type': 'IMAGE_JPEG', 'file_size': '5000000',
                            'full_size': {'width_pixels': str(width), 'height_pixels': str(height)}}}


@pytest.fixture
def pmax(monkeypatch, fake_client):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    state = {'account': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York', 'manager': False},
             'collisions': [], 'locations': [], 'languages': [],
             'images': {'MARKETING_IMAGE': image_state('70', 600, 314),
                        'SQUARE_MARKETING_IMAGE': image_state('71', 300, 300),
                        'LOGO': image_state('72', 128, 128)}}
    monkeypatch.setattr(client, 'pmax_creation_state', lambda *a: copy.deepcopy(state))
    return state, fake_client


def draft(**changes):
    return tools.create_pmax_campaign(**dict(ARGS, **changes))


def plan_for(**changes):
    return rails._DRAFTS[draft(**changes)['draft_id']].plan


def test_paused_closed_graph_with_exact_roles(pmax, fake_gads):
    plan = plan_for()
    context = client.validate_mutation_plan(plan)
    built = [client._build_mutate_operation(fake_gads, op, context) for op in plan.operations]
    campaign = built[1].campaign_operation.create
    assert campaign.status.name == 'PAUSED'
    assert campaign.advertising_channel_type.name == 'PERFORMANCE_MAX'
    assert campaign.brand_guidelines_enabled is True
    assert campaign.maximize_conversions.target_cpa_micros == 10_000_000
    assert len(campaign.asset_automation_settings) == 5
    assert all(item.asset_automation_status.name == 'OPTED_OUT' for item in campaign.asset_automation_settings)
    roles = {'campaign_asset': [], 'asset_group_asset': []}
    for check in plan.post_checks:
        if check['entity_type'] in roles:
            roles[check['entity_type']].append(check['expected']['field_type'])
            assert check['expected']['status'] == 'PAUSED'
    assert roles['campaign_asset'] == ['BUSINESS_NAME', 'LOGO']
    assert set(roles['asset_group_asset']) == {'HEADLINE', 'LONG_HEADLINE', 'DESCRIPTION', 'MARKETING_IMAGE', 'SQUARE_MARKETING_IMAGE'}


@pytest.mark.parametrize(('field', 'value'), [('customer_id', ''), ('customer_id', 123),
    ('customer_id', '0123'), ('daily_budget', True), ('target_cpa', 'NaN'),
    ('headlines', '["a","b","c"]'), ('geo_target_ids', ['02840']),
    ('language_ids', [1000]), ('contains_eu_political_advertising', 'false')])
def test_original_inputs_refused_before_reads(monkeypatch, field, value):
    monkeypatch.setattr(client, 'pmax_creation_state', lambda *a: pytest.fail('read before input validation'))
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


def test_original_mcp_containers_refused(monkeypatch):
    monkeypatch.setattr(client, 'pmax_creation_state', lambda *a: pytest.fail('coerced input reached reads'))
    for field, value in [('headlines', '["a","b","c"]'), ('language_ids', [1000]),
                         ('campaign_name', 123), ('contains_eu_political_advertising', 0)]:
        with pytest.raises(Exception):
            asyncio.run(app.mcp.call_tool('create_pmax_campaign', dict(ARGS, **{field: value})))


def saved_graph(plan):
    """Provider-shaped ordered results and fully populated read rows for this graph."""
    resolved, rows, results = {}, {}, []
    for index, check in enumerate(plan.post_checks):
        entity = check['entity_type']
        state = copy.deepcopy(check['expected'])
        for key in ('campaign_budget', 'campaign', 'asset_group', 'asset'):
            if key in state:
                state[key] = resolved.get(state[key], state[key])
        identity = str(100 + index)
        if entity == 'campaign_criterion':
            identity = state['campaign'].rsplit('/', 1)[1] + '~' + identity
        if entity in {'campaign_asset', 'asset_group_asset'}:
            parent = 'campaign' if entity == 'campaign_asset' else 'asset_group'
            identity = '~'.join([state[parent].rsplit('/', 1)[1], state['asset'].rsplit('/', 1)[1],
                                 client.PMAX_FIELD_NUMBERS[state['field_type']]])
        rn = f'customers/{CID}/{client.PMAX_KINDS[entity]}/{identity}'
        if check['definition']:
            resolved[check['definition']] = rn
        state['resource_name'] = rn
        rows[rn] = {entity: state}
        results.append({'type': entity + '_result', 'resource_name': rn})
    for image in plan.post_checks[0]['images'].values():
        rows[image['resource_name']] = {'asset': copy.deepcopy(image)}
    return {'results': results, 'request_id': None}, rows


def use_saved(monkeypatch, rows, seen=None):
    def read(query, cid):
        assert cid == CID
        rn = query.split("resource_name = '")[1].split("'")[0]
        if seen is not None:
            seen.append(rn)
        return [copy.deepcopy(rows[rn])]
    monkeypatch.setattr(client, 'gaql_all', read)


def test_actual_saved_reader_success_and_consumption(pmax, monkeypatch):
    _, fake = pmax
    result = draft()
    plan = rails._DRAFTS[result['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    reads = []
    use_saved(monkeypatch, rows, reads)
    applied = rails.apply_draft(result['draft_id'])
    assert applied['applied'] is True and applied['verified'] is True
    assert len(reads) == len(plan.operations) + 3
    assert result['draft_id'] not in rails._DRAFTS
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_actual_atomic_dispatch_validate_only(pmax, fake_gads):
    from tests.conftest import make_type
    plan = plan_for()
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    result = client._dispatch_entity(plan, True)
    assert result['validate_only'] is True
    assert len(fake_gads.mutate_calls) == 1
    request = fake_gads.mutate_calls[0]
    assert request['partial_failure'] is False and request['validate_only'] is True
    assert request['customer_id'] == CID and len(request['operations']) == len(plan.operations)
    campaign = request['operations'][1].campaign_operation.create
    populated = {field.name for field, value in campaign._pb.ListFields()}
    assert not populated & {'network_settings', 'bidding_strategy', 'advertising_channel_sub_type', 'shopping_setting'}
    assert campaign._pb.WhichOneof('campaign_bidding_strategy') == 'maximize_conversions'
    group = next(op.asset_group_operation.create for op in request['operations'] if op._pb.WhichOneof('operation') == 'asset_group_operation')
    assert group.status.name == 'PAUSED' and list(group.final_urls) == [ARGS['final_url']]


@pytest.mark.parametrize(('field', 'value'), [
    ('headlines', ['x', 'y']), ('headlines', [str(i) for i in range(16)]),
    ('headlines', ['a'*16, 'b'*16, 'c'*16]), ('headlines', ['x', 'y', 'z'*31]),
    ('headlines', ['短'*16, 'x', 'y']), ('headlines', ['x', 'x', 'y']),
    ('long_headlines', []), ('long_headlines', [str(i) for i in range(6)]),
    ('long_headlines', ['x'*91]), ('long_headlines', ['x', 'x']),
    ('descriptions', ['x']), ('descriptions', [str(i) for i in range(6)]),
    ('descriptions', ['x'*61, 'y'*61]), ('descriptions', ['x'*91, 'y']),
    ('descriptions', ['x', 'x']), ('business_name', 'x'*26),
    ('business_name', '短'*13), ('business_name', ' bad'),
    ('final_url', 'http://example.com'), ('final_url', 'https://evil.test'),
    ('geo_target_ids', []), ('geo_target_ids', ['1']*2),
    ('geo_target_ids', [str(i) for i in range(1,102)]),
    ('language_ids', ('1000',)), ('landscape_image_asset_id', '070'),
    ('square_image_asset_id', 71), ('logo_asset_id', '１２'),
    ('target_cpa', True), ('target_cpa', '50.000001'), ('daily_budget', '1000.000001'),
    ('daily_budget', float('inf')), ('daily_budget', '0.0000001'),
])
def test_input_bounds(pmax, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field: value})


def test_text_maximums_short_boundaries_and_shared_definition(pmax):
    plan = plan_for(headlines=['a'*15] + [str(i)+'x'*28 for i in range(14)],
                    long_headlines=['L'*90] + ['L'*89+str(i) for i in range(4)],
                    descriptions=['D'*60] + ['D'*89+str(i) for i in range(4)], business_name='B'*25)
    assert len([op for op in plan.operations if op.service == 'AssetService']) == 26
    plan = plan_for(headlines=['One', 'Two', 'Three'], long_headlines=['One'],
                    descriptions=['One', 'Two'], business_name='One')
    assert len([op for op in plan.operations if op.service == 'AssetService']) == 3
    assert len([op for op in plan.operations if op.service in {'CampaignAssetService', 'AssetGroupAssetService'}]) == 10
    plan_for(headlines=['短'*7, 'x', '短'*15], descriptions=['短'*30, '短'*45])


@pytest.mark.parametrize(('role', 'field', 'value'), [
    ('MARKETING_IMAGE', 'width_pixels', 599), ('MARKETING_IMAGE', 'height_pixels', 313),
    ('MARKETING_IMAGE', 'width_pixels', 601), ('SQUARE_MARKETING_IMAGE', 'width_pixels', 299),
    ('SQUARE_MARKETING_IMAGE', 'height_pixels', 301), ('LOGO', 'width_pixels', 127),
    ('LOGO', 'height_pixels', 129), ('LOGO', 'file_size', 5000001),
    ('LOGO', 'file_size', 0), ('LOGO', 'file_size', True), ('LOGO', 'file_size', '01'),
    ('LOGO', 'mime_type', 'IMAGE_GIF'), ('LOGO', 'mime_type', 'FAKE'),
    ('LOGO', 'mime_type', 999999), ('LOGO', 'mime_type', True),
    ('LOGO', 'width_pixels', None), ('MARKETING_IMAGE', 'height_pixels', '314.0'),
])
def test_image_role_metadata_bounds(pmax, role, field, value):
    state, _ = pmax
    metadata = state['images'][role]['image_asset']
    (metadata['full_size'] if field.endswith('pixels') else metadata)[field] = value
    with pytest.raises(rails.RailViolation):
        draft()


def test_second_landscape_ratio_and_shared_square_logo(pmax):
    state, _ = pmax
    state['images']['MARKETING_IMAGE'] = image_state('70', 1910, 1000)
    state['images']['LOGO'] = image_state('71', 300, 300)
    plan = plan_for(logo_asset_id='71')
    assert plan.post_checks[0]['images']['LOGO'] == plan.post_checks[0]['images']['SQUARE_MARKETING_IMAGE']


@pytest.mark.parametrize('gate', ['writes', 'write_allowlist', 'read_allowlist'])
def test_gates_before_reads_and_repeated_at_confirm(pmax, monkeypatch, gate):
    _, fake = pmax
    result = draft()
    if gate == 'writes':
        monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    elif gate == 'write_allowlist':
        monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS', '999')
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', f'{CID},999')
    else:
        monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS', '999')
    monkeypatch.setattr(client, 'pmax_creation_state', lambda *a: pytest.fail('gate must precede reads'))
    for call in (draft, lambda: rails.apply_draft(result['draft_id'])):
        with pytest.raises(rails.RailViolation):
            call()
    assert result['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize('field', ['account', 'locations', 'languages', 'images', 'collisions'])
def test_fresh_draft_drift_unconsumed(pmax, field):
    state, fake = pmax
    result = draft()
    if field == 'account':
        state[field]['currency_code'] = 'EUR'
    elif field == 'images':
        state[field]['LOGO']['image_asset']['file_size'] = '4900000'
    else:
        state[field].append({'changed': True})
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(result['draft_id'])
    assert result['draft_id'] in rails._DRAFTS and fake.dispatch_calls == []


@pytest.mark.parametrize('change', ['kind', 'action', 'mask', 'channel', 'status', 'portfolio',
    'network', 'brand', 'automation', 'criterion', 'group_parent', 'group_field',
    'text_extra', 'text_duplicate', 'unused_text', 'link_parent', 'link_owner', 'link_role',
    'link_status', 'link_extra', 'descriptor', 'descriptor_images', 'duplicate_op'])
def test_tampered_graph_refused_before_dispatch(pmax, fake_gads, change):
    plan = copy.deepcopy(plan_for())
    ops = plan.operations
    def at(service):
        return next(i for i, op in enumerate(ops) if op.service == service)
    i = at('AssetGroupService')
    t = at('AssetService')
    link = at('AssetGroupAssetService')
    if change == 'kind':
        object.__setattr__(plan, 'kind', 'alien')
    elif change == 'action':
        ops[1].operation['update'] = ops[1].operation.pop('create')
    elif change == 'mask':
        object.__setattr__(ops[1], 'update_mask', ['status'])
    elif change in {'channel', 'status', 'portfolio', 'network', 'brand', 'automation'}:
        key, value = {'channel': ('advertising_channel_type', 'SEARCH'), 'status': ('status', 'ENABLED'),
                      'portfolio': ('bidding_strategy', f'customers/{CID}/biddingStrategies/55'),
                      'network': ('network_settings', {}), 'brand': ('brand_guidelines_enabled', False),
                      'automation': ('asset_automation_settings', [])}[change]
        ops[1].operation['create'][key] = value
    elif change == 'criterion':
        ops[2].operation['create']['negative'] = True
    elif change == 'group_parent':
        ops[i].operation['create']['campaign'] = f'customers/{CID}/campaigns/55'
    elif change == 'group_field':
        ops[i].operation['create']['path1'] = 'hidden'
    elif change == 'text_extra':
        ops[t].operation['create']['final_urls'] = ['https://evil.test']
    elif change == 'text_duplicate':
        ops[t+1].operation['create']['text_asset'] = ops[t].operation['create']['text_asset']
    elif change == 'unused_text':
        ops.pop(link)
    elif change.startswith('link_'):
        key, value = {'link_parent': ('asset_group', f'customers/{CID}/assetGroups/99'),
                      'link_owner': ('asset', 'customers/999/assets/70'),
                      'link_role': ('field_type', 'LOGO'), 'link_status': ('status', 'ENABLED'),
                      'link_extra': ('performance_label', 'BEST')}[change]
        ops[link].operation['create'][key] = value
    elif change == 'descriptor':
        plan.post_checks[-1]['expected']['field_type'] = 'HEADLINE'
    elif change == 'descriptor_images':
        plan.post_checks[0]['images']['LOGO']['resource_name'] = 'customers/999/assets/72'
    else:
        ops.append(copy.deepcopy(ops[-1]))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert fake_gads.mutate_calls == []


@pytest.mark.parametrize('change', ['kind', 'owner', 'id', 'duplicate', 'criterion_parent', 'link_parent', 'link_asset', 'link_field'])
def test_ordered_results_rejected_before_postreads(pmax, monkeypatch, change):
    plan = plan_for()
    result, rows = saved_graph(plan)
    entries = result['results']
    link = next(i for i, item in enumerate(entries) if item['type'] == 'asset_group_asset_result')
    if change == 'kind':
        entries[0]['type'] = 'campaign_result'
    elif change == 'owner':
        entries[0]['resource_name'] = 'customers/999/campaignBudgets/100'
    elif change == 'id':
        entries[0]['resource_name'] = f'customers/{CID}/campaignBudgets/0100'
    elif change == 'duplicate':
        entries[-1] = copy.deepcopy(entries[-2])
    elif change == 'criterion_parent':
        entries[2]['resource_name'] = f'customers/{CID}/campaignCriteria/999~102'
    else:
        rn = entries[link]['resource_name']
        parts = rn.rsplit('/', 1)[1].split('~')
        parts[{'link_parent': 0, 'link_asset': 1, 'link_field': 2}[change]] = '999'
        entries[link]['resource_name'] = rn.rsplit('/', 1)[0] + '/' + '~'.join(parts)
    monkeypatch.setattr(client, 'gaql_all', lambda *a: pytest.fail('malformed result caused a read'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, result)


@pytest.mark.parametrize(('entity', 'field', 'value'), [
    ('campaign', 'status', 'FAKE'), ('campaign', 'advertising_channel_type', 999999),
    ('campaign', 'bidding_strategy_type', True), ('campaign', 'contains_eu_political_advertising', 'UNKNOWN'),
    ('campaign', 'brand_guidelines_enabled', False), ('campaign', 'maximize_conversions', {}),
    ('campaign', 'bidding_strategy', None), ('campaign', 'campaign_budget', 'customers/999/campaignBudgets/100'),
    ('campaign', 'geo_target_type_setting', {}), ('campaign', 'asset_automation_settings', []),
    ('campaign_budget', 'period', 'UNKNOWN'), ('campaign_budget', 'amount_micros', '1'),
    ('campaign_budget', 'explicitly_shared', 0), ('asset_group', 'status', 'FAKE'),
    ('asset_group', 'campaign', 'customers/999/campaigns/101'), ('asset_group', 'final_urls', []),
    ('asset', 'type_', 'FAKE'), ('asset', 'text_asset', {'text': 'different'}),
    ('campaign_criterion', 'campaign', 'customers/999/campaigns/101'),
    ('campaign_criterion', 'status', 'PAUSED'), ('campaign_criterion', 'location', {}),
    ('asset_group_asset', 'status', True), ('asset_group_asset', 'field_type', 'FAKE'),
    ('asset_group_asset', 'asset_group', 'customers/999/assetGroups/104'),
    ('campaign_asset', 'asset', 'customers/999/assets/72'),
    ('campaign_asset', 'field_type', 99999), ('campaign_asset', 'status', 'ENABLED'),
])
def test_actual_saved_reader_mismatch_consumed(pmax, monkeypatch, entity, field, value):
    _, fake = pmax
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    row = next(row[entity] for row in rows.values() if entity in row)
    row[field] = value
    use_saved(monkeypatch, rows)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    assert pending['draft_id'] not in rails._DRAFTS
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


@pytest.fixture
def population(monkeypatch):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    account = {'rows': [{'customer': {'id': CID, 'currency_code': 'USD',
               'time_zone': 'America/New_York', 'manager': False, 'status': 'ENABLED'}}],
               'pages_complete': True, 'returned_count': 1, 'total_results_count': 1}
    data = {'campaign': [{'campaign': {'resource_name': f'customers/{CID}/campaigns/8',
                                      'name': 'Other', 'status': 'PAUSED'}}],
            'campaign_budget': [{'campaign_budget': {'resource_name': f'customers/{CID}/campaignBudgets/9',
                                                     'name': 'Other', 'status': 'ENABLED'}}],
            'geo_target_constant': [{'geo_target_constant': {'resource_name': 'geoTargetConstants/2840',
                                                             'name': 'United States', 'status': 'ENABLED'}}],
            'language_constant': [{'language_constant': {'resource_name': 'languageConstants/1000',
                                                         'name': 'English', 'targetable': True}}],
            '70': [{'asset': image_state('70', 600, 314)}],
            '71': [{'asset': image_state('71', 300, 300)}],
            '72': [{'asset': image_state('72', 128, 128)}]}
    calls = []
    monkeypatch.setattr(client, 'account_info', lambda cid: copy.deepcopy(account))
    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        calls.append(query)
        entity = query.split(' FROM ')[1].split()[0]
        key = query.split("resource_name = '")[1].split("'")[0].rsplit('/', 1)[1] if entity == 'asset' else entity
        return copy.deepcopy(data[key])
    monkeypatch.setattr(client, 'gaql_all', read)
    return account, data, calls


def test_real_creation_readers_accept_complete_exact_population(population):
    _, _, calls = population
    result = draft()
    assert len(calls) == 7
    assert result['preview']['account']['manager'] is False
    assert result['preview']['images']['LOGO']['image_asset']['file_size'] == 5000000


@pytest.mark.parametrize(('target', 'field', 'value'), [
    ('account', 'manager', True), ('account', 'manager', None), ('account', 'status', 'FAKE'),
    ('account', 'status', True), ('account', 'status', 999999), ('account', 'id', '999'),
    ('campaign', 'status', 'FAKE'), ('campaign', 'status', 999999), ('campaign', 'status', True),
    ('campaign', 'resource_name', 'customers/999/campaigns/8'), ('campaign', 'name', 'PMax One'),
    ('campaign_budget', 'resource_name', 'customers/999/campaignBudgets/9'),
    ('campaign_budget', 'status', 'FAKE'), ('campaign_budget', 'name', 'PMax One'),
    ('geo_target_constant', 'status', 'FAKE'), ('geo_target_constant', 'status', 999999),
    ('geo_target_constant', 'status', True), ('geo_target_constant', 'resource_name', 'geoTargetConstants/1'),
    ('language_constant', 'targetable', 1), ('language_constant', 'resource_name', 'languageConstants/999'),
    ('70', 'resource_name', 'customers/999/assets/70'), ('70', 'type_', 'TEXT'),
    ('70', 'type_', 'FAKE'), ('70', 'type_', 999999), ('70', 'type_', True),
    ('70', 'image_asset', {}),
])
def test_real_creation_readers_reject_malformed_population(population, target, field, value):
    account, data, _ = population
    item = account['rows'][0]['customer'] if target == 'account' else data[target][0]['asset' if target.isdigit() else target]
    item[field] = value
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('entity', ['campaign', 'campaign_budget', 'geo_target_constant', 'language_constant', '70', '71', '72'])
def test_duplicate_population_rows_fail_closed(population, entity):
    _, data, _ = population
    data[entity].append(copy.deepcopy(data[entity][0]))
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('key', ['pages_complete', 'returned_count', 'total_results_count', 'error'])
def test_account_incomplete_or_error_refuses(population, key):
    account, _, _ = population
    account[key] = False if key == 'pages_complete' else ('failed' if key == 'error' else 2)
    with pytest.raises(rails.RailViolation):
        draft()


def test_population_uses_complete_scan_and_refuses_count_mismatch(monkeypatch, fake_gads):
    from tests.conftest import make_search_response
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(client, '_creation_account', lambda *a, **kw: {'manager': False})
    fake_gads.search_responses[(CID, '')] = make_search_response([], total=1)
    with pytest.raises(rails.RailViolation) as exc:
        draft()
    assert exc.value.code == 'SCAN_INCOMPLETE'
    assert fake_gads.mutate_calls == []


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'error', 'image_drift', 'bad_automation_enum', 'bad_automation_status', 'duplicate_automation'])
def test_postread_failures_consumed_without_retry(pmax, monkeypatch, failure):
    _, fake = pmax
    pending = draft()
    plan = rails._DRAFTS[pending['draft_id']].plan
    fake.dispatch_result, rows = saved_graph(plan)
    if failure in {'missing', 'duplicate', 'error'}:
        def read(*args):
            if failure == 'error':
                raise RuntimeError('saved read failed')
            return [] if failure == 'missing' else [next(iter(rows.values()))]*2
        monkeypatch.setattr(client, 'gaql_all', read)
    else:
        if failure == 'image_drift':
            rows[f'customers/{CID}/assets/72']['asset']['image_asset']['file_size'] = 4999999
        else:
            settings_list = next(row['campaign']['asset_automation_settings'] for row in rows.values() if 'campaign' in row)
            if failure == 'duplicate_automation':
                settings_list[1] = copy.deepcopy(settings_list[0])
            else:
                settings_list[0]['asset_automation_type' if failure == 'bad_automation_enum' else 'asset_automation_status'] = 'FAKE'
        use_saved(monkeypatch, rows)
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1


def test_transport_ambiguity_consumes_once(pmax):
    from tests.test_final_fixes import mapped_error
    _, fake = pmax
    pending = draft()
    fake.dispatch_error = mapped_error('remapped')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(pending['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(pending['draft_id'])
    assert len(fake.dispatch_calls) == 1


def test_actual_mcp_boundary_refuses_coercion_and_preserves_valid_inputs(pmax, monkeypatch):
    from tests.test_protocol_errors import boundary, payload
    for field, value in [('headlines', '["One", "Two", "Three"]'),
                         ('headlines', ['One', 'Two', 3]), ('language_ids', [1000]),
                         ('campaign_name', 123), ('customer_id', False),
                         ('contains_eu_political_advertising', 'false'), ('target_cpa', True)]:
        result = payload(boundary(app.mcp, 'create_pmax_campaign', dict(ARGS, **{field: value})))
        assert result['code'] == 'BAD_INPUT'
    result = boundary(app.mcp, 'create_pmax_campaign', ARGS)
    assert not result.is_error


def test_full_target_population_and_png_images(pmax):
    state, _ = pmax
    for image in state['images'].values():
        image['image_asset']['mime_type'] = 'IMAGE_PNG'
        image['image_asset']['file_size'] = 1
    plan = plan_for(geo_target_ids=[str(i) for i in range(1, 101)],
                    language_ids=[str(i) for i in range(1, 101)])
    assert len([op for op in plan.operations if op.service == 'CampaignCriterionService']) == 200


def test_configured_domain_and_content_rules(pmax, monkeypatch):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: '')
    with pytest.raises(rails.RailViolation):
        draft()
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'example.com')
    monkeypatch.setattr(settings, 'blocked_terms', lambda: ['cool'])
    with pytest.raises(rails.RailViolation):
        draft()


def test_saved_populated_v25_rows_decode_without_credentials(pmax, monkeypatch):
    from tests.conftest import make_type
    plan = plan_for()
    result, rows = saved_graph(plan)
    converted = {}
    for rn, item in rows.items():
        row = make_type('GoogleAdsRow')
        entity, state = next(iter(item.items()))
        setattr(row, entity, state)
        converted[rn] = type(row).to_dict(row)
    use_saved(monkeypatch, converted)
    client.verify_created_results(plan.post_checks, result)


@pytest.mark.parametrize('entity', ['campaign', 'campaign_budget'])
def test_population_noncanonical_owner_resource_refuses(population, entity):
    _, data, _ = population
    data[entity][0][entity]['resource_name'] = f'customers/{CID}/' + ('campaigns/08' if entity == 'campaign' else 'campaignBudgets/09')
    with pytest.raises(rails.RailViolation):
        draft()


def test_validate_only_cannot_enter_saved_proof(pmax, monkeypatch):
    plan = plan_for()
    monkeypatch.setattr(client, 'gaql_all', lambda *a: pytest.fail('validate-only must not read saved state'))
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(plan.post_checks, {'validate_only': True, 'results': []})


def test_malformed_result_is_consumed_unverified(pmax, monkeypatch):
    _, fake = pmax
    pending = draft()
    monkeypatch.setattr(client, 'gaql_all', lambda *a: pytest.fail('malformed result must not read saved state'))
    result = rails.apply_draft(pending['draft_id'])
    assert result['applied'] is True and result['verified'] is False
    assert pending['draft_id'] not in rails._DRAFTS and len(fake.dispatch_calls) == 1
