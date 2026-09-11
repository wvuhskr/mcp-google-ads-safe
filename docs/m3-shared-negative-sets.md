# Shared negative keyword sets: bounded offline contract

Implemented: `create_shared_negative_set(name: str, customer_id=None)` creates one
empty, unattached owned `NEGATIVE_KEYWORDS` SharedSet.
`add_to_shared_set(shared_set_id: str, keywords: list[dict], customer_id=None)` adds
ordered negative keywords to an existing owned set.
`attach_shared_set(shared_set_id: str, campaign_id: str, customer_id=None)` attaches
one existing set to one existing PAUSED standard Search campaign.

Both `GOOGLE_ADS_ENABLE_WRITES` and `GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT`
(default false, strictly parsed each call) are required, along with read and write
account allowlisting before account construction or reads. Confirmation rechecks
these gates, blocked content, the complete account/name fingerprint and operation digest.

Original MCP values cannot be coerced: name and supplied customer ID must be actual
strings; unknown arguments and explicit JSON null refuse. Omitted customer uses the
configured default. Customer IDs are canonical positive ASCII decimal strings at
most signed-int64, without leading zeros. Names preserve whitespace and reuse the
128-character, 255-UTF-8-byte nonblank/control-free creation limit and blocked terms.

The owner must be an ENABLED non-manager advertiser with exact owned resource and ID,
currency and time zone. Every page of active same-type name inventory is scanned,
without LIMIT; returned and total counts must reconcile. Exact name collisions,
duplicate/foreign/malformed rows and unknown type/status evidence refuse. Other
sets' member populations and counts are not creation fingerprints.

The only admitted create has exactly name and type_='NEGATIVE_KEYWORDS'. No status,
ID, resource_name, counts, mask, update/remove or mixed operations are admitted.
SharedSet status is output-only; the common create helper already leaves this family
status-free. A closed proof and saved-check descriptor are mandatory even if a caller
strips the action marker. Dispatch uses one GoogleAdsService.mutate, immutable account,
partial_failure=False and no mutation retries.

Saved success requires exactly one ordered shared_set_result with canonical positive
owned ID before any saved reads; exact ENABLED/NEGATIVE_KEYWORDS/name; explicitly present
zero member_count and reference_count; complete empty member and link scans including
removed link tombstones; unchanged account; and original active same-type name inventory
plus exactly the returned set. Sparse dictionaries or absent optional zeros never prove
emptiness. Counts are provider fields; empty populations are separately counted complete
local scans. Saved query failure or mismatch is applied but unverified and consumes the
draft. Ambiguous writes consume once and report UNKNOWN_WRITE_OUTCOME. Pre-dispatch drift
retains the draft but refuses. Validation-only results never claim saved success.

Empty unattached inventory has no serving effect. Adding exclusions, attaching to a
PAUSED standard Search campaign, and later enabling that campaign are separate prerequisites.
Existing-set additions prove every affected campaign is PAUSED SEARCH with explicitly
selected UNSPECIFIED subtype before and after dispatch. Name checks and campaign snapshots
are point-in-time evidence, not locks against external edits.

Offline tests prove local admission, generated v25 message serialization and verification
behavior with explicit fake clients only. They do not prove GAQL selectable-field compatibility,
provider mutation acceptance, count/tombstone timing, eventual consistency or serving approval.
If provider evidence is insufficient, refuse or mark applied/unverified; do not guess defaults.
Live/test-account validation requires separate authorization. No credentials, provider calls,
network, server replacement, dependency changes, push, merge or publication were used here.

## Existing-set additions

Original IDs use the same canonical positive string contract. Keywords must be a nonempty
exact list of exact dictionaries containing only `text` and `match_type`, both strings.
Text is trimmed and checked for blocked content; match type is EXACT, PHRASE or BROAD.
Duplicate normalized text/match pairs and existing-member collisions refuse the whole draft.

Complete raw selected reads cover owned set identity/name/type/status, explicitly present
member/reference counts, and absent inapplicable vertical type; all member identities and
exact contents; all ENABLED and REMOVED link identities; and every active linked campaign.
Members must be KEYWORD with the keyword oneof and explicitly present negative=True.
Every active campaign must be owned PAUSED standard Search. Complete member counts must
equal member_count; active unique campaigns must equal reference_count. Removed tombstones
are retained for drift but excluded from active reference reconciliation. Different provider
count semantics refuse. No manager-owned or foreign scope is inferred safe.

Preview lists every affected campaign ID/name/status, exact normalized additions/match types,
provider counts and separately counted complete populations. Adding changes the shared list
used by every listed campaign. Later enabling a campaign activates its exclusions.

One status-free SharedCriterionService create per keyword contains only owned shared_set,
negative=True and keyword. Closed proof validation reconstructs the operations independently
before any provider factory call. Confirmation recompiles full populations and operation
digest. One atomic mutate returns ordered unique owned compound criterion identities.
Each returned identity is bound to its corresponding submitted keyword. Saved success requires
the exact old member identity/content population plus those additions, member_count increasing
by exactly the additions, and unchanged account, set metadata, reference_count, active links,
removed tombstones and campaigns. Missing/extra members, changed siblings, swapped results or
changed attachments yield applied/unverified and consume the draft without retry.


## Single campaign attachment

The target and every existing active attachment must be owned PAUSED standard Search,
with complete raw selected proof before and after mutation. The target is independently
read and fingerprinted even when unattached. Existing ENABLED or REMOVED target links
refuse; no recreate semantics are assumed. An empty list is allowed, with an explicit
preview warning that it currently contains no exclusions and membership can change later.

Preview includes the complete before and proposed after campaign lists, exact current
keywords/match types, account/currency, provider counts and separately counted populations.
A CampaignSharedSet has no pause control: its ENABLED status is output-only. Exclusions
activate when the campaign is enabled; later membership edits affect every attached campaign.

The single atomic CampaignSharedSetService create contains only owned campaign and shared_set
references, without status, output resource, mask or other fields. Closed admission validates
both population proof and separate target proof independently of agreeing operation/check values.
Exactly one campaign_shared_set_result must equal the requested canonical compound identity
before saved reads. The complete saved population must equal the old proof plus precisely the
new ENABLED link and target campaign, with reference_count increased by one. Members and their
count, old links/tombstones, account and set metadata must be unchanged; all campaigns remain
PAUSED standard Search. Only-target verification is insufficient. Outside status/content edits
can race dispatch and produce applied/unverified; this is point-in-time evidence, not a lock.
