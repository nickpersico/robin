from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from ..extensions import db
from ..models.user import User, ROLE_ADMIN, ROLE_MEMBER, STATUS_ACTIVE, STATUS_PENDING, STATUS_SUSPENDED

admin_bp = Blueprint("admin", __name__)


def admin_required(f):
    """Decorator that restricts a route to admin users only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


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
