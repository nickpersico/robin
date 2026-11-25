"""
Assignment engine — polls Close for new leads and runs each Lead List's actions.

Called by APScheduler every 5 minutes. For each active ongoing Lead List:
  1. Query Close's search API for leads that match the list's filter
     conditions and were created since the list's checkpoint (last_checked_at),
     minus a short overlap window, oldest first. Leads this list already
     acted on are skipped.
  2. For each matching lead, run the list's enabled Actions:
       * Assign — pick the next member of the list's Group (Rotation) and
         write that user's ID into the configured custom field. Advance the
         rotation pointer.
       * Workflow — trigger the configured Close Workflow on the lead's
         first contact.
  3. Log the event(s) in AssignmentLog (denormalized so the UI can render
     history without API calls).
  4. Advance last_checked_at to the poll's start time, unless some leads still
     need another pass (backlog over the per-poll cap, or failed actions); then
     hold it back so the next poll sees them again.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set, Tuple

from ..extensions import db
from ..models.lead_list import LeadList, TYPE_ONGOING, STATUS_ACTIVE
from ..models.assignment_log import AssignmentLog
from ..models.user import User
from .close_api import CloseClient, CloseAPIError

logger = logging.getLogger(__name__)

# Cap on how many leads a single poll will actually act on (assign / trigger a
# workflow for). Skipped leads — already-assigned ones caught by the guard —
# are cheap and don't count. When a poll hits this cap it leaves the list's
# checkpoint (last_checked_at) untouched so the next cycle re-scans and
# continues; the guard skips whatever was already done. This keeps one list's
# big backlog from monopolising a poll cycle or hammering Close all at once.
MAX_ACTIONS_PER_POLL = 500

# Each poll's date window reaches this far back before the checkpoint. It covers
# two gaps that a hard "created after the last poll" cutoff would fall into:
#   * a lead created just before a poll that only starts matching the filter a
#     few seconds later (e.g. created, then its status is set), and
#   * Close's search index lagging slightly behind writes.
# Leads in the overlap that this list already acted on are skipped via
# AssignmentLog, so the overlap never causes a double assignment.
POLL_OVERLAP = timedelta(minutes=10)

# How long a lead whose action failed (Close error, no active rotation members)
# may hold the checkpoint back so later polls retry it. Bounded so one
# permanently failing lead can't pin the window open forever.
MAX_RETRY_HOLDBACK = timedelta(hours=6)

# Close's search cursor fails past 10,000 results, so an unbounded search over
# a huge window (a giant import, or a long retry hold-back) would fail on every
# poll and stall the list. Instead a poll searches in oldest-first chunks of at
# most MAX_SEARCH_RESULTS, each starting at the previous chunk's last
# date_created, until it has a full poll's worth of leads to act on or has
# walked MAX_SEARCH_CHUNKS chunks.
MAX_SEARCH_RESULTS = 5000
MAX_SEARCH_CHUNKS = 10

# Process leads oldest-first so that when a poll stops at MAX_ACTIONS_PER_POLL,
# everything before the stopping point is done and the rest is still ahead.
SORT_OLDEST_FIRST = [{
    "direction": "asc",
    "field": {"object_type": "lead", "type": "regular_field", "field_name": "date_created"},
}]


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


def _inject_date_filter(filters_json: dict, on_or_after: Optional[datetime]) -> dict:
    """
    Return a new filter dict that ANDs in date_created >= on_or_after (naive UTC).

    Close's moment_range takes `on_or_after` / `before`, each a moment object
    such as {"type": "fixed_utc", "value": ...}. Close silently ignores keys it
    doesn't recognise, so a malformed condition (this used to send
    {"after": "<string>"}) quietly turns into "no date filter at all".
    """
    start = on_or_after or datetime(1970, 1, 1)
    date_condition = {
        "type": "field_condition",
        "field": {"type": "regular_field", "field_name": "date_created", "object_type": "lead"},
        "condition": {
            "type": "moment_range",
            "on_or_after": {
                "type": "fixed_utc",
                "value": start.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
            },
        },
    }

    base = copy.deepcopy(filters_json)
    if base.get("type") == "and":
        base.setdefault("queries", []).append(date_condition)
        return base
    return {"type": "and", "queries": [base, date_condition]}


def _get_org_client(close_org_id: str):
    """
    Return a CloseClient backed by the first active org member whose Close
    connection actually works, or None if the org has no usable connection.

    Robin syncs an org through one team member's Close OAuth connection. That
    connection can go stale — a member revokes Robin's access, or a rotated
    refresh token was lost (e.g. during a database outage). Rather than fail the
    whole org when the first member's connection is broken, we try each active
    member in creation order and use the first one that can authenticate. An org
    keeps syncing as long as *any* active member has a working connection.
    """
    users = (
        User.query
        .filter_by(close_org_id=close_org_id, status="active")
        .order_by(User.created_at)
        .all()
    )
    for user in users:
        client = CloseClient(user)
        try:
            client._ensure_fresh_token()  # no-op when the token is still valid
            return client
        except CloseAPIError as e:
            logger.warning(
                "Org %s: Close connection for %s is unusable (%s) — trying next member",
                close_org_id, user.email, e,
            )
            continue
    return None


def _sync_rotation_member_active_flags(rotation, active_close_user_ids: set) -> int:
    """
    Mirror RotationMember.is_active to Close's Team Management state so a
    member who was deactivated in Close is never assigned a lead. A member
    whose close_user_id is in `active_close_user_ids` is set active; anyone
    missing (deactivated or removed from the org entirely) is set inactive.

    Returns the number of members whose flag actually flipped, so callers
    can decide whether to log.
    """
    changed = 0
    for member in rotation.members:
        desired = member.close_user_id in active_close_user_ids
        if member.is_active != desired:
            member.is_active = desired
            changed += 1
    return changed


def _list_org_id(lead_list: LeadList) -> Optional[str]:
    """Resolve the org for a Lead List — prefer the denormalised column."""
    if lead_list.close_org_id:
        return lead_list.close_org_id
    if lead_list.rotation is not None:
        return lead_list.rotation.close_org_id
    return None


def _parse_close_datetime(value) -> Optional[datetime]:
    """Parse a Close ISO timestamp into a naive UTC datetime (None if unparseable)."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _poll_window_start(lead_list: LeadList) -> datetime:
    """Earliest date_created the next poll of this list will consider."""
    checkpoint = lead_list.last_checked_at or lead_list.created_at
    return checkpoint - POLL_OVERLAP


