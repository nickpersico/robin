from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models.rotation import Rotation, RotationMember
from ..services.close_api import CloseClient, CloseAPIError

rotations_bp = Blueprint("rotations", __name__)


def _get_rotation_or_404(rotation_id: str) -> Rotation:
    rotation = db.session.get(Rotation, rotation_id)
    if rotation is None or rotation.close_org_id != current_user.close_org_id:
        abort(404)
    return rotation


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@rotations_bp.route("/")
@login_required
def list_rotations():
    # Consolidated into the main index — redirect to avoid stale bookmarks.
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@rotations_bp.route("/new", methods=["GET", "POST"])
@login_required
def create_rotation():
    org_users = []
    error = None
    try:
        client = CloseClient(current_user)
        org_users = client.get_active_org_members()
    except CloseAPIError as e:
        error = str(e)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        member_ids = request.form.getlist("member_ids")

        if not name:
            flash("Group name is required.", "error")
        elif not member_ids:
            flash("Select at least one member.", "error")
        else:
            rotation = Rotation(
                name=name,
                description=description or None,
                owner_id=current_user.id,
                close_org_id=current_user.close_org_id,
            )
            db.session.add(rotation)
            db.session.flush()

            user_lookup = {u["id"]: u for u in org_users}
            for position, close_user_id in enumerate(member_ids):
                user_info = user_lookup.get(close_user_id, {})
                member = RotationMember(
                    rotation_id=rotation.id,
                    close_user_id=close_user_id,
                    close_user_email=user_info.get("email", ""),
                    close_user_name=_display_name(user_info),
                    position=position,
                )
                db.session.add(member)

            db.session.commit()
            return redirect(url_for("rotations.view_rotation", rotation_id=rotation.id))

    existing_ids = request.form.getlist("member_ids") if request.method == "POST" else []
    return render_template(
        "rotations/create.html",
        org_users=org_users,
        existing_ids=existing_ids,
        error=error,
    )


# ---------------------------------------------------------------------------
# View / Detail
# ---------------------------------------------------------------------------


@rotations_bp.route("/<rotation_id>")
@login_required
def view_rotation(rotation_id: int):
    rotation = _get_rotation_or_404(rotation_id)
    member_counts = {m.id: m.assignment_count for m in rotation.members}
    queues = rotation.queues.order_by("created_at").all()
    return render_template(
        "rotations/detail.html",
        rotation=rotation,
        member_counts=member_counts,
        queues=queues,
    )


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@rotations_bp.route("/<rotation_id>/edit", methods=["GET", "POST"])
@login_required
def edit_rotation(rotation_id: int):
    rotation = _get_rotation_or_404(rotation_id)

    org_users = []
    error = None
    try:
        client = CloseClient(current_user)
        org_users = client.get_active_org_members()
    except CloseAPIError as e:
        error = str(e)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        member_ids = request.form.getlist("member_ids")

        if not name:
            flash("Group name is required.", "error")
        elif not member_ids:
            flash("Select at least one member.", "error")
        else:
            rotation.name = name
            rotation.description = description or None

            for m in rotation.members:
                db.session.delete(m)
            db.session.flush()

            user_lookup = {u["id"]: u for u in org_users}
            for position, close_user_id in enumerate(member_ids):
                user_info = user_lookup.get(close_user_id, {})
                member = RotationMember(
                    rotation_id=rotation.id,
                    close_user_id=close_user_id,
                    close_user_email=user_info.get("email", ""),
                    close_user_name=_display_name(user_info),
                    position=position,
                )
                db.session.add(member)

            if rotation.current_index >= len(member_ids):
                rotation.current_index = 0

            db.session.commit()
            return redirect(url_for("rotations.view_rotation", rotation_id=rotation.id))

    existing_ids = request.form.getlist("member_ids") if request.method == "POST" else [
        m.close_user_id for m in rotation.members
    ]
    return render_template(
        "rotations/edit.html",
        rotation=rotation,
        org_users=org_users,
        existing_ids=existing_ids,
        error=error,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@rotations_bp.route("/<rotation_id>/delete", methods=["POST"])
@login_required
def delete_rotation(rotation_id: int):
    rotation = _get_rotation_or_404(rotation_id)
    name = rotation.name
    db.session.delete(rotation)
    db.session.commit()
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_name(user_info: dict) -> str:
    parts = [user_info.get("first_name", ""), user_info.get("last_name", "")]
    name = " ".join(p for p in parts if p).strip()
    return name or user_info.get("email", user_info.get("id", "Unknown"))
