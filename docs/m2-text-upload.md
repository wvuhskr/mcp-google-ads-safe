# Bare text asset upload

`upload_text_asset(text: str, name: str, customer_id: str | None = None)` is offline
verified only. No Google validation or live write has been performed for this tool.

In an MCP client connected to this server, call `upload_text_asset` with plain text
and a name. Review the exact account, text and name in its preview, then use the
existing `confirm_and_apply` tool with its draft ID to authorize the write.
Only an omitted or null customer ID uses the configured default.

Text must be a nonempty string with no surrounding whitespace, controls, Unicode
format/unassigned characters, or braces/dynamic substitution syntax. The conservative
local limit is 90 characters, with East Asian wide/full-width characters counted twice.
This is not Google's universal TextAsset maximum and does not establish eligibility
for headlines, business names or any other role. Later attachment limits may be stricter.
Exact input is preserved, with no trimming or Unicode normalization. Names follow the
existing nonblank, 128-character/255 UTF-8-byte limit and cannot contain controls.
Both text and name are checked against configured blocked terms and appear in preview
and audit records, as with other plain text tools.

Confirmation rechecks enabled writes, both account allowlists, blocked terms, complete
account identity and the exact requested payload. Expiry, account or input drift, and
changed draft content prevent dispatch. One standalone AssetService create is serialized
inside one GoogleAdsService atomic request with partial failure disabled. It creates no
campaign/ad-group connection, status, temporary identity, URL, update or removal.

Bare assets have no paused status and cannot be deleted through the API, so inventory
residue persists. Google may return an existing matching asset and ignore the requested
name. The tool does not claim newness or modify existing content.

Before any saved-content read, the result must contain exactly one positive asset resource
in the intended account. A complete exact-resource read then requires TEXT type and exact
saved text equality, including case, spacing and Unicode. Saved name differences, absence
or blankness do not fail verification. Unlike image metadata checks, this verifies actual
text content, but not policy approval or serving. Missing, ambiguous, incomplete or failed
readback reports an applied but unverified result; the consumed draft cannot be retried.
Validation-only requests never read saved content or claim it was saved. Unknown write
outcomes retain existing structured error/audit behavior and consume the draft.

Provider references: [AssetOperation](https://developers.google.com/google-ads/api/reference/rpc/v25/AssetOperation),
[TextAsset](https://developers.google.com/google-ads/api/reference/rpc/v25/TextAsset),
[Asset](https://developers.google.com/google-ads/api/reference/rpc/v25/Asset), and
[standalone text creation example](https://developers.google.com/google-ads/api/samples/add-performance-max-campaign).
