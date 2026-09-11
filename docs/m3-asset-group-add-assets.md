# Add existing assets to a Performance Max asset group

`add_asset_group_assets(asset_group_id, assets, customer_id=None)` returns a reviewable
draft that creates PAUSED connections between existing owned assets and one existing
PAUSED asset group. Each requested item contains only `asset_id` and `field_type`.
Confirmation sends one atomic `GoogleAdsService.Mutate` request with partial failure
disabled. The request contains only `AssetGroupAssetService` creates. It cannot upload or
create assets, edit the group or campaign, change statuses, branding, bidding, targeting,
automation, signals, feeds, listing groups, logos, videos or calls to action.

The supported combined existing plus requested inventory is:

| Role | Minimum | Maximum | Content requirement |
| --- | ---: | ---: | --- |
| `HEADLINE` | 3 | 15 | 30 weighted characters each; at least one is 15 or fewer |
| `LONG_HEADLINE` | 1 | 5 | 90 weighted characters each |
| `DESCRIPTION` | 2 | 5 | 90 weighted characters each; at least one is 60 or fewer |
| `MARKETING_IMAGE` | 1 | 20 | JPEG/PNG, at most 5,000,000 bytes, 600 x 314 minimum at the accepted 1.91:1 ratio |
| `SQUARE_MARKETING_IMAGE` | 1 | 20 | JPEG/PNG, at most 5,000,000 bytes, square and at least 300 x 300 |

Double-width text characters count as two. Text content must be unique within each role,
even when the asset identifiers differ. One existing asset can be used in different roles
when its content satisfies each role. A request refuses a pair that is already linked.

The safety read scans every nonremoved connection and joins the complete owned asset
record. Every existing connection must already be PAUSED, use one of the five supported
roles and have valid readable content or image metadata. The existing inventory may be
below the listed minimums only when the requested additions bring the final combined
inventory into every listed range. Unknown, enabled, malformed, foreign, duplicated or
unsupported existing connections refuse the draft.

The draft fingerprint contains the exact group, eligible nonretail Performance Max parent,
inherited business-name and logo proof, full existing connection inventory and requested
asset content or metadata. Confirmation repeats the write and read account permissions,
rereads the complete fingerprint and compares the mutation digest before any provider
call. A saved result is verified only after every ordered provider identity matches and a
full reread equals the exact old-plus-new PAUSED connection union with unchanged group,
parent, branding and asset proof.

The role ranges and weighted text rules come from the retained Google Ads specifications
in `../pmax-evidence/google-specs-user-paste.txt` and
`../pmax-evidence/source-notes.md`. Those sources support separate 20-image ceilings for
landscape and square marketing images. They do not establish safe limits for optional
video, logo, vertical-image, call-to-action or other roles, so this tool refuses them.

All current evidence is offline. Tests use the installed Google Ads API v25 message types
and fake provider responses. They do not prove Google acceptance, policy approval, serving,
traffic, leads, sales or any other business result. A validation-only response does not
prove links were saved. An unknown transport result or failed saved-state check consumes
the draft and must not be retried without reading the account first.

Run the focused offline proof from the project folder in the macOS Terminal app:

```sh
PYTHONPATH=.:../image-upload-evidence/deps .venv/bin/python -m pytest -q tests/test_pmax_asset_group_assets.py
```
