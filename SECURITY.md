# Security policy

## Supported versions

Security fixes go into the latest release on the `main` branch. Older versions are not patched; upgrade to the latest release to pick up a fix.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository: open the **Security** tab and choose **Report a vulnerability**. That opens a private advisory only the maintainer can see. Do not open a public issue, pull request, or discussion for a suspected vulnerability.

A useful report includes:

- The version, operating system, Python version, and how you installed it.
- What the problem is and what an attacker could do with it.
- Minimal steps to reproduce, using synthetic data or an account you are authorized to test.
- Sanitized logs or screenshots with credentials and customer identifiers removed.

Never include OAuth client secrets, refresh or access tokens, developer tokens, service-account keys, complete credential files, real customer IDs, or advertising performance data. Describe sensitive evidence first; if it is needed, a safe way to share it can be agreed inside the advisory.

You should get an acknowledgement within a few days. This is a one-person project with no bug bounty and no guaranteed fix timeline, but reports are read and taken seriously. Please allow a fix to ship before publishing details that would let someone exploit the problem.

## Testing boundaries

Test only systems and accounts you own or have explicit permission to assess. Prefer synthetic data and the offline test suite. Do not change live campaigns, budgets, bids, or billing without the account owner's authorization. This policy does not authorize testing Google's systems or any other third party.

## Keeping your own installation safe

- Keep the credential file outside the project folder, readable only by you. The server refuses a credential file that contains keys other than the documented sign-in keys.
- Never commit credentials or paste them into issues, chats, or diagnostic output. If a secret leaks, revoke or rotate it through Google's own controls.
- Leave writes off unless you intend to change an account. The rails are a second line of defense, not a substitute for account permissions and reviewing previews.
- The local audit log can contain campaign names, keyword text, and IDs. Protect it like any other operational log.

## Trust boundaries

Everything the server returns is visible to the assistant client you connect. The draft-then-confirm flow is not a human approval step: any connected caller holding a draft id can confirm it. If you need a person in the loop, run the confirm yourself or keep the server read-only.
