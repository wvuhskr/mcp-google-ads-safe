# Paused Performance Max asset-group updates

`update_asset_group(asset_group_id: str, *, name: str | None = None,
final_url: str | None = None, customer_id: str | None = None)` returns a reviewable draft.
It updates the name, the single desktop final URL, or both on one existing `PAUSED` asset
group. At least one requested field must differ from the saved value. An explicitly
unchanged field refuses the whole request.

The target must belong to the selected customer and an existing `PAUSED`, non-retail
Performance Max campaign that passes the same account, bidding, geography, automation,
political-advertising, business-name and logo checks used by asset-group creation. The
target must have exactly one configured-domain HTTPS final URL and no mobile final URLs
or URL path fields. Every nonremoved sibling is read to prevent name collisions and to
detect account changes before confirmation.

Confirmation sends one atomic Google Ads API v25 `AssetGroupService` update with
`partial_failure=false`. The update contains only the target resource name and requested
`name` and/or `final_urls` fields, with the exact corresponding update mask. Campaign,
budget, group status, branding, creative assets and connections, bidding, targeting and
automation are never included.

After a non-validation update response, verification first requires the exact expected
asset-group result identity. It then rereads the complete target, parent branding and
sibling inventory. A mismatch is reported as applied but unverified, consumes the draft
and must not be retried automatically. Validation-only responses never claim a saved
update.

All current evidence is offline. It proves local validation, Google Ads v25 message
construction, confirmation gating and simulated readback behavior. It does not prove live
Google acceptance, policy approval, saved state, serving, traffic or business results.
