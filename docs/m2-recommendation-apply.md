# Bounded campaign-budget recommendation apply

`apply_recommendation(recommendation_id, customer_id=None)` returns a draft for exactly
one non-dismissed `CAMPAIGN_BUDGET` recommendation. IDs are opaque safe ASCII path
segments, preserved exactly. Only an omitted customer selects the default account.

The feature is off by default: `GOOGLE_ADS_ALLOW_APPLY_RECOMMENDATION=true` is required
alongside global writes and read/write account permission. Existing daily-budget caps,
shared-budget permission and complete attachment checks apply. The affected budget is
the primary identity; an optional recommendation campaign must belong to that budget.
Every attached nonremoved campaign must have a known enabled or paused status.

The preview states the currency, before/after amount, recommendation state and every
affected campaign. Confirmation recompiles the entire approval and refuses changed
recommendation amounts, budget properties, campaign population, permissions or caps.
The request supplies exactly the approved `campaign_budget.new_budget_amount_micros`,
with one operation and `partial_failure=False`. Other recommendation types, arbitrary
parameters, batch application and a public dismiss tool are outside this scope.

RecommendationService has no validation-only support. The read/check/apply/read sequence
is not atomic. There is no automated rollback or retry. A malformed or error-bearing
response is an unknown write outcome with a consumed draft. A successful response is
followed by a complete saved-budget and attached-campaign check. A read failure or
mismatch is applied but unverified: read account state before any further write.
Recommendation disappearance alone never proves success and is not required for the
saved-configuration check. This does not guarantee bidding outcomes or provider acceptance.

Verification is offline using installed Google Ads v25 request/response messages and
synthetic account data. No live provider application was performed. Existing paginated
`list_recommendations` remains a read-only report; internal approval scans are complete.
