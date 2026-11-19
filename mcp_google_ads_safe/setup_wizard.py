"""Guided `mcp-google-ads-safe setup` and `reauth`: plain questions, then the existing
Desktop sign-in. Everything that touches credentials or files stays in auth_helper, called
exactly as from the command line; this module only collects three answers and prints the
client configuration. It never reads, prints or stores a secret."""
import json
import os
import re

import yaml

from . import auth_helper

DEFAULT_PROFILE = "~/.mcp-google-ads-safe/google-ads.yaml"
_TRIES = 3
_SOURCE = "git+https://github.com/wvuhskr/mcp-google-ads-safe"


class _GiveUp(Exception):
    pass


def _path(answer: str) -> str:
    """A typed or dragged-in path: outer quotes and spaces dropped, ~ expanded, made
    absolute."""
    return os.path.abspath(os.path.expanduser(answer.strip().strip("'\"").strip()))


def _ask(ask, question, parse, complaint):
    for _ in range(_TRIES):
        value = parse(ask(question + "\n> "))
        if value is not None:
            return value
        print(complaint)
    raise _GiveUp()


def _manager_id(answer):
    digits = re.sub(r"[\s-]", "", answer)
    return digits if re.fullmatch(r"[0-9]{10}", digits, re.ASCII) else None


def _existing_file(answer):
    path = _path(answer) if answer.strip() else None
    return path if path and os.path.isfile(path) else None


def _client_json(ask):
    return _ask(
        ask,
        "Where is the Desktop client JSON file you downloaded from Google Cloud? "
        "Type the path, or drag the file into this window.",
        _existing_file, "No file at that path. Try again.")


def _config_text(profile: str) -> str:
    env = {"GOOGLE_ADS_YAML": profile, "GOOGLE_ADS_READ_SCOPE": "manager"}
    block = {"mcpServers": {"google-ads-safe": {
        "command": "uvx", "args": ["--from", _SOURCE, "mcp-google-ads-safe"], "env": env}}}
    flags = " ".join(f"-e {key}={value}" for key, value in env.items())
    return "\n".join([
        "",
        "Sign-in saved. Connect your assistant with ONE of these.",
        "",
        "Claude Desktop: open Settings > Developer > Edit Config and add this, then quit and "
        "reopen Claude:",
        "",
        json.dumps(block, indent=2),
        "",
        "Claude Code: run this in your terminal, in the folder you work from:",
        "",
        f"  claude mcp add google-ads-safe {flags} -- uvx --from {_SOURCE} mcp-google-ads-safe",
        "",
        "This is read-only: reports work on every enabled account under your manager account, "
        "and nothing can be changed. Then ask your assistant: run health_check, then "
        "find_accounts.",
        "",
        "To allow changes later, add GOOGLE_ADS_ENABLE_WRITES=true and "
        "GOOGLE_ADS_WRITE_CUSTOMER_IDS=<the account numbers that may be changed> to the same "
        "env block. See docs/configuration.md.",
    ])


def _setup(ask) -> int:
    print("This sets up a read-only Google Ads sign-in in three questions. It opens Google's "
          "sign-in page in your browser. Nothing is changed in any ad account.\n"
          "(Using a service account instead? See docs/google-auth-transition.md.)\n")
    client_json = _client_json(ask)
    manager = _ask(
        ask,
        "What is your manager account number (the 10 digits shown at the top of Google Ads, "
        "for example 123-456-7890)? If you have a single account with no manager, use that "
        "account's number.",
        _manager_id, "That is not a 10-digit account number. Try again.")
    profile = _ask(
        ask, f"Where should the sign-in be saved? Press Return for {DEFAULT_PROFILE}",
        lambda answer: _path(answer or DEFAULT_PROFILE), "")
    if os.path.lexists(profile):
        print(f"{profile} already exists, and setup never overwrites a sign-in. To sign in "
              "again and replace it, run: mcp-google-ads-safe reauth")
        return 2
    os.makedirs(os.path.dirname(profile), mode=0o700, exist_ok=True)
    code = auth_helper.main(["--client-secrets", client_json, "--profile", profile,
                             "--login-customer-id", manager])
    if code == 0:
        print(_config_text(profile))
    return code


def _reauth(ask) -> int:
    print("This signs in again and replaces an existing saved sign-in. Nothing is changed in "
          "any ad account.\n")
    profile = _ask(
        ask, f"Which saved sign-in should be replaced? Press Return for {DEFAULT_PROFILE}",
        lambda answer: _existing_file(answer or DEFAULT_PROFILE),
        "No saved sign-in at that path. For a first sign-in run: mcp-google-ads-safe setup")
    try:
        with open(profile, encoding="utf-8") as handle:
            saved = yaml.safe_load(handle)
        manager = _manager_id(str(saved["login_customer_id"]))
    except Exception:
        manager = None
    if manager is None:
        print("That file has no usable manager account number, so it cannot be renewed in "
              "place. Run: mcp-google-ads-safe setup (and save to a new path).")
        return 2
    client_json = _client_json(ask)
    return auth_helper.main(["--client-secrets", client_json, "--profile", profile,
                             "--login-customer-id", manager, "--replace"])


def main(reauth: bool = False, ask=input) -> int:
    try:
        return _reauth(ask) if reauth else _setup(ask)
    except _GiveUp:
        print("Stopped after three tries. Nothing was saved.")
        return 2
    except EOFError:
        print("This command asks questions, so run it yourself in the macOS Terminal app (or "
              "your system's terminal), not through an assistant. Nothing was saved.")
        return 2
    except KeyboardInterrupt:
        print("\nStopped. Nothing was saved by setup itself; if the browser sign-in had "
              "already finished, check the saved file before retrying.")
        return 3
