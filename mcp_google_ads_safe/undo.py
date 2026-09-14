"""undo_change: draft the reverse of an applied change.

Reads the audit log (the only record that survives a restart), finds the "apply" event for
the given draft_id, and builds the reverse Intent from the before-state that the original
preview recorded. The reverse rides the SAME rails as any other write: it returns a new
draft that still needs confirm_and_apply, and every cap, allowlist and drift check runs
again against the live account. undo never dispatches anything itself.

What is reversible in v1 and how:
  update_campaign / update_ad_group   -> restore the recorded previous value(s)
  pause_entity / enable_entity        -> set the recorded previous status
  draft_keywords / add_negative_keywords -> remove the criteria the apply created
  remove_keywords / remove_negative_keywords -> re-add the removed text + match type
                                         (new criterion ids; keyword-level bids are lost)
  update_keyword_bid                  -> restore the recorded previous bid
  set_campaign_schedule               -> restore the recorded previous week
  creations (campaign / ad group)     -> pause the created entity if it has been enabled;
                                         a still-paused creation has nothing to reverse
Everything else is reported as not reversible with the reason. Removals are permanent at
Google and can never be undone.
"""
import json
import os

from . import audit, client, rails

_MINUTE_ENUM_TO_INT = {"ZERO": 0, "FIFTEEN": 15, "THIRTY": 30, "FORTY_FIVE": 45}
_APPLIED = {"apply", "apply_unverified"}


def _events():
    path = audit.audit_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn line must not hide the rest of the log
    return out


def _find_applied(draft_id: str, events) -> dict:
    mine = [e for e in events if e.get("draft_id") == draft_id]
    if not mine:
        raise rails.RailViolation(f"no audit record for draft_id {draft_id!r}", code="UNKNOWN_DRAFT")
    applied = [e for e in mine if e.get("phase") in _APPLIED]
    if not applied:
        phases = sorted({e.get("phase") for e in mine})
        if "unknown" in phases:
            raise rails.RailViolation(
                "that change has an UNKNOWN outcome; read the account before deciding what "
                "to reverse", code="OUTCOME_UNKNOWN")
        raise rails.RailViolation(
            f"draft {draft_id!r} was never applied (phases seen: {phases})", code="NOT_APPLIED")
    prior = [e for e in events if e.get("tool") == "undo_change" and e.get("undo_of") == draft_id]
    if prior:
        raise rails.RailViolation(
            f"an undo draft already exists for {draft_id!r} (draft "
            f"{prior[-1].get('draft_id')!r}); confirm or let it expire before drafting another",
            code="ALREADY_UNDONE")
    return applied[-1]


def _money(micros):
    return client.from_micros(micros) if micros not in (None, "", "0", 0) else None


def _campaign_target(current: dict, kind: str):
    if kind == "cpa":
        return _money((current.get("target_cpa") or {}).get("target_cpa_micros")
                      or (current.get("maximize_conversions") or {}).get("target_cpa_micros"))
    roas = ((current.get("target_roas") or {}).get("target_roas")
            or (current.get("maximize_conversion_value") or {}).get("target_roas"))
    return None if roas in (None, 0, "0") else roas


def _reverse_update(event: dict):
    from . import tools
    p = event["preview"]
    cid = p["customer_id"]
    if "current_daily_budget" in p and "requested_changes" not in p:
        # budget-only path has its own preview shape
        return tools.update_campaign(p["campaign_id"], p["current_daily_budget"], cid)
    current, changed = p["current"], p["requested_changes"]
    kwargs, notes = {}, []
    if "status" in changed:
        kwargs["status"] = current["status"]
    if "name" in changed:
        kwargs["name"] = current["name"]
    if "daily_budget" in changed:
        if "current_daily_budget" not in p:
            raise rails.RailViolation(
                "previous daily budget was not recorded for this change", code="NOT_REVERSIBLE")
        kwargs["daily_budget"] = p["current_daily_budget"]
    campaign = event["tool"] == "update_campaign"
    if "target_cpa" in changed or "clear_target_cpa" in changed:
        prev = (_campaign_target(current, "cpa") if campaign
                else _money(current.get("target_cpa_micros")))
        if prev is None:
            kwargs["clear_target_cpa"] = True
            notes.append("previous state had no explicit target CPA; the undo clears it")
        else:
            kwargs["target_cpa"] = prev
    if campaign and ("target_roas" in changed or "clear_target_roas" in changed):
        prev = _campaign_target(current, "roas")
        if prev is None:
            kwargs["clear_target_roas"] = True
            notes.append("previous state had no explicit target ROAS; the undo clears it")
        else:
            kwargs["target_roas"] = prev
    if "cpc_bid" in changed:
        prev = _money(current.get("cpc_bid_micros"))
        if prev is None:
            raise rails.RailViolation(
                "the ad group had no explicit CPC before, and no tool clears a CPC bid; "
                "set the bid you want directly", code="NOT_REVERSIBLE")
        kwargs["cpc_bid"] = prev
    if not kwargs:
        raise rails.RailViolation("nothing reversible in that update", code="NOT_REVERSIBLE")
    if campaign:
        out = tools.update_campaign(p["entity_id"], kwargs.pop("daily_budget", None), cid, **kwargs)
    else:
        out = tools.update_ad_group(p["entity_id"], cid, **kwargs)
    if notes:
        out["undo_notes"] = notes
    return out


