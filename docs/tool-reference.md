# mcp-google-ads-safe

An unofficial Model Context Protocol (MCP) server, a standard way for an assistant to call
tools, for Google Ads API v25. The current source contains 60 tools: `undo_change` (below), the 25 Milestone 1 tools for account and
performance reads, campaign and ad-group updates, status changes, keyword and negative
keyword work, location exclusions, and campaign schedules, plus four Milestone 2 reads:
`list_extensions`, `get_policy_issues`, `get_conversion_actions`, and `list_recommendations`.
The two creation tools, `draft_campaign` and `create_ad_group`, draft PAUSED Search
campaigns and ad groups. Campaign creation includes a dedicated daily budget and explicit
location, language and political-advertising choices. Current-source evidence is offline
only. Historical September 7, 2026 records show validation-only acceptance for all five
campaign bidding shapes and no-bid ad-group creation, plus one applied and independently
read-back Manual CPC campaign and explicit-CPC ad group. Smart Bidding campaign creation
and no-bid group saved proof remain absent; see [docs/m2-creation.md](m2-creation.md).
`create_pmax_campaign` drafts one PAUSED non-retail Performance Max campaign, a dedicated
daily budget, one PAUSED asset group and PAUSED creative connections. It requires explicit
target cost per acquisition (CPA), geography/languages and existing owned images. Google
acceptance remains unverified; see [docs/m2-pmax-creation.md](m2-pmax-creation.md).
`draft_demand_gen_ad` drafts one PAUSED single-image Demand Gen ad from existing
square image/logo inventory. It requires an externally existing PAUSED Demand Gen
group and campaign; Search group creation remains unchanged. Offline schema/test
proof only, with no provider-acceptance claim. [Contract and limits](m3-demand-gen-ad.md).

`create_demand_gen_campaign` drafts one PAUSED Demand Gen campaign with a dedicated
daily budget and explicit campaign-level locations and languages. Its fixed targeting
ownership cannot later be switched to upgraded ad-group targeting on that campaign.
No groups or ads are created; see [docs/m3-demand-gen-campaign.md](m3-demand-gen-campaign.md).
`create_asset_group` drafts one additional PAUSED asset group under an existing PAUSED,
non-retail Performance Max campaign. It reuses two existing marketing images and inherits
the parent campaign's business name, logo and safety settings without changing them. Its
evidence is offline only; see [docs/m3-asset-group-creation.md](m3-asset-group-creation.md).
`update_asset_group` drafts a name and/or final URL update for one existing PAUSED asset
group under an eligible PAUSED non-retail Performance Max campaign. It does not change
status, parent, branding, creatives, bidding, targeting or automation. Its evidence is
offline only; see [docs/m3-asset-group-update.md](m3-asset-group-update.md).
`add_asset_group_assets` links existing owned text or marketing-image assets to one
eligible PAUSED asset group using PAUSED connections only. Existing connections must all
be PAUSED and valid; an underfilled group is accepted only when the requested additions
produce the complete supported creative set. No asset upload or content creation occurs.
Its evidence is offline only; see
[docs/m3-asset-group-add-assets.md](m3-asset-group-add-assets.md).
`set_listing_group_filter` drafts an exact product item allowlist for one PAUSED retail
Performance Max asset group under a PAUSED campaign. It atomically replaces only an
admitted shallow SHOPPING tree and explicitly excludes all remaining product IDs.
No product availability, eligibility or serving proof is claimed offline.
See [the bounded retail filter contract](m3-listing-group-filter.md).

`remove_asset_group_asset` removes one existing PAUSED group-to-asset connection while
retaining the bare asset and every other connection. The complete remaining creative
inventory must still meet all five supported role and content requirements. Its evidence
is offline only; see
[docs/m3-asset-group-remove-asset.md](m3-asset-group-remove-asset.md).
`draft_responsive_search_ad` drafts one PAUSED responsive Search ad with plain,
unpinned text under an existing standard Search ad group. The current source is offline
verified only. A September 7, 2026 validation-only sample was accepted for the minimum
text counts and both display paths; no RSA was saved, and policy approval, serving and
applied-result verification remain absent. See [docs/m2-responsive-search-ad.md](m2-responsive-search-ad.md).
`draft_sitelinks` drafts 1 to 10 new plain sitelinks and PAUSED campaign or ad-group
links in one atomic request. This slice is offline verified only and has not been sent to
Google; see [docs/m2-sitelinks.md](m2-sitelinks.md).
`create_callouts` drafts 1 to 10 new plain callout assets and PAUSED campaign or ad-group
connections in one atomic request. Despite its name, it returns a draft that requires
confirmation. This slice is offline verified only and has not been sent to Google; see
[docs/m2-callouts.md](m2-callouts.md).
`create_structured_snippets` drafts 1 to 10 new structured snippet assets and PAUSED
campaign or ad-group connections. It accepts the supported English headers and 3 to 10
plain values per snippet; see [docs/m2-structured-snippets.md](m2-structured-snippets.md).
`remove_entity` drafts permanent removal of one paused standard Search campaign,
standard Search ad group, or responsive search ad. It inventories the full affected
child population before confirmation and retains budgets, bare assets, and history.
This path is offline-tested only; no live Google Ads removal has been performed.

