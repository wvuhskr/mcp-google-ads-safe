# Changelog

## 1.1.0 - 2026-09-14

- New tool `undo_change(draft_id)`: drafts the reverse of an applied change from the audit
  log. Rides the normal draft/confirm rails and never dispatches on its own. Reversible:
  campaign and ad-group updates, pause/enable, keyword and negative additions or removals,
  keyword bids, schedules; enabled creations are paused. Removals and uploads are reported
  as permanent. See the [tool reference](docs/tool-reference.md#undo).
- Combined `update_campaign` previews record `current_daily_budget` so a budget change made
  alongside other fields can be undone.
- 3,853 tests. Tool count 60.

## 1.0.1 - 2026-09-14

First public release. A same-day public-readiness review found two High and seven Medium
issues; all are fixed here. Breaking for nobody: every new control defaults to the old
behavior or to "off".

Safety:
- Clearing target CPA or target ROAS on a Performance Max campaign is refused
  (`PMAX_TARGET_CLEAR`). The daily budget still caps spend; this protects cost per result.
  Raise the target in steps instead. A product choice, not a Google requirement.
- The draft store is serialized with a lock, so two concurrent confirms cannot both dispatch.
- Only documented sign-in keys are read from the credential YAML; any other key (including
  `logging`) stops startup.
- Transport-class failures from the mutate RPC (Unavailable, DeadlineExceeded, Internal,
  Unknown, Aborted, Cancelled, ResourceExhausted) are audited as `unknown`. Definite
  server-side rejections (InvalidArgument, PermissionDenied, NotFound, Unauthenticated and
  similar) stay `error`, since Google answered and saved nothing.
- Refused confirms for unknown, expired or tampered drafts are audited (`UNKNOWN_DRAFT`,
  `DRAFT_EXPIRED`, `PLAN_TAMPERED`).
- A write that landed but failed read-back is audited as `apply_unverified`, not `error`.
- New `GOOGLE_ADS_MAX_TARGET_CPA` cap, separate from `GOOGLE_ADS_MAX_CPC` (falls back to
  it when unset).
- New `GOOGLE_ADS_ALLOW_REMOVE_ENTITY` opt-in (default false) for permanent removal,
  checked at draft and again at confirm.
- One generic input guard rejects string arguments that would coerce to JSON null, list
  or object, for every tool.
- Report dates must be `YYYY-MM-DD`; geo-target search escapes backslashes.

Project:
- README rewritten for a public audience with a comparison against other Google Ads MCP
  servers. SECURITY.md routes reports through GitHub private vulnerability reporting.
- CI: ruff, the full test suite and a high-severity Bandit scan on Ubuntu and macOS across
  Python 3.12, 3.13 and 3.14. Dependabot for pip and GitHub Actions.
- The three extension-state readers collapsed into one; no behavior change.
- 3,830 tests (was 3,807).

## 1.0.0 - 2026-09-11

Stable source release, reviewed privately before the public 1.0.1. The operation inventory
retains explicit distinctions between current reads, historical write evidence and
synthetic tests.

- Added bounded read and draft/confirm operations for Search, Performance Max,
  Demand Gen, assets, audiences, conversions, recommendations, keyword research and
  shared negative sets. See the [operation inventory](docs/offline-capability-matrix.md)
  for branch-specific scope and evidence.
- Documented future approval and independent saved-state checks in
  [validation readiness](docs/validation-readiness.md). Offline synthetic evidence,
  dated provider reads, exact-payload validation and applied/read-back evidence remain
  separate; no blanket provider acceptance, serving or performance claim is made.
- Retired superseded test-account setup directions, clarified real startup preflight,
  and recorded remaining [release gates](docs/release-readiness.md).
- Reorganized public onboarding around source installation, an existing private Google Ads
  credential profile, a generic MCP client connection, a live read-only first check,
  [configuration](docs/configuration.md), and [troubleshooting](docs/troubleshooting.md).
- Moved the complete capability and evidence ledger to the
  [tool reference](docs/tool-reference.md) so the main README can lead with setup and safety.
- Required an explicit credential profile path through `GOOGLE_ADS_YAML` or the client
  factory argument, removing the former implicit path.
- Adopted `google-ads>=32.0.0,<33` so service-account profiles can omit the retired
  developer token, while retaining existing Desktop user profiles and all server account
  and write guards. This is offline compatibility evidence, not a stable release claim.
- Recorded a September 10, 2026 bounded check in which one existing user-authorization profile
  refreshed authorization and completed account discovery plus installed startup
  prerequisites and health for one selected advertiser with writes disabled. This does
  not prove first-time setup, recovery, an assistant connection, every provider operation,
  or stable-release readiness.

Final artifact checks, exact source/history privacy clearance and publication controls
remain separate from the bounded validations listed below. Alternative setup paths and
mutation branches retain their documented evidence limits.
Existing private Alpha artifacts predate these documentation changes.

- Verified Desktop initial sign-in, explicit private-profile replacement and restarted
  health check through an MCP client (OpenAI Codex) with writes disabled. Service-account provisioning and deliberately
  revoked-token recovery remain unverified alternatives.
- Qualified fresh macOS Apple Silicon installations on Python 3.12.13, 3.13.14 and
  3.14.6 with complete synthetic suites; retained exact dependency resolutions.
- Verified current account/report/entity/policy/configuration/recommendation reads
  for one account. A direct query reconciled campaign counts and spend; negative
  keywords exercised multiple pages. Search terms remain capped.
- Verified one keyword-discovery page and an unsaved campaign forecast; results are
  provider estimates with explicit unavailable fields, not actual advertising outcomes.
- Included setup/reference documentation and this changelog in source distributions.