def _criterion_ids(resource_names, prefix):
    ids = []
    for rn in resource_names or []:
        if isinstance(rn, str) and f"/{prefix}/" in rn and "~" in rn:
            ids.append(rn.rsplit("~", 1)[-1])
    return ids


def _removed_rows(event: dict):
    """Rows the original removal deleted, recovered from the recorded pre-state."""
    p = event["preview"]
    removed = {op["operation"]["remove"] for op in p.get("operations", []) if "remove" in op.get("operation", {})}
    return [r for r in p["current"].get("rows", []) if r.get("resource_name") in removed]


def _reverse_criteria(event: dict):
    from . import tools
    p, tool = event["preview"], event["tool"]
    cid, pid = p["customer_id"], p["parent_id"]
    names = (event.get("result") or {}).get("resource_names") or []
    if tool == "draft_keywords":
        ids = _criterion_ids(names, "adGroupCriteria")
        if not ids:
            raise rails.RailViolation("created keyword ids were not recorded", code="NOT_REVERSIBLE")
        return tools.remove_keywords(pid, ids, cid)
    if tool == "add_negative_keywords":
        ids = _criterion_ids(names, "campaignCriteria")
        if not ids:
            raise rails.RailViolation("created negative ids were not recorded", code="NOT_REVERSIBLE")
        return tools.remove_negative_keywords(pid, ids, cid)
    if tool in {"remove_keywords", "remove_negative_keywords"}:
        rows = _removed_rows(event)
        kws = [{"text": r["keyword"]["text"], "match_type": r["keyword"]["match_type"]}
               for r in rows if r.get("keyword")]
        if not kws:
            raise rails.RailViolation("removed keyword text was not recorded", code="NOT_REVERSIBLE")
        if tool == "remove_keywords":
            out = tools.draft_keywords(pid, kws, cid)
            out["undo_notes"] = ["re-added keywords get new criterion ids; keyword-level bids "
                                 "and status history are not restored"]
            return out
        by_match = {}
        for k in kws:
            by_match.setdefault(k["match_type"], []).append(k["text"])
        if len(by_match) != 1:
            raise rails.RailViolation(
                "removed negatives had mixed match types; re-add them in separate calls "
                f"({ {m: t for m, t in by_match.items()} })", code="NOT_REVERSIBLE")
        (match, texts), = by_match.items()
        return tools.add_negative_keywords(pid, texts, match, cid)
    if tool == "update_keyword_bid":
        target = None
        for op in p.get("operations", []):
            upd = op.get("operation", {}).get("update")
            if upd and "cpc_bid_micros" in upd:
                target = upd["resource_name"]
        row = next((r for r in p["current"].get("rows", []) if r.get("resource_name") == target), None)
        prev = _money((row or {}).get("cpc_bid_micros"))
        if prev is None or target is None:
            raise rails.RailViolation("previous keyword bid was not recorded", code="NOT_REVERSIBLE")
        return tools.update_keyword_bid(pid, target.rsplit("~", 1)[-1], prev, cid)
    if tool == "set_campaign_schedule":
        rows = [r for r in _removed_rows(event) if r.get("ad_schedule")]
        if not rows:
            raise rails.RailViolation(
                "the campaign had no schedule before (24/7); undo cannot express 'no schedule'. "
                "Remove the schedule windows in the Google Ads UI.", code="NOT_REVERSIBLE")
        schedules = []
        for r in rows:
            s = r["ad_schedule"]
            schedules.append({"day_of_week": s["day_of_week"],
                              "start_hour": int(s["start_hour"]),
                              "start_minute": _MINUTE_ENUM_TO_INT[s["start_minute"]],
                              "end_hour": int(s["end_hour"]),
                              "end_minute": _MINUTE_ENUM_TO_INT[s["end_minute"]]})
        out = tools.set_campaign_schedule(pid, schedules, cid)
        out["undo_notes"] = ["previous bid modifiers on schedule windows are not restored"]
        return out
    raise rails.RailViolation(f"{tool} is not reversible via undo", code="NOT_REVERSIBLE")