`remove_extension` drafts removal of one campaign or ad-group sitelink, callout, or
structured-snippet connection while retaining the asset and its other connections; see
[docs/m2-remove-extension.md](m2-remove-extension.md).
`upload_image_asset` drafts one bare JPEG/PNG image asset without creating a connection.
Confirmation is required; see [docs/m2-image-upload.md](m2-image-upload.md).
`upload_text_asset` drafts one bare plain TEXT asset; confirmation is required. It is
offline verified only. See [docs/m2-text-upload.md](m2-text-upload.md) for the local
text limit, content checks and exact-text verification scope.
`get_policy_issues` excludes removed ads and parents and requires bound
continuation tokens. A bounded September 11, 2026 check accepted its revised query with no returned rows; nonempty-result interpretation remains unverified. See
[docs/policy-issues.md](policy-issues.md).
The four read additions have offline coverage and historical September 7, 2026 live
query acceptance within their stated filters. That record covers the prior policy
query; current bounded read evidence is recorded in [release readiness](release-readiness.md). See
[docs/m2-verification.md](m2-verification.md).

## Undo

`undo_change(draft_id)` drafts the reverse of an applied change. It reads the local audit
log for the "apply" event of that draft, rebuilds the before-state from the preview the
original tool recorded, and calls the matching tool in reverse. The result is an ordinary
draft: writes must be enabled, the account must be write-allowlisted, caps and opt-ins
apply, and `confirm_and_apply` is still required. Undo itself never sends anything.

| Original tool | Reverse |
| --- | --- |
| `update_campaign`, `update_ad_group` | Restores the recorded previous budget, name, status, target CPA / ROAS, or CPC. A target that had no previous value is cleared. An ad-group CPC that had no previous value cannot be reversed (no tool clears a CPC). |
| `pause_entity`, `enable_entity` | Sets the recorded previous status. |
| `draft_keywords`, `add_negative_keywords` | Removes the criteria the apply created (ids from the recorded result). |
| `remove_keywords`, `remove_negative_keywords` | Re-adds the removed text and match type. New criterion ids; keyword-level bids are not restored. |
| `update_keyword_bid` | Restores the recorded previous bid. |
| `set_campaign_schedule` | Restores the recorded previous week. Bid modifiers on windows are not restored; a campaign that had no schedule cannot be returned to 24/7 by undo. |
| `draft_campaign`, `create_ad_group`, `create_pmax_campaign`, `create_demand_gen_campaign` | Pauses the created campaign or ad group if it has been enabled since. A still-paused creation is reported as nothing to reverse. |
| Removals, asset uploads, audiences, conversion actions, recommendation actions, geo changes | Reported as not reversible with the reason. |

Refusals: no audit record (`UNKNOWN_DRAFT`), never applied (`NOT_APPLIED`), original outcome
unknown (`OUTCOME_UNKNOWN`), an undo draft already exists (`ALREADY_UNDONE`), or the needed
before-state was not recorded (`NOT_REVERSIBLE`). Evidence: offline synthetic only.

## Current safety model

The server starts read-only. Writes require `GOOGLE_ADS_ENABLE_WRITES=true`, and target
accounts must be included in both the read and write allowlists. Every write first returns
an expiring draft with a preview and digest. A separate `confirm_and_apply` call rechecks
current account state and the safety rules before sending one mutation.

This two-step flow prevents an assistant from applying a change in the same call that
proposes it. It does not enforce human approval: any connected caller that has the draft
identifier can call `confirm_and_apply`. Drafts live only in memory inside one running
server process, expire after 3,600 seconds by default, and disappear on restart.

Daily-budget and cost-per-click caps default to 1,000 and 50 respectively. These numbers
use the target account's currency, so they mean 1,000 euros in a euro account and 1,000
dollars in a dollar account. The caps are fixed limits, not currency conversions. Shared
budget changes and portfolio bidding-strategy changes are disabled by default.

`customer_id` identifies the account whose entity or report is being requested. Read and
write allowlists are separate; every write-authorized account must also be read-authorized.
For portfolio strategy edits, every attached campaign account must be readable and
write-authorized because one strategy can affect several campaigns.

Standalone portfolio creation supports only TARGET_CPA and TARGET_ROAS in a non-manager
client account. It creates unattached inventory and does not begin serving or attach any
campaign; the final same-name check cannot prevent a concurrent create from racing it.

## Setup and configuration

Follow the current [start-to-finish setup guide](setup.md) for source installation,
credentials, client connection, and the first live read-only check. See
[configuration](configuration.md) for account allowlists, write controls, feature opt-ins,
and advertiser settings.

## Verification status

See the [operation inventory](offline-capability-matrix.md),
[future validation procedure](validation-readiness.md) and
[release checklist](release-readiness.md) for the current boundaries.


