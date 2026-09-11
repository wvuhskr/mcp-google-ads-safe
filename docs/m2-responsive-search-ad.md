# Paused responsive Search ad draft

`draft_responsive_search_ad(ad_group_id, headlines, descriptions, final_url,
customer_id=None, path1=None, path2=None)` drafts one PAUSED responsive Search ad.
Offline tests and one provider validation-only check passed. No actual ad creation
or live post-write verification is claimed.

The tool requires a readable, nonremoved standard Search ad group and standard Search
campaign in the same authorized account. Bidding settings are neither restricted nor
changed. Confirm rechecks account identity, currency, time zone, parent identity/status/type,
and the complete nonremoved responsive Search ad population. The existing digest,
expiry, allowlists, write gate, audit log and consumed-outcome rules apply.

Headlines require 3 to 15 unique strings, descriptions 2 to 4. Limits are 30 characters
per headline, 90 per description and 15 per display path, counting East Asian wide/fullwidth
characters twice. Blank text, surrounding whitespace, controls, surrogates and braces are
rejected without rewriting. Display paths cannot contain slashes; path2 requires path1.
Blocked terms are checked in all submitted text and the URL at draft and confirmation.

The single final URL must be absolute HTTP(S), have a valid hostname and port, and have
no user information or whitespace. A conservative local safeguard caps it at 2048
characters. Query text is preserved exactly. A configured `advertiser_domain` must be a
valid hostname, such as `example.com`; only that normalized host and its dot-delimited
subdomains are permitted. An empty configuration permits any otherwise valid submitted
URL. No website request or search optimization check occurs.

Exact duplicates are refused regardless of headline or description order. Duplicate
comparison includes URL lists, paths, text and pin placement in existing ads. The full
nonremoved responsive Search ad population is snapshotted, so changes to another such
ad require a fresh preview. Concurrent creation can race this check. The provider enforces
account/ad-group ad-count limits; no unconfirmed local nonremoved-ad count is invented.

The dispatcher permits only AdGroupAdService create with an explicit PAUSED status,
a same-account existing ad-group reference and the closed text-only responsive Search
ad payload. After a successful write, result type and positive compound resource identity
are checked before any read, including the ad-group component. Readback must match the
ad group, PAUSED status, responsive Search type, URL list, paths and unpinned text multisets.
Unrelated Google-added output metadata is ignored. A mismatch is reported as applied but
unverified and consumes the draft. Unknown outcomes also consume the draft; neither retries.

This slice excludes pinning, customizers, other ad formats, mobile/tracking URLs, updates,
removals and bidding changes. Extend the dedicated tool and its closed validator only when
those capabilities are requested.

Offline evidence uses the existing fake Google client with real installed v25 messages,
including request construction, reordered readback success, invalid nested assets,
wrong compound identities before read, drift, duplicates, blocked terms, domain spoofing,
character boundaries, write gating and consumed unverified/unknown outcomes.

Sources reviewed September 7, 2026:

- [Create responsive Search ads](https://developers.google.com/google-ads/api/docs/responsive-search-ads/create-responsive-search-ads)
- [Responsive Search ad text limits](https://support.google.com/google-ads/answer/7684791?hl=en)
- [Retrieve responsive Search ads](https://developers.google.com/google-ads/api/docs/responsive-search-ads/get-responsive-search-ads)

## Verification checkpoint

Implementation c66e8ec passed 793 full-suite tests, including 69 focused responsive Search
ad cases; Ruff and the diff check passed. Independent scoped requirements and quality
review found no actionable issues. A separately installed package exposed 32 tools, matched
the checked client/rails/tools/app source files and refused the new tool with writes disabled.
Narrow package inspection found no forbidden files or known private account markers; this
is not a substitute for the planned full source/history privacy review before publication.

On September 7, 2026, Google accepted a neutral sample with the minimum text counts and both
display paths, using an existing paused test group. The request explicitly used
validate_only=True; the before/after ad inventory matched. No ad was created. Exact private
request and response evidence is retained outside the public package. Validation does not
establish policy approval, serving, or applied-result verification.
