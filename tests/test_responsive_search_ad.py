"""One bounded RSA flow, using real v25 messages and no provider connection."""
import copy
import json
from pathlib import Path

import pytest

from mcp_google_ads_safe import audit, client, rails, settings, tools
from tests.conftest import TEST_CUSTOMER_ID as CID
from tests.conftest import make_type

GROUP = f'customers/{CID}/adGroups/77'
CAMPAIGN = f'customers/{CID}/campaigns/88'
AD = f'customers/{CID}/adGroupAds/77~99'


def draft(**changes):
    args = dict(ad_group_id='77', headlines=['Fast Cooling', 'Local Team', 'Book Service'],
                descriptions=['Schedule service today.', 'Help with home cooling.'],
                final_url='https://example.com/service?source=ad', path1='service')
    return tools.draft_responsive_search_ad(**dict(args, **changes))


@pytest.fixture
def rsa(monkeypatch, fake_gads):
    data = {
        'ad_group': {'resource_name': GROUP, 'campaign': CAMPAIGN, 'status': 'PAUSED', 'type_': 'SEARCH_STANDARD'},
        'campaign': {'resource_name': CAMPAIGN, 'status': 'ENABLED', 'advertising_channel_type': 'SEARCH',
                     'advertising_channel_sub_type': 'UNSPECIFIED'},
        'customer': {'id': CID, 'currency_code': 'USD', 'time_zone': 'America/New_York'},
        'ads': [], 'saved': None, 'queries': [],
    }

    def read(query, cid):
        assert cid == CID and 'LIMIT' not in query
        data['queries'].append(query)
        if 'FROM ad_group_ad' in query:
            return copy.deepcopy([{'ad_group_ad': data['saved']}] if '.resource_name =' in query else data['ads'])
        entity = 'ad_group' if 'FROM ad_group ' in query else 'campaign'
        return [{entity: copy.deepcopy(data[entity])}]

    monkeypatch.setattr(client, 'gaql_all', read)
    monkeypatch.setattr(client, 'account_info', lambda cid: {'rows': [{'customer': copy.deepcopy(data['customer'])}],
                        'pages_complete': True, 'returned_count': 1, 'total_results_count': 1})
    return data, fake_gads


def landed(data, fake, plan):
    data['saved'] = copy.deepcopy(plan.operations[0].operation['create'])
    data['saved']['resource_name'] = AD
    data['saved']['ad']['type_'] = 'RESPONSIVE_SEARCH_AD'
    response = make_type('MutateGoogleAdsResponse')
    response.mutate_operation_responses.append({'ad_group_ad_result': {'resource_name': AD}})
    fake.mutate_response = response


def test_real_v25_paused_request_and_verified_reordered_output(rsa):
    data, fake = rsa
    d = draft()
    plan = rails._DRAFTS[d['draft_id']].plan
    landed(data, fake, plan)
    ad = data['saved']['ad']
    ad['responsive_search_ad']['headlines'].reverse()
    ad['responsive_search_ad']['headlines'][0]['asset_performance_label'] = 'BEST'
    ad['name'] = 'Provider metadata'
    result = rails.apply_draft(d['draft_id'])
    assert result['applied'] and result['verified']
    call = fake.mutate_calls[0]
    assert len(call['operations']) == 1 and call['partial_failure'] is False
    saved = call['operations'][0].ad_group_ad_operation.create
    assert saved.status.name == 'PAUSED' and saved.ad_group == GROUP
    assert list(saved.ad.final_urls) == ['https://example.com/service?source=ad']
    assert saved.ad.responsive_search_ad.path1 == 'service'
    assert [v.text for v in saved.ad.responsive_search_ad.headlines] == ['Fast Cooling', 'Local Team', 'Book Service']
    assert all(v.pinned_field.name == 'UNSPECIFIED' for v in saved.ad.responsive_search_ad.headlines)
    assert d['draft_id'] not in rails._DRAFTS


@pytest.mark.parametrize('changes', [
    {'headlines': 'not a list'}, {'headlines': ['a', 'b', 3]}, {'headlines': ['a', 'b']},
    {'headlines': ['a', 'b', 'a']}, {'headlines': [str(i) for i in range(16)]},
    {'descriptions': ['a']}, {'descriptions': [str(i) for i in range(5)]},
    {'headlines': ['a', 'b', '界' * 16]}, {'descriptions': ['a', '界' * 46]},
    {'headlines': ['a', 'b', ' hello']}, {'headlines': ['a', 'b', '{Keyword:hi}']},
    {'headlines': ['a', 'b', 'hi\nthere']}, {'headlines': ['a', 'b', '\ud800']},
    {'path1': '界' * 8}, {'path1': 'a/b'}, {'path1': '', 'path2': 'x'}, {'path1': None, 'path2': 'x'},
    {'final_url': 'https://user:pass@example.com/'}, {'final_url': 'https://example.com:bad/'},
    {'final_url': 'https://example.com:99999/'}, {'final_url': 'https://example.com:/'},
    {'final_url': 'https://example.com/a b'}, {'final_url': 'ftp://example.com'},
    {'final_url': 'https://'}, {'final_url': 'https://example.com/' + 'a' * 2048},
    {'ad_group_id': '0'}, {'ad_group_id': '-1'}, {'ad_group_id': '77 OR 1=1'},
])
def test_input_refusal_before_read(rsa, changes):
    data, fake = rsa
    with pytest.raises(rails.RailViolation):
        draft(**changes)
    assert not data['queries'] and not fake.mutate_calls


