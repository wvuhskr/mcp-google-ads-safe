# Changelog

## 1.0.0 - 2026-09-11

Prepared stable source release. Publication is separate; the operation inventory retains
explicit distinctions between current reads, historical write evidence and synthetic tests.

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
  Codex health with writes disabled. Service-account provisioning and deliberately
  revoked-token recovery remain unverified alternatives.
- Qualified fresh macOS Apple Silicon installations on Python 3.12.13, 3.13.14 and
  3.14.6 with complete synthetic suites; retained exact dependency resolutions.
- Verified current account/report/entity/policy/configuration/recommendation reads
  for one account. A direct query reconciled campaign counts and spend; negative
  keywords exercised multiple pages. Search terms remain capped.
- Verified one keyword-discovery page and an unsaved campaign forecast; results are
  provider estimates with explicit unavailable fields, not actual advertising outcomes.
- Included setup/reference documentation and this changelog in source distributions.
