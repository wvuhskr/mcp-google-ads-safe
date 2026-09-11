import pytest

from mcp_google_ads_safe import client, rails
from tests.conftest import make_type

CID = '1234567890'


def criterion(service='AdGroupCriterionService', **fields):
    parent = {'ad_group': f'customers/{CID}/adGroups/2'} if service.startswith('AdGroup') else {
        'campaign': f'customers/{CID}/campaigns/1'}
    return rails.MutationOp(service, {'create': {**parent, **fields}}, None)


def test_atomic_create_update_remove(fake_gads):
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    ops = [
        rails.safe_create_operation('AdGroupCriterionService', {
            'ad_group': f'customers/{CID}/adGroups/2',
            'keyword': {'text': 'plumber', 'match_type': 'EXACT'}, 'status': 'ENABLED'}),
        criterion('CampaignCriterionService', negative=True,
                  keyword={'text': 'jobs', 'match_type': 'BROAD'}),
        criterion('CampaignCriterionService', ad_schedule={
            'day_of_week': 'MONDAY', 'start_hour': 8, 'end_hour': 17,
            'start_minute': 'ZERO', 'end_minute': 'ZERO'}),
        criterion('CampaignCriterionService', location={'geo_target_constant': 'geoTargetConstants/2840'}),
        rails.MutationOp('CampaignCriterionService', {
            'remove': f'customers/{CID}/campaignCriteria/1~3'}, None),
        rails.MutationOp('CampaignService', {'update': {
            'resource_name': f'customers/{CID}/campaigns/1',
            'maximize_conversions': {'target_cpa_micros': 0}}},
            ['maximize_conversions.target_cpa_micros']),
    ]
    client._dispatch(rails.EntityMutationPlan(CID, ops, True), validate_only=True)
    call, = fake_gads.mutate_calls
    assert call['partial_failure'] is False
    assert call['validate_only'] is True
    built = call['operations']
    assert len(built) == 6
    assert built[0].ad_group_criterion_operation.create.status.name == 'PAUSED'
    assert built[1].campaign_criterion_operation.create.negative
    assert built[2].campaign_criterion_operation.create.ad_schedule.start_hour == 8
    assert built[3].campaign_criterion_operation.create.location.geo_target_constant == 'geoTargetConstants/2840'
    assert built[4].campaign_criterion_operation.remove.endswith('/1~3')
    assert list(built[5].campaign_operation.update_mask.paths) == ['maximize_conversions.target_cpa_micros']


@pytest.mark.parametrize('op', [
    rails.MutationOp('UnknownService', {'remove': f'customers/{CID}/campaigns/1'}, None),
    rails.MutationOp('CampaignService', {'remove': f'customers/{CID}/adGroups/1'}, None),
    rails.MutationOp('CampaignService', {'remove': 'customers/999/campaigns/1'}, None),
    rails.MutationOp('CampaignService', {'remove': f'customers/{CID}/campaigns/x1'}, None),
    rails.MutationOp('CampaignService', {'remove': f'customers/{CID}/campaigns/1'}, ['status']),
    rails.MutationOp('CampaignService', {'create': {}, 'update': {}}, None),
    criterion(ad_group='customers/999/adGroups/2', keyword={'text': 'a', 'match_type': 'EXACT'}),
    criterion(keyword={'text': 'a', 'match_type': 'EXACT', 'resource_name': 'customers/999/campaigns/1'}),
    criterion('CampaignCriterionService', location={'geo_target_constant': 'customers/999/geoTargetConstants/1'}),
    criterion(keyword={'text': 'a', 'match_type': 'EXACT'}, mystery=True),
])
def test_bad_action_refused_before_mutate(fake_gads, op):
    valid = criterion(keyword={'text': 'ok', 'match_type': 'EXACT'}, status='PAUSED')
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.EntityMutationPlan(CID, [valid, op], True))
    assert fake_gads.mutate_calls == []


def test_empty_plan_refused(fake_gads):
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.EntityMutationPlan(CID, [], True))
    assert not fake_gads.mutate_calls


