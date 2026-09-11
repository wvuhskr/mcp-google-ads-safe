# M2 paused Search creation

The first creation slice adds `draft_campaign` and `create_ad_group`. Both return a draft;
`confirm_and_apply` is the separate write step. These paths are offline tested and provider validation-only accepted. The manual-bidding
campaign and explicit-CPC group paths have also passed a separately approved live creation
check through the actual MCP connection. Other creation combinations remain validation-only.

## Campaign scope

`draft_campaign(campaign_name, daily_budget, bidding_strategy, geo_target_ids,
language_ids, contains_eu_political_advertising, customer_id=None, target_cpa=None,
target_roas=None)` prepares one PAUSED standard Search campaign, one nonshared DAILY
STANDARD budget, and its positive locations and languages in one atomic request. No
ad groups, keywords or ads are bundled. No spend can begin from this call.

The caller must supply a boolean political-advertising declaration. The server maps it
to the supported Google enum; it never assumes a declaration or requests a policy exemption.
Google Search is enabled; Search partners, the content network and partner search network
are disabled. Positive and negative location options use PRESENCE.

Supported standard bidding choices are MANUAL_CPC, MAXIMIZE_CONVERSIONS (optional target
CPA, cost per acquisition), and MAXIMIZE_CONVERSION_VALUE (optional target ROAS, return
on ad spend). Money uses the account currency, with exact micros conversion, one million
micros per currency unit. ROAS uses a ratio, so 2 means 200%. Budget and bid/CPA caps apply
at draft and again at confirm. Boolean amounts, zero, negative, nonfinite, overflow and
sub-micro amounts are refused. A target is accepted only for its compatible strategy.

Names are nonblank, at most 128 Unicode characters and 255 UTF-8 bytes, and contain no
control characters. Configured blocked terms apply. Nonshared budget names follow the
campaign name in Google's API, so budget.name is omitted from the request. The preview
shows the effective name equal to the campaign name, and the collision scan checks it.
Budget verification intentionally does not expect a caller-assigned suffix to survive.

Each target list contains 1 to 100 unique canonical positive string IDs. All constants
are read completely and must reconcile exactly, with locations enabled and languages
targetable. Complete campaign and budget name populations are checked locally for exact
collisions, without inserting user names in queries. Unrelated account rows are validated
but excluded from the saved state comparison. Account identity, currency, time zone,
constants and collisions are rechecked before apply. A simultaneous create can still
race the name check; this is not a transactional uniqueness lock.

## Ad-group scope

`create_ad_group(campaign_id, ad_group_name, customer_id=None, cpc_bid=None)` drafts one
PAUSED SEARCH_STANDARD ad group under a readable, nonremoved standard Search campaign.
The parent may be enabled or paused. Specialized or unreadable campaign subtypes refuse.
Names are checked against the complete nonremoved group population in that parent.

An optional CPC, cost per click, requires standard MANUAL_CPC, no portfolio, and the
existing manual-bid guard. With no CPC, a recognized Smart Bidding strategy can inherit.
No default CPC or target is invented. Parent state, effective strategy, account identity,
name collisions and current limits are checked again at apply.

## Request and result safeguards

Only the new budget (-1) and new campaign (-2) use temporary identifiers. The dispatcher
validates every operation and the whole dependency order before creating a Google client.
It rejects forward, dangling, cross-customer, wrong-kind and duplicate references,
negative updates/removals, orphan budgets, unrelated operations, and missing targets.
Existing resource validators retain their positive-ID contract. All entities are sent in
one GoogleAdsService request with partial_failure=False, so creation is atomic.

Post-write verification uses the actual resource identities returned by Google, never
negative placeholders. It validates all ordered identities before reading any resource,
then checks budget settings, campaign settings and budget linkage, each ENABLED location/language,
and ad-group parent/type/name/status and effective CPC when supplied. Missing, duplicate,
wrong-customer or wrong-type results and readback mismatches return applied=True,
verified=False, consume the draft, and require inspection before another write. A transport
failure remains an unknown outcome and cannot be retried with the same draft. Validation-only
acceptance is not proof that a create was applied or its readback verified.

## Evidence and remaining work

Credential-free tests cover the compiler with fake client reads and the real installed v25
message builder with fake Google services. Tests exercise empty bidding-message selection,
atomic dispatch, dependency rejection before client creation, input and drift guards,
provider result identity validation, readback mismatches and consumed-draft behavior.
The corrected implementation passed 724 offline tests; 259 focused tests also passed after
final audit assertions were strengthened. Independent requirements and quality review approved
the slice after correcting omitted Smart Bidding target verification. The installed package
exposes 31 tools and refuses both new tools with WRITES_DISABLED when writes are off.

On September 7, 2026, Google accepted seven validation-only cases: all five supported
campaign bidding combinations and manual/Smart Bidding ad-group creation. Every dispatch
explicitly used validate_only=True; no create was applied. Candidate parent reads were
identical before and after. Five verification-query probes succeeded on existing campaign,
budget, group, location and language records. These establish query acceptance, not actual
creation verification. Private raw account evidence stays outside the public package.
On September 7, 2026, the separately approved manual-CPC campaign and explicit-CPC ad-group
check passed actual MCP drafting/confirmation, exact-plan validation before each application,
post-write verification, fresh parent reads and audit reconciliation. A separate complete
inventory read confirmed paused state, approved settings and absence of ads/keywords. The
paused campaign, dedicated budget, targeting and group remain as disclosed test fixtures.
Google also returned default desktop/mobile/tablet criteria without explicit bid modifiers;
a private inventory verifier was corrected to account for those rows while retaining exact
location/language checks. No creation was retried. The initial failed read evidence is retained.
This proof does not extend to actual Smart Bidding creations or every optional combination.

Display campaigns, bundled ad groups, ads, assets/extensions writes, recommendations
writes and other remaining M2 work are outside this slice. It does not claim full M2 parity.

Sources: [Create campaigns](https://developers.google.com/google-ads/api/docs/campaigns/create-campaigns),
[Create ad groups](https://developers.google.com/google-ads/api/docs/campaigns/create-ad-groups),
[Mutate best practices](https://developers.google.com/google-ads/api/docs/mutating/best-practices),
[CampaignBudget v25](https://developers.google.com/google-ads/api/reference/rpc/v25/CampaignBudget).
Field names, enums, oneofs and resource helper paths were also checked against installed
Google Ads v25 message definitions and generated helpers.
