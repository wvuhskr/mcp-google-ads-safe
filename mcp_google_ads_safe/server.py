"""Entrypoint: `mcp-google-ads-safe = mcp_google_ads_safe.server:main` ([project.scripts]).

Importing this module must stay network-free -- `client.preflight()` (live creds) only
ever runs inside main(), never at import time.
"""
from . import client, settings, tools  # noqa: F401  — importing tools registers them
from .app import mcp


def main():
    settings.load()      # loud failure on a bad advertiser.yaml, before serving
    client.preflight()   # M1 gate: prove the login manager is an ancestor of every read id; fail boot loudly
    mcp.run()


if __name__ == "__main__":
    main()
