"""Offline existing-action update checks using the reviewed complete scanner and real v25."""
import copy
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type
from tests.test_conversion_creation import (
    CAM,
    MANAGED,
    RESULT,
    ROOT,
    child,
    customer,
    goal,
)


@pytest.fixture
def primary(monkeypatch, fake_gads):
    monkeypatch.setenv('GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT', 'true')
    fake_gads.login_customer_id = ROOT
    data = {
        (ROOT, 'customer'): [customer(ROOT, manager=True)],
        (ROOT, 'customer_client'): [child(ROOT, CID)],
        (CID, 'customer'): [customer(CID)],
        (CID, 'conversion_action'): [dict(client.CONVERSION_FIXED, name='Lead', category='DEFAULT',
             resource_name=RESULT, owner_customer=f'customers/{CID}', origin='WEBSITE', **copy.deepcopy(MANAGED))],
        (CID, 'campaign'): [dict(resource_name=CAM, id='123', name='Search', status='PAUSED', advertising_channel_type='SEARCH')],
        (CID, 'customer_conversion_goal'): [goal()],
        (CID, 'campaign_conversion_goal'): [goal(campaign=CAM)],
        (CID, 'conversion_goal_campaign_config'): [dict(resource_name=f'customers/{CID}/conversionGoalCampaignConfigs/123',
                campaign=CAM, custom_conversion_goal='', goal_config_level='CUSTOMER')],
    }
    state = {'data': data, 'reads': [], 'after': None}

    def page(query, cid, token):
        assert token is None and 'LIMIT' not in query
        entity = query.split(' FROM ')[1].split()[0]
        state['reads'].append((cid, entity, query))
        rows = copy.deepcopy(data.get((cid, entity), []))
        if fake_gads.mutate_calls:
            if entity == 'conversion_action':
                for row in rows:
                    if row['resource_name'] == RESULT:
                        row['primary_for_goal'] = fake_gads.mutate_calls[-1]['operations'][0].conversion_action_operation.update.primary_for_goal
            if state['after']:
                state['after'](cid, entity, rows, query)
        if entity == 'conversion_action' and '.resource_name =' in query:
            rows = [row for row in rows if row['resource_name'] == RESULT]
        return [{entity: row} for row in rows], None, len(rows)

    monkeypatch.setattr(client, '_search_one_page', page)
    fake_gads.mutate_response = make_type('MutateGoogleAdsResponse')
    fake_gads.mutate_response.mutate_operation_responses.append({'conversion_action_result': {'resource_name': RESULT}})
    return state, fake_gads


def draft(**kwargs):
    return tools.set_conversion_action_primary_status(kwargs.pop('conversion_action_id', '789'), kwargs.pop('primary_for_goal', True), **kwargs)


@pytest.mark.parametrize('desired', [False, True])
def test_exact_real_request(primary, desired):
    state, fake = primary
    state['data'][CID, 'conversion_action'][0]['primary_for_goal'] = not desired
    d = draft(primary_for_goal=desired)
    effects = d['preview']['goal_effects']
    assert effects[0]['ordinary_eligible_after'] is desired
    assert effects[1]['ordinary_eligible_after'] is False  # CUSTOMER config overrides campaign flag
    result = rails.apply_draft(d['draft_id'])
    assert result['applied'] and result['verified']
    assert len(fake.mutate_calls) == 1
    request = fake.mutate_calls[0]
    assert request['customer_id'] == CID and request['partial_failure'] is False
    op = request['operations'][0]
    sub = type(op).deserialize(type(op).serialize(op)).conversion_action_operation
    assert list(sub.update_mask.paths) == ['primary_for_goal']
    assert sub.update.resource_name == RESULT
    assert sub.update.primary_for_goal is desired and sub.update._pb.HasField('primary_for_goal')
    assert {f.name for f, _ in sub.update._pb.ListFields()} == {'resource_name', 'primary_for_goal'}


