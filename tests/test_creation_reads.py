"""Complete creation reads and provider result readback, with synthetic rows."""
import copy

import pytest

from mcp_google_ads_safe import client, rails
from tests.test_creation_tools import CID


@pytest.fixture
def creation_reads(monkeypatch, fake_gads):
    account = {'pages_complete': True, 'returned_count': 1, 'total_results_count': 1, 'rows': [{'customer': {'id': CID, 'currency_code': 'USD', 'time_zone': 'UTC'}}]}
    parent = {'resource_name': f'customers/{CID}/campaigns/77', 'name': 'Parent', 'status': 'PAUSED',
              'advertising_channel_type': 'SEARCH', 'advertising_channel_sub_type': 'UNSPECIFIED',
              'campaign_budget': f'customers/{CID}/campaignBudgets/78'}
    rows = {'campaign': [], 'campaign_budget': [], 'ad_group': [],
            'geo_target_constant': [{'geo_target_constant': {'resource_name': 'geoTargetConstants/100', 'name': 'Place', 'status': 'ENABLED'}}],
            'language_constant': [{'language_constant': {'resource_name': 'languageConstants/1000', 'name': 'English', 'targetable': True}}]}
    queries = []
    def read(query, cid):
        queries.append(query)
        assert cid == CID
        if 'WHERE campaign.id = 77' in query:
            return [{'campaign': copy.deepcopy(parent)}]
        entity = query.split(' FROM ')[1].split()[0]
        return copy.deepcopy(rows[entity])
    monkeypatch.setattr(client, 'gaql_all', read)
    monkeypatch.setattr(client, 'account_info', lambda cid: copy.deepcopy(account))
    monkeypatch.setattr(client, 'effective_strategy', lambda *a: {'type': 'MANUAL_CPC', 'portfolio_resource_name': None})
    return account, rows, parent, queries


def test_creation_state_scans_complete_and_filters_locally(creation_reads):
    _, rows, _, queries = creation_reads
    rows['campaign'] = [{'campaign': {'resource_name': f'customers/{CID}/campaigns/20', 'name': 'Unrelated', 'status': 'PAUSED'}}]
    first = client.creation_state(CID, "O'Brien", ['100'], ['1000'])
    rows['campaign'][0]['campaign']['name'] = 'Different unrelated'
    second = client.creation_state(CID, "O'Brien", ['100'], ['1000'])
    assert first == second
    assert all("O'Brien" not in query for query in queries)
    assert first['collisions'] == []


@pytest.mark.parametrize('entity', ['campaign', 'campaign_budget', 'ad_group'])
@pytest.mark.parametrize('damage', ['owner', 'status', 'name', 'duplicate', 'parent', 'resource'])
def test_malformed_name_population(creation_reads, entity, damage):
    _, rows, _, _ = creation_reads
    kind = {'campaign': 'campaigns', 'campaign_budget': 'campaignBudgets', 'ad_group': 'adGroups'}[entity]
    row = {'resource_name': f'customers/{CID}/{kind}/20', 'name': 'Elsewhere', 'status': 'ENABLED', 'campaign': f'customers/{CID}/campaigns/77'}
    if damage == 'owner':
        row['resource_name'] = f'customers/88/{kind}/20'
    elif damage == 'status':
        row['status'] = 'UNKNOWN'
    elif damage == 'name':
        row.pop('name')
    elif damage == 'parent':
        if entity != 'ad_group':
            return
        row['campaign'] = f'customers/{CID}/campaigns/999'
    elif damage == 'resource':
        row['resource_name'] = f'customers/{CID}/{kind}/0'
    rows[entity] = [{entity: row}] * (2 if damage == 'duplicate' else 1)
    with pytest.raises(rails.RailViolation):
        client.creation_state(CID, 'New', ['100'], ['1000'], campaign_id='77' if entity == 'ad_group' else None)


