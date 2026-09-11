# Validation readiness

Operation-specific live validation still needs individual approval. Bounded Desktop
sign-in, replacement and health checks have completed as recorded in
[release readiness](release-readiness.md). This future procedure grants no additional calls. The September 4, 2026 decision abandoned test-account
provisioning. Do not retry it. Although the capability matrix retains a conditional
"candidate" label for planning, actual validation uses individually approved probes
against a real account.

## What each evidence stage can establish

1. Offline synthetic checks can establish local input refusal, request construction,
   draft binding, audit behavior and simulated result/readback handling.
2. Exact-payload validation can establish only that Google accepted that exact request
   shape with `validate_only=true` at that time. It saves nothing.
3. One applied request plus an independent raw read can establish the observed saved
   state for that operation. It does not establish policy approval, serving, reach,
   attribution, performance, future consistency or support for a sibling branch.
4. Serving, recommendation availability, policy, conversion behavior, audience
   population, retail product eligibility and performance need later live observation
   in a suitable account and cannot be inferred from a paused saved object.

## Required future sequence for one operation

1. Obtain separate approval for the exact read or write, named account scope, target,
   payload, expected residue and cleanup limit. Credentials and account setup are also
   approval-gated and must never be copied into public evidence.
2. Make fresh, complete prerequisite reads through the approved account. Capture the
   raw immutable input snapshot, all pages, source commit, tool name, account scope,
   request digest and time in private evidence. Stop on missing, partial, stale,
   foreign, unknown or conflicting state.
3. Compile the exact draft. Where supported, send that identical payload once with
   `validate_only=true`. Record the provider response and prove that no saved change is
   claimed. Recommendation apply and dismissal do not support this stage.
4. Ask for individual approval for the live write after the exact draft and residue are
   reviewable. Apply once through `confirm_and_apply`; never bypass the draft/audit path.
5. Independently read the exact resource and the complete affected population described
   in the matrix. Preserve returned resource identities and creation residue. A positive
   mutation response without the required saved-state read is applied but unverified.

An ambiguous transport result, unexpected identity, incomplete read or failed postcheck
consumes the draft. Investigate with reads. Never automatically retry it. Restoration is
a separate write with its own fresh reads, exact draft, validation where supported and
individual approval. Restoration does not erase provider history or created-resource
residue.

## Approval-gated prerequisite families

| Family | Concrete prerequisite before any draft | Required independent proof | Cleanup and safety limit |
| --- | --- | --- | --- |
| Search creation and updates | Eligible paused standard Search parent, dedicated versus shared budget ownership, exact bidding strategy, full children and targeting, unique names | Exact campaign/group/ad/criterion state plus unchanged parent, budget and sibling inventory | Pausing reduces immediate serving risk but cannot protect shared resources or account-wide settings; created resources and removed identities remain history |
| Portfolio bidding | Non-manager owner, explicit portfolio opt-in, every attached campaign readable and writable, fresh attachment population | Exact strategy and every attached campaign/effective value | An unattached create leaves inventory; edits can affect multiple campaigns; no blanket rollback |
| Performance Max, non-retail | Eligible paused non-retail campaign/group, dedicated budget, current brand and safety settings, owned image/text assets | Exact campaign, budget, group, links, targeting and automation fields | Creates campaign/group/assets links; policy and serving remain unknown |
| Retail listing filters | Merchant Center feed, product availability, paused retail campaign/group and a fully readable admitted listing tree | Exact ordered saved tree, product IDs, parent links and unchanged campaign/group snapshots | Tree replacement removes old identities; no automatic inverse; product eligibility/serving need live observation |
| Demand Gen campaign | Account eligibility, unique campaign name, budget, geography/language and fixed targeting ownership | Exact paused campaign, budget and targeting readback | Leaves campaign and budget; creates no group or ad |
| Demand Gen ad | Externally existing eligible paused Demand Gen campaign and ad group, square image and logo inventory, brand and budget proof | Exact PAUSED ad identity, format, destination, text/assets and unchanged parents | Leaves a paused ad; no cleanup tool is promised; default call-to-action, policy and serving remain unknown |
| Asset uploads and links | Owned source assets or approved bytes/text, exact dimensions/types, complete link populations | Provider result identity and exact raw asset/link reads | Bare assets remain after unlinking; asset bytes/newness may not be independently readable |
| Audiences | Explicit audience opt-in, website-list ownership/rules, campaign-level mode and no ad-group targeting conflict | Exact list content, mode, campaign criterion and connection reads | List can collect while unattached; TARGETING can restrict future reach; tag/consent/population remain separate |
| Conversions | Explicit conversion opt-in, controlled manager tree, owner/actions, customer goals, custom goals and bidding-use scope | Exact action settings plus all relevant goal/custom-goal references | Campaign pause is insufficient for account-wide conversion effects; secondary status does not exclude custom-goal bidding |
| Recommendations | A currently visible exact recommendation with serving data, fresh descriptor and all affected campaigns/budgets | Apply: saved budget/configuration. Dismiss: same row with `dismissed=true` | No validation-only method. Stale/disappeared rows are not success; no rollback or durable suppression guarantee |
| Shared negative sets | Shared-edit opt-in, owned set, complete members and links, raw provider counts and removed-link tombstones, every attached campaign paused standard Search | Exact old-plus-new membership/link population, counts, tombstones and unchanged campaigns | Links have no pause control. Later campaign enable or list edits create serving impact. No set delete/detach operation is promised |

## Refusal and stop conditions

- Refuse before provider construction when writes, account scope or a family opt-in is
  absent. Refuse malformed identifiers, explicit nulls where unsupported and extra fields.
- Refuse incomplete pagination, missing raw fields, unknown enum/status/type values,
  foreign ownership, unexpected active parents, duplicate or removed associations and
  any change between draft and confirmation.
- Refuse when the affected population cannot be bounded, including portfolio attachments,
  conversion goals, shared-set raw counts/tombstones, retail trees and recommendation scope.
- Stop after any unknown outcome or failed saved-state proof. Keep the audit and immutable
  creation identities, use reads to establish state, and seek a new decision.

## Evidence packet required for a future claim

For one approved operation, retain privately: source commit and source-file hash; exact
registered tool and material branch; approval record; sanitized account role and target
purpose; fresh prerequisite pages; draft preview and digest; validation-only request and
response when supported; one apply audit; raw mutation result; independent saved-state
pages; refusal probe; cleanup decision; and explicit unknowns. Public documentation may
state only sanitized behavior and limits.

A provider claim must name its tier and date. Validation-only acceptance, saved mutation,
independent readback, policy approval, serving and performance are separate claims. Never
upgrade old evidence merely because the current tool has the same name.

## Current readiness decision

The source has a statically reconciled 59-tool inventory and extensive retained offline
evidence. Desktop sign-in, explicit replacement and restarted Codex health are verified
for one account. Fresh Mac installations on Python 3.12.13, 3.13.14 and 3.14.6 each
passed the full synthetic suite with their recorded dependency resolutions. These
bounded checks do not establish operation-specific provider behavior, from-zero Cloud
setup, every dependency/platform combination or public release. Remaining live probes
require individual approval; privacy/history clearance and final release controls remain
open. Server cutover is a separate operating choice. See [release readiness](release-readiness.md)
for the exact completed scope.
