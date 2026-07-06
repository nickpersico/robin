import logging

from flask import Blueprint, jsonify, redirect, render_template, url_for
from flask_login import current_user
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("lead_lists.index"))
    return render_template("index.html")


@main_bp.route("/healthz")
def healthz():
    """
    Liveness + database-connectivity probe for external uptime monitors.

    Public — no auth required. Returns 200 only when the Postgres connection
    is usable, so external tools (UptimeRobot etc.) can alert on DB outages
    the same way they alert on app crashes. The DB is what took us down for
    days without any signal; a pure "app is up" check would not have caught it.
    """
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True, "db": True}), 200
    except SQLAlchemyError as e:
        logger.exception("healthz: database unreachable")
        return jsonify({"ok": False, "db": False, "error": str(e.__class__.__name__)}), 503
