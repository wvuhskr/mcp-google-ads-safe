# Website-visitor audience creation

`create_custom_audience(name: str, url_contains: list[str], customer_id: str | None = None)`
is offline verified only. No live audience was created or targeted during development.
Despite the parity tool name, this creates Google's rule-based website-visitor UserList,
not a CustomAudience interest segment, Customer Match upload or personal-data list.

In an MCP client connected to this server, call `create_custom_audience` with a name and
1 to 10 exact URL substrings. These are matching text, never fetched remote URLs.
Each substring must be nonblank, have no surrounding whitespace, controls, format
characters, braces or wildcard shorthand (`*`, `?`), and fit 256 UTF-8 bytes. Duplicates
ignore case; matching text retains exact case and spacing. Name must be nonblank, at
most 128 characters and 255 UTF-8 bytes, with no controls. Names and rules reject the
configured blocked terms. Existing owned names collide ignoring case; shared names do not.

The server process must have `GOOGLE_ADS_ENABLE_WRITES=true`, both account allowlists
must permit the account, and `GOOGLE_ADS_ALLOW_SHARED_AUDIENCE_EDIT=true` must be set.
The audience flag defaults false and malformed values refuse. All gates run before
account reads both when drafting and confirming. Only omitted/None customer uses default.
Confirm the returned draft using the existing confirmation flow after reviewing it.

The new list explicitly has OPEN membership collection. Each CONTAINS rule on `url__`
has a 30-day lookback. Rules combine with OR; each operand has one AND_OF_ORS group
and one item. The ignored top-level membership lifespan and prepopulation request are
omitted. Existing tags can collect matching future visitors. OPEN is collection state,
not an ad serving status. No campaign or ad group is attached and the list is not
automatically targeted. New standalone creation is the bounded exception to scanning
existing shared-resource consumers: the preview explicitly shows `attached_campaigns=[]`.

Complete account identity and owned-list inventory are rechecked before confirmation;
changes require a fresh draft. Concurrent creation can still race the name check.
One atomic UserList create is allowed. Readback checks its positive same-account identity,
exact name, RULE_BASED type, OWNED editable status, OPEN collection and complete exact
rule content. Provider enum encoding and operand order are normalized; text is not.
Prepopulation status returned by Google is ignored. Any mismatch/read failure consumes
the draft as applied but unverified; unknown outcomes cannot be blindly retried.
This proves no tag setup, consent configuration, membership count or ad serving.

In the connected MCP client, use read-only `run_gaql` to discover saved IDs:

```sql
SELECT user_list.id, user_list.resource_name, user_list.name,
       user_list.type, user_list.access_reason, user_list.read_only,
       user_list.membership_status
FROM user_list
```

Provider references: [UserList](https://developers.google.com/google-ads/api/reference/rpc/v25/UserList),
[FlexibleRuleOperandInfo](https://developers.google.com/google-ads/api/reference/rpc/v25/FlexibleRuleOperandInfo).
