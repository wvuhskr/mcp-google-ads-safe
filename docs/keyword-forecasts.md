# Keyword forecasts

`get_keyword_forecasts(keyword_texts, match_type, forecast_start_date,
forecast_end_date, max_cpc_bid_micros, customer_id=None, daily_budget_micros=None)`
requests Google's future estimates for one proposed campaign with one ad group.
It creates no saved plan, campaign, ad group, draft or confirmation. The read allowlist
applies; enabling writes is unnecessary. Keyword write blocklists do not apply.

Supply 1..20 unique exact keyword strings, each at most 80 characters and 10 words,
without blanks, outer whitespace or control characters. Text and order are preserved.
Use one common `EXACT`, `PHRASE` or `BROAD` match type and a positive integer bid in
micros (one million per account currency unit). Optional daily budget uses the same
units. Both accept only positive signed 64-bit integers, without coercion or booleans.
Only an omitted/null customer uses the default account; explicit IDs must be canonical
positive ASCII digit strings. The protocol boundary rejects coerced input types.

The configured `keyword_research` settings must supply 1..10 unique positive canonical
location IDs and a concrete language ID. Discovery retains its existing empty-location
and null-language defaults. Forecasts have no network or adult-keyword selectors, so
those discovery settings are explicitly disclosed as unapplied; effective filtering is
not verified. No public targeting overrides or settings changes are supported.

A complete, fixed account metadata read resolves exactly the selected account's ID,
currency and timezone before the forecast. Missing, duplicate, mismatched or malformed
account data fails. Dates must be canonical `YYYY-MM-DD`, ordered, with start strictly
after today in that account's timezone and end no later than one calendar year after
today (February 29 maps to February 28). Currency is the account currency without an
override. Request metadata includes the exact generated request and account assumptions.

One v25 `KeywordPlanIdeaService.GenerateKeywordForecastMetrics` request uses an inline
campaign with Manual CPC (manually selected cost-per-click bidding), explicit dates,
locations, language, keywords and bid. Daily budget is sent only when supplied. No
forecast retries or paging occur. Currency-specific budget minimums remain provider
validation and any provider error is a failure, never a successful empty forecast.

`campaign_forecast_metrics` contains only Google's raw optional fields. Missing fields
stay absent and explicit zero values stay zero; missing whole metrics means unavailable.
`metric_availability` explains whole-message or individual-field absence. Micros retain
the generated message serializer's integer-string representation. Results are estimates
for the whole proposed campaign under a clicks/cost scenario, not guaranteed outcomes,
per-keyword results or historical account performance. There are no invented impressions,
derived metrics, conversion definitions or attribution windows, and no local prediction model.

Offline tests use generated v25 messages with fake services. They prove request shape,
validation, permissions, strict protocol input, calendar boundaries, serialization and
single-call behavior. They do not prove live account eligibility, provider acceptance,
quota, currency minimums, effective network behavior or forecast accuracy. A later bounded live request is documented below.

## Bounded current provider evidence

A September 11, 2026 check used one read-authorized account with writes disabled.
One planless forecast request completed for hypothetical future inputs. Returned
click/cost fields were preserved; missing conversion and cost-per-acquisition fields
remained unavailable. No saved plan, campaign, bid or budget was created or changed.
This does not establish estimate accuracy or support for every input combination.