def test_paths_do_not_load_credentials(monkeypatch):
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('path helper loaded credentials'))
    assert client.campaign_path(CID, 1) == f'customers/{CID}/campaigns/1'
    assert client.ad_group_path(CID, 2) == f'customers/{CID}/adGroups/2'
    assert client.campaign_criterion_path(CID, 1, 3) == f'customers/{CID}/campaignCriteria/1~3'
    assert client.ad_group_criterion_path(CID, 2, 3) == f'customers/{CID}/adGroupCriteria/2~3'
    assert client.geo_target_constant_path(2840) == 'geoTargetConstants/2840'
    for bad in ['id1', '1-2', '', True, -1, 1.5, '１２']:
        with pytest.raises(rails.RailViolation):
            client.campaign_path(CID, bad)


def test_safe_create_only_pauses_positive_keywords():
    fields = {'keyword': {'text': 'a', 'match_type': 'EXACT'}, 'status': 'ENABLED'}
    assert rails.safe_create_operation('AdGroupCriterionService', fields).operation['create']['status'] == 'PAUSED'
    assert fields['status'] == 'ENABLED'
    for fields in [{'negative': True, 'keyword': {'text': 'a'}}, {'ad_schedule': {}}, {'location': {}}]:
        assert 'status' not in rails.safe_create_operation('CampaignCriterionService', fields).operation['create']


@pytest.mark.parametrize('fields', [
    {'keyword': {'text': 'jobs', 'match_type': 'BROAD'}, 'negative': True,
     'location': {'geo_target_constant': 'geoTargetConstants/2840'}},
    {'keyword': {'text': 'jobs', 'match_type': 'BROAD'}},
    {'keyword': {'text': 'jobs', 'match_type': 'BROAD'}, 'negative': 'false'},
])
def test_ambiguous_create_refused(fake_gads, fields):
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.EntityMutationPlan(
            CID, [criterion('CampaignCriterionService', **fields)], True))
    assert not fake_gads.mutate_calls


def test_compile_validates_before_preview(fake_client):
    fake_client.status_info['resource_name'] = f'customers/{CID}/campaigns/x1'
    with pytest.raises(rails.RailViolation):
        rails.compile(rails.SetEntityStatusIntent(CID, 'campaign', '1', 'PAUSED'))
    assert not fake_client.dispatch_calls


def test_build_failure_cannot_send_valid_prior_operation(fake_gads):
    valid = criterion(keyword={'text': 'ok', 'match_type': 'EXACT'}, status='PAUSED')
    bad = criterion(keyword={'text': 'bad', 'match_type': 'INVALID'})
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.EntityMutationPlan(CID, [valid, bad], True))
    assert not fake_gads.mutate_calls


@pytest.mark.parametrize('service,resource', [
    ('CampaignService', 'campaigns/1'), ('AdGroupService', 'adGroups/2'),
    ('CampaignBudgetService', 'campaignBudgets/3'), ('BiddingStrategyService', 'biddingStrategies/4'),
    ('CampaignCriterionService', 'campaignCriteria/1~5'),
    ('AdGroupCriterionService', 'adGroupCriteria/2~6'),
])
def test_remove_shapes(fake_gads, service, resource):
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    rn = f'customers/{CID}/{resource}'
    post_checks = []
    if service in {'CampaignService', 'AdGroupService'}:
        entity_type = 'campaign' if service == 'CampaignService' else 'ad_group'
        post_checks = [{'removal': True, 'entity_type': entity_type, 'customer_id': CID,
                        'resource_name': rn,
                        'parent_resource_name': (None if service == 'CampaignService'
                                                 else f'customers/{CID}/campaigns/1')}]
    client._dispatch(rails.EntityMutationPlan(
        CID, [rails.MutationOp(service, {'remove': rn}, None)], True,
        post_checks=post_checks))
    call, = fake_gads.mutate_calls
    operation, = call['operations']
    field = operation._pb.WhichOneof('operation')
    assert getattr(operation, field).remove == rn


def test_batch_validation_precedes_client_creation(monkeypatch):
    monkeypatch.setattr(client, 'gads', lambda: pytest.fail('client created before validation'))
    valid = criterion(keyword={'text': 'ok', 'match_type': 'EXACT'}, status='PAUSED')
    bad = criterion(ad_group='customers/999/adGroups/2', keyword={'text': 'bad', 'match_type': 'EXACT'})
    with pytest.raises(rails.RailViolation):
        client._dispatch(rails.EntityMutationPlan(CID, [valid, bad], True))
