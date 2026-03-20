"""
Activity Log — global view of all lead assignments across groups and lead lists.

Two routes:
  GET /activity      — HTML page shell (filters populated, table empty)
  GET /activity/api  — JSON endpoint for infinite scroll
"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required, current_user

from ..extensions import db
from ..models.assignment_log import AssignmentLog
from ..models.queue import Queue
from ..models.rotation import Rotation

activity_bp = Blueprint("activity", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_query():
    """AssignmentLog query scoped to the current user's org."""
    return (
        AssignmentLog.query
        .join(Queue, AssignmentLog.queue_id == Queue.id)
        .join(Rotation, Queue.rotation_id == Rotation.id)
        .filter(Rotation.close_org_id == current_user.close_org_id)
    )


def _apply_filters(query):
    """Apply request-param filters and return (filtered_query, sort_order)."""

    sort = request.args.get("sort", "desc")
    timeframe = request.args.get("timeframe", "all")

    # ── Timeframe ──────────────────────────────────────────────────────────
    now = datetime.utcnow()
    if timeframe == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(AssignmentLog.assigned_at >= start)
    elif timeframe == "this_week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        query = query.filter(AssignmentLog.assigned_at >= start)
    elif timeframe == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(AssignmentLog.assigned_at >= start)
    elif timeframe == "custom":
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        if date_from:
            query = query.filter(
                AssignmentLog.assigned_at >= datetime.fromisoformat(date_from)
            )
        if date_to:
            # Include the full end day
            end = datetime.fromisoformat(date_to) + timedelta(days=1)
            query = query.filter(AssignmentLog.assigned_at < end)

    # ── Entity filters (comma-separated IDs) ───────────────────────────────
    group_ids = request.args.get("group_ids", "").strip()
    if group_ids:
        query = query.filter(Rotation.id.in_(group_ids.split(",")))

    lead_list_ids = request.args.get("lead_list_ids", "").strip()
    if lead_list_ids:
        query = query.filter(Queue.id.in_(lead_list_ids.split(",")))

    user_ids = request.args.get("user_ids", "").strip()
    if user_ids:
        query = query.filter(AssignmentLog.close_user_id.in_(user_ids.split(",")))

    search = request.args.get("search", "").strip()
    if search:
        query = query.filter(
            db.or_(
                AssignmentLog.close_lead_name.ilike(f"%{search}%"),
                AssignmentLog.close_lead_id == search,
            )
        )

    return query, sort


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@activity_bp.route("/activity")
@login_required
def index():
    org_id = current_user.close_org_id

    groups = (
        Rotation.query
        .filter_by(close_org_id=org_id)
        .order_by(Rotation.name)
        .all()
    )

    lead_lists = (
        Queue.query
        .join(Rotation)
        .filter(Rotation.close_org_id == org_id)
        .order_by(Queue.name)
        .all()
    )

    users = (
        db.session.query(
            AssignmentLog.close_user_id,
            AssignmentLog.close_user_name,
        )
        .join(Queue, AssignmentLog.queue_id == Queue.id)
        .join(Rotation, Queue.rotation_id == Rotation.id)
        .filter(Rotation.close_org_id == org_id)
        .distinct()
        .order_by(AssignmentLog.close_user_name)
        .all()
    )

    return render_template(
        "activity/index.html",
        groups=groups,
        lead_lists=lead_lists,
        users=users,
    )


# ---------------------------------------------------------------------------
# JSON API (infinite scroll)
# ---------------------------------------------------------------------------

@activity_bp.route("/activity/api")
@login_required
def api():
    query = _base_query()
    query, sort = _apply_filters(query)

    # Total count (before pagination)
    total = query.count()

    # Offset-based pagination — simple and reliable even when many records share
    # the same timestamp (common during bulk queue checks).
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 25, type=int)
    limit = min(limit, 100)  # cap

    order = (
        AssignmentLog.assigned_at.asc()
        if sort == "asc"
        else AssignmentLog.assigned_at.desc()
    )

    rows = query.order_by(order).offset(offset).limit(limit).all()
    has_more = (offset + len(rows)) < total

    items = []
    for log in rows:
        items.append({
            "id": log.id,
            "assigned_at": log.assigned_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "close_lead_id": log.close_lead_id,
            "close_lead_name": log.close_lead_name or log.close_lead_id,
            "close_user_name": log.close_user_name or log.close_user_id,
            "queue_name": log.queue.name,
            "queue_id": log.queue.id,
            "rotation_name": log.queue.rotation.name,
            "rotation_id": log.queue.rotation.id,
        })

    return jsonify({
        "items": items,
        "total": total,
        "has_more": has_more,
        "next_offset": offset + len(rows) if has_more else None,
    })