@pytest.mark.parametrize('field,value', [('conversion_action_id', 789), ('conversion_action_id','0789'), ('conversion_action_id','0'),
 ('conversion_action_id',''), ('conversion_action_id',True), ('conversion_action_id','７８９'),
 ('primary_for_goal',1), ('primary_for_goal','true'), ('primary_for_goal',None),
 ('customer_id',''), ('customer_id',False), ('customer_id','0123')])
def test_strict_inputs_early_audited(primary, field, value):
    with pytest.raises(rails.RailViolation):
        draft(**{field:value})
    assert not primary[0]['reads'] and not primary[1].mutate_calls
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('env,value', [('GOOGLE_ADS_ENABLE_WRITES','false'), ('GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT','false'),
 ('GOOGLE_ADS_READ_CUSTOMER_IDS','456'), ('GOOGLE_ADS_WRITE_CUSTOMER_IDS','456')])
def test_early_gates(primary, monkeypatch, env, value):
    monkeypatch.setenv(env,value)
    with pytest.raises(rails.RailViolation):
        draft()
    assert not primary[0]['reads']
    assert 'refused' in Path(audit.AUDIT_PATH).read_text()


@pytest.mark.parametrize('field,value', [('type_','UPLOAD_CLICKS'), ('type_','UNKNOWN'), ('status','HIDDEN'), ('status','REMOVED'),
 ('primary_for_goal',True), ('primary_for_goal',1), ('owner_customer','customers/456'), ('origin','UNKNOWN')])
def test_action_refusals(primary, field, value):
    primary[0]['data'][CID,'conversion_action'][0][field] = value
    with pytest.raises(rails.RailViolation):
        draft()
    assert not primary[1].mutate_calls


def test_missing(primary):
    with pytest.raises(rails.RailViolation):
        draft(conversion_action_id='888')


def test_no_creation_predictions_any_known_category(primary):
    data = primary[0]['data']
    data[CID,'conversion_action'][0].update(category='PAGE_VIEW', origin='APP')
    for entity, campaign in [('customer_conversion_goal',None),('campaign_conversion_goal',CAM)]:
        data[CID,entity] = [goal(category='PAGE_VIEW',origin='APP',campaign=campaign)]
    d = draft()
    assert len(d['preview']['goal_effects']) == 2
    assert all(item['goal']['origin'] == 'APP' for item in d['preview']['goal_effects'])
    assert rails.apply_draft(d['draft_id'])['verified']


@pytest.mark.parametrize('entity', ['customer_conversion_goal','campaign_conversion_goal','conversion_goal_campaign_config'])
def test_complete_coverage(primary, entity):
    primary[0]['data'][CID,entity] = []
    with pytest.raises(rails.RailViolation):
        draft()


def add_custom(primary):
    data = primary[0]['data']
    rn = f'customers/{CID}/customConversionGoals/321'
    data[CID,'custom_conversion_goal'] = [dict(resource_name=rn,id='321',name='Custom',status='ENABLED',conversion_actions=[RESULT])]
    data[CID,'conversion_goal_campaign_config'][0].update(custom_conversion_goal=rn,goal_config_level='CAMPAIGN')
    return rn


def test_custom_exception_and_campaign_config(primary):
    rn = add_custom(primary)
    d = draft()
    membership = d['preview']['custom_membership'][0]
    assert membership['goal']['resource_name'] == rn
    assert membership['campaigns'][0]['campaign']['status'] == 'PAUSED'
    assert d['preview']['goal_effects'][1]['ordinary_eligible_after'] is True
    assert any('regardless of primary status' in text for text in d['preview']['warnings'])


@pytest.mark.parametrize('entity,field,value', [('conversion_action','name','Changed'), ('conversion_action','value_settings',dict(MANAGED['value_settings'],default_value=9)),
 ('customer_conversion_goal','biddable',False), ('campaign_conversion_goal','biddable',False),
 ('conversion_goal_campaign_config','goal_config_level','CAMPAIGN'), ('campaign','status','ENABLED'),
 ('custom_conversion_goal','name','Changed')])
def test_apply_and_saved_drift(primary, entity, field, value):
    add_custom(primary)
    if entity == 'conversion_goal_campaign_config':
        primary[0]['data'][CID,entity][0]['goal_config_level'] = 'CUSTOMER'
    d = draft()
    primary[0]['data'][CID,entity][0][field] = value
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not primary[1].mutate_calls


