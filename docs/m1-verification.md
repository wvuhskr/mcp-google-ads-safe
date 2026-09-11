# Milestone 1 verification

This page separates local test evidence from calls made to the Google Ads API. “Live read”
means Google accepted the query and returned a complete response. “Validation only” means
Google accepted a mutation payload with `validate_only=true`; no change was applied.

| Tool | Offline evidence | Live evidence through the tool |
|---|---|---|
| `health_check` | MCP call with fake account data | Live authentication, manager ancestry, and account listing confirmed on 2026-09-04 and 2026-09-07 |
| `run_gaql` | MCP call with a fake complete response | Live query and response confirmed on 2026-09-04 |
| `get_account_info` | Fake query and response tests | Live read confirmed on 2026-09-04 |
| `get_campaign_performance` | Fake query, dates, pagination, and response tests | Live read confirmed on 2026-09-04 |
| `get_ad_performance` | Fake query, pagination, and response tests | Live complete-page read confirmed for 2026-09-06 data on 2026-09-07 |
| `get_keyword_performance` | Fake query, pagination, and response tests | Live complete-page read confirmed for 2026-09-06 data on 2026-09-07 |
| `get_search_terms` | Fake query, cap, pagination, and response tests | Live complete-page read confirmed for 2026-09-06 data on 2026-09-07 |
| `get_geo_performance` | Fake query, pagination, and response tests | Live complete-page read confirmed for 2026-09-06 data on 2026-09-07 |
| `get_negative_keywords` | Fake query and response tests | Live read confirmed on 2026-09-04 |
| `search_geo_targets` | Fake query and response tests | Live `Canada` lookup through the tool with writes disabled on 2026-09-07: 17 returned rows matched Google’s total of 17, with `pages_complete=True` |
| `get_entities` | Fake lookup and response tests | Live campaign lookup confirmed on 2026-09-04; other entity types are not separately live verified |
| `list_accounts` | MCP call with fake account data | Registered tool call through the MCP boundary confirmed live with writes disabled on 2026-09-07 |
| `update_campaign` | Fake compile, draft, drift, mutation, and postcheck tests | Budget change applied and independently read back on 2026-09-04. On 2026-09-07, name and standard campaign CPA/ROAS set/clear changes were applied, read back and restored on paused campaigns. Status and portfolio target paths are not separately applied-and-read-back verified. |
| `update_ad_group` | Fake compile, draft, strategy guard, mutation, and postcheck tests | On 2026-09-07, Manual CPC and standard CPA override set/clear changes were applied, effective values read back, and originals restored with paused parent campaigns. Name/status and portfolio paths are not separately live verified. |
| `pause_entity` | Fake draft, refusal, mutation, and readback tests | Ad-group pause applied and independently read back on 2026-09-04; campaign pause is not separately applied-and-read-back verified |
| `enable_entity` | Fake draft, refusal, mutation, and readback tests | Ad-group enable applied and independently read back on 2026-09-04; campaign enable is not separately applied-and-read-back verified |
| `confirm_and_apply` | Fake expiry, tamper, drift, single-dispatch, unknown-outcome, audit, and postcheck tests | Used in the budget and ad-group status canaries on 2026-09-04 and all approved temporary changes/restorations on 2026-09-07. Each new request was first accepted in validation-only mode and linked to its audit record. |
| `draft_keywords` | Fake schema, policy, ownership, drift, mutation, and postcheck tests | On 2026-09-07, an exact-match positive keyword was created in a paused Search campaign and independently confirmed PAUSED. |
| `remove_keywords` | Fake schema, policy, ownership, drift, and mutation tests | On 2026-09-07, only the new test keyword was removed. Google acknowledged the exact resource removal and complete reads confirmed its absence; a historical REMOVED row was not retrieved. |
| `update_keyword_bid` | Fake bid-policy, drift, mutation, and effective-bid postcheck tests | On 2026-09-07, the paused exact-match test keyword received a Manual CPC bid change, and the effective value was read back before the test keyword was removed. |
| `add_negative_keywords` | Fake schema, policy, collision, drift, and mutation tests | On 2026-09-07, an exact-match campaign negative was created on a paused campaign and independently read back. |
| `remove_negative_keywords` | Fake schema, policy, ownership, drift, and mutation tests | On 2026-09-07, only the new test negative was removed. Google acknowledged the exact resource removal and complete reads confirmed its absence; a historical REMOVED row was not retrieved. |
| `exclude_geo_target` | Fake geo lookup, collision, ownership, drift, and mutation tests | On 2026-09-07, one ZIP exclusion was validated, applied and read back on a paused campaign after removing its original inclusion. A private reviewed restoration request removed that exclusion and restored the original inclusion. |
| `remove_geo_target` | Fake geo lookup, ownership, drift, and mutation tests | On 2026-09-07, one included ZIP was validated, removed and independently confirmed absent on a paused campaign, then restored after the exclusion test. |
| `set_campaign_schedule` | Fake time-zone, full-replacement, window-cap, drift, and atomic mutation tests | On 2026-09-07, the full week was replaced on a paused campaign without bid adjustments, read back, and the original hours/status/modifier state restored. Criterion identities changed during replacement. |

