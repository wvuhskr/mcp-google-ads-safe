# Set up from the source folder

This guide prepares the recommended service-account route and provides a Desktop user sign-in fallback. A service account is an app-owned identity. Desktop sign-in is the end-to-end authorization path verified for this release; service-account setup remains an unverified alternative. The source does not create a service identity or key. The Desktop helper can create or replace one private profile that you name; the Google library handles routine access-token refresh for a valid profile. The helper passed synthetic offline tests and a bounded September 11, 2026 real Desktop sign-in/private-save check. The new profile then passed read-only account health and an actual Codex connection check. Explicit profile replacement and post-restart Codex health also passed; revoked-token recovery remains unverified. A bounded September 10, 2026 check did refresh one existing user-authorization profile and verify account discovery plus startup prerequisites and health for one selected advertiser; see [release readiness](release-readiness.md) for the exact limits.

## 1. Prepare the prerequisites

You need Python 3.12 or newer, this project source folder, a Google Cloud project with Google Ads API access, and a Google Ads manager account that can read every advertising customer you plan to authorize. In Google Cloud Console, select or create the project, enable the Google Ads API, then request the needed access level from Google Ads API Overview. New projects begin with test access; production use needs the appropriate upgrade.

The login manager is the manager account named by `login_customer_id` in the private credential profile. The advertising customer is the account whose campaigns and reports you want to use. At startup, the server checks that every read-authorized advertising customer is below the login manager in the accessible manager tree.

For the recommended route, create or select a service account in Google Cloud Console under IAM & Admin > Service Accounts. Select its email, open Keys, choose Add key > Create new key > JSON, and store the one-time download in a private location outside this project. Then add the service-account email in Google Ads under Admin > Access and security > Users. Follow Google's [service-account guide](https://developers.google.com/google-ads/api/docs/oauth/service-accounts) and [key creation guide](https://docs.cloud.google.com/iam/docs/keys-create-delete). If organization policy blocks key creation, use an administrator-approved route or the Desktop fallback below; do not weaken the policy.

## 2. Install the command

In the macOS Terminal app, replace the project path with the absolute path to your source folder:

```sh
cd /absolute/path/to/mcp-google-ads-safe
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

The installed command is `/absolute/path/to/mcp-google-ads-safe/.venv/bin/mcp-google-ads-safe`. Fresh isolated installations passed on macOS Apple Silicon with Python 3.12.13, 3.13.14 and 3.14.6 after downloading public binary dependencies. Each built and installed the source wheel, passed dependency and installed-package checks, and passed all 3,807 synthetic tests with network and real credential access blocked. This qualifies those exact environments, not other platforms or every supported dependency combination. Installation may download dependencies.

## 3. Store the credential profile outside the project

For the recommended service-account route, create a private YAML file outside the source folder. YAML is a plain-text settings format. This profile does not need a developer token:

```yaml
json_key_file_path: "/absolute/private/path/service-account-key.json"
login_customer_id: "1112223333"
use_proto_plus: true
```

Add `impersonated_email` only when your approved Google setup requires delegation. Keep the JSON key and YAML profile private and out of source control.

If you already use Desktop user authorization, the backward-compatible profile remains:

```yaml
client_id: "REPLACE_WITH_OAUTH_CLIENT_ID"
client_secret: "REPLACE_WITH_OAUTH_CLIENT_SECRET"
refresh_token: "REPLACE_WITH_VALID_REFRESH_TOKEN"
login_customer_id: "1112223333"
use_proto_plus: true
```

An existing `developer_token` may remain in a manually maintained Desktop profile as an optional backward-compatibility field. New users should not invent or apply for one.

Do not commit either profile. `GOOGLE_ADS_YAML` must point to it explicitly. The server forces Google Ads API v25 and `use_proto_plus: true` when loading it.

Use the Desktop fallback only when the recommended service-account route is unavailable or inappropriate. First create a Desktop OAuth client in Google Cloud by following Google's [single-user authorization](https://developers.google.com/google-ads/api/docs/oauth/single-user-authentication), [OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent), and [Desktop client](https://developers.google.com/workspace/guides/create-credentials) instructions. Store the downloaded client JSON outside this project.

For the first sign-in, run this in the macOS Terminal app. Replace every example with an absolute private path and use the ten-digit login manager ID without dashes:

```sh
/absolute/path/to/mcp-google-ads-safe/.venv/bin/python -m mcp_google_ads_safe.auth_helper \
  --client-secrets /absolute/private/path/desktop-client.json \
  --profile /absolute/private/path/google-ads.yaml \
  --login-customer-id 1112223333
