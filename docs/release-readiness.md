# Release readiness

Version 1.0.1 is the first public release (2026-09-14). This page is the evidence ledger behind it: what was verified, how, and what was not. Where it says "Codex", that means OpenAI Codex used as the MCP client for a connection check; nothing here depends on that client. The [capability matrix](offline-capability-matrix.md) records operation-level evidence, and the [validation procedure](validation-readiness.md) governs future connected checks.

Since 1.0.0 was reviewed privately, a public-readiness review on 2026-09-14 fixed two High and seven Medium findings (see [CHANGELOG](../CHANGELOG.md)). CI now runs the full suite on Ubuntu and macOS across Python 3.12, 3.13 and 3.14; both pushes on 2026-09-14 passed all six jobs.

Google changed new API access registration on September 9, 2026. The [Google authentication transition note](google-auth-transition.md) records the adopted tokenless client-library route and its remaining connected evidence limits.

## Completed offline evidence

The September 2026 preparation statically reviewed source, metadata, documentation, and bounded privacy markers without reading credentials or contacting Google. A source snapshot was built into a wheel and normalized source distribution, inspected for private markers, installed in fresh cached Python 3.12 environments, and exercised with offline checks. The selected artifacts remain private historical Alpha evidence. They predate these onboarding documents and must not be relabeled as stable v1 artifacts.

The full tool schemas and fake-provider paths have broad offline test coverage. Earlier dated records include bounded connected reads, validation-only requests, and a small number of independently read-back changes. Evidence is specific to the operation and branch documented in the [tool reference](tool-reference.md); it does not prove every current path, policy approval, serving, or performance.

## Completed bounded connection evidence

On September 10, 2026, an independently reviewed check used one existing user-authorization profile in a private Python 3.12 installation with Google Ads Software Development Kit 32.0.0. It successfully refreshed authorization, discovered accessible account identities, and completed the installed settings load, manager-ancestry preflight, and health lookup for one explicitly selected advertiser. Writes were disabled throughout.

This evidence proves only that profile's bounded read-only account discovery and startup prerequisites plus health for that advertiser. It does not prove first-time sign-in, service-account or key creation, reauthorization or recovery, a running server listener, an assistant client connection, every provider operation, the declared compatibility range, or stable-release readiness.

## Completed Desktop sign-in and Codex health evidence

On September 11, 2026, the packaged Desktop helper completed real browser consent, local callback, token exchange and creation of a new owner-private profile using an owner-created Desktop client. The saved file passed required-field, manager and permission checks. A separate private check loaded that tokenless profile and verified selected-account ancestry and identity with writes disabled. After an additive health-only Codex connection was configured and the app reloaded, its actual health_check tool returned success for the selected advertiser. Independent reviews accepted the sign-in/save, private health and actual assistant evidence.

A subsequent explicit replacement check created an owner-private backup, renewed the test profile through real browser consent and confirmed successful save with the backup intact. After the test connection restarted, process start-time evidence placed its new runtime after the replacement; its actual Codex health tool again returned the selected account. Independent review accepted bounded replacement and post-restart health. No token was deliberately revoked, so this does not prove revoked-token recovery.

The initial private test policies blocked name lookup and temporary-file creation. Credential-free and dummy-file checks established those causes; corrected policies passed the complete dummy save sequence before successful real execution. Prior failures remain retained. The application helper did not change. A browser completion page is not proof of token exchange or a saved profile.

This proves one Desktop authorization journey from an already created client and a health-only Codex connection on the qualified private Python 3.12 installation. It does not prove from-zero Cloud registration, service-account/key creation, revoked-token recovery, every assistant tool, Claude client setup, or broader installation compatibility. Earlier private Alpha artifacts predate this documentation update and remain historical evidence.

## Completed bounded fresh-install compatibility evidence

On September 11, 2026, fresh private environments on macOS 26.5.2 Apple Silicon built and installed the same reviewed source snapshot on Python 3.12.13, 3.13.14 and 3.14.6. Dependencies were resolved from public binary packages before offline installation and execution. Each passed dependency consistency, installed-package origin and entry-point checks, and all 3,807 synthetic tests, including tool schemas and disabled-write refusal. The resolved versions included Google Ads SDK 32.0.0, MCP 2.2.0, grpcio 1.83.1, protobuf 7.36.1, Pillow 12.3.0 and PyYAML 6.0.3. Exact inventories, source hashes, private artifact hashes and failed attempts were retained.

The initial test restrictions blocked working-directory access, a system discard file and safe parent-directory traversal; system Python startup also exposed an unrelated installed-package path that remained unreadable. Narrow test-environment corrections resolved those failures without application-code changes. Synthetic privacy and network denials passed after the corrections. These results qualify the exact Mac environments, not Windows, Linux, future Python versions, all dependency combinations, real provider behavior or public distribution. The new test wheels are private Alpha evidence and predate this compatibility documentation update.

## Completed bounded reporting and lookup evidence

On September 11, 2026, the installed current tool paths completed account metadata and a fixed one-day campaign report against one read-authorized account. An independent direct query matched every campaign identity and its impression, click and cost-micros totals; all pages were complete. Separate checks accepted ad, keyword and geographic reports, campaign negatives, geographic-target lookup, and all four entity-reader branches. Campaign negatives exercised actual multi-page continuation. Search terms retained the documented 200-row cap and were not represented as exhaustive.

The current policy query was accepted but returned no rows, establishing query and empty-envelope acceptance only. Extension, conversion-action and recommendation readers completed their returned pages. Private raw responses, exact queries, source hashes and independent reconciliation remain retained. All checks kept writes disabled. These observations do not validate mutation behavior, policy interpretation of nonempty results, serving, attribution accuracy or other account eligibility.

## Completed bounded keyword-read evidence

On September 11, 2026, one current keyword-discovery request returned a 50-row page with a continuation token; it was correctly treated as partial. A separate planless campaign forecast request completed with provider-returned clicks, cost and average-cost-per-click fields. Conversion and cost-per-acquisition fields were unavailable and remained explicitly unavailable. Location/language constants were checked before both requests; account metadata was checked before the forecast. The inputs described a hypothetical future campaign and created no saved plan or ad object. This proves request acceptance and returned-field handling for those inputs, not estimate accuracy, future performance or account-wide eligibility.

The first private targeting guard incorrectly expected an enum label where the installed library serialized its integer value. A generated-message check established the test-code mismatch, and the corrected guard passed before the successful attempt. Application code and request payloads were unchanged; the initial failure was retained.

## What is still unverified

| Area | What remains |
| --- | --- |
| Authentication and setup | Desktop sign-in, explicit replacement and post-restart health are complete. From-zero Cloud provisioning, service-account/key setup and revoked-token recovery remain unverified alternatives. |
| Installation and compatibility | CI covers Ubuntu and macOS on Python 3.12, 3.13 and 3.14 from a fresh install. Windows is untested. No PyPI package yet; install is from source. |
| Connected behavior | Current read tools and a health-only MCP client connection have bounded evidence above. Write operations retain their operation-specific historical or synthetic status in the [capability matrix](offline-capability-matrix.md); no blanket live-write claim is made. |

Security reports go through GitHub private vulnerability reporting (see [SECURITY.md](../SECURITY.md)). Dependency updates arrive weekly via Dependabot.
