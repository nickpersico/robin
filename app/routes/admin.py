from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from ..extensions import db
from ..models.user import User, ROLE_ADMIN, ROLE_MEMBER, STATUS_ACTIVE, STATUS_PENDING, STATUS_SUSPENDED

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

@admin_bp.route("/admin")
@login_required
@superadmin_required
def superadmin_dashboard():
    from ..models.organization import Organization
    from ..models.rotation import Rotation
    from ..models.queue import Queue

    organizations = Organization.query.order_by(Organization.name).all()
    users = User.query.order_by(User.created_at.desc()).all()
    rotations = Rotation.query.order_by(Rotation.close_org_id, Rotation.name).all()
    queues = Queue.query.order_by(Queue.created_at.desc()).all()

    org_map = {o.close_org_id: (o.name or o.close_org_id) for o in organizations}

    return render_template(
        "admin/dashboard.html",
        organizations=organizations,
        users=users,
        rotations=rotations,
        queues=queues,
        org_map=org_map,
    )


@admin_bp.route("/admin/organizations/<org_id>")
@login_required
@superadmin_required
def superadmin_org(org_id):
    from ..models.organization import Organization

    org = db.session.get(Organization, org_id)
    if org is None:
        abort(404)
    org_users = org.users.order_by(User.created_at.asc()).all()
    return render_template("admin/org_detail.html", org=org, org_users=org_users)


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
