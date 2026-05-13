"""
Top-level routes for Lead Lists.

A Lead List is the operational unit that watches Close for matching leads and
runs configured Actions on each one. Actions are inline columns on the model
(``assign_enabled``/``workflow_enabled``) — at least one must be enabled.
"""

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
from ..models.lead_list import (
    LeadList,
    TYPE_ONGOING,
    STATUS_ACTIVE,
    STATUS_PAUSED,
)
from ..models.assignment_log import AssignmentLog
from ..services.close_api import CloseClient, CloseAPIError

lead_lists_bp = Blueprint("lead_lists", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_lead_list_or_404(lead_list_id: str) -> LeadList:
    """Fetch a Lead List and verify it belongs to the current user's org."""
    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None:
        abort(404)
    # Prefer the denormalised column; fall back to rotation for any row that
    # somehow predates the backfill.
    org_id = lead_list.close_org_id or (
        lead_list.rotation.close_org_id if lead_list.rotation else None
    )
    if org_id != current_user.close_org_id:
        abort(404)
    return lead_list


def _load_close_options(client: CloseClient):
    """Fetch custom fields + workflows for the create/edit forms."""
    custom_fields = []
    workflows = []
    errors = []
    try:
        custom_fields = client.get_user_custom_fields()
    except CloseAPIError as e:
        errors.append(f"custom fields: {e}")
    try:
        workflows = client.get_workflows()
    except CloseAPIError as e:
        errors.append(f"workflows: {e}")
    return custom_fields, workflows, ("; ".join(errors) or None)


def _parse_form(form):
    """Pull and normalise form fields used by both create and edit."""
    return {
        "name": form.get("name", "").strip(),
        "filters_raw": form.get("filters_json", "").strip(),
        "assign_enabled": form.get("assign_enabled") == "1",
        "rotation_id": form.get("rotation_id", "").strip() or None,
        "custom_field_id": form.get("custom_field_id", "").strip() or None,
        "overwrite_existing": form.get("overwrite_existing") == "1",
        "workflow_enabled": form.get("workflow_enabled") == "1",
        "workflow_id": form.get("workflow_id", "").strip() or None,
        # Empty string means "use the assigned member" — only valid when assign is on.
        "workflow_run_as_user_id": form.get("workflow_run_as_user_id", "").strip() or None,
    }


def _validate(parsed, rotations, workflows, org_users):
    """Return (errors, filters_parsed) — list of error messages + parsed JSON."""
    errors = []
    if not parsed["name"]:
        errors.append("Lead list name is required.")

    filters_parsed = None
    if not parsed["filters_raw"]:
        errors.append("A lead filter is required.")
    else:
        try:
            filters_parsed = json.loads(parsed["filters_raw"])
        except json.JSONDecodeError:
            errors.append("Lead filter is not valid JSON.")

    if not (parsed["assign_enabled"] or parsed["workflow_enabled"]):
        errors.append("Enable at least one Action (assign a lead or trigger a workflow).")

    if parsed["assign_enabled"]:
        if not parsed["rotation_id"]:
            errors.append("Select a Group to assign leads to.")
        elif not any(r.id == parsed["rotation_id"] for r in rotations):
            errors.append("Selected group is invalid.")
        if not parsed["custom_field_id"]:
            errors.append("Select a custom field for the assignment.")

    if parsed["workflow_enabled"]:
        if not parsed["workflow_id"]:
            errors.append("Select a Workflow to trigger.")
        elif not any(w["id"] == parsed["workflow_id"] for w in workflows):
            errors.append("Selected workflow is invalid.")
        run_as = parsed["workflow_run_as_user_id"]
        if run_as:
            if not any(u["id"] == run_as for u in org_users):
                errors.append("Selected Run-as user is invalid.")
        elif not parsed["assign_enabled"]:
            errors.append("Pick which Close user the workflow should run as.")

    return errors, filters_parsed


# ---------------------------------------------------------------------------
# Index (top-level list)
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/")
@login_required
def index():
    org_id = current_user.close_org_id
    lead_lists = (
        LeadList.query
        .filter(LeadList.close_org_id == org_id)
        .order_by(LeadList.created_at.desc())
        .all()
    )
    return render_template("lead_lists/index.html", lead_lists=lead_lists)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/new", methods=["GET", "POST"])
@login_required
def create_lead_list():
    org_id = current_user.close_org_id
    rotations = (
        Rotation.query
        .filter_by(close_org_id=org_id)
        .order_by(Rotation.name)
        .all()
    )

    custom_fields = workflows = []
    org_users = []
    error = None
    try:
        client = CloseClient(current_user)
        custom_fields, workflows, error = _load_close_options(client)
        # Used by the inline "Create a group" modal in the form.
        try:
            org_users = client.get_active_org_members()
        except CloseAPIError as e:
            # Non-fatal: the modal won't be usable, but the rest of the form works.
            error = (error or "") + (f"; org members: {e}" if error else f"org members: {e}")
    except CloseAPIError as e:
        error = str(e)

    # Pre-select a rotation if one came in via query string (from Group detail).
    preselect_rotation_id = request.args.get("rotation_id", "").strip()

    if request.method == "POST":
        parsed = _parse_form(request.form)
        errors, filters_parsed = _validate(parsed, rotations, workflows, org_users)

        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            field_lookup = {f["id"]: f["name"] for f in custom_fields}
            workflow_lookup = {w["id"]: w["name"] for w in workflows}
            user_lookup = {
                u["id"]: f"{u.get('first_name','')} {u.get('last_name','')}".strip() or u.get("email", u["id"])
                for u in org_users
            }
            run_as_id = parsed["workflow_run_as_user_id"] if parsed["workflow_enabled"] else None
            lead_list = LeadList(
                name=parsed["name"],
                close_org_id=org_id,
                queue_type=TYPE_ONGOING,
                filters_json=filters_parsed,
                rotation_id=parsed["rotation_id"] if parsed["assign_enabled"] else None,
                assign_enabled=parsed["assign_enabled"],
                custom_field_id=parsed["custom_field_id"] if parsed["assign_enabled"] else None,
                custom_field_label=(
                    field_lookup.get(parsed["custom_field_id"])
                    if parsed["assign_enabled"] and parsed["custom_field_id"]
                    else None
                ),
                overwrite_existing=parsed["overwrite_existing"] if parsed["assign_enabled"] else False,
                workflow_enabled=parsed["workflow_enabled"],
                workflow_id=parsed["workflow_id"] if parsed["workflow_enabled"] else None,
                workflow_name=(
                    workflow_lookup.get(parsed["workflow_id"])
                    if parsed["workflow_enabled"] and parsed["workflow_id"]
                    else None
                ),
                workflow_run_as_user_id=run_as_id,
                workflow_run_as_user_name=user_lookup.get(run_as_id) if run_as_id else None,
                status=STATUS_ACTIVE,
            )
            db.session.add(lead_list)
            db.session.commit()

            from ..services.assignment_engine import seed_queue
            seed_queue(lead_list.id)

            return redirect(url_for("lead_lists.view_lead_list", lead_list_id=lead_list.id))

    return render_template(
        "lead_lists/create.html",
        rotations=rotations,
        custom_fields=custom_fields,
        workflows=workflows,
        org_users=org_users,
        preselect_rotation_id=preselect_rotation_id,
        error=error,
    )


# ---------------------------------------------------------------------------
# View / Detail
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/<lead_list_id>")
@login_required
def view_lead_list(lead_list_id: str):
    lead_list = _get_lead_list_or_404(lead_list_id)

    page = request.args.get("page", 1, type=int)
    logs = (
        AssignmentLog.query.filter_by(queue_id=lead_list.id)
        .order_by(AssignmentLog.assigned_at.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    return render_template(
        "lead_lists/detail.html",
        lead_list=lead_list,
        logs=logs,
    )


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/<lead_list_id>/edit", methods=["GET", "POST"])
@login_required
def edit_lead_list(lead_list_id: str):
    lead_list = _get_lead_list_or_404(lead_list_id)

    org_id = current_user.close_org_id
    rotations = (
        Rotation.query
        .filter_by(close_org_id=org_id)
        .order_by(Rotation.name)
        .all()
    )

    custom_fields = workflows = []
    org_users = []
    error = None
    try:
        client = CloseClient(current_user)
        custom_fields, workflows, error = _load_close_options(client)
        try:
            org_users = client.get_active_org_members()
        except CloseAPIError as e:
            error = (error or "") + (f"; org members: {e}" if error else f"org members: {e}")
    except CloseAPIError as e:
        error = str(e)

    if request.method == "POST":
        parsed = _parse_form(request.form)
        errors, filters_parsed = _validate(parsed, rotations, workflows, org_users)

        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            field_lookup = {f["id"]: f["name"] for f in custom_fields}
            workflow_lookup = {w["id"]: w["name"] for w in workflows}
            user_lookup = {
                u["id"]: f"{u.get('first_name','')} {u.get('last_name','')}".strip() or u.get("email", u["id"])
                for u in org_users
            }

            filter_changed = json.dumps(filters_parsed, sort_keys=True) != json.dumps(
                lead_list.filters_json, sort_keys=True
            )

            lead_list.name = parsed["name"]
            lead_list.filters_json = filters_parsed

            lead_list.assign_enabled = parsed["assign_enabled"]
            lead_list.rotation_id = parsed["rotation_id"] if parsed["assign_enabled"] else None
            lead_list.custom_field_id = parsed["custom_field_id"] if parsed["assign_enabled"] else None
            lead_list.custom_field_label = (
                field_lookup.get(parsed["custom_field_id"])
                if parsed["assign_enabled"] and parsed["custom_field_id"]
                else None
            )
            lead_list.overwrite_existing = parsed["overwrite_existing"] if parsed["assign_enabled"] else False

            lead_list.workflow_enabled = parsed["workflow_enabled"]
            lead_list.workflow_id = parsed["workflow_id"] if parsed["workflow_enabled"] else None
            lead_list.workflow_name = (
                workflow_lookup.get(parsed["workflow_id"])
                if parsed["workflow_enabled"] and parsed["workflow_id"]
                else None
            )
            run_as_id = parsed["workflow_run_as_user_id"] if parsed["workflow_enabled"] else None
            lead_list.workflow_run_as_user_id = run_as_id
            lead_list.workflow_run_as_user_name = user_lookup.get(run_as_id) if run_as_id else None

            db.session.commit()

            if filter_changed:
                from ..services.assignment_engine import seed_queue
                seed_queue(lead_list.id)

            return redirect(url_for("lead_lists.view_lead_list", lead_list_id=lead_list.id))

    return render_template(
        "lead_lists/edit.html",
        lead_list=lead_list,
        rotations=rotations,
        custom_fields=custom_fields,
        workflows=workflows,
        org_users=org_users,
        error=error,
    )


# ---------------------------------------------------------------------------
# Toggle pause / resume
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/<lead_list_id>/toggle", methods=["POST"])
@login_required
def toggle_lead_list(lead_list_id: str):
    lead_list = _get_lead_list_or_404(lead_list_id)
    if lead_list.status == STATUS_PAUSED:
        lead_list.status = STATUS_ACTIVE
        resuming = True
    elif lead_list.status == STATUS_ACTIVE:
        lead_list.status = STATUS_PAUSED
        resuming = False
    else:
        flash("This lead list cannot be toggled in its current state.", "error")
        return redirect(url_for("lead_lists.view_lead_list", lead_list_id=lead_list.id))
    db.session.commit()

    if resuming:
        from ..services.assignment_engine import seed_queue
        seed_queue(lead_list.id)

    return redirect(url_for("lead_lists.view_lead_list", lead_list_id=lead_list.id))


# ---------------------------------------------------------------------------
# Manual check ("Check now")
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/<lead_list_id>/check", methods=["POST"])
@login_required
def check_lead_list(lead_list_id: str):
    lead_list = _get_lead_list_or_404(lead_list_id)
    if lead_list.status != STATUS_ACTIVE:
        return jsonify({"error": "Lead list is not active."}), 400

    from ..services.assignment_engine import poll_queue

    try:
        result = poll_queue(lead_list.id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in result:
        return jsonify(result), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@lead_lists_bp.route("/lead-lists/<lead_list_id>/delete", methods=["POST"])
@login_required
def delete_lead_list(lead_list_id: str):
    lead_list = _get_lead_list_or_404(lead_list_id)
    db.session.delete(lead_list)
    db.session.commit()
    return redirect(url_for("lead_lists.index"))
