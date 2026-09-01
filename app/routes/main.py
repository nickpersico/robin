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
    is usable *and writable*, so external tools (UptimeRobot etc.) can alert on
    DB outages the same way they alert on app crashes.

    Checking writability matters: when the DB volume fills, Postgres flips to
    read-only. Reads (and a bare "SELECT 1") keep succeeding, so a read-only
    database is invisible to a liveness check — but every write (login, lead
    assignment) is dead. That exact failure took us down without any signal, so
    we surface read-only as unhealthy (503) here.
    """
    try:
        db.session.execute(text("SELECT 1"))
        read_only = db.session.execute(text("SHOW transaction_read_only")).scalar()
        if read_only != "off":
            logger.error("healthz: database is in read-only mode")
            return jsonify({"ok": False, "db": True, "writable": False}), 503
        return jsonify({"ok": True, "db": True, "writable": True}), 200
    except SQLAlchemyError as e:
        logger.exception("healthz: database unreachable")
        return jsonify({"ok": False, "db": False, "error": str(e.__class__.__name__)}), 503
