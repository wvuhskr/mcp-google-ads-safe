# Remove one asset-group connection

`remove_asset_group_asset(asset_group_id, asset_id, field_type, customer_id=None)` returns
a reviewable draft for one existing PAUSED connection in one existing PAUSED Performance
Max asset group. It removes the connection only. The owned bare asset, other roles using
that asset, other groups, parent campaign, branding, status, budget and settings remain.

The supported roles are `HEADLINE`, `LONG_HEADLINE`, `DESCRIPTION`, `MARKETING_IMAGE` and
`SQUARE_MARKETING_IMAGE`. Before drafting, the tool completely reads every nonremoved
connection and its selected asset. All links must be PAUSED, owned, canonical, distinct
and valid. After simulating the removal, the remaining inventory must contain 3 to 15
headlines, 1 to 5 long headlines, 2 to 5 descriptions, 1 to 20 landscape marketing
images and 1 to 20 square marketing images. At least one headline must be 15 weighted
characters or fewer and one description must be 60 or fewer. Text remains unique within
each role, and all prior text length and image metadata rules still apply.

Confirmation repeats the write and read permissions and rereads the exact parent,
branding, group, current connection inventory and selected bare asset. It sends one
atomic `GoogleAdsService.Mutate` request with partial failure disabled and one
`AssetGroupAssetService` remove operation. No `AssetService` delete is allowed.

Saved verification first checks the one returned compound connection identity. It then
requires a complete exact-target query to show either zero rows or one matching REMOVED
tombstone, proves the selected bare asset still exists unchanged, and rereads the complete
remaining link and asset union. The group, parent and inherited branding must also remain
unchanged. A validation-only request performs no saved-state reads and never claims the
connection was removed. An unknown transport outcome or any saved-state mismatch consumes
the draft and must not be retried without a fresh account read.

All evidence is offline. Tests use Google Ads API v25 message types and fake provider
responses. They do not prove provider acceptance, policy approval, serving, traffic,
leads, sales or any other business result.

Run the focused offline proof from the project folder in the macOS Terminal app:

```sh
PYTHONPATH=.:../image-upload-evidence/deps .venv/bin/python -m pytest -q tests/test_pmax_asset_group_asset_remove.py
```
