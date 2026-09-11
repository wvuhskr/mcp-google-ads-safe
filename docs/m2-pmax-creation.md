# Paused non-retail Performance Max creation

`create_pmax_campaign` returns a reviewable draft. Confirmation creates one campaign,
one dedicated nonshared DAILY budget, one asset group, text definitions and creative
connections in one atomic Google Ads request. Campaign, group and all connections are
explicitly PAUSED. Bare text assets have no status; Google may reuse identical text
assets instead of allocating new ones.

This implementation is offline tested against installed Google Ads v25 messages.
No Google account read, validation or write was performed for this slice. In particular,
Google acceptance of paused creative connections and business-name verification remains
unproven. A later provider validation needs separate authorization.

## Public inputs

| Input | Contract |
| --- | --- |
| `campaign_name`, `asset_group_name` | Plain nonblank names using existing name/content checks. Existing campaign or budget name collisions refuse. |
| `daily_budget`, `target_cpa` | Positive finite amounts in the selected account's currency, including decimal strings. Target cost per acquisition (CPA) is required. Existing daily-budget and bid caps apply; boolean, nonfinite and fractional-micro amounts refuse. |
| `geo_target_ids`, `language_ids` | Actual lists, each containing 1 to 100 unique canonical positive numeric strings. All requested locations must be enabled and languages targetable. Positive and negative location options use PRESENCE. |
| `headlines` | 3 to 15 unique plain texts, each at most 30 weighted characters, with at least one at most 15. |
| `long_headlines` | 1 to 5 unique plain texts, each at most 90 weighted characters. |
| `descriptions` | 2 to 5 unique plain texts, each at most 90 weighted characters, with at least one at most 60. |
| `business_name` | One plain text at most 25 weighted characters. Syntax/content checking does not establish legal business-name verification. |
| `final_url` | One HTTPS URL within the configured `advertiser_domain`. The domain must be configured. |
| `landscape_image_asset_id` | Existing same-customer IMAGE, JPEG or PNG, positive file size at most 5,000,000 bytes; at least 600 by 314 pixels and exact ratio 1.91:1 or 600:314. |
| `square_image_asset_id` | Same metadata requirements; square and at least 300 by 300 pixels. |
| `logo_asset_id` | Same metadata requirements; square and at least 128 by 128 pixels. |
| `contains_eu_political_advertising` | Required actual boolean declaring whether the campaign contains European Union political advertising. |
| `customer_id` | Optional canonical positive numeric string. Only `None` uses `GOOGLE_ADS_CUSTOMER_ID`; empty strings and numeric inputs refuse. Account must be readable, write-allowlisted, enabled and non-manager. |

Text limits count double-width characters as two, using the existing text helper.
The same text may appear in different roles and shares one deterministic temporary text
definition. Each role still gets its own connection. A square image can also serve as
the logo when it meets both role requirements. Image bytes are neither downloaded nor
uploaded by this tool. The exact image ratio and byte limits are conservative local
rules, narrower than undocumented provider tolerances.

## Campaign behavior

The campaign uses `MAXIMIZE_CONVERSIONS` with explicit `target_cpa_micros` and
`brand_guidelines_enabled=True`. Business name and logo connect at campaign level.
Headlines, long headlines, descriptions and the two marketing images connect at
asset-group level. The call to action is automated by default. Google may automatically
generate video when no video is supplied.

These five campaign automation types are explicitly `OPTED_OUT`:

1. `FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION`
2. `TEXT_ASSET_AUTOMATION`
3. `GENERATE_IMAGE_EXTRACTION`
4. `GENERATE_IMAGE_ENHANCEMENT`
5. `GENERATE_ENHANCED_YOUTUBE_VIDEOS`

These settings do not establish that all generated video is prevented. No portfolio
bidding, alternative bidding, feeds, retail/travel settings, signals, listing groups,
optional assets, channel subtype or Search network settings are exposed.

## Confirmation and evidence

Write and account gates precede safety reads and repeat at confirmation. Complete
account, name, location/language and owned-image reads contribute to the draft
fingerprint. Confirmation recompiles the same intent and rejects changed account or
image evidence before mutation. A concurrent same-name creation can still race this
check.

The dispatcher admits only the closed graph: budget, campaign, positive criteria,
asset group, unique text definitions and exact role connections. Definitions precede
consumers, all resources share one owner, and `partial_failure=False`. Every operation
has an exact ordered saved-result descriptor. Existing standalone asset uploads,
Search creation and extension restrictions remain separate.

After a real mutation response, every result kind and resource owner is checked before
any saved-state read. Compound connection identities must match the resolved parent,
asset and field type. Readback then requires explicit budget, campaign, targeting,
group, text, connection and image metadata proof. Malformed or missing enums, duplicate
rows, foreign resources, incomplete scans or mismatched settings cannot verify success.
Provider text reuse proves the saved content and intended connections, not new asset
allocation. Validation-only results never establish creation or trigger saved-state
reads. Ambiguous transport or post-write proof failure consumes the draft; it is never
automatically retried. Fresh-draft drift refuses before mutation without consumption.

## Offline verification

From this repository folder, the focused command is:

```sh
PYTHONPATH=.:../image-upload-evidence/deps .venv/bin/python -m pytest tests/test_pmax_creation.py -q
```

The suite exercises actual MCP input handling, installed v25 serialization, complete
safety scan failures, strict populated readers, tampered graphs and result identities,
exact saved-state success, drift and consumed failure outcomes. The shared `.venv`
remains read-only; no dependencies were installed.

The approved contract and public-source excerpts are retained outside the repository
in `../pmax-plan.md`, `../pmax-evidence/source-notes.md` and
`../pmax-evidence/google-specs-user-paste.txt`.
