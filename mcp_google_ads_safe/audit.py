"""Append-only JSONL audit log.

Phases: draft / refused / apply / apply_unverified / error / unknown.
  apply            = mutate succeeded and (where applicable) the read-back matched
  apply_unverified = mutate succeeded but the read-back did not match; the write LANDED
  error            = failed before dispatch; nothing landed
  unknown          = failed at the wire or in response parsing; the write MAY have landed Every event carries `tool`, `phase`,
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
