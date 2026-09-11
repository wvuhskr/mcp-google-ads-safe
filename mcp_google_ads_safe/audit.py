"""Append-only JSONL audit log. Ported from the Microsoft-Ads blueprint; only the
default path and env var change for Google.

Phases: draft / refused / apply / error / unknown. Every event carries `tool`, `phase`,
and a UTC-offset `ts`; the caller (rails) merges in the Google fields it has on hand
(customer_id = mutate_customer_id, login_customer_id, currency, request_id,
operation_count, digest). Missing fields are simply not passed -- audit never fabricates.

stdout is the MCP JSON-RPC channel, so nothing here prints to stdout; a write failure is
raised to the caller (rails decides whether that failure may mask a landed write)."""
import json
import os
import time

AUDIT_PATH = os.path.expanduser("~/.mcp-google-ads-safe/audit.jsonl")


def audit_path() -> str:
    return os.environ.get("GOOGLE_ADS_AUDIT_PATH") or AUDIT_PATH


def log_event(tool: str, phase: str, data: dict, path: str | None = None) -> dict:
    if path is None:
        path = audit_path()
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "phase": phase, **data}
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry
