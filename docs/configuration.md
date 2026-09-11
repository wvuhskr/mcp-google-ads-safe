# Configuration

Credentials and advertiser settings are separate files. Keep both outside the project.

The recommended credential YAML is a plain-text settings file with `json_key_file_path`, `login_customer_id`, and `use_proto_plus: true`. `impersonated_email` is optional. Existing Desktop user profiles with `client_id`, `client_secret`, `refresh_token`, and the backward-compatible optional `developer_token` remain supported. See [setup](setup.md) for private storage and official Google preparation steps.

## Credential and account environment

| Name | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_ADS_YAML` | none, required | Absolute path to the private Google Ads YAML credential profile. Other environment variables do not replace it. |
| `GOOGLE_ADS_CUSTOMER_ID` | none | Default advertising customer and fallback for either allowlist when it is unset or blank. |
| `GOOGLE_ADS_READ_CUSTOMER_IDS` | default customer only | Comma-separated customers allowed for reads. Every entry must descend from the profile's `login_customer_id`. |
| `GOOGLE_ADS_WRITE_CUSTOMER_IDS` | default customer only | Comma-separated customers allowed for writes. This must be a subset of the read allowlist. |
| `GOOGLE_ADS_MAX_PAGES` | `1` | Maximum pages for user queries. Must be an integer of at least 1. Internal safety scans still run to completion. |

## Write controls

| Name | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_ADS_ENABLE_WRITES` | `false` | Enables draft creation and confirmation. Values: `true`, `false`, `1`, or `0`. |
| `GOOGLE_ADS_MAX_DAILY_BUDGET` | `1000` | Positive cap in the target account's currency, with no conversion. |
| `GOOGLE_ADS_MAX_CPC` | `50` | Positive cost-per-click or target cost-per-acquisition cap in the target account's currency. |
| `GOOGLE_ADS_DRAFT_TTL_SECONDS` | `3600` | Positive lifetime for an in-memory draft. Drafts also disappear on restart and cannot be reused after confirmation. |
| `GOOGLE_ADS_AUDIT_PATH` | `~/.mcp-google-ads-safe/audit.jsonl` | Append-only local audit-log path. |

Independent opt-ins default to `false`: `GOOGLE_ADS_ALLOW_SHARED_BUDGET_EDIT`, `GOOGLE_ADS_ALLOW_PORTFOLIO_EDIT`, `GOOGLE_ADS_ALLOW_CONVERSION_GOAL_EDIT`, `GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT`, `GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION`, and `GOOGLE_ADS_ALLOW_SHARED_NEGATIVE_SET_EDIT`. Set only the feature you intend to use. Global writes and account allowlists still apply.

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
