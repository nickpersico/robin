"""
Assignment engine — polls Close for new leads and runs each Lead List's actions.

Called by APScheduler every 5 minutes. For each active ongoing Lead List:
  1. Query Close's search API for leads created since last_checked_at that
     match the list's filter conditions.
  2. For each matching lead, run the list's enabled Actions:
       * Assign — pick the next member of the list's Group (Rotation) and
         write that user's ID into the configured custom field. Advance the
         rotation pointer.
       * Workflow — trigger the configured Close Workflow on the lead's
         first contact.
  3. Log the event(s) in AssignmentLog (denormalized so the UI can render
     history without API calls).
  4. Save the current timestamp as last_checked_at.
"""

import copy
import logging
from datetime import datetime
from typing import Optional

from ..extensions import db
from ..models.lead_list import LeadList, TYPE_ONGOING, STATUS_ACTIVE
from ..models.assignment_log import AssignmentLog
from ..models.user import User
from .close_api import CloseClient, CloseAPIError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_filter(filters_json: dict) -> dict:
    """
    Close's UI exports the full search payload (with limit/query/sort keys).
    If that's what was saved, extract just the inner query object.
    """
    if "query" in filters_json and "type" not in filters_json:
        return filters_json["query"]
    return filters_json


def _inject_date_filter(filters_json: dict, after_dt: Optional[datetime]) -> dict:
    """Return a new filter dict that ANDs in date_created > after_dt."""
    after_str = (
        after_dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
        if after_dt
        else "1970-01-01T00:00:00.000000+00:00"
    )
    date_condition = {
        "type": "field_condition",
        "field": {"type": "regular_field", "field_name": "date_created", "object_type": "lead"},
        "condition": {"type": "moment_range", "after": after_str},
    }

    base = copy.deepcopy(filters_json)
    if base.get("type") == "and":
        base.setdefault("queries", []).append(date_condition)
        return base
    return {"type": "and", "queries": [base, date_condition]}


def _get_org_user(close_org_id: str) -> Optional[User]:
    """Find any active user for the org — used to make API calls during polling."""
    return (
        User.query
        .filter_by(close_org_id=close_org_id, status="active")
        .order_by(User.created_at)
        .first()
    )


def _list_org_id(lead_list: LeadList) -> Optional[str]:
    """Resolve the org for a Lead List — prefer the denormalised column."""
    if lead_list.close_org_id:
        return lead_list.close_org_id
    if lead_list.rotation is not None:
        return lead_list.rotation.close_org_id
    return None


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_queue(lead_list_id: str):
    """
    Snapshot all leads currently matching the list's filter so the engine
    never acts on them — they pre-existed the list's creation/resume.
    Must be called inside a Flask app context.
    """
    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None or not lead_list.filters_json:
        return

    org_id = _list_org_id(lead_list)
    if not org_id:
        logger.warning("seed_queue %s: cannot resolve org (workflow-only list with no rotation?)",
                       lead_list_id)
        return

    org_user = _get_org_user(org_id)
    if not org_user:
        logger.warning("seed_queue %s: no active user for org %s", lead_list_id, org_id)
        return

    client = CloseClient(org_user)
    try:
        existing = client.search_leads(_normalize_filter(lead_list.filters_json))
    except CloseAPIError as e:
        logger.error("seed_queue %s: search failed: %s", lead_list_id, e)
        return

    lead_list.seeded_lead_ids = [lead["id"] for lead in existing]
    db.session.commit()
    logger.info("seed_queue %s: seeded %d lead(s)", lead_list_id, len(lead_list.seeded_lead_ids))


# ---------------------------------------------------------------------------
# Core poll logic
# ---------------------------------------------------------------------------

def _run_assign_action(client, lead_list, lead, lead_id, lead_name) -> dict:
    """
    Try to assign the lead. Returns a dict that may include keys:
      - skipped_reason: str (e.g. "field_already_set", "no_active_members")
      - member: RotationMember (the one assigned)
      - error: str
    """
    rotation = lead_list.rotation
    if rotation is None:
        return {"error": "no_rotation"}

    if not lead_list.overwrite_existing and lead_list.custom_field_id:
        existing = (lead.get("custom") or {}).get(lead_list.custom_field_id)
        if existing:
            return {"skipped_reason": "field_already_set"}

    member = rotation.next_member()
    if not member:
        return {"skipped_reason": "no_active_members"}

    try:
        if lead_list.custom_field_id:
            client.assign_lead(lead_id, lead_list.custom_field_id, member.close_user_id)
        rotation.advance()
        return {"member": member}
    except CloseAPIError as e:
        return {"error": str(e)}