@pytest.mark.parametrize('entity,field,value', [('conversion_action','name','Changed'), ('conversion_action','primary_for_goal',False),
 ('conversion_action','value_settings',dict(MANAGED['value_settings'],default_value=9)),
 ('campaign_conversion_goal','biddable',False), ('customer_conversion_goal','biddable',False),
 ('conversion_goal_campaign_config','goal_config_level','CAMPAIGN'), ('campaign','status','ENABLED'),
 ('custom_conversion_goal','name','Changed')])
def test_postcheck_drift_second_target_read_consumed(primary, entity, field, value):
    add_custom(primary)
    if entity == 'conversion_goal_campaign_config':
        primary[0]['data'][CID,entity][0]['goal_config_level'] = 'CUSTOMER'
    d = draft()
    def change(cid, observed_entity, rows, query):
        if observed_entity == entity and '.resource_name =' not in query:
            rows[0][field] = value
    primary[0]['after'] = change
    out = rails.apply_draft(d['draft_id'])
    assert out['applied'] and not out['verified']
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(primary[1].mutate_calls) == 1


@pytest.mark.parametrize('result', [{'results':[]}, {'results':[{'type':'conversion_action_result','resource_name':f'customers/{CID}/conversionActions/999'}]},
 {'results':[{'type':'campaign_result','resource_name':CAM}]}])
def test_result_identity_before_reads(primary, result):
    compiled = rails.compile(rails.SetConversionActionPrimaryStatusIntent(CID,'789',True))
    primary[0]['reads'].clear()
    with pytest.raises(rails.RailViolation):
        client.verify_created_results(compiled.plan.post_checks, result)
    assert not primary[0]['reads']


def test_validate_only(primary):
    d = draft()
    result = client._dispatch_entity(rails._DRAFTS[d['draft_id']].plan, True)
    assert not result.get('applied')
    assert not any('.resource_name =' in query for _,_,query in primary[0]['reads'])


@pytest.mark.parametrize('values,mask', [({'resource_name':RESULT,'primary_for_goal':1},['primary_for_goal']),
 ({'resource_name':RESULT,'primary_for_goal':False,'name':'extra'},['primary_for_goal']),
 ({'resource_name':RESULT,'primary_for_goal':False},['primary_for_goal','name']),
 ({'resource_name':RESULT,'primary_for_goal':False},('primary_for_goal',)),
 ({'resource_name':RESULT,'primary_for_goal':False},[]),
 ({'resource_name':RESULT.replace('/789','/0789'),'primary_for_goal':False},['primary_for_goal']),
 ({'resource_name':'customers/456/conversionActions/789','primary_for_goal':False},['primary_for_goal'])])
def test_closed_dispatch_before_provider(monkeypatch, values, mask):
    monkeypatch.setattr(client,'gads',lambda: pytest.fail('provider accessed'))
    plan = rails.EntityMutationPlan(CID,[rails.MutationOp('ConversionActionService',{'update':values},mask)],True)
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan,False)


def test_mixed_plan_and_remove_refuse(monkeypatch):
    monkeypatch.setattr(client,'gads',lambda: pytest.fail('provider accessed'))
    op = rails.MutationOp('ConversionActionService',{'update':{'resource_name':RESULT,'primary_for_goal':True}},['primary_for_goal'])
    for ops in [[op,op],[rails.MutationOp('ConversionActionService',{'remove':RESULT},None)]]:
        with pytest.raises(rails.RailViolation):
            client._dispatch_entity(rails.EntityMutationPlan(CID,ops,True),False)


@pytest.mark.parametrize('value', [0, 1, 'true', 'false'])
def test_mcp_strict_bool(primary, value):
    import asyncio

    from mcp_google_ads_safe.app import mcp
    async def check():
        try:
            result = await mcp.call_tool('set_conversion_action_primary_status',
                                         {'conversion_action_id': '789', 'primary_for_goal': value})
        except Exception:
            pass  # SDK tool validation reports invalid arguments as an exception.
        else:
            assert getattr(result, 'is_error', getattr(result, 'isError', False))
    asyncio.run(check())
    assert not primary[0]['reads'] and not primary[1].mutate_calls