def _next_checkpoint(
    now: datetime,
    backlog_from: Optional[datetime],
    retry_from: Optional[datetime],
) -> datetime:
    """
    Where to put a list's checkpoint after a poll that started at `now`.

    Normally `now`. If leads still need another pass, hold it back so the next
    poll's window (which starts POLL_OVERLAP before the checkpoint) starts
    exactly at the earliest of them, not a further POLL_OVERLAP earlier, so
    a held window never re-walks more already-done leads than it must:
      * backlog (over the per-poll cap or the search cap): held back as far as
        needed, since that backlog must be drained;
      * failed leads: held back at most MAX_RETRY_HOLDBACK, so a lead that
        keeps failing eventually stops pinning the window.
    Never later than `now`. Already-actioned leads in a re-scanned window are
    skipped via AssignmentLog.
    """
    hold = backlog_from
    if retry_from is not None:
        retry_hold = max(retry_from, now - MAX_RETRY_HOLDBACK)
        hold = retry_hold if hold is None else min(hold, retry_hold)
    if hold is None:
        return now
    return min(now, hold + POLL_OVERLAP)


def _already_actioned_lead_ids(lead_list_id: str, lead_ids: Iterable[str]) -> Set[str]:
    """Which of these leads this list has already acted on (has an AssignmentLog row)."""
    ids = [i for i in lead_ids if i]
    done: Set[str] = set()
    chunk = 500
    for start in range(0, len(ids), chunk):
        rows = (
            db.session.query(AssignmentLog.close_lead_id)
            .filter(
                AssignmentLog.queue_id == lead_list_id,
                AssignmentLog.close_lead_id.in_(ids[start:start + chunk]),
            )
            .all()
        )
        done.update(r[0] for r in rows)
    return done


