"""Offline real-message checks for bounded campaign audience attachment."""
import copy

import pytest

from mcp_google_ads_safe import client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

CAM = f'customers/{CID}/campaigns/123'
LIST = f'customers/{CID}/userLists/456'
RESULT = f'customers/{CID}/campaignCriteria/123~789'
REAL_GAQL_ALL = client.gaql_all


@pytest.fixture
def target(monkeypatch, fake_gads):
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT', 'true')
    data = {'campaign': {'resource_name': CAM, 'name': 'Search', 'status': 'PAUSED',
        'advertising_channel_type': 'SEARCH', 'advertising_channel_sub_type': 'UNSPECIFIED',
        'campaign_budget': f'customers/{CID}/campaignBudgets/1'},
        'mode': [{'targeting_dimension': 'AUDIENCE', 'bid_only': True}], 'groups': [],
        'list': {'resource_name': LIST, 'name': 'Visitors', 'type_': 'RULE_BASED',
            'membership_status': 'OPEN', 'access_reason': 'OWNED', 'read_only': False,
            'rule_based_user_list': client.audience_rules(['/service'])},
        'inventory': [], 'usage': [], 'group_usage': [], 'saved': [], 'reads': []}
    monkeypatch.setattr(client, '_creation_account', lambda cid: {'id': CID})
    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        data['reads'].append(query)
        if 'FROM user_list' in query:
            return [{'user_list': copy.deepcopy(data['list'])}]
        if 'FROM ad_group_criterion' in query:
            return copy.deepcopy(data['group_usage'])
        if 'FROM campaign_criterion' in query:
            key = 'saved' if '.resource_name =' in query else 'usage' if '.user_list.user_list =' in query else 'inventory'
            rows = copy.deepcopy(data[key])
            if key in {'usage', 'inventory'} and fake_gads.mutate_calls:
                rows += copy.deepcopy(data['saved'])
            return rows
        if 'FROM ad_group' in query:
            return copy.deepcopy(data['groups'])
        if 'targeting_setting' in query:
            return [{'campaign': {'resource_name': CAM, 'targeting_setting': {'target_restrictions': copy.deepcopy(data['mode'])}}}]
        return [{'campaign': copy.deepcopy(data['campaign'])}]
    monkeypatch.setattr(client, 'gaql_all', read)
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    fake_gads.mutate_response.mutate_operation_responses.append({'campaign_criterion_result': {'resource_name': RESULT}})
    data['saved'] = [{'campaign_criterion': {'resource_name': RESULT, 'campaign': CAM,
        'type_': 'USER_LIST', 'status': 'PAUSED', 'negative': False, 'user_list': {'user_list': LIST}}}]
    return data, fake_gads


def draft(mode='OBSERVATION', **kwargs):
    return tools.add_audience_targeting('123', '456', mode, **kwargs)


@pytest.mark.parametrize('mode', ['OBSERVATION', 'TARGETING'])
def test_real_request(target, mode):
    data, fake = target
    data['mode'][0]['bid_only'] = mode == 'OBSERVATION'
    d = draft(mode)
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and out['verified']
    assert len(fake.mutate_calls) == 1 and fake.mutate_calls[0]['partial_failure'] is False
    op = fake.mutate_calls[0]['operations'][0]
    decoded = type(op).deserialize(type(op).serialize(op))
    obj = decoded.campaign_criterion_operation.create
    assert obj.campaign == CAM and obj.user_list.user_list == LIST
    assert obj.status.name == 'PAUSED' and not obj.negative


@pytest.mark.parametrize('mode', [None, '', 'observation', False, 1, {}, 'DISPLAY'])
def test_bad_mode(target, mode):
    with pytest.raises(rails.RailViolation):
        draft(mode)
    assert not target[0]['reads']


@pytest.mark.parametrize('value', [None, [], [{'targeting_dimension': 'AUDIENCE', 'bid_only': 1}],
    [{'targeting_dimension': 'AUDIENCE', 'bid_only': 'true'}], [{'targeting_dimension': 'UNKNOWN', 'bid_only': True}],
    [{'targeting_dimension': 'AUDIENCE'}], [{'targeting_dimension': 'AUDIENCE', 'bid_only': False}],
    [{'targeting_dimension': 'AUDIENCE', 'bid_only': True}]*2])
def test_mode_refusals(target, value):
    target[0]['mode'] = value
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('field,value', [('status','ENABLED'), ('advertising_channel_type','DISPLAY'),
    ('advertising_channel_sub_type','SEARCH_MOBILE_APP'), ('resource_name','customers/2/campaigns/123')])
def test_parent_refusals(target, field, value):
    target[0]['campaign'][field] = value
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('field,value', [('membership_status','CLOSED'),('read_only',True),
    ('access_reason','SHARED'),('type_','CRM_BASED'),('rule_based_user_list',{}),('resource_name','customers/2/userLists/456')])
