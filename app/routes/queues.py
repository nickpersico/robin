"""
Legacy redirects for the pre-Lead-Lists-as-first-class URLs.

Old URL shape:
    /groups/<rotation_id>/lead-lists/<queue_id>[/edit|/toggle|/check|/delete|/new]

These redirect to the equivalent top-level Lead List URL so that any bookmarks
or external links keep working. We deliberately keep the blueprint registered
so users don't hit 404s.

For new code, use ``lead_lists_bp`` instead.
"""

from flask import Blueprint, abort, redirect, url_for
from flask_login import login_required

from ..extensions import db
from ..models.lead_list import LeadList

queues_bp = Blueprint("queues", __name__)


def _resolve(lead_list_id: str) -> LeadList:
    lead_list = db.session.get(LeadList, lead_list_id)
    if lead_list is None:
        abort(404)
    return lead_list


@queues_bp.route("/groups/<rotation_id>/lead-lists/new")
@login_required
def create_queue(rotation_id: str):
    return redirect(
        url_for("lead_lists.create_lead_list", rotation_id=rotation_id),
        code=302,
    )


@queues_bp.route("/groups/<rotation_id>/lead-lists/<queue_id>")
@login_required
def view_queue(rotation_id: str, queue_id: str):
    _resolve(queue_id)
    return redirect(
        url_for("lead_lists.view_lead_list", lead_list_id=queue_id),
        code=302,
    )


@queues_bp.route("/groups/<rotation_id>/lead-lists/<queue_id>/edit")
@login_required
def edit_queue(rotation_id: str, queue_id: str):
    _resolve(queue_id)
    return redirect(
        url_for("lead_lists.edit_lead_list", lead_list_id=queue_id),
        code=302,
    )


@queues_bp.route("/groups/<rotation_id>/lead-lists/<queue_id>/toggle", methods=["POST"])
@login_required
def toggle_queue(rotation_id: str, queue_id: str):
    _resolve(queue_id)
    return redirect(
        url_for("lead_lists.toggle_lead_list", lead_list_id=queue_id),
        code=307,  # preserves POST
    )


@queues_bp.route("/groups/<rotation_id>/lead-lists/<queue_id>/check", methods=["POST"])
@login_required
def check_queue(rotation_id: str, queue_id: str):
    _resolve(queue_id)
    return redirect(
        url_for("lead_lists.check_lead_list", lead_list_id=queue_id),
        code=307,
    )


@queues_bp.route("/groups/<rotation_id>/lead-lists/<queue_id>/delete", methods=["POST"])
@login_required
def delete_queue(rotation_id: str, queue_id: str):
    _resolve(queue_id)
    return redirect(
        url_for("lead_lists.delete_lead_list", lead_list_id=queue_id),
        code=307,
    )