The four 2026-09-07 performance reads returned complete pages: the returned row count
matched Google's `total_results_count`. This proves query acceptance, pagination, and the
response contract. It does not independently validate conversion attribution, which is the
rule deciding which ad receives credit and how far back that credit can reach.

The 2026-09-07 criteria validation covered all eight criteria mutations. The separate update
validation covered campaign name, ad-group CPC, campaign CPA and ROAS, campaign CPA clear,
ad-group CPA, and ad-group CPA clear. Every validation forced `validate_only=true`, and no
actual application occurred. Private raw proof stays outside the tracked project.

Six live refusal probes on 2026-09-07 also returned their intended codes before dispatch:
writes disabled, budget above cap, shared budget, Smart Bidding, CPC above cap, and an amount
that cannot be represented exactly. A registered `list_accounts` call also succeeded through
the MCP boundary with writes disabled. These checks changed no advertising data.

The source distribution and wheel both built successfully offline on 2026-09-07. A narrow
artifact inspection confirmed that the wheel contains only the package, license, metadata,
entry point, and record files. The source distribution intentionally includes the test suite;
that inspection found a real customer identifier in one test fixture, which was replaced with
synthetic data. The corrected wheel and source distribution were then rebuilt and passed the
same narrow inspection. A separate environment installed the corrected wheel with its declared
dependencies, including `google-ads` 31.4.0 and `mcp` 2.2.0. Running outside the source folder
confirmed the installed package import, exactly 25 tool names with serializable input schemas,
and the console entry point.

After the final-review code corrections on 2026-09-07, the parent reran the full offline suite:
501 tests passed with warnings treated as errors; Ruff and the whitespace diff check passed.
The final corrected wheel and source distribution were rebuilt and passed narrow inspection.
Installing that final wheel outside the source folder again confirmed exactly 25 tool schemas
and the console entry point, and the installed client file's hash matched the source file.
The scoped reviewer marked I1, I2 and M1 addressed and reported no new defect.

The parent also exercised the corrected shared-budget path against Google on 2026-09-07:
the scope read returned two attached paused campaigns, matching the budget's authoritative
reference count of two. Google then accepted the proposed budget request with validation-only
mode forced; `applied=False`. This establishes live scope reconciliation and request acceptance,
not an applied shared-budget change or effective-state readback. These are parent-recorded
verification results; private proof remains outside the tracked project. A broad privacy and
release review is still required.

## Remaining release gates

- Apply narrow, reversible canaries for each unverified mutation family and independently read back the effective result.
- Complete milestone 2 and milestone 3 scope and verification; milestone 1 alone does not authorize publication.
- Run the broad public privacy review, finish public documentation, and rebuild and inspect release artifacts after any further changes before publication.

## Approved live checks on 2026-09-07

After explicit user approval, a private runner invoked the registered tool implementations
and `confirm_and_apply` for seven bounded sequences: campaign name, ad-group Manual CPC,
full-week schedule, positive keyword creation/bid/removal, campaign negative creation/removal,
standard campaign/ad-group CPA targets, and a standard campaign ROAS target. These were real
account writes through the new server code; the runner did not replace the connected MCP
server or claim a separate live wire-protocol test.

Each of the 17 mutation requests, including restorations, was validated with Google first,
applied once, independently read back, and matched to its audit record. All selected campaigns
remained paused. Final reads matched the saved starting campaign/ad-group settings, original
keywords and negatives, schedule hours/status/modifiers, and other campaign criteria. New
schedule resource identities and change records remain; creation/removal is not an erasure
of history. Google's exact removed-resource queries returned no historical rows for the test
keyword and negative. Removal proof consists of Google's exact-resource acknowledgement plus
complete current-inventory and exact-resource reads showing no surviving test entry.