def test_list_refusals(target, field, value):
    target[0]['list'][field] = value
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES','false'),
    ('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT','false'),('GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT','junk'),
    ('GOOGLE_ADS_READ_CUSTOMER_IDS','2'),('GOOGLE_ADS_WRITE_CUSTOMER_IDS','2')])
def test_gates(target, monkeypatch, env, value):
    d = draft()
    target[0]['reads'].clear()
    monkeypatch.setenv(env,value)
    for call in (draft, lambda: rails.apply_draft(d['draft_id'])):
        with pytest.raises(rails.RailViolation):
            call()
    assert not target[0]['reads'] and not target[1].mutate_calls


@pytest.mark.parametrize('field', ['campaign_id','audience_id','customer_id'])
@pytest.mark.parametrize('value', ['', '0', '-1', '１２３', True, 1, '1 2'])
def test_ids(target, field, value):
    args = dict(campaign_id='123', audience_id='456', targeting_mode='OBSERVATION')
    args[field] = value
    with pytest.raises(rails.RailViolation):
        tools.add_audience_targeting(**args)
    assert not target[0]['reads']


@pytest.mark.parametrize('negative', [True, False])
def test_duplicate(target, negative):
    target[0]['inventory'] = copy.deepcopy(target[0]['saved'])
    target[0]['inventory'][0]['campaign_criterion']['negative'] = negative
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('settings', [None, {}, {'target_restrictions':[{'targeting_dimension':'AUDIENCE','bid_only':True}]}])
def test_group_conflict(target, settings):
    target[0]['groups'] = [{'ad_group': {'resource_name': f'customers/{CID}/adGroups/9',
        'campaign': CAM, 'targeting_setting': settings}}]
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('damage', ['mixed','mask','negative','status','resource_name','bid_modifier','user_list','campaign','update'])
def test_closed_dispatch(target, monkeypatch, damage):
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    op = plan.operations[0]
    if damage == 'mixed':
        plan.operations.append(copy.deepcopy(op))
    elif damage == 'mask':
        object.__setattr__(op, 'update_mask', ['status'])
    elif damage == 'update':
        op.operation['update'] = op.operation.pop('create')
    else:
        op.operation['create'][damage] = {'negative':True, 'status':'ENABLED',
            'user_list':{'user_list':'customers/2/userLists/456'}, 'campaign':f'customers/{CID}/campaigns/-1'}.get(damage,'unexpected')
    monkeypatch.setattr(client,'gads',lambda:pytest.fail('provider constructed'))
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan,False)


@pytest.mark.parametrize('rn', [f'customers/{CID}/campaignCriteria/999~789',f'customers/{CID}/campaignCriteria/123~0','customers/2/campaignCriteria/123~789'])
def test_result_before_reads(target, rn):
    data,fake=target
    d=draft()
    checks=rails._DRAFTS[d['draft_id']].plan.post_checks
    data['reads'].clear()
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(checks, {'results':[{'type':'campaign_criterion_result','resource_name':rn}]})
    assert not data['reads']


@pytest.mark.parametrize('field,value', [('negative', True),('status','ENABLED'),('user_list',{'user_list':f'customers/{CID}/userLists/999'}),('type_','KEYWORD')])
def test_saved_failures_consumed(target, field, value):
    data,fake=target
    d=draft()
    data['saved'][0]['campaign_criterion'][field]=value
    out=rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified']
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls)==1


def test_drift_and_validate_only(target):
    data,fake=target
    d=draft()
    data['list']['name']='Changed'
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    d=draft()
    data['reads'].clear()
    assert client._dispatch_entity(rails._DRAFTS[d['draft_id']].plan,True)['validate_only']
    assert not data['reads']


def test_connections_preview_and_postcheck(target):
    data, fake = target
    row = copy.deepcopy(data['saved'][0])
    row['campaign_criterion'].update(resource_name=f'customers/{CID}/campaignCriteria/88~9',campaign=f'customers/{CID}/campaigns/88')
    data['usage']=[row]
    data['group_usage']=[{'ad_group_criterion':{'resource_name':f'customers/{CID}/adGroupCriteria/77~9',
        'ad_group':f'customers/{CID}/adGroups/77','type_':'USER_LIST','status':'ENABLED','negative':False,
        'user_list':{'user_list':LIST}},'ad_group':{'resource_name':f'customers/{CID}/adGroups/77','campaign':f'customers/{CID}/campaigns/66'}}]
    d=draft()
    preview=rails._DRAFTS[d['draft_id']].preview
    assert set(preview['attached_campaigns'])=={CAM,f'customers/{CID}/campaigns/88',f'customers/{CID}/campaigns/66'}
    assert len(preview['existing_connections'])==2
    assert rails.apply_draft(d['draft_id'])['verified']


