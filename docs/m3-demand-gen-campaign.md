# Paused Demand Gen campaign creation

`create_demand_gen_campaign` returns a reviewable draft for one PAUSED Demand Gen
campaign. Confirmation sends one atomic request containing a dedicated nonshared DAILY
budget, the campaign, then all location criteria in input order and all language criteria
in input order. It creates no ad groups, ads, assets or associations.

This implementation is tested offline against installed Google Ads v25 messages. No
account, credential, provider or network call was made. Google acceptance, eligibility,
policy approval and serving remain unverified.

## Inputs and fixed behavior

The campaign name must be a plain nonblank string of at most 128 Unicode characters and
255 UTF-8 bytes. The daily budget must be a positive finite amount in the selected
account currency, with exact whole micros and within the configured budget cap. Each of
`geo_target_ids` and `language_ids` must be an actual list containing 1 through 100 unique
canonical positive numeric strings. The political-advertising declaration must be an
actual boolean. The optional customer ID must be a canonical positive numeric string;
omission uses the configured default, while explicit null and the literal string `null`
refuse at the Model Context Protocol boundary.

The strategy is fixed to `MAXIMIZE_CONVERSIONS` with no target cost per acquisition or
bid limits. Both positive and negative geographic modes are fixed to `PRESENCE`. The
campaign explicitly saves `upgraded_targeting=False`, so locations and languages belong
to the campaign. Google documents that this choice is immutable: this campaign cannot
later switch to upgraded ad-group targeting.

## Safety and saved proof

Write, read and account allowlists are checked before safety reads and again at
confirmation. The account must be enabled and non-manager, with readable identity,
currency and time zone. Complete uncapped scans prove no exact campaign or budget name
collision and prove every requested location is enabled and language is targetable.
Confirmation recompiles the copied input and refuses drift before dispatch.

The dispatcher accepts only the exact graph, with temporary budget ID `-1` and campaign
ID `-2`, `partial_failure=False`, and no update, removal or retry. It validates every
ordered returned identity before saved reads. Raw v25 readback then proves the dedicated
budget fields, PAUSED Demand Gen campaign, standard strategy with zero optional targets,
explicit immutable targeting setting, political declaration, PRESENCE settings, no
portfolio/feed/travel/hotel attachment, and the complete exact positive target population.

A validation-only response never starts saved reads or claims application. Any bad or
missing result, saved-state mismatch or read failure after dispatch returns applied but
unverified and consumes the draft. An ambiguous transport result also consumes the draft
and is never retried automatically.

An authorized live creation leaves a paused campaign, its dedicated budget and targeting
resources in the account, including if saved-state verification fails. Creation is not
reversible through this tool; this tool does not automatically undo creation.

## Offline verification

After following the [installation instructions](setup.md), run this in the macOS
Terminal app from your project source folder with development dependencies installed:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/test_demand_gen_campaign.py
```
