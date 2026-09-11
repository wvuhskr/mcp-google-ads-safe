# Paused structured snippet drafts

`create_structured_snippets(snippets, campaign_id=None, ad_group_id=None, customer_id=None)`
accepts 1 to 10 objects and drafts new structured snippet assets plus PAUSED connections
for exactly one existing standard Search campaign or ad group. Confirmation is required
before the one atomic request is sent. Ten is a local safety cap, not a Google Ads limit.

Each object must contain exactly `header` and `values`. The header must exactly match one
supported English header: Brands, Amenities, Styles, Types, Destinations, Services,
Courses, Neighborhoods, Shows, Insurance coverage, Degree programs, Featured hotels, or
Models. Values must be a list of 3 to 10 unique plain strings, each 1 to 25 characters;
East Asian wide and fullwidth characters count twice.

Duplicate values are compared without case. Duplicate snippets use the exact header and
their sorted case-insensitive values, so reordering values does not bypass the check. The
requested order is preserved in the preview, mutation, and exact readback.

Existing broader or localized inventory remains readable and part of the apply-time state
fingerprint. It blocks a draft only when its header and values are plainly equivalent to
the requested content. URLs, tracking settings, custom parameters, alternate asset
content, schedules, account-level links, existing-asset reuse, enabling and removal are
outside this initial English-only creation slice.

Every new asset uses a distinct negative temporary identity and is immediately followed
by one PAUSED campaign or ad-group connection. Before readback, the response must contain
the exact ordered asset and connection result kinds, positive same-account identities,
matching target and asset IDs, and structured snippet field identity 12. Readback verifies
the exact header and ordered values, structured snippet asset type, empty unrelated fields,
and PAUSED connection status. A mismatch is reported as applied but unverified and consumes
the draft.

This slice has offline fake-client coverage with real installed Google Ads API version 25
messages. No provider validation or live creation has been performed.

Official references reviewed September 7, 2026:

- [StructuredSnippetAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/StructuredSnippetAsset)
- [Structured snippet headers](https://developers.google.com/google-ads/api/data/structured-snippet-headers)
