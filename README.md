# Google Ads MCP Safe

Broad Google Ads coverage. Safety gates built into every account change.

Google Ads MCP Safe gives an AI assistant 59 purpose-built tools for researching keywords, inspecting accounts, reporting performance, and preparing controlled changes across Search, Performance Max, and Demand Gen. MCP means Model Context Protocol, the standard that lets an assistant call these tools. This is an unofficial project using Google Ads API v25.

Version 1.0.0 is available here for private owner review. The repository has not been approved for public launch, and no public package release is announced.

## Why use it?

### Broad coverage across the paid-search workflow

Work from research and reporting through campaign, creative, targeting, and conversion configuration in one toolset. Instead of writing every request from scratch, an assistant can call tools with defined inputs, previews, and operation-specific limits.

| Workflow | Included capabilities |
| --- | --- |
| Research and reporting | Keyword ideas and forecasts; account, campaign, ad, keyword, search-term and geographic reporting; policy and recommendation reads. |
| Campaign management | Search campaign and ad-group creation; non-retail Performance Max and bounded Demand Gen campaign creation; budget, bid, status and schedule changes. |
| Ads and assets | Responsive search ads; sitelinks, callouts and structured snippets; image and text assets; bounded Performance Max asset-group and Demand Gen ad workflows. |
| Targeting and exclusions | Keywords, negative keywords and shared negative lists; geographic exclusions; supported audience and listing-group controls. |
| Conversions and recommendations | Supported conversion-action configuration and primary-status changes; supported recommendation application and dismissal. |

The count is verified from the 59 registered tools in this version's [source](mcp_google_ads_safe/tools.py), including reads, draft operations, health and confirmation. A tool count describes the interface, not complete Google Ads feature coverage or a measure of quality. Exact supported shapes and restrictions are in the [tool reference](docs/tool-reference.md).

For a concrete comparison, [Google's official MCP guide](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server), checked September 11, 2026, describes a read-only server for account discovery, queries and resource metadata. This project adds dedicated management workflows and guarded changes. That comparison is to the documented official server at that date, not a claim to have more features than every third-party Google Ads MCP.

### Safety gates built into the change workflow

The safeguards run in the server. They do not depend on an assistant remembering a prompt instruction.

| Gate | What it does |
| --- | --- |
| Read-only startup and account permissions | Writes are off by default. Enabling them also requires explicit permission for the target account in both the read and write account lists. |
| Preview, then confirm | A change-producing tool returns an expiring draft. A separate `confirm_and_apply` call is required before the server sends it to Google. |
| Fresh checks before execution | Confirmation rechecks current account state and applicable safety rules; an invalid or stale plan can be refused. |
| Budget, bid and shared-resource controls | Configured daily-budget and cost-per-click caps, bidding-strategy rules, and additional opt-ins restrict supported changes to sensitive or shared resources. These are operation limits, not an account-wide spending guarantee. |
| Outcome tracking | The local audit trail records change attempts and outcomes. Uncertain or applied-but-unverified outcomes are not automatically retried; investigate before preparing another change. |

Typical change flow:

```text
Request change → check permissions and limits → return draft and preview
Confirm draft → recheck account state and limits → send change → record outcome
```

In plain terms: the assistant can prepare a change, but the server checks whether that change is allowed before it can be applied.

The confirmation step is not proof of human approval. Any connected caller with a valid draft identifier can confirm it, including an assistant. Drafts live in one running process, expire after 3,600 seconds by default, and disappear on restart. Use a trusted assistant client, review previews, and keep writes disabled when you only need reporting. Current-state checks are point-in-time checks, not a lock against concurrent account changes. These controls do not guarantee policy approval, successful ad delivery, or profitable performance. See [configuration](docs/configuration.md) for exact settings.

## What has been verified?

The private 1.0.0 build passed 3,807 synthetic tests on each of Python 3.12.13, 3.13.14 and 3.14.6 on macOS Apple Silicon. Separately authorized checks covered Desktop sign-in and renewal, account health, bounded current reporting and lookups, keyword discovery, and an unsaved forecast. These results come from this project's September 11, 2026 build and validation records; they are not advertising performance metrics.

Live write evidence varies by operation. Some management paths have only offline tests, and creation tools support deliberately limited campaign and asset shapes. Neither the feature table nor the test count means every feature has been accepted by Google on a live account. Read the [operation evidence matrix](docs/offline-capability-matrix.md) and [release evidence](docs/release-readiness.md) before enabling writes.

## Install from a source folder

You need Python 3.12 or newer, this source folder, a private Google Ads credential profile, and manager access to every advertising account you authorize. The recommended profile uses a service account, an app-owned identity; an existing Desktop user authorization profile remains supported. Fresh isolated installations on macOS Apple Silicon passed build, dependency, installed-package and complete synthetic tests on Python 3.12.13, 3.13.14 and 3.14.6 with `google-ads` 32.0.0. These installation checks did not access real credentials or Google accounts; other platforms and dependency combinations remain unverified. Read the [Google authentication transition note](docs/google-auth-transition.md) before a fresh setup.

In the macOS Terminal app, replace the path with this source folder:

```sh
cd /absolute/path/to/mcp-google-ads-safe
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Installation may download dependencies. It was not executed as part of this documentation change.

## First read-only connection

Follow the [start-to-finish setup guide](docs/setup.md) to configure a private credential file, select permitted accounts, connect an assistant client and run a read-only health check. Keep credentials outside the project folder. The Desktop helper supports creating or replacing a private sign-in profile; service-account setup requires an existing identity and key. Revoked-token recovery and additional platforms or assistant clients remain unverified.

## Documentation and contributing

- [Setup](docs/setup.md), [configuration](docs/configuration.md) and [troubleshooting](docs/troubleshooting.md).
- [Complete tool reference](docs/tool-reference.md) and [Google authentication transition](docs/google-auth-transition.md).
- [Operation evidence](docs/offline-capability-matrix.md) and [release readiness](docs/release-readiness.md).
- [Contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md).
- [MIT license](LICENSE) and [change history](CHANGELOG.md).
