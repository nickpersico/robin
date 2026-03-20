import json

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
from ..models.rotation import Rotation
from ..models.queue import (
    Queue,
    TYPE_ONGOING,
    STATUS_ACTIVE,
    STATUS_PAUSED,
)
from ..models.assignment_log import AssignmentLog
from ..services.close_api import CloseClient, CloseAPIError

queues_bp = Blueprint("queues", __name__)


def _get_rotation_or_404(rotation_id: str) -> Rotation:
    rotation = db.session.get(Rotation, rotation_id)
    if rotation is None or rotation.close_org_id != current_user.close_org_id:
        abort(404)
    return rotation


def _get_queue_or_404(rotation_id: str, queue_id: str) -> Queue:
    rotation = _get_rotation_or_404(rotation_id)
    queue = db.session.get(Queue, queue_id)
    if queue is None or queue.rotation_id != rotation.id:
        abort(404)
    return queue


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@queues_bp.route(
    "/groups/<rotation_id>/lead-lists/new", methods=["GET", "POST"]
)
@login_required
def create_queue(rotation_id: int):
    rotation = _get_rotation_or_404(rotation_id)

    custom_fields = []
    error = None
    try:
        client = CloseClient(current_user)
        custom_fields = client.get_user_custom_fields()
    except CloseAPIError as e:
        error = str(e)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        filters_raw = request.form.get("filters_json", "").strip()
        custom_field_id = request.form.get("custom_field_id", "").strip() or None
        overwrite_existing = request.form.get("overwrite_existing") == "1"

        filters_parsed = None
        if not filters_raw:
            flash("A lead filter is required.", "error")
            return render_template(
                "queues/create.html",
                rotation=rotation,
                custom_fields=custom_fields,
                error=error,
            )
        try:
            filters_parsed = json.loads(filters_raw)
        except json.JSONDecodeError:
            flash("Lead filter is not valid JSON.", "error")
            return render_template(
                "queues/create.html",
                rotation=rotation,
                custom_fields=custom_fields,
                error=error,
            )

        if not name:
            flash("Lead list name is required.", "error")
        else:
            field_lookup = {f["id"]: f["name"] for f in custom_fields}
            queue = Queue(
                rotation_id=rotation.id,
                name=name,
                queue_type=TYPE_ONGOING,
                filters_json=filters_parsed,
                custom_field_id=custom_field_id,
                custom_field_label=field_lookup.get(custom_field_id) if custom_field_id else None,
                overwrite_existing=overwrite_existing,
                status=STATUS_ACTIVE,
            )
            db.session.add(queue)
            db.session.commit()

            # Snapshot existing matching leads so the engine never assigns them
            from ..services.assignment_engine import seed_queue
            seed_queue(queue.id)

            return redirect(
                url_for(
                    "queues.view_queue",
                    rotation_id=rotation.id,
                    queue_id=queue.id,
                )
            )

    return render_template(
        "queues/create.html",
        rotation=rotation,
        custom_fields=custom_fields,
        error=error,
    )


# ---------------------------------------------------------------------------
# View / Detail
# ---------------------------------------------------------------------------


