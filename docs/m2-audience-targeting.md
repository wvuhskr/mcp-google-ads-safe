# Bounded audience targeting

`add_audience_targeting(campaign_id, audience_id, targeting_mode, customer_id=None)` drafts one positive PAUSED campaign UserList criterion. IDs must be positive ASCII numeric strings; only omitted/None account uses the configured default. Mode must be exactly `OBSERVATION` or `TARGETING`.

Requires writes enabled, both account allowlists and `GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT=true`, checked before reads at draft and confirmation. The existing campaign must be PAUSED standard SEARCH, and the existing same-account list must be OWNED, editable, OPEN and RULE_BASED with readable rule content matching the supported 30-day OR URL-CONTAINS rules used by `create_custom_audience`. Other rule patterns refuse. This is a website-visitor UserList, not a CustomAudience interest segment. Existing positive or negative selections of that list refuse.

The campaign must already have exactly one explicit AUDIENCE restriction whose boolean `bid_only` is true for OBSERVATION or false for TARGETING. Missing, unknown, duplicate or mismatching settings refuse. Any nonempty or unreadable ad-group restriction refuses. Configure the intended campaign-level mode separately before using this tool. This tool never edits mode settings, which could change other audiences' reach.

OBSERVATION does not narrow reach. TARGETING narrows reach. The mode applies only after the campaign and new criterion are activated later; both remain PAUSED here. No activation, list/membership/tag changes, bids, customer-match upload, interest segments, Display, Performance Max or ad-group attachment is supported. No eligibility, size, consent, tag readiness or serving claim is made.

Preview identifies the account, campaign, list/name, mode, existing campaign and ad-group connections, and every attached campaign plus the selected new target. Usage is complete within the selected account, not a universal cross-account claim. This links an owned list without editing it, so no outside-account consumer scan is required.

Complete reads fingerprint account, parent, settings, list content, campaign criterion inventory and list connections. Confirmation recompiles and refuses drift. One atomic GoogleAdsService request creates the criterion with `partial_failure=False`; no direct mutation RPC or automatic retry. Strict same-account compound result identity must match the existing campaign before saved reads. Saved content plus campaign/list/settings are reread; races or failed verification consume the draft and report applied but unverified. No repair or retry is attempted. Validation-only dispatch performs no saved reads.

In a Codex chat, use `run_gaql` with the selected account and these discovery queries. Discovery does not authorize a write:

```sql
SELECT campaign.id, campaign.resource_name, campaign.name, campaign.status,
       campaign.advertising_channel_type, campaign.advertising_channel_sub_type,
       campaign.targeting_setting.target_restrictions
FROM campaign WHERE campaign.status = 'PAUSED'
```

```sql
SELECT user_list.id, user_list.resource_name, user_list.name, user_list.type,
       user_list.access_reason, user_list.read_only, user_list.membership_status,
       user_list.rule_based_user_list
FROM user_list
```

```sql
SELECT ad_group.resource_name, ad_group.campaign,
       ad_group.targeting_setting.target_restrictions
FROM ad_group
```

Evidence is offline only: tests use real installed v25 messages and simulated complete reads, with no Google account, validation or live mutation calls. Provider fields and targeting settings establish request shape, not live account acceptance. [Google targeting settings](https://developers.google.com/google-ads/api/docs/targeting/targeting-settings) explains campaign/ad-group mode interactions.