@pytest.mark.parametrize('entity', ['geo_target_constant', 'language_constant'])
@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'extra', 'disabled', 'unnamed'])
def test_constant_reconciliation(creation_reads, entity, damage):
    rows = creation_reads[1]
    row = rows[entity][0][entity]
    if damage == 'missing':
        rows[entity] = []
    elif damage == 'duplicate':
        rows[entity].append(copy.deepcopy(rows[entity][0]))
    elif damage == 'extra':
        row['resource_name'] += '1'
    elif damage == 'disabled':
        row['status' if entity == 'geo_target_constant' else 'targetable'] = 'REMOVAL_PLANNED' if entity == 'geo_target_constant' else False
    else:
        row.pop('name')
    with pytest.raises(rails.RailViolation):
        client.creation_state(CID, 'New', ['100'], ['1000'])


@pytest.mark.parametrize('field,value', [('status','REMOVED'), ('status','UNKNOWN'), ('resource_name','customers/88/campaigns/77'), ('advertising_channel_type','DISPLAY'), ('advertising_channel_sub_type','SEARCH_MOBILE_APP'), ('advertising_channel_sub_type','UNKNOWN'), ('advertising_channel_sub_type',None), ('campaign_budget','customers/88/campaignBudgets/78'), ('name',None)])
def test_parent_gates(creation_reads, field, value):
    creation_reads[2][field] = value
    with pytest.raises(rails.RailViolation):
        client.creation_state(CID, 'New', campaign_id='77')


@pytest.mark.parametrize('field,value', [('id','88'), ('currency_code',None), ('currency_code',''), ('time_zone',None)])
def test_account_read_refusal(creation_reads, field, value):
    creation_reads[0]['rows'][0]['customer'][field] = value
    with pytest.raises(rails.RailViolation):
        client.creation_state(CID, 'New', ['100'], ['1000'])


def test_incomplete_read_refuses(creation_reads, monkeypatch):
    def fail(*a):
        raise rails.RailViolation('count does not reconcile')
    monkeypatch.setattr(client, 'gaql_all', fail)
    with pytest.raises(rails.RailViolation, match='reconcile'):
        client.creation_state(CID, 'New', ['100'], ['1000'])


def test_created_readback_real_v25_enums(fake_gads, monkeypatch):
    from tests.conftest import make_row, make_search_response
    row = make_row(**{'campaign.resource_name': f'customers/{CID}/campaigns/902',
                     'campaign.name': 'New', 'campaign.status': 'PAUSED',
                     'campaign.advertising_channel_type': 'SEARCH',
                     'campaign.bidding_strategy_type': 'MANUAL_CPC',
                     'campaign.contains_eu_political_advertising': 'DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING',
                     'campaign.geo_target_type_setting.positive_geo_target_type': 'PRESENCE',
                     'campaign.geo_target_type_setting.negative_geo_target_type': 'PRESENCE'})
    fake_gads.search_responses[(CID, '')] = make_search_response([row])
    state = client.created_resource_state(CID, 'campaign', f'customers/{CID}/campaigns/902')
    assert state['status'] == 'PAUSED'
    assert state['bidding_strategy_type'] == 'MANUAL_CPC'
    assert state['geo_target_type_setting'] == client.SEARCH_GEO_OPTIONS


def test_real_account_info_envelope(fake_gads):
    from tests.conftest import make_row, make_search_response
    row = make_row(**{'customer.id': int(CID), 'customer.currency_code': 'USD', 'customer.time_zone': 'UTC'})
    fake_gads.search_responses[(CID, '')] = make_search_response([row])
    assert client._creation_account(CID) == {'id': CID, 'currency_code': 'USD', 'time_zone': 'UTC'}


@pytest.mark.parametrize('strategy,field', [('maximize_conversions', 'target_cpa_micros'),
                                          ('maximize_conversion_value', 'target_roas')])
@pytest.mark.parametrize('representation', ['absent', 'empty', 'zero'])
def test_v25_unset_optional_target_normalization(fake_gads, strategy, field, representation):
    from tests.conftest import make_row, make_search_response

    row = make_row(**{'campaign.resource_name': f'customers/{CID}/campaigns/902',
                     'campaign.bidding_strategy_type': strategy.upper()})
    if representation != 'absent':
        setattr(row.campaign, strategy, {} if representation == 'empty' else {field: 0})
    fake_gads.search_responses[(CID, '')] = make_search_response([row])
    state = client.created_resource_state(CID, 'campaign', f'customers/{CID}/campaigns/902')
    if representation == 'absent':
        assert strategy not in state
    else:
        assert state[strategy][field] == ('0' if field.endswith('_micros') else 0.0)