def find_poll_candidates(
    client, lead_list: LeadList
) -> Tuple[List[dict], datetime, Optional[datetime]]:
    """
    Run the search a poll of this list would run and return
    (candidate leads oldest-first, window start, resume_from).

    Candidates match the list's filter, were created inside the poll window,
    and are neither in the list's seeded snapshot nor already acted on by this
    list. resume_from is set when the search stopped before reaching the end
    of the window: it's the date_created where the next poll must pick up.
    Raises CloseAPIError if the search fails.
    """
    window_start = _poll_window_start(lead_list)
    base_filter = _normalize_filter(lead_list.filters_json)

    # Explicitly request the target custom field so it comes back in the result
    # (as a flat "custom.<id>" key) and the "field already set" guard can see it.
    search_fields = ["id", "display_name", "date_created", "custom"]
    if lead_list.custom_field_id:
        search_fields.append(f"custom.{lead_list.custom_field_id}")

    seeded = set(lead_list.seeded_lead_ids or [])
    candidates: List[dict] = []
    candidate_ids: Set[str] = set()
    resume_from: Optional[datetime] = None
    chunk_start = window_start

    for _ in range(MAX_SEARCH_CHUNKS):
        chunk = client.search_leads(
            _inject_date_filter(base_filter, chunk_start),
            fields=search_fields, sort=SORT_OLDEST_FIRST, max_results=MAX_SEARCH_RESULTS,
        )
        fresh = [l for l in chunk
                 if l.get("id") and l["id"] not in seeded and l["id"] not in candidate_ids]
        done = _already_actioned_lead_ids(lead_list.id, [l["id"] for l in fresh])
        for lead in fresh:
            if lead["id"] not in done:
                candidates.append(lead)
                candidate_ids.add(lead["id"])

        if len(chunk) < MAX_SEARCH_RESULTS:
            resume_from = None   # reached the end of the window
            break
        last_created = _parse_close_datetime(chunk[-1].get("date_created"))
        if last_created is None or last_created <= chunk_start:
            # A full chunk sharing one timestamp: we can't page past it by date.
            logger.warning("LeadList %s: %d+ leads share date_created %s — cannot page further",
                           lead_list.id, MAX_SEARCH_RESULTS, chunk_start)
            resume_from = None
            break
        # The next chunk starts at (not after) the last timestamp, so leads that
        # share it aren't skipped; candidate_ids drops the repeats.
        chunk_start = resume_from = last_created
        if len(candidates) >= MAX_ACTIONS_PER_POLL:
            break

    return candidates, window_start, resume_from


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_queue(lead_list_id: str):
    """
    (Re)start a list from "now", so the engine never acts on leads that
    existed before the list was created, resumed, or had its filter changed.

    Polls only look at leads created after the checkpoint (minus
    POLL_OVERLAP), so moving the checkpoint to now excludes everything older.
    The leads that already match inside the overlap window are snapshotted
    into seeded_lead_ids so the next poll's overlap doesn't pick them up.
    Must be called inside a Flask app context.
    """
    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None or not lead_list.filters_json:
        return

    now = datetime.utcnow()
    lead_list.last_checked_at = now
    db.session.commit()

    org_id = _list_org_id(lead_list)
    if not org_id:
        logger.warning("seed_queue %s: cannot resolve org (workflow-only list with no rotation?)",
                       lead_list_id)
        return

    client = _get_org_client(org_id)
    if client is None:
        logger.warning("seed_queue %s: no usable Close connection for org %s", lead_list_id, org_id)
        return

    try:
        recent = client.search_leads(
            _inject_date_filter(_normalize_filter(lead_list.filters_json), now - POLL_OVERLAP),
            fields=["id"],
        )
    except CloseAPIError as e:
        logger.error("seed_queue %s: search failed: %s", lead_list_id, e)
        return

    lead_list.seeded_lead_ids = [lead["id"] for lead in recent if lead.get("id")]
    db.session.commit()
    logger.info("seed_queue %s: checkpoint reset to %s, seeded %d recent lead(s)",
                lead_list_id, now, len(lead_list.seeded_lead_ids))


# ---------------------------------------------------------------------------
# Core poll logic
# ---------------------------------------------------------------------------

