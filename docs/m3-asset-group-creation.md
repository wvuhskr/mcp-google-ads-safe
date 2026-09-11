# Paused asset group creation

`create_asset_group` returns a reviewable draft for one new PAUSED asset group in one
existing PAUSED, non-retail Performance Max campaign. Confirmation sends one atomic
Google Ads mutation containing only the asset group, deduplicated text assets and PAUSED
asset-group connections. It never writes the campaign, budget, bidding, targeting,
business name, logo, feeds, signals or listing groups.

The parent must be an enabled own-account, non-manager campaign using standard
MAXIMIZE_CONVERSIONS with a positive explicit target cost per acquisition. It must use
brand guidelines, PRESENCE geography options, a recognized political declaration and all
five supported automation types set to OPTED_OUT. The reader checks actual Google Ads v25
message presence and refuses shopping, vehicle, travel or hotel settings. Exactly one
existing BUSINESS_NAME and one LOGO campaign connection must be readable; both are
fingerprinted and inherited unchanged.

Inputs are canonical string IDs for the campaign and two existing images, one configured-
domain HTTPS final URL, a nonblank group name, and the same headline, long-headline and
description rules as `create_pmax_campaign`. Images are read as owned JPEG or PNG assets
and must pass the existing size, dimensions and ratio checks. Image bytes are not read or
uploaded. Identical text across roles shares one temporary definition while retaining one
connection per role.

Write and read allowlists are checked before reads and again at confirmation. The complete
parent, branding, existing asset-group names and image metadata form the draft fingerprint.
Confirmation recompiles the intent and refuses drift before sending anything. After a
write response, all ordered result identities and group-asset-role relationships are
checked before saved reads. Saved group, text and connection state must match exactly;
the parent, branding and images are then reread unchanged. The new group is deliberately
excluded from the old-name collision comparison during post-write proof.

This path is verified offline against installed Google Ads API v25 messages and fakes.
No account, credential, provider validation or write call was made. Offline evidence does
not prove Google acceptance, policy approval, asset allocation, serving or generated-video
behavior. An ambiguous write or failed post-write proof consumes the draft and is never
retried automatically; validation-only never claims creation.

From this repository folder in the macOS Terminal app, run:

```sh
PYTHONPATH=.:../image-upload-evidence/deps .venv/bin/python -m pytest tests/test_pmax_asset_group_creation.py -q
```
