"""
Assignment engine — polls Close for new leads and assigns them via active queues.

Called by APScheduler every 5 minutes. Each active ongoing queue:
  1. Queries Close's search API for leads created since last_checked_at
     that match the queue's filter conditions.
  2. For each matching lead, assigns it to the next member in the rotation
     by writing the user's ID to the configured custom field.
  3. Logs each assignment in AssignmentLog and advances the rotation pointer.
  4. Saves the current timestamp as last_checked_at so the next poll picks up
     only leads created after this run.
"""

import copy
import logging
from datetime import datetime, timezone
from typing import Optional

from ..extensions import db
from ..models.queue import Queue, TYPE_ONGOING, STATUS_ACTIVE
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
    """
    Return a new filter dict that is the user's filter AND date_created > after_dt.
    If after_dt is None, uses the Unix epoch (effectively no lower bound).
    """
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
    # Wrap non-AND filters in a new AND
    return {"type": "and", "queries": [base, date_condition]}


def _get_org_user(close_org_id: str) -> Optional[User]:
    """Find any active user for the org — used to make API calls during polling."""
    return (
        User.query
        .filter_by(close_org_id=close_org_id, status="active")
        .order_by(User.created_at)
        .first()
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_queue(queue_id: str):
    """
    Fetch all leads that currently match the queue's filter and store their IDs
    so the engine never assigns them — they existed before the queue was created.
    Must be called inside a Flask app context.
    """
    queue = db.session.get(Queue, queue_id)
    if queue is None or not queue.filters_json:
        return

    rotation = queue.rotation
    org_user = _get_org_user(rotation.close_org_id)
    if not org_user:
        logger.warning("seed_queue %d: no active user for org %s", queue_id, rotation.close_org_id)
        return

    client = CloseClient(org_user)
    try:
        existing = client.search_leads(_normalize_filter(queue.filters_json))
    except CloseAPIError as e:
        logger.error("seed_queue %d: search failed: %s", queue_id, e)
        return

    queue.seeded_lead_ids = [lead["id"] for lead in existing]
    db.session.commit()
    logger.info("seed_queue %d: seeded %d lead(s)", queue_id, len(queue.seeded_lead_ids))


# ---------------------------------------------------------------------------
# Core poll logic
# ---------------------------------------------------------------------------

def poll_queue(queue_id: str) -> dict:
    """
    Poll Close for one queue and assign any new matching leads.
    Must be called inside a Flask app context.
    Returns a summary dict.
    """
    queue = db.session.get(Queue, queue_id)
    if queue is None:
        return {"error": "queue_not_found"}
    if queue.status != STATUS_ACTIVE:
        return {"skipped": True, "reason": "not_active"}
    if not queue.filters_json:
        return {"skipped": True, "reason": "no_filters"}

    rotation = queue.rotation
    org_user = _get_org_user(rotation.close_org_id)
    if not org_user:
        logger.error("Queue %d: no active user found for org %s", queue_id, rotation.close_org_id)
        return {"error": "no_org_user"}

    client = CloseClient(org_user)
    now = datetime.utcnow()

    # Use created_at as the floor on the very first poll so we don't pick up
    # leads that pre-date the queue being set up.
    after_dt = queue.last_checked_at or queue.created_at

    search_query = _inject_date_filter(_normalize_filter(queue.filters_json), after_dt)

    try:
        leads = client.search_leads(search_query)
    except CloseAPIError as e:
        logger.error("Queue %d: search failed: %s", queue_id, e)
        return {"error": str(e)}

    # Filter out leads that were present when the queue was created
    seeded = set(queue.seeded_lead_ids or [])
    leads = [l for l in leads if l.get("id") not in seeded]

    logger.info("Queue %d '%s': found %d lead(s) since %s (%d seeded/skipped)",
                queue_id, queue.name, len(leads), after_dt, len(seeded))

    assigned = skipped = errors = 0

    for lead in leads:
        lead_id = lead.get("id", "")
        lead_name = lead.get("display_name", "")

        # Honour overwrite_existing setting
        if not queue.overwrite_existing and queue.custom_field_id:
            existing = (lead.get("custom") or {}).get(queue.custom_field_id)
            if existing:
                logger.debug("Queue %d: skipping lead %s (field already set)", queue_id, lead_id)
                skipped += 1
                continue

        member = rotation.next_member()
        if not member:
            logger.warning("Queue %d: rotation has no active members — stopping", queue_id)
            break

        try:
            if queue.custom_field_id:
                client.assign_lead(lead_id, queue.custom_field_id, member.close_user_id)

            log = AssignmentLog(
                queue_id=queue.id,
                rotation_member_id=member.id,
                close_lead_id=lead_id,
                close_lead_name=lead_name,
                close_user_id=member.close_user_id,
                close_user_name=member.close_user_name,
            )
            db.session.add(log)
            rotation.advance()
            assigned += 1

            logger.info("Queue %d: assigned lead '%s' → %s",
                        queue_id, lead_name or lead_id, member.close_user_name)

        except CloseAPIError as e:
            logger.error("Queue %d: failed to assign lead %s: %s", queue_id, lead_id, e)
            errors += 1

    queue.last_checked_at = now
    db.session.commit()

    logger.info("Queue %d done — assigned: %d, skipped: %d, errors: %d",
                queue_id, assigned, skipped, errors)
    return {"assigned": assigned, "skipped": skipped, "errors": errors}


def poll_all_queues():
    """
    Entry point called by APScheduler every 5 minutes.
    Finds all active ongoing queues and polls each one.
    Must be called inside a Flask app context.
    """
    queues = (
        Queue.query
        .filter_by(queue_type=TYPE_ONGOING, status=STATUS_ACTIVE)
        .all()
    )

    if not queues:
        logger.debug("Scheduler: no active lead lists to poll.")
        return

    logger.info("Scheduler: polling %d active lead list(s)...", len(queues))
    for queue in queues:
        try:
            poll_queue(queue.id)
        except Exception:
            logger.exception("Scheduler: unexpected error polling queue %d", queue.id)
