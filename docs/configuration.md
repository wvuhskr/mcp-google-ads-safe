# Configuration

Credentials and advertiser settings are separate files. Keep both outside the project.

The recommended credential YAML is a plain-text settings file with `json_key_file_path`, `login_customer_id`, and `use_proto_plus: true`. `impersonated_email` is optional. Existing Desktop user profiles with `client_id`, `client_secret`, `refresh_token`, and the backward-compatible optional `developer_token` remain supported. See [setup](setup.md) for private storage and official Google preparation steps.

## Credential and account environment

| Name | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_ADS_YAML` | none, required | Absolute path to the private Google Ads YAML credential profile. Other environment variables do not replace it. |
| `GOOGLE_ADS_CUSTOMER_ID` | none | Default advertising customer and fallback for either allowlist when it is unset or blank. |
| `GOOGLE_ADS_READ_SCOPE` | `list` | How far reads reach. `list`: only the accounts in the read list. `manager`: any enabled, visible account under the profile's `login_customer_id`, with no list to maintain. Any other value stops startup. Writes are list-only in both. See [Agency use](#agency-use-many-client-accounts). |
| `GOOGLE_ADS_READ_CUSTOMER_IDS` | default customer only | Comma-separated customers allowed for reads in `list` scope. Every entry must descend from the profile's `login_customer_id`. |
| `GOOGLE_ADS_WRITE_CUSTOMER_IDS` | default customer only | Comma-separated customers allowed for writes, in every scope. In `list` scope this must be a subset of the read allowlist; in `manager` scope every entry must be an enabled, visible account under the login manager. |
| `GOOGLE_ADS_MAX_PAGES` | `1` | Maximum pages for user queries. Must be an integer of at least 1. Internal safety scans still run to completion. |

## Agency use: many client accounts

Set `GOOGLE_ADS_READ_SCOPE=manager` when one sign-in manages many client accounts. Then:

- `find_accounts("smith dental")` searches every account under the manager by name, in one request, however large or deep the manager account is.
- Reports work on any enabled, visible account under the manager. The first report on an account costs one extra lookup to prove it belongs to the manager; the proof is reused for ten minutes, so an account that is hidden, suspended, canceled or unlinked stops being readable within that window. The manager account itself and any sub-manager count as readable; Google returns no performance numbers for a manager.
- Changes are still limited to `GOOGLE_ADS_WRITE_CUSTOMER_IDS` (or the single `GOOGLE_ADS_CUSTOMER_ID`). Manager scope never widens what can be changed. Every change looks the account up fresh, never from the ten-minute memory, and startup refuses a write list that names a canceled, suspended or hidden account (`ACCOUNT_NOT_ACTIVE`).
- `use_account("123-456-7890")` picks the account that reports use when none is named, and `current_account` shows the pick. The pick is for reports only. A change must name its `customer_id` every time, and is refused with `ACCOUNT_REQUIRED` if it does not while a pick is active. The pick is held by the server process, and a desktop client can share one process across several chats, so a pick made in one chat is visible in the others. Every report answer carries an `account` block saying which account it came from and whether that was the named `customer_id`, the `use_account` pick or the configured default.

In `list` scope `find_accounts` still shows every account the sign-in can reach, with `can_read` and `can_write` marking the ones the tools would actually allow; names and numbers of unlisted accounts are visible, their data is not.

Startup and `health_check` prove the listed accounts with one request to Google in either scope. Earlier versions walked the manager tree level by level and refused managers deeper than three levels or larger than 500 accounts (`ANCESTRY_TRUNCATED`); that limit is gone.

## Write controls

| Name | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_ADS_ENABLE_WRITES` | `false` | Enables draft creation and confirmation. Values: `true`, `false`, `1`, or `0`. |
| `GOOGLE_ADS_MAX_DAILY_BUDGET` | `1000` | Positive cap in the target account's currency, with no conversion. |
| `GOOGLE_ADS_MAX_CPC` | `50` | Positive cost-per-click bid cap in the target account's currency. |
| `GOOGLE_ADS_MAX_TARGET_CPA` | same as `GOOGLE_ADS_MAX_CPC` | Positive target cost-per-acquisition cap in the target account's currency. Set it separately so a realistic target CPA does not require raising the CPC cap. |
| `GOOGLE_ADS_DRAFT_TTL_SECONDS` | `3600` | Positive lifetime for an in-memory draft. Drafts also disappear on restart and cannot be reused after confirmation. |
| `GOOGLE_ADS_AUDIT_PATH` | `~/.mcp-google-ads-safe/audit.jsonl` | Append-only local audit-log path. |

