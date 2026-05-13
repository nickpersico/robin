from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models.assignment_log import AssignmentLog
from ..models.lead_list import LeadList
from ..models.rotation import Rotation, RotationMember
from ..services.close_api import CloseClient, CloseAPIError

rotations_bp = Blueprint("rotations", __name__)


def _get_rotation_or_404(rotation_id: str) -> Rotation:
    rotation = db.session.get(Rotation, rotation_id)
    if rotation is None or rotation.close_org_id != current_user.close_org_id:
        abort(404)
    return rotation


# ---------------------------------------------------------------------------
# List (Groups dashboard)
# ---------------------------------------------------------------------------


@rotations_bp.route("/")
@login_required
def list_rotations():
    rotations = (
        Rotation.query
        .filter_by(close_org_id=current_user.close_org_id)
        .order_by(Rotation.created_at.desc())
        .all()
    )
    for r in rotations:
        r.lead_list_count = r.lead_lists.count()
        last_log = (
            AssignmentLog.query
            .join(LeadList, AssignmentLog.queue_id == LeadList.id)
            .filter(LeadList.rotation_id == r.id)
            .order_by(AssignmentLog.assigned_at.desc())
            .first()
        )
        r.last_assignment_at = last_log.assigned_at if last_log else None
    return render_template("rotations/list.html", rotations=rotations)


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
    lead_lists = rotation.lead_lists.order_by("created_at").all()
    return render_template(
        "rotations/detail.html",
        rotation=rotation,
        member_counts=member_counts,
        lead_lists=lead_lists,
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
# Inline create (JSON API used by the Lead List create modal)
# ---------------------------------------------------------------------------


@rotations_bp.route("/api/create", methods=["POST"])
@login_required
def api_create_rotation():
    """
    Create a Group from an AJAX request. Returns JSON so the caller (e.g. the
    "Create a group" modal on the Lead List page) can splice the new group
    into a dropdown without a full page reload.

    Body: form-encoded (so it matches existing fetch() / FormData usage):
      name=...&description=...&member_ids=u1&member_ids=u2
    """
    if current_user.is_pending:
        return jsonify({"ok": False, "error": "Your account is pending approval."}), 403

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    member_ids = request.form.getlist("member_ids")

    if not name:
        return jsonify({"ok": False, "error": "Group name is required."}), 400
    if not member_ids:
        return jsonify({"ok": False, "error": "Select at least one member."}), 400

    try:
        client = CloseClient(current_user)
        org_users = client.get_active_org_members()
    except CloseAPIError as e:
        return jsonify({"ok": False, "error": f"Could not load org users: {e}"}), 502

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
    return jsonify({"ok": True, "group": {"id": rotation.id, "name": rotation.name}})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_name(user_info: dict) -> str:
    parts = [user_info.get("first_name", ""), user_info.get("last_name", "")]
    name = " ".join(p for p in parts if p).strip()
    return name or user_info.get("email", user_info.get("id", "Unknown"))