@pytest.mark.parametrize('kind', ['campaign','mode','list','groups','inventory','usage'])
def test_snapshot_drift(target, kind):
    data,fake=target
    d=draft()
    if kind=='campaign':
        data[kind]['name']='Changed'
    elif kind=='mode':
        data[kind].append({'targeting_dimension':'AGE_RANGE','bid_only':True})
    elif kind=='list':
        data[kind]['name']='Changed'
    elif kind=='groups':
        data[kind].append({'ad_group':{'resource_name':f'customers/{CID}/adGroups/9','campaign':CAM,
                                     'targeting_setting':{'target_restrictions':[]}}})
    else:
        row=copy.deepcopy(data['saved'][0])
        if kind=='inventory':
            row['campaign_criterion']['user_list']['user_list']=f'customers/{CID}/userLists/999'
        data[kind].append(row)
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not fake.mutate_calls


@pytest.mark.parametrize('kind', ['campaign','mode','list'])
def test_post_dispatch_race_consumed(target, monkeypatch, kind):
    data,fake=target
    d=draft()
    original=client.verify_created_results
    def verify(checks,result):
        if kind=='campaign':
            data[kind]['status']='ENABLED'
        elif kind=='mode':
            data[kind][0]['bid_only']=False
        else:
            data[kind]['name']='Changed'
        return original(checks,result)
    monkeypatch.setattr(client,'verify_created_results',verify)
    out=rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified'] and len(fake.mutate_calls)==1
    assert d['draft_id'] not in rails._DRAFTS


def test_tamper_expiry_and_read_error(target, monkeypatch):
    d=draft()
    rails._DRAFTS[d['draft_id']].plan.operations[0].operation['create']['negative']=True
    with pytest.raises(rails.RailViolation,match='modified|tamper|digest'):
        rails.apply_draft(d['draft_id'])
    d=draft()
    rails._DRAFTS[d['draft_id']].created_at-=4000
    with pytest.raises(rails.RailViolation,match='expired'):
        rails.apply_draft(d['draft_id'])
    monkeypatch.setattr(client,'gaql_all',lambda *a: (_ for _ in ()).throw(rails.RailViolation('incomplete pages')))
    with pytest.raises(rails.RailViolation,match='incomplete'):
        draft()
    assert not target[1].mutate_calls


def test_narrow_paused_helper():
    op=rails.safe_create_operation('CampaignCriterionService',{'user_list':{'user_list':LIST},'status':'ENABLED'})
    assert op.operation['create']['status']=='PAUSED'
    op=rails.safe_create_operation('CampaignCriterionService',{'negative':True,'location':{'geo_target_constant':'geoTargetConstants/1'}})
    assert 'status' not in op.operation['create']


@pytest.mark.parametrize('damage', ['lookback','operator','unknown','exclusion'])
def test_invalid_list_rules(target, damage):
    rules=target[0]['list']['rule_based_user_list']
    flex=rules['flexible_rule_user_list']
    if damage=='lookback':
        flex['inclusive_operands'][0]['lookback_window_days']=0
    elif damage=='operator':
        flex['inclusive_rule_operator']='UNSPECIFIED'
    elif damage=='exclusion':
        flex['exclusive_operands']=copy.deepcopy(flex['inclusive_operands'])
    else:
        rules['unknown']='bad'
    with pytest.raises(rails.RailViolation):
        draft()


@pytest.mark.parametrize('total', [1,2])
def test_complete_connection_reader(target, monkeypatch, total):
    from tests.conftest import make_search_response
    data,fake=target
    row=make_type('GoogleAdsRow')
    row.campaign_criterion=data['saved'][0]['campaign_criterion']
    fake.search_responses[(CID,'')]=make_search_response([row],total=total)
    monkeypatch.setattr(client,'gaql_all',REAL_GAQL_ALL)
    if total==1:
        assert len(client._audience_connections(CID,'campaign_criterion',f"campaign_criterion.campaign = '{CAM}'"))==1
    else:
        with pytest.raises(rails.RailViolation):
            client._audience_connections(CID,'campaign_criterion',f"campaign_criterion.campaign = '{CAM}'")
    assert fake.search_requests[0].search_settings.return_total_results_count


@pytest.mark.parametrize('scan', ['inventory', 'usage'])
@pytest.mark.parametrize('field,value', [('status', 'ENABLED'), ('negative', True),
    ('user_list', {'user_list': f'customers/{CID}/userLists/999'}),
    ('campaign', f'customers/{CID}/campaigns/999'), ('type_', 'KEYWORD')])
def test_later_created_connection_race_consumed(target, monkeypatch, scan, field, value):
    data, fake = target
    d = draft()
    original = client.gaql_all

    def read(query, cid):
        rows = original(query, cid)
        is_usage = '.user_list.user_list =' in query
        if (fake.mutate_calls and 'FROM campaign_criterion' in query
                and '.resource_name =' not in query and is_usage == (scan == 'usage')):
            for row in rows:
                if row['campaign_criterion']['resource_name'] == RESULT:
                    row['campaign_criterion'][field] = value
        return rows

    monkeypatch.setattr(client, 'gaql_all', read)
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified']
    assert data['saved'][0]['campaign_criterion']['status'] == 'PAUSED'
    assert data['saved'][0]['campaign_criterion']['negative'] is False
    assert d['draft_id'] not in rails._DRAFTS
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls) == 1
