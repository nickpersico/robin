import json
import logging
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from ..extensions import db
from ..models.user import User, ROLE_ADMIN, ROLE_MEMBER, STATUS_ACTIVE, STATUS_PENDING, STATUS_SUSPENDED

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)

# Emails allowed to access the system-level super-admin dashboard.
SUPERADMIN_EMAILS = {"nick@close.com"}


def admin_required(f):
    """Decorator that restricts a route to org admin users only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def superadmin_required(f):
    """Decorator that restricts a route to super-admin accounts only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.email not in SUPERADMIN_EMAILS:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Super-admin dashboard
# ---------------------------------------------------------------------------

def _org_name_map():
    """close_org_id -> display name (name or the id as fallback)."""
    from ..models.organization import Organization
    return {o.close_org_id: (o.name or o.close_org_id) for o in Organization.query.all()}


@admin_bp.route("/system")
@login_required
@superadmin_required
def superadmin_dashboard():
    """Scannable overview: headline stats, system health, recent activity."""
    from datetime import datetime, timedelta
    from sqlalchemy import func, or_, text
    from ..models.organization import Organization
    from ..models.lead_list import LeadList, STATUS_ACTIVE
    from ..models.assignment_log import AssignmentLog
    from ..models.error_log import ErrorLog

    now = datetime.utcnow()

    def assigned_since(delta):
        q = AssignmentLog.query
        if delta is not None:
            q = q.filter(AssignmentLog.assigned_at >= now - delta)
        return q.count()

    stats = {
        "assigned_24h": assigned_since(timedelta(hours=24)),
        "assigned_7d": assigned_since(timedelta(days=7)),
        "assigned_30d": assigned_since(timedelta(days=30)),
        "assigned_all": assigned_since(None),
        "active_lists": LeadList.query.filter_by(status=STATUS_ACTIVE).count(),
        "active_users": User.query.filter_by(status=STATUS_ACTIVE).count(),
        "organizations": Organization.query.count(),
    }

    # Health — cheap signals only (no live Close API calls on page load).
    try:
        db_writable = db.session.execute(text("SHOW transaction_read_only")).scalar() == "off"
    except Exception:
        db_writable = False
    stale_cutoff = now - timedelta(minutes=15)
    health = {
        "db_writable": db_writable,
        "last_assignment_at": db.session.query(func.max(AssignmentLog.assigned_at)).scalar(),
        "stale_lists": LeadList.query.filter(
            LeadList.status == STATUS_ACTIVE,
            or_(LeadList.last_checked_at.is_(None), LeadList.last_checked_at < stale_cutoff),
        ).count(),
        "errors_24h": ErrorLog.query.filter(ErrorLog.occurred_at >= now - timedelta(hours=24)).count(),
    }

    recent = (
        AssignmentLog.query
        .order_by(AssignmentLog.assigned_at.desc())
        .limit(15)
        .all()
    )

    return render_template(
        "admin/dashboard.html",
        stats=stats, health=health, recent=recent, org_map=_org_name_map(), now=now,
    )


@admin_bp.route("/system/organizations")
@login_required
@superadmin_required
def superadmin_organizations():
    from sqlalchemy import func
    from ..models.organization import Organization
    from ..models.lead_list import LeadList

    q = (request.args.get("q") or "").strip()
    query = Organization.query
    if q:
        query = query.filter(Organization.name.ilike(f"%{q}%"))
    organizations = query.order_by(Organization.name).all()
    list_counts = dict(
        db.session.query(LeadList.close_org_id, func.count(LeadList.id))
        .group_by(LeadList.close_org_id).all()
    )
    return render_template(
        "admin/system_organizations.html",
        organizations=organizations, list_counts=list_counts, q=q,
    )