def test_exact_length_and_domain_rules(rsa, monkeypatch):
    monkeypatch.setattr(settings, 'advertiser_domain', lambda: 'EXAMPLE.TEST.')
    assert draft(headlines=['界' * 15, 'x' * 30, 'Third'], descriptions=['界' * 45, 'x' * 90],
                 path1='x' * 15, final_url='https://sub.example.test/?x=KEEP%20ME')['dry_run']
    for domain in ('badexample.test', 'example.test.evil.test'):
        with pytest.raises(rails.RailViolation):
            draft(final_url='https://' + domain)
    for config in ('https://example.test', 'example.test/path', ' example.test', 'example.test..', 'exa mple.test'):
        monkeypatch.setattr(settings, 'advertiser_domain', lambda: config)
        with pytest.raises(rails.RailViolation):
            draft()


@pytest.mark.parametrize('damage', ['status', 'root', 'ad_extra', 'asset_pin', 'asset_custom', 'asset_list', 'foreign', 'zero', 'negative', 'update', 'remove'])
def test_closed_dispatcher_refuses_payload(rsa, damage):
    _, fake = rsa
    plan = copy.deepcopy(rails._DRAFTS[draft()['draft_id']].plan)
    op = plan.operations[0]
    values = op.operation['create']
    if damage == 'status':
        values['status'] = 'ENABLED'
    elif damage == 'root':
        values['tracking_url_template'] = 'https://example.com'
    elif damage == 'ad_extra':
        values['ad']['final_mobile_urls'] = ['https://example.com']
    elif damage in {'asset_pin', 'asset_custom'}:
        values['ad']['responsive_search_ad']['headlines'][0][
            'pinned_field' if damage == 'asset_pin' else 'ad_asset_customizer'] = 'HEADLINE_1'
    elif damage == 'asset_list':
        values['ad']['responsive_search_ad']['headlines'][0] = ['hidden']
    elif damage in {'foreign', 'zero', 'negative'}:
        values['ad_group'] = {'foreign': 'customers/2/adGroups/77', 'zero': f'customers/{CID}/adGroups/0',
                              'negative': f'customers/{CID}/adGroups/-1'}[damage]
    else:
        op.operation.clear()
        op.operation[damage] = AD if damage == 'remove' else {'resource_name': AD, 'status': 'PAUSED'}
    with pytest.raises(rails.RailViolation):
        client._dispatch_entity(plan, False)
    assert not fake.mutate_calls
    assert rails.safe_create_operation('AdGroupAdService', {'status': 'ENABLED'}).operation['create']['status'] == 'PAUSED'


@pytest.mark.parametrize('damage', ['parent_status', 'parent_type', 'campaign_type', 'campaign_subtype', 'campaign_owner', 'currency', 'inventory'])
def test_confirmation_rechecks_relevant_state(rsa, damage):
    data, fake = rsa
    d = draft()
    if damage == 'parent_status':
        data['ad_group']['status'] = 'ENABLED'
    elif damage == 'parent_type':
        data['ad_group']['type_'] = 'SEARCH_DYNAMIC_ADS'
    elif damage == 'campaign_type':
        data['campaign']['advertising_channel_type'] = 'DISPLAY'
    elif damage == 'campaign_subtype':
        data['campaign']['advertising_channel_sub_type'] = 'SEARCH_MOBILE_APP'
    elif damage == 'campaign_owner':
        data['ad_group']['campaign'] = 'customers/2/campaigns/88'
    elif damage == 'currency':
        data['customer']['currency_code'] = 'EUR'
    else:
        landed(data, fake, rails._DRAFTS[d['draft_id']].plan)
        data['saved']['ad']['responsive_search_ad']['headlines'][0]['text'] = 'Different ad'
        data['ads'] = [{'ad_group_ad': data['saved']}]
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not fake.mutate_calls and d['draft_id'] in rails._DRAFTS


def test_duplicate_unordered_and_blocked_terms_at_draft_and_apply(rsa, monkeypatch):
    data, fake = rsa
    d = draft()
    landed(data, fake, rails._DRAFTS[d['draft_id']].plan)
    data['saved']['ad']['responsive_search_ad']['headlines'].reverse()
    data['ads'] = [{'ad_group_ad': data['saved']}]
    with pytest.raises(rails.RailViolation, match='identical'):
        draft()
    data['ads'] = []
    for blocked in ('cooling', 'service', 'source=ad'):
        monkeypatch.setattr(settings, 'blocked_terms', lambda: (blocked,))
        with pytest.raises(rails.RailViolation, match='blocked term'):
            draft()
        with pytest.raises(rails.RailViolation, match='blocked term'):
            rails.apply_draft(d['draft_id'])
    assert not fake.mutate_calls


