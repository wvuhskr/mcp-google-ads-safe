# Google Ads MCP Safe

A Google Ads MCP server that lets an AI assistant change your account, with the brakes built into the server instead of the prompt.

MCP (Model Context Protocol) is the standard that lets assistants like Claude, Codex, or Cursor call tools. Most Google Ads MCP servers are either read-only (Google's own is, on purpose) or hand the assistant raw write access and hope for the best. This one sits in the middle: 60 tools that cover research, reporting, and account changes across Search, Performance Max, and Demand Gen, where every change has to pass a set of checks the assistant cannot talk its way around.

Independent, third-party project. Not affiliated with, endorsed by, or maintained by Google. Uses Google Ads API v25. MIT licensed.

## Why another Google Ads MCP?

I manage paid media day to day. I wanted an assistant that could pull search terms, draft negatives, and adjust a target CPA on a Monday morning without me worrying that a misread instruction would triple a budget or un-pause a campaign I had killed for a reason. The write-capable servers I found had one or two safeguards each. None had the full set, and the ones that came closest had a single user.

So the design goal here is simple: the assistant proposes, the server decides whether that proposal is allowed, and nothing reaches Google until a second explicit call says go.

### How it compares

| | Google official server | Typical community write server | Google Ads MCP Safe |
| --- | --- | --- | --- |
| Reads (GAQL, reports, account discovery) | Yes | Yes | Yes |
| Writes | No, by design | Yes | Yes, off by default |
| Per-account write allowlist | n/a | Rare | Yes, must be a subset of the read allowlist |
| Preview then separate confirm call | n/a | Some | Yes, every write |
| Daily budget, CPC, and target CPA ceilings | n/a | Some | Yes, three separate caps |
| Refuses to clear a Performance Max target (budget still caps spend; this protects cost per result) | n/a | Not found | Yes |
| Extra opt-in for shared or blast-radius resources | n/a | Not found | Yes, seven separate switches (shared budgets, portfolios, conversion goals, shared audiences, recommendations, shared negative lists, permanent removal) |
| New campaigns, ad groups, ads created PAUSED | n/a | Some | Yes, always |
| Local audit log | n/a | Some log applied writes | Logs drafts, refusals, applies, and unknown outcomes |
| Fresh state re-check at confirm time | n/a | Not found | Yes |
| Undo for applied changes | n/a | Not found | Yes, drafts the reverse from the audit log |
| Automated tests | Unknown | Usually few or none | 3,851 |

"Typical community write server" summarizes the open-source write-capable servers I could find on GitHub in September 2026. The best of them ship two or three of these gates. To my knowledge none combine all of them in a maintained project, but I have not audited every repo, and this table is a snapshot, not a scoreboard. The [Google official server](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server) is the right choice if you only need reads and want Google-maintained code.

## What the assistant can do

| Area | Tools |
| --- | --- |
| Reporting and lookups | Account info, campaign / ad / keyword / search-term / geo performance, negatives, extensions, policy issues, conversion actions, recommendations, raw GAQL, geo-target search |
| Keyword research | Keyword Planner ideas and campaign forecasts |
| Campaign and ad-group changes | Budget, name, status, target CPA / ROAS, ad-group CPC, pause / enable, ad schedule, remove |
| Creation | Search campaigns and ad groups, responsive search ads, non-retail Performance Max campaigns and asset groups, Demand Gen campaigns and single-image ads, portfolio bid strategies |
| Keywords and targeting | Add / remove keywords and bids, campaign and shared negative lists, geo exclusions, audience targeting, custom audiences, listing-group filters |
| Assets and extensions | Sitelinks, callouts, structured snippets, image and text asset upload |
| Conversions and recommendations | Create conversion actions, set primary status, apply or dismiss recommendations (each behind its own opt-in) |

The full list with inputs, limits, and what has and has not been tested live is in the [tool reference](docs/tool-reference.md). Creation tools deliberately support a narrow set of campaign shapes. If you need every knob in the Google Ads UI, this is not that.

## How a change actually happens

```text
assistant calls update_campaign(...)
  -> server checks: writes enabled? account on the write allowlist? feature opt-in set?
  -> server reads current state, checks budget / bid ceilings and bidding rules
  -> returns a preview plus a draft id (expires in 60 min, dies on restart)

assistant calls confirm_and_apply(draft_id)
  -> server re-reads account state, re-runs the same checks
  -> sends exactly one mutate to Google
  -> appends the outcome to ~/.mcp-google-ads-safe/audit.jsonl
```

If Google's answer is ambiguous (a timeout after the request left the building), the server records the outcome as unknown and refuses to retry on its own. You read the account and decide.

Changed your mind? `undo_change(draft_id)` reads the audit log, finds what the original change replaced, and drafts the reverse. It is a normal draft: same caps, same allowlists, same confirm step, and a fresh look at the account before anything is sent. Budgets, names, statuses, targets, bids, keyword and negative additions or removals, and schedules can be reversed. A creation that has since been enabled gets paused. Removals and asset uploads are permanent at Google, and undo says so instead of pretending.

Things worth being honest about:

- `confirm_and_apply` is a second call, not a human approval step. Any connected client holding the draft id can confirm it. If you want a person in the loop, do the confirm yourself or run the server in read-only mode and use it for reporting.
- Ceilings are per-operation limits, not an account spend guarantee. A cap on daily budget does not stop ten campaigns each at the cap.
- State checks are point-in-time. A change made in the Google Ads UI between preview and confirm can still slip through if it does not alter the fields the server compares.
- Drafts live in the server process's memory. Restart the server and they are gone.
- If you enable writes without setting a write allowlist, the default account from `GOOGLE_ADS_CUSTOMER_ID` becomes writable. Set the allowlist explicitly.

Details and every environment variable: [configuration](docs/configuration.md).

## Test coverage and live evidence

Every one of the 3,851 tests runs offline against a fake Google Ads API. CI runs them on Ubuntu and macOS across Python 3.12, 3.13, and 3.14 on every push. That proves the request shapes, the safety logic, and the refusal paths. It does not prove Google accepts a given operation on your account.

Live evidence is uneven and documented per tool. All reporting and lookup tools have been run against a real account. A subset of writes (budget, name, target CPA / ROAS, ad-group CPC, ad-group pause / enable, exact-match keywords, Manual CPC campaign and ad-group creation) has been applied to a real account and read back. Most of the Performance Max, Demand Gen, asset, conversion, and recommendation writes have offline tests only. The [evidence matrix](docs/offline-capability-matrix.md) says which is which. Read it before enabling writes for anything you would not want to fix by hand.

## Install

Requires Python 3.12 or newer, a Google Ads API credential file, and manager (MCC) access to any account you plan to allowlist. A service account is the recommended credential; an existing Desktop OAuth profile also works. Read the [authentication note](docs/google-auth-transition.md) first, since Google changed API access registration in September 2026.

In a terminal, from the folder where you cloned this repo:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Then follow the [setup guide](docs/setup.md) to point `GOOGLE_ADS_YAML` at your credential file, set your read allowlist, connect your MCP client, and run `health_check`. Writes stay off until you set `GOOGLE_ADS_ENABLE_WRITES=true` and a write allowlist. Keep credentials outside the repo folder.

## Status

Version 1.0.1. Source install only; no PyPI package yet. CI runs lint, the full test suite, and a high-severity Bandit scan on Ubuntu and macOS across Python 3.12, 3.13, and 3.14. Windows has not been tested.

## Docs

- [Setup](docs/setup.md), [configuration](docs/configuration.md), [troubleshooting](docs/troubleshooting.md)
- [Tool reference](docs/tool-reference.md) and [evidence matrix](docs/offline-capability-matrix.md)
- [Contributing](CONTRIBUTING.md), [security policy](SECURITY.md), [changelog](CHANGELOG.md), [license](LICENSE)