def _resolve_run_as(lead_list, assigned_member, sender_cache: dict, client) -> dict:
    """
    Determine which Close user a workflow trigger should run as, and look up
    their email sender info. Returns either:
      {"ok": True, "user_id": ..., "sender_account_id": ..., "sender_name": ..., "sender_email": ...}
      {"error": "..."}

    sender_cache is mutated to memoize lookups within a single poll cycle so
    we don't re-call /connected_account/ for every lead.
    """
    if lead_list.workflow_run_as_user_id:
        run_as_user_id = lead_list.workflow_run_as_user_id
        run_as_user_name = lead_list.workflow_run_as_user_name or ""
    elif assigned_member is not None:
        run_as_user_id = assigned_member.close_user_id
        run_as_user_name = assigned_member.close_user_name or ""
    else:
        return {"error": "no_run_as_user"}

    if run_as_user_id in sender_cache:
        return sender_cache[run_as_user_id]

    try:
        accounts = client.get_user_email_accounts(run_as_user_id)
    except CloseAPIError as e:
        result = {"error": f"could not load email accounts: {e}"}
        sender_cache[run_as_user_id] = result
        return result

    if not accounts:
        result = {"error": f"user {run_as_user_id} has no active email account"}
        sender_cache[run_as_user_id] = result
        return result

    account = accounts[0]
    result = {
        "ok": True,
        "user_id": run_as_user_id,
        "sender_account_id": account["id"],
        "sender_name": run_as_user_name or account.get("display_name") or "",
        "sender_email": account["email"],
    }
    sender_cache[run_as_user_id] = result
    return result


def _run_workflow_action(client, lead_list, lead_id, run_as: dict) -> dict:
    """Trigger the configured Workflow on the lead's first contact."""
    if not lead_list.workflow_id:
        return {"error": "no_workflow_id"}
    try:
        response = client.subscribe_lead_to_workflow(
            lead_id,
            lead_list.workflow_id,
            sender_account_id=run_as["sender_account_id"],
            sender_name=run_as["sender_name"],
            sender_email=run_as["sender_email"],
        )
        return {"ok": True, "subscription_id": (response or {}).get("id")}
    except CloseAPIError as e:
        return {"error": str(e)}


