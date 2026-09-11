# Paused sitelink drafts

`draft_sitelinks(sitelinks, campaign_id=None, ad_group_id=None, customer_id=None)` drafts
new sitelink assets and PAUSED links for exactly one existing standard Search campaign or
ad group. It accepts 1 to 10 items per draft. Ten is this tool's conservative local batch
cap, not a Google Ads provider limit.

Each item requires `link_text` and one HTTP(S) `final_url`. `description1` and
`description2` are optional but must appear together. Link text is limited to 25
characters and each description to 35, counting East Asian wide and fullwidth characters
twice. The existing blocked-term and advertiser-domain rules apply. Scheduling, tracking,
mobile URLs, dynamic fields, existing-asset reuse, account-wide links and other asset types
are excluded.

Existing target inventory may contain schedules, dates, multiple final URLs, mobile URLs or
tracking fields created elsewhere. Those valid rows remain in the state fingerprint and do
not make this plain-only tool unusable. They count as identical only when their complete
relevant content is equivalent to the submitted plain sitelink.

Confirmation rereads the account, Search parent and complete nonremoved sitelink-link
population. Each asset create uses a negative temporary identity and is immediately linked
to the chosen target in the same atomic request. Assets have no status; every association
is explicitly PAUSED. Google may deduplicate identical asset creates. The atomic batch
prevents a failed link from leaving a newly created bare asset, although later removal of a
successful link can leave its asset in inventory.

Before readback, the response must contain the exact alternating asset/link result kinds,
positive same-account identities, matching asset IDs, the exact target and the SITELINK
field identity. Readback then verifies saved text, URL, asset type and PAUSED link status.
A post-write mismatch is reported as applied but unverified and consumes the draft.

This slice has offline fake-client coverage with real installed Google Ads API version 25
messages. No provider validation or live creation has been performed.

Official references reviewed September 7, 2026:

- [SitelinkAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/SitelinkAsset)
- [CampaignAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/CampaignAsset)
- [AdGroupAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/AdGroupAsset)
- [Asset creation and usage](https://developers.google.com/google-ads/api/docs/assets/working-with-assets)