All tool definitions and their input schemas are covered by offline tests, including
calls through the MCP boundary with fake Google responses. Historical records document
bounded applied and independently read-back M1 campaign, ad-group and targeting
canaries, plus selected reads; those records do not establish the same proof for every
current-source branch, status or portfolio path. This source tree is not a published
package, and milestone 1 does not complete the later release milestones.

`create_custom_audience` drafts an OPEN website-visitor UserList with exact URL-CONTAINS
rules and a 30-day per-rule lookback. It requires explicit audience opt-in and confirmation,
creates no attachment, and is offline verified only. See [audience scope](m2-custom-audience.md).

`add_audience_targeting` creates one PAUSED audience criterion on a PAUSED standard Search
campaign with an existing matching explicit campaign-level mode. See [bounded audience
targeting](m2-audience-targeting.md) for discovery, restrictions and offline evidence.

`create_conversion_action` drafts an ENABLED secondary website conversion with explicit
false primary status in its original create request. It requires conversion-goal opt-in,
a complete manager-tree control scan and confirmation. Automatic goals and custom-goal
bidding exceptions are previewed; see [conversion creation](m2-conversion-creation.md).

`set_conversion_action_primary_status` drafts only the primary flag change on an existing
ENABLED WEBPAGE conversion action. Custom goals can still bid on secondary actions.
Complete visible manager-tree and goal checks are required; see
[the offline-only contract](m2-conversion-primary-status.md).


`apply_recommendation(recommendation_id, customer_id=None)` drafts only a non-dismissed
CAMPAIGN_BUDGET recommendation, with its exact recommended daily amount and all attached
campaigns. Requires `GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION=true`, global writes, account
read/write permission, budget caps, and shared-budget opt-in when applicable. Confirm the
draft separately. No validation-only support, automatic rollback, or retry. Offline tests
verify request construction and saved configuration checks; live provider acceptance and
bidding results are not verified. See [bounded recommendation apply](m2-recommendation-apply.md).

`dismiss_recommendation(recommendation_id, customer_id=None)` drafts dismissal of one
exact suggestion with separate confirmation. Global writes and account read/write
permission are required; the apply-recommendation opt-in is not. It does not execute
the proposal or disable future suggestions or auto-apply subscriptions. No validation-only
support, automatic rollback or retry. Verification requires observing the same suggestion
with `dismissed=true`; disappearance reports applied but unverified. Live row visibility,
timing, suppression duration and undo are unverified. See
[recommendation dismissal](m2-recommendation-dismiss.md).


### Keyword discovery and forecasts

`discover_keywords(seed_keywords, customer_id=None, page_token=None)` returns one page
of historical keyword ideas and estimates with writes disabled. It requires READ account
permission and 1..20 exact keyword strings. Targeting comes from `keyword_research` settings;
empty geography settings and a null language mean all geographies and languages.
See [the request, metric and continuation contract](keyword-discovery.md).
A bounded September 11, 2026 live discovery check returned a partial page, and a separate unsaved forecast completed. This establishes acceptance for those inputs, not forecast accuracy or exhaustive results; see [release readiness](release-readiness.md).

`get_keyword_forecasts` requests Google's future campaign estimates for supplied keywords,
one match type, explicit dates and a Manual CPC (manually selected cost-per-click) bid.
It requires configured locations/language and uses the selected account's currency and
timezone. This read-only tool creates no saved plan or account changes; absent metrics
remain unavailable. Network/adult discovery settings do not apply. A bounded September 11, 2026 live request completed; this does not prove forecast accuracy or acceptance for every input. See [release readiness](release-readiness.md).
See [keyword forecast contract](keyword-forecasts.md) for inputs and limits.


`create_shared_negative_set` drafts one empty, unattached owned negative keyword set.
Both write enablement and `GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT=true` are required;
the independent shared-negative gate defaults off. It checks complete same-type name
inventory before confirmation and verifies exact saved empty membership and attachment
populations. No serving effect while unattached; provider acceptance is unproved offline.
See [the bounded shared negative contract](m3-shared-negative-sets.md).

`add_to_shared_set` drafts negative keyword additions to one existing owned shared list.
All attached campaigns must be PAUSED standard Search, and both write/shared-edit gates
are required. Preview lists every affected campaign; additions change the list used by
all of them. Complete member/link/count proof and exact saved old-plus-new membership
are mandatory. Removed links remain part of drift proof. Later enabling campaigns activates
exclusions. This is point-in-time, offline-tested safety behavior, not provider acceptance.


`attach_shared_set` drafts one existing shared negative list attachment to one existing
PAUSED standard Search campaign. All attached campaigns must also be PAUSED; existing active
or removed target links refuse. Empty lists are allowed with a warning. The link's ENABLED
status is output-only and has no pause control: later enabling the campaign activates exclusions,
and later list edits affect all attachments. Saved checks require the exact full old-plus-target
population and unchanged members/tombstones, not only the new link. Both write/shared-edit gates
are required. Offline proof only; see [shared negative contract](m3-shared-negative-sets.md).