_CREATED_ENTITY = {"campaigns": "campaign", "adGroups": "ad_group"}


def _reverse_creation(event: dict):
    from . import tools
    cid = event["preview"]["customer_id"]
    names = (event.get("result") or {}).get("resource_names") or []
    for rn in names:
        parts = rn.split("/") if isinstance(rn, str) else []
        if len(parts) == 4 and parts[2] in _CREATED_ENTITY:
            entity_type, entity_id = _CREATED_ENTITY[parts[2]], parts[3]
            info = client.entity_status(cid, entity_type, entity_id)
            if info.get("status") == "ENABLED":
                return tools.pause_entity(entity_type, entity_id, cid)
            return {"reversible": False, "undo_of": event["draft_id"],
                    "reason": f"created {entity_type} {entity_id} is {info.get('status')}, "
                              "not serving; nothing to reverse. Remove it by hand if unwanted."}
    raise rails.RailViolation(
        "creation did not record a campaign or ad group id to pause", code="NOT_REVERSIBLE")


_NOT_REVERSIBLE = {
    "remove_entity": "removal is permanent at Google",
    "remove_extension": "removal is permanent at Google",
    "remove_asset_group_asset": "removal is permanent at Google",
    "exclude_geo_target": "no tool removes a negative location yet",
    "remove_geo_target": "no tool re-adds a positive location yet",
    "upload_image_asset": "assets cannot be deleted through the API",
    "upload_text_asset": "assets cannot be deleted through the API",
    "create_custom_audience": "audiences cannot be deleted through this server",
    "create_conversion_action": "conversion actions cannot be deleted through this server",
    "apply_recommendation": "Google does not expose an un-apply",
    "dismiss_recommendation": "Google does not expose an un-dismiss",
}


def undo_change(draft_id: str) -> dict:
    if type(draft_id) is not str or not draft_id.strip():
        raise rails.RailViolation("draft_id must be a nonempty string", code="BAD_INPUT")
    rails.check_writes_enabled()
    event = _find_applied(draft_id, _events())
    tool = event.get("tool")
    if tool in _NOT_REVERSIBLE:
        return {"reversible": False, "undo_of": draft_id, "original_tool": tool,
                "reason": _NOT_REVERSIBLE[tool]}
    if tool in {"update_campaign", "update_ad_group"}:
        out = _reverse_update(event)
    elif tool in {"pause_entity", "enable_entity"}:
        p = event["preview"]
        out = getattr(tools_module(), "pause_entity" if p["current_status"] == "PAUSED"
                      else "enable_entity")(p["entity_type"], p["entity_id"], p["customer_id"])
    elif tool in {"draft_keywords", "add_negative_keywords", "remove_keywords",
                  "remove_negative_keywords", "update_keyword_bid", "set_campaign_schedule"}:
        out = _reverse_criteria(event)
    elif tool in {"draft_campaign", "create_ad_group", "create_pmax_campaign",
                  "create_demand_gen_campaign"}:
        out = _reverse_creation(event)
    else:
        return {"reversible": False, "undo_of": draft_id, "original_tool": tool,
                "reason": f"{tool} is not covered by undo yet"}
    if out.get("reversible") is False:
        return out
    out.update(undo_of=draft_id, original_tool=tool)
    # Link the reverse draft to the original in the log so a second undo is refused.
    audit.log_event("undo_change", "draft", {"undo_of": draft_id, "draft_id": out["draft_id"],
                                            "original_tool": tool,
                                            "customer_id": event.get("customer_id")})
    return out


def tools_module():
    from . import tools
    return tools