@queues_bp.route(
    "/groups/<rotation_id>/lead-lists/<queue_id>"
)
@login_required
def view_queue(rotation_id: int, queue_id: int):
    queue = _get_queue_or_404(rotation_id, queue_id)

    page = request.args.get("page", 1, type=int)
    logs = (
        AssignmentLog.query.filter_by(queue_id=queue.id)
        .order_by(AssignmentLog.assigned_at.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    return render_template(
        "queues/detail.html",
        rotation=queue.rotation,
        queue=queue,
        logs=logs,
    )


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@queues_bp.route(
    "/groups/<rotation_id>/lead-lists/<queue_id>/edit",
    methods=["GET", "POST"],
)
@login_required
def edit_queue(rotation_id: int, queue_id: int):
    queue = _get_queue_or_404(rotation_id, queue_id)

    custom_fields = []
    error = None
    try:
        client = CloseClient(current_user)
        custom_fields = client.get_user_custom_fields()
    except CloseAPIError as e:
        error = str(e)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        filters_raw = request.form.get("filters_json", "").strip()
        custom_field_id = request.form.get("custom_field_id", "").strip() or None
        overwrite_existing = request.form.get("overwrite_existing") == "1"

        filters_parsed = None
        if not filters_raw:
            flash("A lead filter is required.", "error")
            return render_template(
                "queues/edit.html",
                rotation=queue.rotation,
                queue=queue,
                custom_fields=custom_fields,
                error=error,
            )
        try:
            filters_parsed = json.loads(filters_raw)
        except json.JSONDecodeError:
            flash("Lead filter is not valid JSON.", "error")
            return render_template(
                "queues/edit.html",
                rotation=queue.rotation,
                queue=queue,
                custom_fields=custom_fields,
                error=error,
            )

        if not name:
            flash("Lead list name is required.", "error")
        else:
            import json as _json
            filter_changed = _json.dumps(filters_parsed, sort_keys=True) != _json.dumps(
                queue.filters_json, sort_keys=True
            )
            field_lookup = {f["id"]: f["name"] for f in custom_fields}
            queue.name = name
            queue.filters_json = filters_parsed
            queue.custom_field_id = custom_field_id
            queue.custom_field_label = field_lookup.get(custom_field_id) if custom_field_id else None
            queue.overwrite_existing = overwrite_existing
            db.session.commit()

            if filter_changed:
                from ..services.assignment_engine import seed_queue
                seed_queue(queue.id)

            return redirect(
                url_for(
                    "queues.view_queue",
                    rotation_id=queue.rotation_id,
                    queue_id=queue.id,
                )
            )

    return render_template(
        "queues/edit.html",
        rotation=queue.rotation,
        queue=queue,
        custom_fields=custom_fields,
        error=error,
    )


# ---------------------------------------------------------------------------
# Toggle pause / resume
# ---------------------------------------------------------------------------


@queues_bp.route(
    "/groups/<rotation_id>/lead-lists/<queue_id>/toggle",
    methods=["POST"],
)
@login_required
def toggle_queue(rotation_id: int, queue_id: int):
    queue = _get_queue_or_404(rotation_id, queue_id)
    if queue.status == STATUS_PAUSED:
        queue.status = STATUS_ACTIVE
        msg = "resumed"
        resuming = True
    elif queue.status == STATUS_ACTIVE:
        queue.status = STATUS_PAUSED
        msg = "paused"
        resuming = False
    else:
        flash("This lead list cannot be toggled in its current state.", "error")
        return redirect(
            url_for(
                "queues.view_queue",
                rotation_id=rotation_id,
                queue_id=queue_id,
            )
        )
    db.session.commit()

    if resuming:
        from ..services.assignment_engine import seed_queue
        seed_queue(queue.id)

    return redirect(
        url_for(
            "queues.view_queue",
            rotation_id=rotation_id,
            queue_id=queue_id,
        )
    )


# ---------------------------------------------------------------------------
# Manual check ("Check now")
# ---------------------------------------------------------------------------


@queues_bp.route(
    "/groups/<rotation_id>/lead-lists/<queue_id>/check",
    methods=["POST"],
)
@login_required
def check_queue(rotation_id: int, queue_id: int):
    """
    Trigger an immediate poll for a single queue.
    Returns JSON so the frontend can show a toast with the result.
    """
    queue = _get_queue_or_404(rotation_id, queue_id)
    if queue.status != STATUS_ACTIVE:
        return jsonify({"error": "Lead list is not active."}), 400

    from ..services.assignment_engine import poll_queue

    try:
        result = poll_queue(queue.id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in result:
        return jsonify(result), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@queues_bp.route(
    "/groups/<rotation_id>/lead-lists/<queue_id>/delete",
    methods=["POST"],
)
@login_required
def delete_queue(rotation_id: int, queue_id: int):
    queue = _get_queue_or_404(rotation_id, queue_id)
    name = queue.name
    rotation_id = queue.rotation_id
    db.session.delete(queue)
    db.session.commit()
    return redirect(url_for("rotations.view_rotation", rotation_id=rotation_id))