```

The helper's first-create destination must not exist, and it refuses to overwrite an existing profile. To renew or replace the same named profile, run the same command in the macOS Terminal app with `--replace` at the end. Replacement writes only `client_id`, `client_secret`, `refresh_token`, `login_customer_id`, and `use_proto_plus`; it removes any other fields from that chosen profile, including an older optional `developer_token`. Removing that optional field from the file does not revoke or change the developer token issued by Google or alter any other profile. The original regular file stays in place until the new sign-in is ready, but a message that says the profile may be saved requires inspection before any retry.

The command does not put secrets on the command line or ask you to paste a token. It opens Google's sign-in page and saves the returned authorization privately. On `Private profile saved. Restart the server to load it.`, restart the Model Context Protocol client, meaning the assistant app that launches this server, so it loads the new file. Real Desktop sign-in, new-profile loading and a health-only Codex connection passed a bounded September 11, 2026 check. Explicit profile replacement and post-restart health also passed; recovery after token revocation remains unverified. The browser completion page alone does not prove the profile was saved; confirm the helper reports a successful save.

## 4. Choose the read-only account scope

In the macOS Terminal app, use the advertising customer ID without dashes:

```sh
export GOOGLE_ADS_YAML="/absolute/private/path/google-ads.yaml"
export GOOGLE_ADS_CUSTOMER_ID="4445556666"
export GOOGLE_ADS_READ_CUSTOMER_IDS="4445556666"
```

Leave `GOOGLE_ADS_ENABLE_WRITES` unset. The server is read-only by default. For several customers, separate IDs with commas; each must descend from the login manager.

## 5. Connect an MCP client

For Claude Code, put this `mcpServers` object in `.mcp.json` at the root of the project where you will use the server. For Claude Desktop on macOS, put it in `~/Library/Application Support/Claude/claude_desktop_config.json`. Replace every path and customer ID. These Claude placements follow the local Microsoft Ads MCP comparison and remain untested here. A separate health-only Codex connection was verified; that result does not establish these Claude configurations. Other clients may use a different outer structure, so follow that client's documentation.

```json
{
  "mcpServers": {
    "google-ads-safe": {
      "command": "/absolute/path/to/mcp-google-ads-safe/.venv/bin/mcp-google-ads-safe",
      "env": {
        "GOOGLE_ADS_YAML": "/absolute/private/path/google-ads.yaml",
        "GOOGLE_ADS_CUSTOMER_ID": "4445556666",
        "GOOGLE_ADS_READ_CUSTOMER_IDS": "4445556666"
      }
    }
  }
}
```

Desktop clients normally do not inherit a virtual environment activated in the macOS Terminal app. Use the absolute installed command path and place the required environment values in the client configuration. Restart the client after changing it.

## 6. Run the first live check

In the connected assistant chat, call `health_check`. This is a live Google Ads call, even with writes disabled. Startup performs the same live credential and manager-ancestry preflight before accepting requests.

Success returns `ok: true` and an account list. Compare the intended account's `customer_id`, `descriptive_name`, and `currency_code`; a missing value is not proof of identity. Then call `get_account_info` if you also need to verify its time zone or status. Failure means credentials, manager access, or the read allowlist must be corrected; see [troubleshooting](troubleshooting.md).

Only after proving the correct account should you consider the separate [write configuration](configuration.md). Every write still requires a draft and later `confirm_and_apply`.

## Maintainer history

On September 4, 2026, the project abandoned its test-account provisioning approach after it did not establish an eligible test manager. Do not retry that walkthrough or assume a test account exists. Future connected checks follow the [validation procedure](validation-readiness.md).
