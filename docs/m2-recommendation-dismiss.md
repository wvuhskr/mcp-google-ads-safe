# Recommendation dismissal

`dismiss_recommendation(recommendation_id, customer_id=None)` previews dismissal of one
exact suggestion and requires separate `confirm_and_apply`. Only `None` selects the
default account. Use a canonical numeric account ID and a bare opaque recommendation
ID containing only letters, digits, underscores or hyphens.

Global writes and the selected account's read/write permissions are required before
reads and checked again at confirmation. The apply-recommendation, budget, shared-budget,
portfolio and conversion gates are not needed: dismissal does not execute the proposal.
Known non-sentinel v25 recommendation types are accepted locally; live provider support
for every type is not claimed. Already-dismissed, missing or ambiguous targets refuse.

Approval covers the exact resource, type, dismissed flag and optional budget, campaign
and ad-group associations. Empty associations are valid. The reader scans all pages,
requires exactly one same-account target, and does not filter away dismissed rows.
This is a narrow identity projection, not a complete fingerprint of recommendation
content, impact estimates or account configuration. Confirmation independently recompiles
that projection and compares the approved plan, preview and fingerprint before consuming
the draft. Internal unchecked dismissal plans are refused too.

The request contains one resource-name-only operation, with partial failure disabled.
There is no validation-only support, automatic rollback or automatic retry. An uncertain
submission or invalid response consumes the draft and reports `UNKNOWN_WRITE_OUTCOME`.
After a valid response, verification requires the exact observed projection with
`dismissed=true`. Disappearance, read failure, duplication or changed associations/type
reports applied but unverified. Absence is never proof of success.

Dismissal does not alter ads, budgets or bidding, disable future suggestions or an
auto-apply subscription, or guarantee suppression duration or undo. Verification covers
only the projected suggestion state. Live row visibility and read-after-write timing
remain unverified; Google may accept a dismissal that this tool cannot positively
verify because the row disappears or the read has not caught up.

Offline tests use real v25 request/response/row messages and fake provider reads and
writes. SDK row conversion represents known enums numerically and omits unset optional
association strings; the reader normalizes those to known names and empty strings.
The dismissed flag must still be explicitly present and boolean.
