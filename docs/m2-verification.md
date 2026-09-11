# Milestone 2 read verification

The first Milestone 2 slice adds four read tools. All return the existing GAQL response
structure unchanged, including rows, returned count, provider total, completion flags,
and continuation token. Callers must follow continuation tokens before claiming a complete
inventory. Queries have no LIMIT. Every wrapper checks the selected account against the
read allowlist before calling Google; no automatic account traversal or writes are added.

| Tool | Scope | Evidence tier |
| --- | --- | --- |
| `list_extensions` | Nonremoved campaign asset links with asset text, including structured snippet values. Does not inventory account, ad-group, inherited, or asset-group links. | Offline verified; live query accepted on 2026-09-07 |
| `get_policy_issues` | Ads with approval other than APPROVED; removed ads and removed parent campaigns/ad groups excluded. No asset-wide policy claim or dated performance metrics. | Revised query offline verified only; historical query accepted on 2026-09-07. See [current contract](policy-issues.md) |
| `get_conversion_actions` | Nonremoved actions visible in the chosen customer, with owner, counting, value and attribution settings. No cross-account routing or exhaustive goal/bidding usage claim. | Offline verified; live query accepted on 2026-09-07 |
| `list_recommendations` | Non-dismissed Google recommendations and estimated impact. No ranking, approval, application or dismissal. | Offline verified; live query accepted on 2026-09-07 |

A secondary conversion action can still be used for bidding inside a custom goal.
The owner field may be absent for system-defined actions. Both details follow the
[Google v25 conversion action reference](https://developers.google.com/google-ads/api/fields/v25/conversion_action).
Recommendation impact and account context fields follow the
[Google v25 recommendation reference](https://developers.google.com/google-ads/api/fields/v25/recommendation).
These references and installed v25 message types support field selection and response shape;
they do not prove that Google has accepted the complete queries live.

Offline evidence on 2026-09-07: `tests/test_m2_reads.py` covers default/explicit customer
selection, denied and missing customers before provider calls, exact continuation-token
forwarding, unchanged incomplete responses with unknown/missing totals, exact selected
fields and filters, and provider failures. Synthetic Google v25 response messages exercise
the real client's nested serialization and continuation path without credentials.
Enum values remain numeric, integer64 fields remain strings, and `type_` keys retain the
existing serializer's spelling. No response normalization was introduced.

The focused read/inventory/error suite passed 126 tests; the full suite passed 550 tests
with warnings treated as errors, using the local MCP 2.2.0 environment. Ruff passed.
These are test counts from pytest, not advertising results. The new schema test accepts
MCP 1 and 2 schema attribute spellings; this run does not claim an MCP 1 execution.

The implementation worker made no live Google calls or credential reads. After independent
specification and quality review approved the change, the parent completed the read-only
connection checks below. No account change, connected-server cutover, push or publication
was performed for this slice. M1 evidence remains separately documented in
[m1-verification.md](m1-verification.md).


## Parent live read proof

On 2026-09-07 an isolated actual MCP connection loaded commit `9dabcda` with writes disabled.
All four queries succeeded. Every returned count matched the number of rows and Google's
reported total, with no query LIMIT and no remaining continuation token. This verifies
complete results within each tool's stated filters, not exhaustive coverage outside them.

| Read tool | Returned rows | Google total | Pages |
|---|---:|---:|---:|
| list_extensions | 619 | 619 | 1 |
| get_policy_issues | 0 | 0 | 1 |
| get_conversion_actions | 51 | 51 | 1 |
| list_recommendations | 3 | 3 | 1 |

Source: the configured, read-allowlisted real account used for the approved M1 checks,
September 7, 2026 configuration queries. These counts describe matching records, not
advertising results, so conversion attribution does not apply. The private evidence index
maps that account and preserves complete MCP responses in `geo-canary/m2-reads/` adjacent
to the working copy. The empty policy result proves accepted query/empty-response behavior;
nonempty policy-topic serialization is proven offline, not by this live response. It does
not establish that every asset or campaign is free of policy issues.

The rebuilt wheel and source distribution passed the narrow private-file/known-marker
inspection. A separate environment installed the wheel and confirmed exact source equality,
29 tools with serializable schemas, and the four callable read wrappers using a stubbed
provider. All actual Google proof above was performed separately with writes disabled.
Full history/privacy clearance and subsequent milestone checks remain publication gates.