def _lead_custom_value(lead: dict, field_id: Optional[str]):
    """
    Read a lead's custom-field value from a Close search result, tolerant of
    both response shapes:
      - flat top-level key   {"custom.cf_x": value}   (how /data/search returns
        custom fields, and how we write them in assign_lead)
      - nested dict          {"custom": {"cf_x": value}}

    The original guard only checked the nested shape, but the search API returns
    the flat shape — so the "field already set" check never matched, and every
    poll re-assigned + re-logged the same lead. This reads both so the guard
    actually fires. Returns None if unset/absent.
    """
    if not field_id:
        return None
    flat = lead.get(f"custom.{field_id}")
    if flat is not None:
        return flat
    nested = lead.get("custom")
    if isinstance(nested, dict):
        return nested.get(field_id)
    return None


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
        existing = _lead_custom_value(lead, lead_list.custom_field_id)
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

    client = _get_org_client(org_id)
    if client is None:
        logger.error(
            "LeadList %s: no usable Close connection for org %s — every active "
            "member's token is invalid; the org must re-authenticate",
            lead_list_id, org_id,
        )
        return {"error": "no_usable_connection"}

    now = datetime.utcnow()

    # Sync rotation member is_active against Close's Team Management before we
    # decide who's next in the round-robin. Anyone deactivated in Close (left
    # the company, suspended, etc.) gets flipped to inactive and skipped by
    # next_member(). Anyone reactivated gets flipped back automatically on
    # the next tick. Only relevant when the Assign action is enabled —
    # workflow-only lists don't consult the rotation.
    if lead_list.assign_enabled and lead_list.rotation is not None:
        try:
            active_ids = {m["id"] for m in client.get_active_org_members()}
            changed = _sync_rotation_member_active_flags(lead_list.rotation, active_ids)
            if changed:
                logger.info(
                    "LeadList %s: synced is_active on %d rotation member(s) from Close",
                    lead_list_id, changed,
                )
        except CloseAPIError as e:
            # Non-fatal — proceed with the last-known is_active state.
            logger.warning(
                "LeadList %s: could not sync member status from Close: %s",
                lead_list_id, e,
            )

    try:
        leads, window_start, resume_from = find_poll_candidates(client, lead_list)
    except CloseAPIError as e:
        logger.error("LeadList %s: search failed: %s", lead_list_id, e)
        return {"error": str(e)}

    logger.info("LeadList %s '%s': found %d new lead(s) created since %s",
                lead_list_id, lead_list.name, len(leads), window_start)

    assigned = workflow_triggered = skipped = errors = 0
    actions = 0          # leads we actually acted on this cycle (assign/workflow)
    capped = False       # True if we stopped early at MAX_ACTIONS_PER_POLL
    sender_cache: dict = {}  # memoized run-as resolution per poll cycle

    # Earliest date_created among leads a later poll must see again:
    #   backlog_from: leads past MAX_ACTIONS_PER_POLL, or past the search's
    #                 MAX_SEARCH_RESULTS cap (resume_from)
    #   retry_from:   leads whose action failed (retried for MAX_RETRY_HOLDBACK)
    backlog_from: Optional[datetime] = resume_from
    retry_from: Optional[datetime] = None
    if resume_from is not None:
        logger.info("LeadList %s: search stopped before the end of the window — "
                    "continuing from %s next cycle", lead_list_id, resume_from)

    def _created(lead: dict) -> datetime:
        # Unparseable timestamps fall back to the window start, which keeps
        # the lead inside the next poll's window.
        return _parse_close_datetime(lead.get("date_created")) or window_start

    def _hold_for_retry(lead: dict):
        nonlocal retry_from
        created = _created(lead)
        retry_from = created if retry_from is None else min(retry_from, created)

    for index, lead in enumerate(leads):
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
                _hold_for_retry(lead)
                # Don't trigger the workflow if assignment errored unexpectedly.
                continue
            if assign_result.get("skipped_reason") == "no_active_members":
                logger.warning("LeadList %s: rotation has no active members — stopping",
                               lead_list_id)
                # Leads are oldest-first, so this one and everything after it
                # still need assigning once a member is active again.
                _hold_for_retry(lead)
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
                    _hold_for_retry(lead)
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
                        _hold_for_retry(lead)
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

        actions += 1
        if actions >= MAX_ACTIONS_PER_POLL:
            if index + 1 < len(leads):
                capped = True
                next_created = _created(leads[index + 1])
                backlog_from = next_created if backlog_from is None else min(backlog_from, next_created)
                logger.info(
                    "LeadList %s: hit the per-poll cap of %d — %d lead(s) left for "
                    "the next cycle", lead_list_id, MAX_ACTIONS_PER_POLL,
                    len(leads) - index - 1,
                )
            break

    lead_list.last_checked_at = _next_checkpoint(now, backlog_from, retry_from)
    db.session.commit()

    logger.info(
        "LeadList %s done — assigned: %d, workflow: %d, skipped: %d, errors: %d%s%s",
        lead_list_id, assigned, workflow_triggered, skipped, errors,
        " (capped, more next cycle)" if capped else "",
        " (failed leads will be retried)" if retry_from is not None else "",
    )
    return {
        "assigned": assigned,
        "workflow_triggered": workflow_triggered,
        "skipped": skipped,
        "errors": errors,
        "capped": capped,
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
