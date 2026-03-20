import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import login_user, logout_user, login_required, current_user

from ..extensions import db
from ..models.organization import Organization
from ..models.user import User, ROLE_ADMIN, ROLE_MEMBER, STATUS_ACTIVE, STATUS_PENDING
from ..services.close_api import exchange_code_for_tokens, revoke_token, CloseClient, CloseAPIError

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": current_app.config["CLOSE_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": current_app.config["CLOSE_REDIRECT_URI"],
        "scope": "all.full_access offline_access",
        "state": state,
    }
    authorize_url = current_app.config["CLOSE_AUTHORIZE_URL"] + "?" + urlencode(params)
    return redirect(authorize_url)


@auth_bp.route("/callback")
def callback():
    # Validate CSRF state
    state = request.args.get("state")
    if not state or state != session.pop("oauth_state", None):
        flash("Invalid OAuth state. Please try again.", "error")
        return redirect(url_for("main.index"))

    error = request.args.get("error")
    if error:
        flash(f"Authorization denied: {error}", "error")
        return redirect(url_for("main.index"))

    code = request.args.get("code")
    if not code:
        flash("No authorization code received.", "error")
        return redirect(url_for("main.index"))

    try:
        token_data = exchange_code_for_tokens(code)
    except CloseAPIError as e:
        flash(f"Failed to authenticate with Close: {e}", "error")
        return redirect(url_for("main.index"))

    close_user_id = token_data.get("user_id")
    close_org_id = token_data.get("organization_id")

    if not close_user_id or not close_org_id:
        flash("Unexpected response from Close. Please try again.", "error")
        return redirect(url_for("main.index"))

    # ── 1. Find or create the Organization ───────────────────────────────────
    org = Organization.query.filter_by(close_org_id=close_org_id).first()
    is_new_org = org is None

    if is_new_org:
        org = Organization(close_org_id=close_org_id, name=close_org_id)  # name filled below
        db.session.add(org)
        db.session.flush()

    # ── 2. Find or create the User ────────────────────────────────────────────
    user = User.query.filter_by(close_user_id=close_user_id, close_org_id=close_org_id).first()
    is_new_user = user is None

    if is_new_user:
        # First user in the org becomes admin and is immediately active.
        # Everyone else starts as a pending member.
        role = ROLE_ADMIN if is_new_org else ROLE_MEMBER
        status = STATUS_ACTIVE if is_new_org else STATUS_PENDING
        user = User(
            close_user_id=close_user_id,
            close_org_id=close_org_id,
            organization_id=org.id,
            role=role,
            status=status,
            access_token=token_data["access_token"],  # set before flush
            email="",
        )
        db.session.add(user)
    else:
        # Backfill org link for pre-existing users
        if user.organization_id is None:
            user.organization_id = org.id

    # ── 3. Update tokens ──────────────────────────────────────────────────────
    user.access_token = token_data["access_token"]
    user.refresh_token = token_data.get("refresh_token")
    if token_data.get("expires_in"):
        user.token_expires_at = datetime.utcnow() + timedelta(seconds=token_data["expires_in"])
    user.last_login_at = datetime.utcnow()
    db.session.flush()  # ensure user.id exists before API calls

    # ── 4. Fetch profile from Close (and org name if this is a new org) ───────
    try:
        client = CloseClient(user)
        me = client.get_me()
        user.email = me.get("email", "")
        user.first_name = me.get("first_name", "")
        user.last_name = me.get("last_name", "")

        if is_new_org:
            org_data = client.get_org()
            org.name = org_data.get("name", close_org_id)
    except CloseAPIError:
        pass  # Non-fatal; tokens are valid, profile is cosmetic

    # ── 5. Check Close membership status for returning active users ───────────
    # If an existing user's Close membership was deactivated, suspend their access.
    if not is_new_user and user.status == STATUS_ACTIVE:
        try:
            client = CloseClient(user)
            active_members = client.get_active_org_members()
            active_ids = {m["id"] for m in active_members}
            if user.close_user_id not in active_ids:
                user.status = "suspended"
        except CloseAPIError:
            pass  # Non-fatal — don't suspend on API error

    db.session.commit()
    login_user(user, remember=True)

    # ── 6. Route based on status ──────────────────────────────────────────────
    if user.is_pending:
        flash("Welcome to Robin! Your account is pending approval from an admin — you have read-only access in the meantime.", "info")

    if user.is_suspended:
        logout_user()
        flash("Your Robin access has been suspended because your Close membership is no longer active.", "error")
        return redirect(url_for("main.index"))

    next_page = request.args.get("next")
    return redirect(next_page or url_for("main.index"))


@auth_bp.route("/pending")
@login_required
def pending():
    """Shown to users who have signed in but not yet been approved by an admin."""
    if current_user.is_active_user:
        return redirect(url_for("main.index"))
    return render_template("auth/pending.html")


@auth_bp.route("/switch-org/<target_user_id>", methods=["POST"])
@login_required
def switch_org(target_user_id: str):
    """Switch the active session to a different org for the same person."""
    from ..extensions import db
    target = db.session.get(User, target_user_id)
    # Guard: target must exist and belong to the same person (matched by email)
    if not target or target.email.lower() != current_user.email.lower():
        abort(403)
    login_user(target, remember=True)
    return redirect(url_for("main.index"))


@auth_bp.route("/logout")
@login_required
def logout():
    try:
        if current_user.access_token:
            revoke_token(current_user.access_token)
    except Exception:
        pass

    logout_user()
    return redirect(url_for("main.index"))
