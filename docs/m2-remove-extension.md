# Remove one extension connection

`remove_extension(asset_id, extension_type, campaign_id=None, ad_group_id=None,
customer_id=None)` drafts removal of one existing campaign or ad-group connection. The
three exact extension types are `SITELINK`, `CALLOUT`, and `STRUCTURED_SNIPPET`. Every ID
must be a positive numeric string, and exactly one campaign ID or ad-group ID is required.

The preview shows the selected account, target, extension type, asset ID and content,
current connection status, and exact association that confirmation will remove.
`confirm_and_apply` is required before the request is sent. Removal affects only that
connection. It retains the underlying asset and every other connection to it. Other or
higher-level associations can therefore continue to serve; this tool does not promise to
eliminate inherited or account-level serving.

`list_extensions` provides campaign-level `asset.id`, target, and field type. It does not
list ad-group connections. To discover an ad-group connection, run this read-only Google
Ads Query Language query through `run_gaql`, replacing `AD_GROUP_ID`:

```sql
SELECT ad_group_asset.resource_name, ad_group_asset.ad_group,
       ad_group_asset.asset, ad_group_asset.field_type, ad_group_asset.status,
       asset.id, asset.type
FROM ad_group_asset
WHERE ad_group.id = AD_GROUP_ID
  AND ad_group_asset.status != 'REMOVED'
```

The draft and apply steps read the complete selected-family inventory again and refuse if
the parent, campaign, selected row, content, status, or any other selected-family
association changed. The request contains exactly one campaign-asset or ad-group-asset
remove operation and never deletes an asset. After a successful response, the exact result
identity is checked before a complete saved-state query accepts only an absent row or an
exact row with `REMOVED` status. A mismatch consumes the applied draft and tells the caller
to read current account state before considering another write.

This slice is verified offline with installed Google Ads API version 25 messages. No live
provider validation or advertising write has been performed.
