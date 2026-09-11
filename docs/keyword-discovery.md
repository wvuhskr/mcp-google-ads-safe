# Keyword discovery

`discover_keywords(seed_keywords, customer_id=None, page_token=None)` is a read-only
research tool. It works when writes are disabled and requires the READ account allowlist.
Only an omitted or null account uses `GOOGLE_ADS_CUSTOMER_ID`; explicit accounts must be
canonical positive ASCII digit strings without leading zeroes or separators.

Pass an actual list of 1..20 strings, preserving desired spelling and order. Blank text,
outer whitespace, control characters, other data types and JSON-encoded list strings
are refused. Nothing is trimmed, deduplicated or filtered through write content rules.
There is no URL/site seed, account edit, forecast, automatic keyword addition or confirmation.

## Targeting

The tool uses effective advertiser `keyword_research` settings with no public overrides:

| Setting | Meaning |
| --- | --- |
| `geo_target_constant_ids` | At most 10 distinct canonical positive IDs, converted to `geoTargetConstants/<id>`. Empty means all geographies. |
| `language_constant_id` | Canonical positive integer/string ID converted to `languageConstants/<id>`. Null means all languages. |
| `keyword_plan_network` | Exactly `GOOGLE_SEARCH` (default) or `GOOGLE_SEARCH_AND_PARTNERS`. |
| `include_adult_keywords` | Actual boolean, default false; explicit true is honored. |

Invalid effective settings refuse before constructing the provider client. Cached settings
are not modified. No user market, language or currency is inferred.

## Results and meaning

One `KeywordPlanIdeaService.GenerateKeywordIdeas` request asks for a fixed page size of 50.
The tool consumes only that provider page, regardless of `GOOGLE_ADS_MAX_PAGES`.
`keyword_ideas` holds every raw normalized result on the page. `returned_count` counts that
page; `total_results_count` is the provider's total, not the page count. An actual empty
response reports an empty list and provider total zero. Errors are never empty successes.

`request` records the account, exact seeds, effective resource names, network, adult setting,
page size, service and whether all geographies/languages were requested. `source` explains
that metrics are historical estimates, not realized account results or future forecasts.
The provider default historical window is the past 12 months; returned monthly rows identify
which months are actually supplied. No precise start/end dates or attribution window are
invented. Attribution is not applicable to keyword research estimates.

Raw fields follow other reads: snake_case keys, numeric enums and string-valued 64-bit
integers. For example, an explicitly returned search count of zero is `"0"`; an omitted
optional metric stays omitted and means unavailable. Competition/month enum numbers retain
Google's enum meanings. Monetary `*_micros` fields are millionths of the selected account's
currency unit. The currency code is not fetched and must not be assumed to be USD. No
average cost per click is requested or promised, no unavailable metric is filled with zero,
and no ranking or calculated cost estimate is added.

## Continuation

A short page can still have a `next_page_token`. Continue only with that complete token and
the same account, seeds in the same order and effective settings. `pages_complete` means
there is no following provider token. A changed request or service, raw/empty/malformed token,
or immediately repeated provider token refuses. Tokens bind the whole immutable request,
including fixed page size and service; they are mismatch guards, not authentication,
encryption or secret storage. They include the provider continuation value behind a digest.
No automatic retry or multi-page scan occurs.

Offline tests use real v25 request/result messages and a pager fake that fails on hidden
iteration. These offline checks do not establish live availability, quota or access;
the later bounded provider observation is documented below.

## Bounded current provider evidence

A September 11, 2026 check used one read-authorized account with writes disabled.
One discovery request returned 50 ideas and a continuation token, so the saved result
was explicitly partial. These are historical provider estimates, not actual account
performance; no keyword or campaign was created.