Independent opt-ins default to `false`: `GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT`, `GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT`, `GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT`, `GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT`, `GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION`, `GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT`, and `GOOGLE_ADS_ALLOW_REMOVE_ENTITY` (permanent removal of a campaign, ad group, or ad). Set only the feature you intend to use. Global writes and account allowlists still apply.

Clearing the target CPA or target ROAS on a Performance Max campaign is refused outright (`PMAX_TARGET_CLEAR`). The campaign's daily budget still caps spend; the refusal protects cost per result, which in practice degrades sharply when a Performance Max target is removed. Raise the target in steps instead. This is a product choice, not a Google requirement.

The credential YAML may contain only the keys the Google Ads client library needs for authentication (`developer_token`, `client_id`, `client_secret`, `refresh_token`, `json_key_file_path`, `impersonated_email`, `login_customer_id`, `linked_customer_id`, `use_proto_plus`, `use_cloud_org_for_api_access`). Any other key, including `logging`, stops startup.

Audit log phases: `draft`, `refused`, `apply`, `apply_unverified` (the change landed but the read-back did not match), `error` (failed before dispatch, nothing landed), and `unknown` (failed at the wire, the change may have landed).

## Per-account rules for agencies

`advertiser.yaml` may carry an `accounts:` map keyed by customer id (dashes are fine).
Each entry can set `advertiser_domain`, `blocked_terms`, `keyword_research`,
`max_daily_budget`, `max_cpc` and `max_target_cpa` for that one account:

- `advertiser_domain` replaces the top-level value for that account. `keyword_research`
  keys an account sets replace the top-level ones; keys it leaves out keep the top-level
  values.
- `blocked_terms` are added to the top-level list. A house-wide ban cannot be lifted per
  account.
- The caps replace `GOOGLE_ADS_MAX_DAILY_BUDGET`, `GOOGLE_ADS_MAX_CPC` and
  `GOOGLE_ADS_MAX_TARGET_CPA` for that account, in that account's own currency. Without
  them one environment number means 1,000 dollars in one account and 1,000 yen in another.
  They are not limited by the environment numbers and can be higher, so check them as
  carefully as the environment caps; startup prints each account that has its own caps.
  An account that sets any cap must set `max_daily_budget` and `max_cpc` together, so no
  cap is ever quietly taken from a number written for another currency. `max_target_cpa`
  is optional and falls back to that account's `max_cpc`.
- An account not listed uses the top-level values and the environment caps.

A commented example is in [advertiser.example.yaml](../advertiser.example.yaml). Unknown
keys, a bad account number, an account listed twice or a non-positive cap stop startup and
name the problem.

Every draft shows, beside its preview, the account it changes and the caps that apply
(`account`, `caps`, with `caps.source` saying whether they came from the `accounts:`
section or the environment). The account's name and currency appear when an earlier report
or pick already learned them; drafting a change makes no extra request to fill them in.

## Advertiser settings

`GOOGLE_ADS_ADVERTISER_CONFIG` selects an optional advertiser YAML file. Its default is `~/.mcp-google-ads-safe/advertiser.yaml`. A missing or empty file uses empty defaults. Unknown keys or invalid values stop startup with an error naming the file and field.

```yaml
blocked_terms:
  - "competitor name"
advertiser_domain: "https://example.test"
keyword_research:
  geo_target_constant_ids: []
  language_constant_id: null
  keyword_plan_network: "GOOGLE_SEARCH"
  include_adult_keywords: false
```

`blocked_terms` must contain non-empty text. A present `advertiser_domain` cannot be empty. Keyword research accepts at most 10 positive geography IDs, a positive number or non-empty text language ID or `null`, a non-empty network name, and a boolean adult-keyword choice.

Changing environment values or advertiser settings requires restarting the server or desktop client. Existing drafts are then lost. Review every preview before `confirm_and_apply`; confirmation rechecks current account state and can still refuse the change.
