# Security policy

This policy covers the private review stage of Google Ads MCP Safe. A public reporting channel must be established before public launch.

## Supported versions

Version 1.0.0 is the current private review version. On public launch, the security maintenance target will be the latest stable release. Earlier private builds and prereleases are not supported public releases. Security fixes may require upgrading to the latest stable version; backports to older versions are not promised.

## Reporting a vulnerability

Please do not report suspected vulnerabilities through public issues, public code-change requests, or public discussions. Never post credentials or customer data.

During private review, contact the maintainer through the private channel used to arrange your access. First state that you need to report a security concern without including exploit details or secrets; agree on a private way to share the report. Do not put sensitive reports in repository issues, even while the repository is private, because issues may become visible when its visibility changes.

GitHub's [private vulnerability reporting feature](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) is intended for public repositories. It is not represented as enabled here. Before public launch, the maintainer must establish and verify a monitored private reporting route, publish its exact instructions here, and check notification delivery. If you do not have an existing private contact channel, do not submit sensitive details until a reporting route is published.

A useful private report includes:

- The affected version, operating system, Python version, and installation method.
- A description of the problem and its potential impact.
- Minimal reproduction steps using synthetic data or an account you are authorized to test.
- Sanitized logs or screenshots, with credentials and identifying customer details removed.
- Any suggested mitigation and a way to contact you privately.

Do not include OAuth client secrets, access or refresh tokens, developer tokens, complete sign-in profiles, customer identifiers, or advertising performance data. Describe sensitive evidence first so a safe way to share it can be agreed upon if necessary.

## Handling reports

The maintainer will assess reports and coordinate any necessary fix and disclosure privately. There is no guaranteed response or resolution deadline and no paid bug bounty is offered. Please allow time for assessment and a fix before publishing technical details that would enable exploitation.

## Testing boundaries

Test only systems and accounts you own or have explicit permission to assess. Prefer synthetic data and local tests. Do not change live campaigns, budgets, bids, billing, or other advertising resources without the account owner's explicit authorization. Do not access other users' data or disrupt services. This policy does not authorize testing Google's systems or any other third-party service.

## Keeping your installation safe

Keep sign-in files outside the project folder and restrict access to their owner. Never commit credentials or paste them into issues, chats, or diagnostic output. If credentials are exposed, use the relevant provider's official controls to revoke or rotate them.

Keep writes disabled unless you intend to authorize account changes. Application write guards are an additional safeguard, not a replacement for account permissions or careful review. Review updates before installing them, and use the documented configuration and troubleshooting instructions.

## Data and trust boundaries

Account information returned by this server is visible to the assistant client you connect. Review that client's access and data-handling settings. Local audit records can also contain sensitive operational details and need appropriate access controls. The draft-and-confirm workflow is not a human approval mechanism: a connected caller with the draft identifier can confirm a change.