These counts describe test requests, not advertising performance. Raw before/after snapshots,
validation responses, mutation responses, audit records and the first historical-visibility
check are preserved privately outside the tracked source. No location change, campaign enable,
budget change, shared strategy change, server cutover or publication was performed in this
approved batch. Location canaries still need a separately prepared and approved restoration
method; the full milestone exit gate remains open.


## Location and connection closeout on 2026-09-07

After separate explicit approval, the parent completed the single-ZIP sequence with the
registered tool implementations and existing draft/apply rails: remove its positive inclusion,
create its negative exclusion, then atomically remove the exact new exclusion and recreate the
original positive inclusion through a reviewed private restoration helper. All three requests
passed Google validation first, applied once, were independently read back and matched to draft
and apply audit records. The final repeat read matched the saved original targeting, campaign
status, budget and bidding strategy. The campaign remained paused. Google reused the same
location criterion identity across polarity changes; no claim of erased change history is made.
The helper is private test equipment, not a public arbitrary-operation tool.

An isolated real MCP connection with writes disabled exercised all twelve M1 read tools.
The negative-keyword inventory was retrieved across every returned page and reconciled against
Google's total. A connection-library error-masking problem was corrected in commit `2bf026a`:
known safety refusals now expose code and reason, uncertain outcomes instruct inspection before
retry, and unexpected exception details stay hidden. The corrected package passed 509 tests
with MCP 2.2.0; eight focused boundary tests also passed with MCP 1.28.1. Three older-library
whole-suite test-shape failures were reproduced on the unchanged baseline and remain a test
portability limitation. The corrected package was independently installed, checked against
source and narrowly inspected for private files and markers.

The subsequent actual-connection refusal probe mapped disabled-write refusals to budget,
standard target CPA, ad-group update, pause, enable, schedule, keyword create/remove/bid,
negative create/remove, location exclude/remove and confirmation. Every call visibly returned
`WRITES_DISABLED`; no apply audit event occurred. These are shared write-gate proofs, not a
claim that every operation-specific policy refusal was separately tested live. Earlier cap,
shared-budget and Smart Bidding refusal evidence remains applicable within its recorded scope.

Real Google applications were invoked through registered Python tool implementations. Actual
MCP transport proof covers reads and refusals; a real draft/confirm application through that
transport has not been separately demonstrated. Before replacing the current daily driver,
retain this limitation and complete a separately approved bounded transport application check.
No server replacement is performed by these checks. Local M2 development can proceed.

### Weekly-workflow evidence index

Private proof paths below are relative to the working copy's parent folder and are not release
files. They identify preserved source evidence rather than embed account data in public docs.

| Workflow | Preview/application/readback/audit | Refusal | Restoration or limit |
|---|---|---|---|
| Daily budget | September 4 dated record in CLAUDE.md | Recorded cap/shared-budget refusals; geo-canary/weekly-refusal-summary.json budget | Standalone budget restored; shared-budget live application unverified |
| Standard target CPA | live-approved/campaign_cpa_set.json, adgroup_cpa_set.json and audit.jsonl | weekly-refusal-summary.json target_cpa | Clear/restore records and final-readback.json; portfolio application unverified |
| Pause/enable | September 4 dated record in CLAUDE.md | Dated status refusals plus weekly-refusal-summary.json pause/enable | Ad-group status under paused parent; campaign status application unverified |
| Schedule | live-approved/schedule_set.json and audit.jsonl | weekly-refusal-summary.json schedule | schedule_restore.json and final-readback.json; replacement identities remain |
| Campaign negatives | live-approved/negative_create.json and audit.jsonl | weekly-refusal-summary.json negative_add/negative_remove | negative_remove.json and final-readback.json; exact acknowledgement plus absence |
| Keywords and manual bid | live-approved/keyword_create.json, keyword_bid.json and audit.jsonl | weekly-refusal-summary.json keyword_add/keyword_remove/keyword_bid; prior Smart Bidding/cap probes | keyword_remove.json and final-readback.json; exact acknowledgement plus absence |
| Location | geo-canary/approved/remove.json, exclude.json and audit.jsonl | weekly-refusal-summary.json geo_remove/geo_exclude | approved/restore.json and final.json; original inclusion and unrelated settings match |
| Account data | geo-canary/protocol-*.json and protocol-finish-summary.json | Read allowlist offline tests; write stages not applicable | Query acceptance/count reconciliation, not attribution validation |

All September 7 source data here are configuration checks or query acceptance checks. The
performance query window was September 6, 2026; conversion attribution was not independently
validated. Test counts describe local execution, not marketing results. Final cutover sign-off
and the broader publication/privacy gates remain open.