@admin_bp.route("/system/users")
@login_required
@superadmin_required
def superadmin_users():
    from ..models.organization import Organization

    status = request.args.get("status", "active")
    role = request.args.get("role") or ""
    org = request.args.get("org") or ""
    query = User.query
    if status and status != "all":
        query = query.filter(User.status == status)
    if role:
        query = query.filter(User.role == role)
    if org:
        query = query.filter(User.close_org_id == org)
    page = request.args.get("page", 1, type=int)
    users = query.order_by(User.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template(
        "admin/system_users.html",
        users=users, org_map=_org_name_map(),
        all_orgs=Organization.query.order_by(Organization.name).all(),
        status=status, role=role, org=org,
    )


@admin_bp.route("/system/groups")
@login_required
@superadmin_required
def superadmin_groups():
    from ..models.rotation import Rotation
    from ..models.organization import Organization

    org = request.args.get("org") or ""
    query = Rotation.query
    if org:
        query = query.filter(Rotation.close_org_id == org)
    page = request.args.get("page", 1, type=int)
    groups = query.order_by(Rotation.close_org_id, Rotation.name).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template(
        "admin/system_groups.html",
        groups=groups, org_map=_org_name_map(),
        all_orgs=Organization.query.order_by(Organization.name).all(),
        org=org,
    )


@admin_bp.route("/system/lead-lists")
@login_required
@superadmin_required
def superadmin_lead_lists():
    from ..models.lead_list import LeadList
    from ..models.organization import Organization

    status = request.args.get("status", "active")
    org = request.args.get("org") or ""
    query = LeadList.query
    if status and status != "all":
        query = query.filter(LeadList.status == status)
    if org:
        query = query.filter(LeadList.close_org_id == org)
    page = request.args.get("page", 1, type=int)
    lists = query.order_by(LeadList.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template(
        "admin/system_lead_lists.html",
        lists=lists, org_map=_org_name_map(),
        all_orgs=Organization.query.order_by(Organization.name).all(),
        status=status, org=org,
    )


@admin_bp.route("/system/errors")
@login_required
@superadmin_required
def superadmin_errors():
    """Recent user-facing errors (500s + signed-in 404s) for support triage."""
    from datetime import datetime, timedelta
    from ..models.error_log import ErrorLog

    # Optional filters: ?status=500|404, ?org=<close_org_id>, ?days=<int>
    status = request.args.get("status", type=int)
    org = request.args.get("org")
    days = request.args.get("days", default=7, type=int)

    q = ErrorLog.query
    if status:
        q = q.filter(ErrorLog.status_code == status)
    if org:
        q = q.filter(ErrorLog.close_org_id == org)
    if days and days > 0:
        q = q.filter(ErrorLog.occurred_at >= datetime.utcnow() - timedelta(days=days))

    # Cap the page to keep the view fast; newest first.
    errors = q.order_by(ErrorLog.occurred_at.desc()).limit(250).all()

    from ..models.organization import Organization
    org_map = {o.close_org_id: (o.name or o.close_org_id) for o in Organization.query.all()}

    return render_template(
        "admin/errors.html",
        errors=errors,
        org_map=org_map,
        status=status,
        org=org,
        days=days,
    )


@admin_bp.route("/system/organizations/<org_id>")
@login_required
@superadmin_required
def superadmin_org(org_id):
    from ..models.organization import Organization

    org = db.session.get(Organization, org_id)
    if org is None:
        abort(404)
    org_users = org.users.order_by(User.created_at.asc()).all()
    return render_template("admin/org_detail.html", org=org, org_users=org_users)


@admin_bp.route("/system/lead-lists/<lead_list_id>")
@login_required
@superadmin_required
def superadmin_lead_list(lead_list_id):
    """
    Support-triage view showing everything Robin knows about a Lead List
    without going through any of the customer's org scoping. Meant for
    answering "why isn't this list assigning?" without SSH access.
    """
    from ..models.lead_list import LeadList
    from ..models.assignment_log import AssignmentLog
    from ..models.organization import Organization

    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None:
        abort(404)

    org = None
    if lead_list.close_org_id:
        org = Organization.query.filter_by(close_org_id=lead_list.close_org_id).first()

    page = request.args.get("page", 1, type=int)
    logs = (
        AssignmentLog.query
        .filter_by(queue_id=lead_list.id)
        .order_by(AssignmentLog.assigned_at.desc())
        .paginate(page=page, per_page=50, error_out=False)
    )

    seeded_ids = lead_list.seeded_lead_ids or []

    return render_template(
        "admin/lead_list_detail.html",
        lead_list=lead_list,
        org=org,
        logs=logs,
        seeded_count=len(seeded_ids),
        seeded_preview=seeded_ids[:10],
    )


@admin_bp.route("/system/lead-lists/<lead_list_id>/toggle", methods=["POST"])
@login_required
@superadmin_required
def superadmin_toggle_lead_list(lead_list_id):
    """
    Flip a customer's Lead List between active and paused as a support
    action. Matches the customer-facing toggle: resuming triggers a
    seed_queue so we don't retroactively assign leads accumulated during
    the paused window.
    """
    from ..models.lead_list import LeadList, STATUS_ACTIVE, STATUS_PAUSED
    from ..services.assignment_engine import seed_queue

    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None:
        abort(404)

    if lead_list.status == STATUS_PAUSED:
        lead_list.status = STATUS_ACTIVE
        resuming = True
    elif lead_list.status == STATUS_ACTIVE:
        lead_list.status = STATUS_PAUSED
        resuming = False
    else:
        flash(f"Lead list is in status {lead_list.status!r} — cannot toggle from /system.", "error")
        return redirect(url_for("admin.superadmin_lead_list", lead_list_id=lead_list_id))

    db.session.commit()

    logger.warning(
        "SUPER-ADMIN %s toggled lead list %s (%s) → status=%s",
        current_user.email, lead_list.id, lead_list.close_org_id, lead_list.status,
    )

    if resuming:
        seed_queue(lead_list.id)
        flash("Resumed and re-seeded — Robin will start polling this list on the next tick.", "success")
    else:
        flash("Paused. Robin will stop polling this list.", "success")

    return redirect(url_for("admin.superadmin_lead_list", lead_list_id=lead_list_id))


@admin_bp.route("/system/lead-lists/<lead_list_id>/update-filter", methods=["POST"])
@login_required
@superadmin_required
def superadmin_update_lead_list_filter(lead_list_id):
    """
    Replace a Lead List's filter JSON as a support action. Mirrors the
    customer-facing edit path: JSON is validated, and a successful change
    triggers a re-seed so we don't sweep in a backlog of leads that
    happened to match the new filter's history.
    """
    from ..models.lead_list import LeadList
    from ..services.assignment_engine import seed_queue

    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None:
        abort(404)

    raw = (request.form.get("filters_json") or "").strip()
    if not raw:
        flash("Filter JSON cannot be empty.", "error")
        return redirect(url_for("admin.superadmin_lead_list", lead_list_id=lead_list_id))

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        flash(f"Invalid JSON: {exc}", "error")
        return redirect(url_for("admin.superadmin_lead_list", lead_list_id=lead_list_id))

    changed = json.dumps(parsed, sort_keys=True) != json.dumps(lead_list.filters_json, sort_keys=True)
    if not changed:
        flash("Filter is unchanged — nothing to save.", "info")
        return redirect(url_for("admin.superadmin_lead_list", lead_list_id=lead_list_id))

    lead_list.filters_json = parsed
    db.session.commit()

    logger.warning(
        "SUPER-ADMIN %s replaced filter_json on lead list %s (%s)",
        current_user.email, lead_list.id, lead_list.close_org_id,
    )

    # Re-seed so the new filter doesn't retroactively fire on old matches.
    seed_queue(lead_list.id)
    flash("Filter updated and list re-seeded from current Close state.", "success")

    return redirect(url_for("admin.superadmin_lead_list", lead_list_id=lead_list_id))


@admin_bp.route("/admin/users")
@login_required
@admin_required
def users():
    org_users = (
        User.query
        .filter_by(close_org_id=current_user.close_org_id)
        .order_by(User.created_at.asc())
        .all()
    )
    return render_template("admin/users.html", org_users=org_users)


@admin_bp.route("/admin/users/<user_id>/approve", methods=["POST"])
@login_required
@admin_required
def approve_user(user_id):
    user = _get_org_user_or_404(user_id)
    if user.is_pending:
        user.status = STATUS_ACTIVE
        db.session.commit()
        flash(f"{user.full_name} has been approved.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<user_id>/suspend", methods=["POST"])
@login_required
@admin_required
def suspend_user(user_id):
    user = _get_org_user_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot suspend yourself.", "error")
        return redirect(url_for("admin.users"))
    user.status = STATUS_SUSPENDED
    db.session.commit()
    flash(f"{user.full_name}'s access has been suspended.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<user_id>/reactivate", methods=["POST"])
@login_required
@admin_required
def reactivate_user(user_id):
    user = _get_org_user_or_404(user_id)
    if user.is_suspended:
        user.status = STATUS_ACTIVE
        db.session.commit()
        flash(f"{user.full_name}'s access has been restored.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/<user_id>/toggle-role", methods=["POST"])
@login_required
@admin_required
def toggle_role(user_id):
    user = _get_org_user_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot change your own role.", "error")
        return redirect(url_for("admin.users"))
    user.role = ROLE_MEMBER if user.is_admin else ROLE_ADMIN
    db.session.commit()
    label = "Admin" if user.is_admin else "Member"
    flash(f"{user.full_name} is now a {label}.", "success")
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org_user_or_404(user_id: str) -> User:
    user = db.session.get(User, user_id)
    if user is None or user.close_org_id != current_user.close_org_id:
        abort(404)
    return user
