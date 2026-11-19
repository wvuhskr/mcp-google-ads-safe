# Google Ads MCP Safe

A Google Ads MCP server that lets an AI assistant change your account, with the brakes built into the server instead of the prompt.

MCP (Model Context Protocol) is the standard that lets assistants like Claude, Codex, or Cursor call tools. Most Google Ads MCP servers are either read-only (Google's own is, on purpose) or hand the assistant raw write access and hope for the best. This one sits in the middle: 64 tools that cover research, reporting, and account changes across Search, Performance Max, and Demand Gen, where every change has to pass a set of checks the assistant cannot talk its way around.

Independent, third-party project. Not affiliated with, endorsed by, or maintained by Google. Uses Google Ads API v25. MIT licensed.

## Why another Google Ads MCP?

I manage paid media day to day. I wanted an assistant that could pull search terms, draft negatives, and adjust a target CPA on a Monday morning without me worrying that a misread instruction would triple a budget or un-pause a campaign I had killed for a reason. The write-capable servers I found had one or two safeguards each. None had the full set, and the ones that came closest had a single user.

So the design goal here is simple: the assistant proposes, the server decides whether that proposal is allowed, and nothing reaches Google until a second explicit call says go.

### How it compares

| | Google official server | Typical community write server | Google Ads MCP Safe |
| --- | --- | --- | --- |
| Reads (GAQL, reports, account discovery) | Yes | Yes | Yes, readable, row-limited, each naming its account and dates |
| Agency use (many client accounts under one manager) | Varies | Varies | Find by name, read any, change only listed ones, per-client rules and spend limits |
| Writes | No, by design | Yes | Yes, off by default |
| Per-account write allowlist | n/a | Rare | Yes. Reads can optionally cover a whole manager account; changes never do |
| Preview then separate confirm call | n/a | Some | Yes, every write |
| Daily budget, CPC, and target CPA ceilings | n/a | Some | Yes, three separate caps |
| Refuses to clear a Performance Max target (budget still caps spend; this protects cost per result) | n/a | Not found | Yes |
| Extra opt-in for shared or blast-radius resources | n/a | Not found | Yes, seven separate switches (shared budgets, portfolios, conversion goals, shared audiences, recommendations, shared negative lists, permanent removal) |
| New campaigns, ad groups, ads created PAUSED | n/a | Some | Yes, always |
| Local audit log | n/a | Some log applied writes | Logs drafts, refusals, applies, and unknown outcomes |
| Fresh state re-check at confirm time | n/a | Not found | Yes |
| Undo for applied changes | n/a | Not found | Yes, drafts the reverse from the audit log |
| Automated tests | Unknown | Usually few or none | 4,001 |

"Typical community write server" summarizes the open-source write-capable servers I could find on GitHub in September 2026. The best of them ship two or three of these gates. To my knowledge none combine all of them in a maintained project, but I have not audited every repo, and this table is a snapshot, not a scoreboard. The [Google official server](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server) is the right choice if you only need reads and want Google-maintained code.

## What you can ask

Once connected, talk to your assistant in plain words. It picks the tools.

- "Find the Smith Dental account and use it."
- "How did each campaign do in the last 30 days? Show spend, conversions and cost per conversion."
- "Which search terms spent money with no conversions last month?"
- "What share of search impressions are we losing to budget, by campaign?"
- "Do the same check for the next client on my list."
- "Draft a negative keyword list from those search terms." (a change: previewed first, applied only on a second explicit step, and only if you turned changes on)

Every report says which account it came from, its currency and time zone, the exact dates it covers, and whether it was cut short. Money is shown in whole currency units, statuses as words. The standard performance reports return 100 rows unless you ask for more (up to 1,000). Custom queries and account lookups return up to one page (10,000 rows) and say when more exist, so add a limit to those yourself.

## Quick start

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) (a small tool that runs Python programs without a manual install), a Google Cloud project with Google Ads API access (the [setup guide](docs/setup.md) explains how to get one), and access to the account or manager account you want to use.

1. In your terminal (on a Mac, the Terminal app), run the guided setup. It asks three plain questions, opens Google's sign-in page, and saves a private sign-in file:

   ```sh
   uvx mcp-google-ads-safe setup
   ```

2. Paste the configuration it prints into your assistant (Claude Desktop or Claude Code; other MCP clients take the same values).
3. Quit and reopen the assistant.
4. Ask it: "run health_check, then find_accounts".
5. Ask it: "use the account named ..." and start asking questions.

