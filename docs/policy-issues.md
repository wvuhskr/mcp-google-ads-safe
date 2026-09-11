# Ad policy snapshot

`get_policy_issues(customer_id=None, page_token=None)` reads current ads whose approval
status is not APPROVED, excluding removed ads and removed parent campaigns/ad groups.
It returns campaign and ad-group identities/statuses, ad identity/status, and Google's raw
approval, review and policy-topic fields. Approved ads still under review are excluded.
This is not an asset/account policy inventory, an appeal tool, or a policy classification.

The account must be a positive ASCII decimal string without leading zeros or separators.
Only `None` uses the configured default. The READ allowlist is checked before the client;
reads work with writes disabled. Continuations accept only the returned nonempty token
bound to this query and account. Other query tools keep their existing token behavior.

The existing `rows`, `returned_count`, `total_results_count`, `query_limited`,
`pages_complete` and `next_page_token` fields pass through unchanged. Added `source`
metadata identifies the account, Google service, exact scope and current snapshot with
no attribution window. Missing/unknown totals stay missing/unknown. An empty page does
not imply completion; follow the returned token. Even a complete empty result does not
establish that the account has no policy issues outside these filters.

There are no dates, performance metrics or query LIMIT. Results are ordered by campaign,
ad group and ad ID. Google's raw policy entries retain the existing v25 serializer's
numeric enums, string integer64 fields and optional/default field behavior. Provider
errors remain errors. No duplicate query engine or provider calls were introduced.

The tool already existed, so the inventory remains 46 tools. This revision hardens inputs,
adds context/status fields and ordering, and retains the existing removed-entity exclusions.
It is verified offline using synthetic v25 messages and actual MCP calls. Historical live
proof in `m2-verification.md` applies to the prior query; this revision has no live proof.
