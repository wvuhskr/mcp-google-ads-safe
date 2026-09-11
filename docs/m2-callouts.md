# Paused callout drafts

`create_callouts(callouts, campaign_id=None, ad_group_id=None, customer_id=None)` accepts a
list of 1 to 10 plain strings and drafts new callout assets plus PAUSED connections for
exactly one existing standard Search campaign or ad group. Despite its name, the tool does
not write immediately; a separate confirmation is required. Ten is a local safety cap, not
a Google Ads limit.

Each string must contain 1 to 25 characters, counting East Asian wide and fullwidth
characters twice. Blank text, surrounding whitespace, control characters, dynamic syntax,
case-insensitive duplicates and configured blocked terms are refused. New callouts do not
accept URLs, dates, schedules, tracking settings, existing-asset reuse or other asset types.

Existing target inventory can contain valid dates, schedules and provider defaults. Those
rows remain in the apply-time state fingerprint and count as identical only when they are
equivalent to submitted plain content. Confirmation rereads the account, Search parent and
complete nonremoved callout-link population before sending one atomic request.

Every new asset uses a distinct negative temporary identity and is immediately followed by
one PAUSED campaign or ad-group connection. Before readback, the response must contain the
exact ordered asset and connection result kinds, positive same-account identities, matching
target and asset IDs, and the CALLOUT field identity. Readback verifies exact text, asset
type, empty optional settings and PAUSED connection status. A mismatch is reported as
applied but unverified and consumes the draft.

This slice has offline fake-client coverage with real installed Google Ads API version 25
messages. No provider validation or live creation has been performed.

Official references reviewed September 7, 2026:

- [CalloutAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/CalloutAsset)
- [CampaignAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/CampaignAsset)
- [AdGroupAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/AdGroupAsset)
