# Demand Gen single-image ad draft

`draft_demand_gen_ad` creates a reviewed draft for exactly one PAUSED Demand Gen multi-asset ad, using one existing square marketing image and one existing square logo. Despite the provider family name, this bounded tool creates a single-image creative, not a carousel.

## Prerequisite and boundary

An externally existing PAUSED ad group inside an existing PAUSED DEMAND_GEN campaign is required. `create_ad_group` remains Search-only. `create_demand_gen_campaign` does not supply a Demand Gen group, and this is not an end-to-end campaign builder.

Required original MCP strings: `ad_group_id`, `headline`, `description`, `business_name`, `square_marketing_image_asset_id`, `logo_image_asset_id`, `final_url`. `customer_id` can be omitted for the configured default; explicit MCP null refuses. IDs are positive canonical ASCII decimal strings within signed-int64 bounds. Unknown keys and scalar coercion refuse. The direct Python default remains `None`.

Headline, description and business name allow 30, 90 and 25 weighted characters respectively, using the existing plain-text policy. Wide characters count twice. Text and URL retain their exact submitted spelling and must pass current blocked-content policy. A configured advertiser domain is mandatory. The one literal HTTPS URL must match that domain or a subdomain, with no credentials/macros and at most 2048 characters. HTTPS is a local restriction, not a claimed provider requirement.

Both referenced assets must have exact owned ID/resource identity, IMAGE type, explicit positive metadata, JPEG/PNG format and at most 5,000,000 bytes. Dimensions must be exactly square, at least 300 pixels for the marketing image and 128 for the logo. The size ceiling and exact ratio are conservative local policy. The same asset may fill both roles when it meets both minima; it is read once but submitted in both roles. No image download, upload, bare asset creation or association operation occurs.

## Safety and operation

Writes are disabled by default. Both account allowlists run before reads and again on confirmation. The immutable typed intent compiles through `rails.compile`, the shared paused-create helper and one closed `AdGroupAdService` create operation. The private dispatcher uses one atomic `GoogleAdsService.mutate` request with partial failure disabled, no retries, no update mask or temporary IDs.

Complete raw-row reads prove account identity, enabled non-manager status, currency/time zone, paused group/campaign identity and linkage, DEMAND_GEN channel, current budget and asset metadata. Known raw group type values, including explicitly selected UNSPECIFIED zero, are retained for drift; no group type is claimed compatible from its name. Parent subtype, bidding strategy and portfolio reference are observed unchanged. This tool neither changes nor promises targeting or bidding policy. Shared budgets may be read without edit permission.

The family guard recognizes both actual creative shape and its descriptor marker. It reconstructs the exact operation and check from validated intent and closed proof, refusing missing/replaced checks, mixed operations, foreign references, tracking, other creative families and extra nested fields before provider-client construction. Draft confirmation repeats current policy and all proof reads and compares the exact fingerprint/digest. Pre-dispatch refusal retains the draft. One-hour monotonic expiry, audit phases, provider errors and single-consumption rules are inherited.

## Saved verification and limits

The result must contain exactly one owned `ad_group_ad_result` with the selected group in its positive bounded compound ID before any saved read. Raw readback proves exact identity, PAUSED status, DEMAND_GEN_MULTI_ASSET_AD and the matching oneof, destination, both creative roles, text, business name, no pinning and no unrequested image, call-to-action, tracking, mobile or custom URL settings. Text-asset performance labels are ignored as provider output metadata. Sparse dictionaries and missing optional proof fields refuse instead of supplying defaults. Parent, account, budget and image proof are read again and must match confirmation.

An applied response that cannot be verified consumes its draft and returns `applied=True, verified=False`. An ambiguous transport outcome consumes once and never redispatches automatically. Internal validate-only dispatch makes no saved claim or saved read. Existing apply result semantics are retained for an injected validate-only result: `applied=True, verified=False`, with the nested `validate_only=True`; the public confirmation tool does not expose a validate-only argument.

Creation is not reversible and can leave paused residue after any separately authorized live use. It does not prove inventory-wide absence, policy approval, serving, reach, placement coverage or future status.

Evidence is local v25 message serialization and offline tests only. Account eligibility, existing group compatibility, query combinations, asset readiness, policy, saved default call-to-action behavior and exact provider readback remain unproven. Any provider validation or live creation requires separate authorization.

## Test isolation

The test suite refuses provider construction by default and routes its configuration path to a nonexistent temporary file. Explicit provider fakes are required. Python socket/DNS hooks also deny network attempts for the test process. These hooks are defense in depth, not an operating-system network sandbox: C-backed gRPC can bypass Python socket functions, so the default provider-factory refusal is the primary barrier. The isolated factory test uses only a synthetic temporary configuration and a mocked loader.