That setup is read-only and can report on every enabled account under your manager account. Nothing can be changed until you turn changes on. The command installs the package from [PyPI](https://pypi.org/project/mcp-google-ads-safe/). The configuration that setup prints launches the same version from GitHub; both work. A service account, or a manual setup with explicit account lists, is in the [setup guide](docs/setup.md). To sign in again later: the same command with `reauth` in place of `setup`.

### Turning on changes

Add two values to the same configuration block and restart the assistant:

- `GOOGLE_ADS_ENABLE_WRITES=true`
- `GOOGLE_ADS_WRITE_CUSTOMER_IDS=1234567890,2345678901` (only these accounts can ever be changed, whatever else can be read)

Every change is then drafted, shown as a preview that names the account and the spend limits that apply, and sent only by a separate confirm step. For an agency, give each client its own website domain, banned words and spend limits in `advertiser.yaml`; see [advertiser.example.yaml](advertiser.example.yaml).

## Every tool, in plain words

Accounts
- `health_check`: is the sign-in working, and how many accounts can it reach.
- `find_accounts`: search the accounts under your manager account by name.
- `use_account`, `current_account`: pick the account reports use, and show the pick. Reports only; a change always names its account.
- `list_accounts`: the accounts named in your configuration.
- `get_account_info`: one account's name, currency, time zone and status.

Reports
- `get_campaign_performance`, `get_ad_performance`, `get_keyword_performance`, `get_search_terms`, `get_geo_performance`: the standard reports, last 30 days unless you give dates.
- `run_gaql`: any custom report, with ready-made recipes in its description.
- `describe_fields`: what a custom report can ask for, so nobody needs Google's field names.
- `get_negative_keywords`, `list_extensions`, `get_policy_issues`, `get_conversion_actions`, `list_recommendations`, `get_entities`, `search_geo_targets`: look up what is in the account.
- `discover_keywords`, `get_keyword_forecasts`: Keyword Planner ideas and forecasts.

Changes to what exists (all drafted first)
- `update_campaign`, `update_ad_group`: budget, name, status, target CPA or ROAS, ad-group bid.
- `pause_entity`, `enable_entity`, `remove_entity`: pause, resume or permanently remove.
- `draft_keywords`, `remove_keywords`, `update_keyword_bid`: keywords and their bids.
- `add_negative_keywords`, `remove_negative_keywords`, `create_shared_negative_set`, `add_to_shared_set`, `attach_shared_set`: negative keywords and shared lists.
- `exclude_geo_target`, `remove_geo_target`, `set_campaign_schedule`: locations and ad schedule.
- `add_audience_targeting`, `create_custom_audience`, `set_listing_group_filter`: audiences and product filters.

Creating new things (always created paused)
- `draft_campaign`, `create_ad_group`, `draft_responsive_search_ad`: Search campaigns, ad groups and ads.
- `create_pmax_campaign`, `create_asset_group`, `update_asset_group`, `add_asset_group_assets`, `remove_asset_group_asset`: Performance Max.
- `create_demand_gen_campaign`, `draft_demand_gen_ad`: Demand Gen.
- `create_portfolio_bidding_strategy`: shared bid strategies.
- `draft_sitelinks`, `create_callouts`, `create_structured_snippets`, `remove_extension`, `upload_image_asset`, `upload_text_asset`: assets and extensions.
- `create_conversion_action`, `set_conversion_action_primary_status`, `apply_recommendation`, `dismiss_recommendation`: conversions and Google's suggestions, each behind its own extra switch.

Applying and undoing
- `confirm_and_apply`: send a drafted change. The only tool that changes anything.
- `undo_change`: draft the reverse of a change already made.

Inputs, limits, and what has and has not been tested live are in the [tool reference](docs/tool-reference.md). Creation tools deliberately support a narrow set of campaign shapes. If you need every knob in the Google Ads UI, this is not that.

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

Every one of the 4,001 tests runs offline against a fake Google Ads API. CI runs them on Ubuntu and macOS across Python 3.12, 3.13, and 3.14 on every push. That proves the request shapes, the safety logic, and the refusal paths. It does not prove Google accepts a given operation on your account.

Live evidence is uneven and documented per tool. All reporting and lookup tools have been run against a real account. A subset of writes (budget, name, target CPA / ROAS, ad-group CPC, ad-group pause / enable, exact-match keywords, Manual CPC campaign and ad-group creation) has been applied to a real account and read back. Most of the Performance Max, Demand Gen, asset, conversion, and recommendation writes have offline tests only. The [evidence matrix](docs/offline-capability-matrix.md) says which is which. Read it before enabling writes for anything you would not want to fix by hand.

## Status

Version 2.0.0. Published on [PyPI](https://pypi.org/project/mcp-google-ads-safe/); runs with one command. CI runs lint, the full test suite on the newest and the oldest supported MCP library, a known-vulnerability audit of dependencies, and a high-severity Bandit scan, on Ubuntu and macOS across Python 3.12, 3.13, and 3.14. Windows has not been tested.

Upgrading from 1.x: report answers changed shape (readable rows plus `account` and `provenance` blocks; `run_gaql(raw=True)` returns the old rows), and `health_check` returns different keys. See the [changelog](CHANGELOG.md).

## Docs

- [Setup](docs/setup.md), [configuration](docs/configuration.md), [troubleshooting](docs/troubleshooting.md)
- [Tool reference](docs/tool-reference.md) and [evidence matrix](docs/offline-capability-matrix.md)
- [Contributing](CONTRIBUTING.md), [security policy](SECURITY.md), [changelog](CHANGELOG.md), [license](LICENSE)
