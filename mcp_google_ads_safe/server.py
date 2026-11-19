"""Entrypoint: `mcp-google-ads-safe = mcp_google_ads_safe.server:main` ([project.scripts]).

Importing this module must stay network-free -- `client.preflight()` (live creds) only
ever runs inside main(), never at import time.
"""
import sys

from . import client, settings, tools  # noqa: F401  — importing tools registers them
from .app import mcp


def main():
    # `mcp-google-ads-safe setup` / `reauth` are run by a person in a terminal. With no
    # argument (how an MCP client launches it) the command serves, exactly as before.
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "reauth"):
        from . import setup_wizard
        sys.exit(setup_wizard.main(reauth=sys.argv[1] == "reauth"))
    settings.load()      # loud failure on a bad advertiser.yaml, before serving
    client.preflight()   # M1 gate: prove the login manager is an ancestor of every read id; fail boot loudly
    mcp.run()


if __name__ == "__main__":
    main()