def test_unknown_consumed_audited(primary, monkeypatch):
    d = draft()
    error = rails.UnknownWriteOutcome('unknown',request_id='req',failure={'code':'X'})
    monkeypatch.setattr(client,'_dispatch',lambda *args: (_ for _ in ()).throw(error))
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(d['draft_id'])
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert 'unknown' in Path(audit.AUDIT_PATH).read_text()


def test_plan_tamper(primary):
    d = draft()
    rails._DRAFTS[d['draft_id']].plan.operations[0].operation['update']['primary_for_goal'] = False
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not primary[1].mutate_calls


def test_scope_sibling_unallowlisted(primary):
    data = primary[0]['data']
    data[ROOT,'customer_client'].append(child(ROOT,'456'))
    data['456','customer'] = [customer('456',CID)]
    with pytest.raises(rails.RailViolation,match='tracking customer'):
        draft()


def test_scope_unreadable_branch(primary):
    primary[0]['data'][ROOT,'customer_client'].append(child(ROOT,'456'))
    with pytest.raises(rails.RailViolation):
        draft()


def test_scope_postwrite_race(primary):
    d = draft()
    def change(cid, entity, rows, query):
        if entity == 'customer' and cid == ROOT:
            rows[0]['status'] = 'CANCELED'
    primary[0]['after'] = change
    assert rails.apply_draft(d['draft_id'])['verified'] is False


@pytest.mark.parametrize('change', ['expiry','digest','gate','tracking_owner','tree','missing_custom_ref','attribution'])
def test_confirm_drift_and_expiry(primary, monkeypatch, change):
    add_custom(primary)
    d = draft()
    saved = rails._DRAFTS[d['draft_id']]
    data = primary[0]['data']
    if change == 'expiry':
        saved.created_at -= 4000
    if change == 'digest':
        saved.digest = 'wrong'
    if change == 'gate':
        monkeypatch.setenv('GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT','false')
    if change == 'tracking_owner':
        data[CID,'customer'][0]['conversion_tracking_setting']['google_ads_conversion_customer'] = 'customers/456'
    if change == 'tree':
        data[ROOT,'customer_client'].append(child(ROOT,'456'))
    if change == 'missing_custom_ref':
        data[CID,'custom_conversion_goal'][0]['conversion_actions'] = []
    if change == 'attribution':
        data[CID,'conversion_action'][0]['attribution_model_settings']['attribution_model'] = 'EXTERNAL'
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not primary[1].mutate_calls


def test_selected_tracking_account_routes_to_owner(primary, monkeypatch):
    data = primary[0]['data']
    data[ROOT,'customer_client'].append(child(ROOT,'456'))
    data['456','customer'] = [customer('456',CID)]
    data['456','customer_conversion_goal'] = [goal('456')]
    monkeypatch.setenv('GOOGLE_ADS_READ_CUSTOMER_IDS',f'{CID},456')
    monkeypatch.setenv('GOOGLE_ADS_WRITE_CUSTOMER_IDS',f'{CID},456')
    d = draft(customer_id='456')
    assert d['preview']['customer_id'] == '456'
    assert d['preview']['mutate_customer_id'] == CID
    assert rails.apply_draft(d['draft_id'])['verified']
    assert primary[1].mutate_calls[0]['customer_id'] == CID


def test_postcheck_exact_mismatch(primary):
    d = draft()
    def change(cid, entity, rows, query):
        if entity == 'conversion_action' and '.resource_name =' in query:
            rows[0]['name'] = 'Changed'
    primary[0]['after'] = change
    assert rails.apply_draft(d['draft_id'])['verified'] is False


def test_postcheck_unreadable(primary):
    d = draft()
    def change(cid, entity, rows, query):
        raise RuntimeError('unreadable')
    primary[0]['after'] = change
    result = rails.apply_draft(d['draft_id'])
    assert result['applied'] and not result['verified']
    assert len(primary[1].mutate_calls) == 1
