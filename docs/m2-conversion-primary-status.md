# Existing conversion primary status

In an MCP client connected to this server, call
`set_conversion_action_primary_status(conversion_action_id, primary_for_goal, customer_id=None)`.
This drafts a change for confirmation. IDs must be canonical positive numeric strings;
primary_for_goal must be an exact boolean. Only None selects the default customer.

Only existing ENABLED WEBPAGE actions are supported. Missing, hidden, removed, unsupported,
unknown, foreign-owner and unchanged actions refuse. Any known category is supported when
complete existing category/origin goal coverage reconciles. Only primary_for_goal changes:
no creation, deletion, tagging, uploads, value, attribution, category, origin, status or goal edits.

Writes, selected and owner read/write permission, and
GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT=true are required. The entire visible configured login
manager tree must be accessible, including unrelated branches; every account tracking through
the owner must be write-authorized. This establishes only that configured tree's scope.

The preview names the selected account, conversion owner, action before/after, tracking
accounts, unchanged customer and campaign goals, their applicable configuration, and every
custom goal containing the action with its campaign references, including historical statuses.
Primary status allows ordinary bidding only for relevant biddable goals, subject to CUSTOMER
versus CAMPAIGN settings; it never guarantees serving. A campaign using a custom goal
containing this action can still bid on it regardless of primary status.
No automatic goal creation or goal flag change is expected for this existing-action update.

Confirmation rechecks the complete action/control/goal/custom inventory and approved request.
The request has one ConversionAction update through the existing atomic GoogleAdsService
mutate path, with an exact primary_for_goal mask. Validation-only makes no saved-state reads
and does not claim an applied change. After application, the exact returned owner identity
is checked before reading the action and the complete inventory again. Any mismatch or
unreadable state reports applied but unverified and consumes the draft; no retry or restore.

Verification is offline with fake accounts and real installed v25 message classes. No live
provider acceptance, serving behavior, or account changes have been tested for this tool.
