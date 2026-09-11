# Paused retail product item filter

`set_listing_group_filter(asset_group_id, product_item_ids, customer_id=None)` drafts an exact product allowlist for one already-paused retail Performance Max asset group under its already-paused campaign. All remaining product IDs are explicitly excluded by this group's filter. Other campaign/group settings are unchanged. This does not promise the campaign serves only product ads.

The public tool accepts only an asset-group ID, an actual list of exact product item strings, and the optional account. Omission uses the configured account. Explicit JSON null is refused; direct Python None follows the existing default-account convention. Account/group IDs must be canonical positive ASCII digits. The local request cap is 1 through 20 unique strings, each nonempty and at most 50 UTF-8 bytes, without surrounding whitespace, control characters or literal `null`. These are conservative local caps, not claimed provider limits. Case and exact characters are preserved; sorting only determines canonical operation order. Item IDs never enter queries and no wildcard expansion occurs.

## Admission and unchanged proof

Writes must be enabled and both account read/write gates must pass before any scan or provider construction. Complete raw account, group, parent and listing-node scans use the existing complete-scan primitive. Missing/duplicate/dictionary rows, incomplete scans and cross-owner identities refuse.

The account must be the exact enabled non-manager client. The group and campaign must be PAUSED. The parent must be PERFORMANCE_MAX with UNSPECIFIED subtype, a positive merchant ID and a feed label matching `[A-Z0-9_-]{1,20}`. Local, partner, vehicle, hotel, travel, disabled-product-feed, ignored-shopping-brand-exclusion and nonzero-priority configurations refuse. Vehicle refusal also checks campaign listing_type. Supported own-campaign bidding is MAXIMIZE_CONVERSIONS or MAXIMIZE_CONVERSION_VALUE, with nonnegative finite targets and no unsupported populated bidding fields or portfolio strategy.

Selection includes every descriptor leaf of the checked shopping and bidding messages, including unsupported bidding alternatives; empty message descriptors select the message itself. Selected raw messages are retained as canonical hexadecimal serialized protobuf snapshots, preserving optional-field presence and untouched settings. Decoding rejects unknown fields and noncanonical snapshots. Group name/destinations/paths, campaign budget/branding/automation/political/geography settings are bound unchanged without imposing creative-creation policy. No brand-asset or creative scan is performed. Existing non-retail creative proofs remain unchanged and still refuse retail.

Merchant/feed identify configured product scope only. They do not prove Merchant Center linkage, product existence, approval, stock, performance or availability. No Merchant Center service, credential or product scan belongs to this tool.

## Exact atomic replacement

Only an empty inventory, one SHOPPING all-products included root, or a shallow canonical item-ID allowlist with one excluded typed remainder is admitted. Unsupported hierarchies, dimensions, sources, roots, duplicates, missing remainder, malformed case presence and more than 20 included IDs refuse. An equal requested set is NO_CHANGES.

The new graph is one SHOPPING SUBDIVISION root, canonical included item-ID leaves, and one UNIT_EXCLUDED remainder whose product_item_id message is present with its optional value absent. An absent case, empty string value and typed empty item dimension are different states. Nodes have no status. Output-only ID/path, masks and updates are never generated.

Old children are removed in resource-name order before their root. The new root precedes leaves, using generated compound paths with unique negative filter IDs and the real positive group ID. At most 44 ordered operations are sent in one existing GoogleAdsService mutate request with partial_failure false. The exact same graph is used for validate-only and apply. Dedicated plan and operation-builder checks reject graph changes before provider construction; bare node operations and unrelated contexts cannot authorize nodes.

Preview identifies account, paused group/campaign, merchant/feed, old/new IDs, all-products narrowing, remaining-product exclusion, ordered operations and removal/create counts. Preview and validate-only do not claim a saved change.

## Confirmation and outcome

Confirmation repeats gates and fresh exact snapshots before dispatch. Drift aborts the draft. Each returned operation result must have the expected listing-filter kind and exact ordered removal identity or a unique new positive owned compound path. Creation positions map returned IDs to requested nodes. Complete saved-tree equality proves the mapping, including parent references, exact IDs, case presence and no old/extra nodes. Populated output paths must match semantic leaf dimensions; no root-path representation is invented.

Saved account/group/parent snapshots must remain equal. Result/readback failures are applied but unverified and consume the draft. Transport ambiguity follows existing unknown-outcome handling. There is no automatic retry, rollback or inverse mutation.

## Evidence boundary

Offline tests use real installed v25 messages and fake provider responses. They prove local request construction, safety refusal, snapshot binding and outcome handling. Actual query acceptance, mutation acceptance, saved state, product eligibility and serving require a separately authorized future live boundary. Package/source equality and recovery proof are performed by the coordinating acceptance run, not established merely by these unit tests.