@pytest.mark.parametrize('damage', ['wrong_group', 'wrong_customer', 'zero', 'negative', 'wrong_kind', 'text', 'pinned', 'url', 'path', 'status', 'type', 'extra_text'])
def test_landed_mismatch_consumed_and_audited(rsa, damage):
    data, fake = rsa
    d = draft()
    landed(data, fake, rails._DRAFTS[d['draft_id']].plan)
    identities = {'wrong_group': f'customers/{CID}/adGroupAds/78~99', 'wrong_customer': 'customers/2/adGroupAds/77~99',
                  'zero': f'customers/{CID}/adGroupAds/77~0', 'negative': f'customers/{CID}/adGroupAds/77~-1',
                  'wrong_kind': f'customers/{CID}/adGroups/77'}
    if damage in identities:
        fake.mutate_response.mutate_operation_responses[0].ad_group_ad_result.resource_name = identities[damage]
    else:
        ad = data['saved']['ad']
        rsa = ad['responsive_search_ad']
        if damage == 'text':
            rsa['headlines'][0]['text'] = 'Changed text'
        elif damage == 'pinned':
            rsa['headlines'][0]['pinned_field'] = 'HEADLINE_1'
        elif damage == 'url':
            ad['final_urls'].append('https://example.com/extra')
        elif damage == 'path':
            rsa['path2'] = 'extra'
        elif damage == 'status':
            data['saved']['status'] = 'ENABLED'
        elif damage == 'type':
            ad['type_'] = 'TEXT_AD'
        else:
            rsa['headlines'].append({'text': 'extra'})
    result = rails.apply_draft(d['draft_id'])
    assert result['applied'] and result['verified'] is False
    assert result['code'] == 'POST_WRITE_VERIFICATION_FAILED'
    assert d['draft_id'] not in rails._DRAFTS and len(fake.mutate_calls) == 1
    if damage in identities:
        assert not any('ad_group_ad.resource_name =' in q for q in data['queries'])
    assert 'POST_WRITE_VERIFICATION_FAILED' in Path(audit.AUDIT_PATH).read_text()
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert len(fake.mutate_calls) == 1
    assert all(json.loads(line) for line in Path(audit.AUDIT_PATH).read_text().splitlines())


def test_writes_off_and_unknown_consumed(rsa, monkeypatch):
    data, fake = rsa
    d = draft()
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'false')
    with pytest.raises(rails.RailViolation):
        draft()
    with pytest.raises(rails.RailViolation):
        rails.apply_draft(d['draft_id'])
    assert not fake.mutate_calls
    monkeypatch.setenv('GOOGLE_ADS_ENABLE_WRITES', 'true')
    fake.mutate_error = rails.UnknownWriteOutcome('unknown')
    with pytest.raises(rails.UnknownWriteOutcome):
        rails.apply_draft(d['draft_id'])
    assert d['draft_id'] not in rails._DRAFTS


def test_real_v25_serialized_read_shapes(rsa, monkeypatch):
    data, fake = rsa
    d = draft()
    plan = rails._DRAFTS[d['draft_id']].plan
    landed(data, fake, plan)
    read = client.gaql_all

    def proto_read(query, cid):
        rows = []
        for value in read(query, cid):
            row = make_type('GoogleAdsRow')
            for entity, fields in value.items():
                setattr(row, entity, fields)
            rows.append(type(row).to_dict(row))
        return rows

    monkeypatch.setattr(client, 'gaql_all', proto_read)
    assert rails.apply_draft(d['draft_id'])['verified'] is True


@pytest.mark.parametrize('damage', ['duplicate_row', 'foreign_group', 'wrong_compound_group', 'removed', 'missing_text', 'missing_type'])
def test_inventory_rows_fail_closed(rsa, damage):
    data, fake = rsa
    d = draft()
    landed(data, fake, rails._DRAFTS[d['draft_id']].plan)
    row = data['saved']
    row['ad']['responsive_search_ad']['headlines'][0]['text'] = 'Another ad'
    data['ads'] = [{'ad_group_ad': row}]
    if damage == 'duplicate_row':
        data['ads'] *= 2
    elif damage == 'foreign_group':
        row['ad_group'] = 'customers/2/adGroups/77'
    elif damage == 'wrong_compound_group':
        row['resource_name'] = f'customers/{CID}/adGroupAds/78~99'
    elif damage == 'removed':
        row['status'] = 'REMOVED'
    elif damage == 'missing_type':
        row['ad'].pop('type_')
    else:
        row['ad']['responsive_search_ad']['headlines'][0].pop('text')
    with pytest.raises(rails.RailViolation):
        draft()
    assert not fake.mutate_calls
