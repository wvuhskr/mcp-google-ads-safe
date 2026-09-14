# Troubleshooting

## Command not found

In the MCP client configuration, use the absolute command path inside the project virtual environment, such as `/absolute/path/to/mcp-google-ads-safe/.venv/bin/mcp-google-ads-safe`. Desktop clients do not reliably inherit a macOS Terminal activation. Restart the client after correcting the path or environment.

## Missing profile or expired authentication

`set GOOGLE_ADS_YAML or pass an explicit credential profile path` means the environment value is absent. A file-not-found or Google authentication error means the private profile is missing, unreadable, or invalid. Point `GOOGLE_ADS_YAML` to an existing valid profile outside the project.

For a service account, confirm that `json_key_file_path` is an absolute path to the private JSON key and that the service-account email has Google Ads access. If organization policy blocks keys, use an administrator-approved route or the Desktop fallback; do not weaken the policy. For a Desktop profile, use the helper in [setup](setup.md) to create or explicitly replace one private profile. The Google library handles routine access-token refresh for a valid profile. The helper passed synthetic offline tests and a bounded real Desktop sign-in/private-save check on September 11, 2026. The new profile also passed read-only health through an MCP client (OpenAI Codex); explicit profile replacement and post-restart health also passed. Recovery after token revocation remains unverified. The earlier bounded check of one existing user-authorization profile remains described in [release readiness](release-readiness.md).

## Desktop sign-in helper errors

Run the helper from the macOS Terminal app with absolute paths, as shown in [setup](setup.md). On failure, it prints one of these safe outcomes:

- `Invalid input or file operation failed. Existing profile unchanged. Check paths and permissions.` Check that the Desktop client file is valid JSON downloaded for an installed Desktop app, is a regular file no larger than 65,536 bytes (64 KiB), both paths are absolute and contain no symbolic links, and the login manager ID is ten digits without dashes. The destination must be absent for a first creation or an existing regular file when using `--replace`. This message also covers a changed file during sign-in or a folder/file permission failure.
- `Sign-in failed or no refresh token returned. Existing profile unchanged.` This generic message does not identify one cause. The helper asks the operating system for an available local callback port, so do not assume another app occupied it. Retry once; if it repeats, confirm the OAuth client is a Desktop client and that local security software permits the Python process to receive a loopback callback, meaning a browser response sent back to the same Mac. If the browser completed, repeat consent so Google can return a refresh token.
- `Sign-in cancelled or timed out. Existing profile unchanged.` This safe message does not establish whether the sign-in was cancelled or timed out. The local browser callback wait is three minutes. Run the command again and finish the browser step within that time; a keyboard interruption has the separate message below.
- `Sign-in interrupted. Check the chosen profile before retrying.` Inspect whether the named profile exists and has a recent modification time before retrying; an interruption may arrive after the file was saved.
- `Profile may be saved, but disk confirmation or cleanup failed. Inspect it before any retry.` Do not run `--replace` again until you inspect the named file. The save may have completed even though the helper could not confirm the final disk flush or temporary-file cleanup.

After a successful save, restart the Model Context Protocol client, meaning the assistant app that launches this server. Do not paste the client secret, refresh token, callback address, or profile contents into the Terminal app or a support message.

## Invalid advertiser YAML

The error names the file and invalid field. Remove unknown keys and compare it with the [configuration example](configuration.md). The root and `keyword_research` must be mappings, `blocked_terms` must contain non-empty text, and a present `advertiser_domain` cannot be empty.

## `NO_LOGIN_MANAGER`

Add the manager customer ID as `login_customer_id` in the private credential profile. It must be the manager through which the authorized advertising customers are reachable. Restart the client.

## `ANCESTRY_NOT_DESCENDANT` or `ANCESTRY_TRUNCATED`

`ANCESTRY_NOT_DESCENDANT` means the completed manager-tree scan did not find an authorized read customer below the login manager. Correct the login manager or remove the customer from `GOOGLE_ADS_READ_CUSTOMER_IDS`.

`ANCESTRY_TRUNCATED` means the scan could not prove ancestry safely. If it names the depth ceiling and a missing target, use a nearer appropriately authorized login manager. If it says the walk exceeded 500 nodes, the full manager tree itself exceeded the safety ceiling, so reducing the read allowlist does not fix it. Do not bypass either check; choose an appropriately authorized nearer manager and restart.

## Writes disabled or account unauthorized

`WRITES_DISABLED` means `GOOGLE_ADS_ENABLE_WRITES` is off. Enable it only after the read-only identity check. `NOT_ALLOWLISTED` means the customer is absent from the needed allowlist, or the write list is not a subset of the read list. Correct the explicit IDs; do not widen access merely to silence the error.

## Draft expired, disappeared, or was used

Create an expired draft again so its preview uses current account state. An unknown or already-applied draft may have expired during cleanup, been consumed, or disappeared on restart. Read current account state first if confirmation may have run, then create a new draft only if another change is needed.

## Unknown or applied-but-unverified write result

Do not automatically retry. A transport or response error can occur after Google accepted a mutation, and some operations consume the draft before saved-state verification finishes. Use read-only tools to reconcile the exact resource. Retry only after proving the first request did not land.