def poll_queue(lead_list_id: str) -> dict:
    """
    Poll Close for one Lead List and run its actions on any new matching leads.
    Must be called inside a Flask app context.
    """
    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None:
        return {"error": "lead_list_not_found"}
    if lead_list.status != STATUS_ACTIVE:
        return {"skipped": True, "reason": "not_active"}
    if not lead_list.filters_json:
        return {"skipped": True, "reason": "no_filters"}
    if not (lead_list.assign_enabled or lead_list.workflow_enabled):
        return {"skipped": True, "reason": "no_actions_enabled"}

    org_id = _list_org_id(lead_list)
    if not org_id:
        logger.error("LeadList %s: cannot resolve org", lead_list_id)
        return {"error": "no_org"}

    org_user = _get_org_user(org_id)
    if not org_user:
        logger.error("LeadList %s: no active user for org %s", lead_list_id, org_id)
        return {"error": "no_org_user"}

    client = CloseClient(org_user)
    now = datetime.utcnow()

    after_dt = lead_list.last_checked_at or lead_list.created_at
    search_query = _inject_date_filter(_normalize_filter(lead_list.filters_json), after_dt)

    try:
        leads = client.search_leads(search_query)
    except CloseAPIError as e:
        logger.error("LeadList %s: search failed: %s", lead_list_id, e)
        return {"error": str(e)}

    seeded = set(lead_list.seeded_lead_ids or [])
    leads = [l for l in leads if l.get("id") not in seeded]

    logger.info("LeadList %s '%s': found %d lead(s) since %s (%d seeded/skipped)",
                lead_list_id, lead_list.name, len(leads), after_dt, len(seeded))

    assigned = workflow_triggered = skipped = errors = 0
    sender_cache: dict = {}  # memoized run-as resolution per poll cycle

    for lead in leads:
        lead_id = lead.get("id", "")
        lead_name = lead.get("display_name", "")

        assign_result = None
        workflow_result = None

        if lead_list.assign_enabled:
            assign_result = _run_assign_action(client, lead_list, lead, lead_id, lead_name)
            if assign_result.get("error"):
                logger.error("LeadList %s: assign failed for %s: %s",
                             lead_list_id, lead_id, assign_result["error"])
                errors += 1
                # Don't trigger the workflow if assignment errored unexpectedly.
                continue
            if assign_result.get("skipped_reason") == "no_active_members":
                logger.warning("LeadList %s: rotation has no active members — stopping",
                               lead_list_id)
                break
            if assign_result.get("skipped_reason") == "field_already_set":
                skipped += 1
                continue

        if lead_list.workflow_enabled:
            assigned_member = (assign_result or {}).get("member")
            run_as = _resolve_run_as(lead_list, assigned_member, sender_cache, client)
            if run_as.get("error"):
                logger.error("LeadList %s: cannot resolve workflow sender for %s: %s",
                             lead_list_id, lead_id, run_as["error"])
                if not assigned_member:
                    errors += 1
                    continue
                # Assignment succeeded — fall through so the assign log is recorded.
                workflow_result = None
            else:
                workflow_result = _run_workflow_action(client, lead_list, lead_id, run_as)
                if workflow_result.get("error"):
                    logger.error("LeadList %s: workflow trigger failed for %s: %s",
                                 lead_list_id, lead_id, workflow_result["error"])
                    if not assigned_member:
                        errors += 1
                        continue

        member = (assign_result or {}).get("member") if assign_result else None
        workflow_ok = bool(workflow_result and workflow_result.get("ok"))
        # Nothing actually happened? Don't log.
        if member is None and not workflow_ok:
            continue

        log = AssignmentLog(
            queue_id=lead_list.id,
            rotation_member_id=member.id if member else None,
            close_lead_id=lead_id,
            close_lead_name=lead_name,
            close_user_id=member.close_user_id if member else None,
            close_user_name=member.close_user_name if member else None,
            workflow_id=lead_list.workflow_id if workflow_ok else None,
            workflow_name=lead_list.workflow_name if workflow_ok else None,
            workflow_subscription_id=(
                workflow_result.get("subscription_id") if workflow_ok else None
            ),
        )
        db.session.add(log)

        if member:
            assigned += 1
            logger.info("LeadList %s: assigned lead '%s' → %s",
                        lead_list_id, lead_name or lead_id, member.close_user_name)
        if workflow_ok:
            workflow_triggered += 1
            logger.info("LeadList %s: triggered workflow %s for lead '%s'",
                        lead_list_id, lead_list.workflow_name, lead_name or lead_id)

    lead_list.last_checked_at = now
    db.session.commit()

    logger.info(
        "LeadList %s done — assigned: %d, workflow: %d, skipped: %d, errors: %d",
        lead_list_id, assigned, workflow_triggered, skipped, errors,
    )
    return {
        "assigned": assigned,
        "workflow_triggered": workflow_triggered,
        "skipped": skipped,
        "errors": errors,
    }


def poll_all_queues():
    """
    Entry point called by APScheduler every 5 minutes.
    Finds all active ongoing Lead Lists and polls each one.
    Must be called inside a Flask app context.
    """
    lead_lists = (
        LeadList.query
        .filter_by(queue_type=TYPE_ONGOING, status=STATUS_ACTIVE)
        .all()
    )

    if not lead_lists:
        logger.debug("Scheduler: no active lead lists to poll.")
        return

    logger.info("Scheduler: polling %d active lead list(s)...", len(lead_lists))
    for lead_list in lead_lists:
        try:
            poll_queue(lead_list.id)
        except Exception:
            logger.exception("Scheduler: unexpected error polling lead list %s", lead_list.id)
