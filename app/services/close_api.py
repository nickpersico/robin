"""
Close CRM API client.

Handles OAuth token exchange/refresh and wraps common API calls.
All methods raise CloseAPIError on non-2xx responses.
"""

import time
from datetime import datetime, timedelta
from typing import Optional, List
import requests
from flask import current_app
from sqlalchemy import text


# Rate-limit (HTTP 429) retry policy. Close throttles per-organization, and
# Robin funnels every org's polling through one token, so 429s are expected
# under load. We honor Close's own "wait this long" hint and retry a bounded
# number of times before surfacing the error.
_RATELIMIT_MAX_RETRIES = 3      # retries after the first attempt
_RATELIMIT_DEFAULT_WAIT = 2.0   # seconds, when Close gives no reset hint
_RATELIMIT_MAX_WAIT = 30.0      # cap a single wait so a poll can't hang
# Cap the *cumulative* time spent sleeping on 429 retries within a single
# request. Login happens on a synchronous request that shares gunicorn's
# 120s worker timeout, so unbounded backoff could hang a request until the
# worker is killed (a raw 500). Once we've waited this long in total, give
# up and surface the 429 instead of sleeping further.
_RATELIMIT_MAX_TOTAL_WAIT = 45.0

# (connect, read) timeout for every Close HTTP call. Without this, a stalled
# Close connection holds the worker thread until gunicorn's 120s timeout
# kills it — surfacing as an unexplained Internal Server Error.
_HTTP_TIMEOUT = (5.0, 30.0)


class CloseAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _retry_after_seconds(resp) -> float:
    """
    How long Close is asking us to wait before retrying a 429, in seconds.

    Prefers the standard ``Retry-After`` header, then the IETF RateLimit
    ``reset`` hint — Close sends it both as a ``RateLimit-Reset`` header and
    packed into a combined ``ratelimit`` header (e.g. ``limit=60, reset=1.5``).
    Falls back to a fixed default, and always clamps to ``_RATELIMIT_MAX_WAIT``.
    """
    candidates = []

    for header in ("Retry-After", "RateLimit-Reset"):
        raw = resp.headers.get(header)
        if raw:
            try:
                candidates.append(float(raw))
            except ValueError:
                pass

    combined = resp.headers.get("ratelimit") or resp.headers.get("RateLimit")
    if combined:
        for part in combined.replace(";", ",").split(","):
            part = part.strip()
            if part.startswith("reset="):
                try:
                    candidates.append(float(part.split("=", 1)[1]))
                except ValueError:
                    pass

    wait = max(candidates) if candidates else _RATELIMIT_DEFAULT_WAIT
    return min(max(wait, 0.0), _RATELIMIT_MAX_WAIT)


def _parse_json(action: str, resp) -> dict:
    """
    Decode a 2xx response body as JSON, converting a malformed/empty body
    into a CloseAPIError instead of letting a raw JSONDecodeError (a
    ValueError) escape as an unhandled 500. Close can occasionally answer a
    2xx through a proxy with a non-JSON body.
    """
    try:
        return resp.json()
    except ValueError as exc:
        raise CloseAPIError(
            f"{action} returned a non-JSON response (HTTP {resp.status_code})"
        ) from exc


def _format_http_error(action: str, resp) -> str:
    """
    Build a diagnostic error string that always names the HTTP status code
    and flags an empty response body — so operators aren't left with a
    useless "action failed:" tail when Close returns e.g. a 502/504 with
    no body.
    """
    body = (resp.text or "").strip()
    if not body:
        body = "(empty response body)"
    return f"{action} failed: HTTP {resp.status_code} — {body}"


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    try:
        resp = requests.post(
            current_app.config["CLOSE_TOKEN_URL"],
            data={
                "client_id": current_app.config["CLOSE_CLIENT_ID"],
                "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": current_app.config["CLOSE_REDIRECT_URI"],
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise CloseAPIError(f"Token exchange failed: {exc.__class__.__name__}") from exc
    if not resp.ok:
        raise CloseAPIError(
            _format_http_error("Token exchange", resp), status_code=resp.status_code
        )
    data = _parse_json("Token exchange", resp)
    if not data.get("access_token"):
        raise CloseAPIError("Token exchange succeeded but returned no access_token.")
    return data


def refresh_access_token(refresh_token: str) -> dict:
    """Use a refresh token to get a new access token."""
    try:
        resp = requests.post(
            current_app.config["CLOSE_TOKEN_URL"],
            data={
                "client_id": current_app.config["CLOSE_CLIENT_ID"],
                "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise CloseAPIError(f"Token refresh failed: {exc.__class__.__name__}") from exc
    if not resp.ok:
        raise CloseAPIError(
            _format_http_error("Token refresh", resp), status_code=resp.status_code
        )
    data = _parse_json("Token refresh", resp)
    if not data.get("access_token"):
        raise CloseAPIError("Token refresh succeeded but returned no access_token.")
    return data


def revoke_token(token: str):
    """Revoke an access or refresh token."""
    requests.post(
        current_app.config["CLOSE_REVOKE_URL"],
        data={
            "client_id": current_app.config["CLOSE_CLIENT_ID"],
            "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
            "token": token,
        },
        timeout=_HTTP_TIMEOUT,
    )


def _db_is_writable() -> bool:
    """
    True only if the current DB connection can accept writes.

    Close rotates the refresh token on every refresh — the moment we call the
    refresh endpoint, Close invalidates the old token. If we then can't persist
    the new one (e.g. the primary is in read-only mode because its disk filled),
    the token is lost forever and the org must re-authenticate by hand. So we
    check writability *before* refreshing and refuse to rotate a token we can't
    save. Any error here is treated as "not writable" — safer to skip a refresh
    than to burn a token.
    """
    from ..extensions import db

    try:
        return db.session.execute(text("SHOW transaction_read_only")).scalar() == "off"
    except Exception:
        return False


class CloseClient:
    """
    An authenticated Close API client for a specific user.
    Automatically refreshes the access token when needed and persists
    the new tokens back to the User model.
    """

    def __init__(self, user):
        self.user = user

    def _ensure_fresh_token(self):
        """Refresh the access token if it's expired or about to expire."""
        if self.user.token_expires_at is None:
            return
        # Refresh if less than 60 seconds remain
        if datetime.utcnow() >= self.user.token_expires_at - timedelta(seconds=60):
            if not self.user.refresh_token:
                raise CloseAPIError("Access token expired and no refresh token available.")
            # Don't rotate a token we can't persist — see _db_is_writable. On a
            # read-only DB this fails the caller gracefully (a caught
            # CloseAPIError) and leaves the token intact, so syncs auto-resume
            # once the DB is writable again instead of every org needing to
            # re-authenticate.
            if not _db_is_writable():
                raise CloseAPIError(
                    "Database is read-only; skipping token refresh to avoid "
                    "invalidating a token that can't be saved."
                )
            token_data = refresh_access_token(self.user.refresh_token)
            self._update_user_tokens(token_data)

    def _update_user_tokens(self, token_data: dict):
        """Persist refreshed tokens to the user model."""
        from ..extensions import db

        self.user.access_token = token_data["access_token"]
        if "refresh_token" in token_data:
            self.user.refresh_token = token_data["refresh_token"]
        if "expires_in" in token_data:
            self.user.token_expires_at = datetime.utcnow() + timedelta(
                seconds=token_data["expires_in"]
            )
        db.session.commit()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """
        Issue an authenticated request to Close, transparently retrying on
        HTTP 429 (rate limit). Close tells us how long to wait via its
        Retry-After / RateLimit-Reset headers; we honor that (capped) and
        retry a bounded number of times. Every other non-2xx raises
        immediately, preserving the diagnostic status + empty-body marker.
        """
        self._ensure_fresh_token()
        url = f"{current_app.config['CLOSE_API_BASE']}{path}"
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        attempt = 0
        total_waited = 0.0
        while True:
            try:
                resp = requests.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {self.user.access_token}"},
                    **kwargs,
                )
            except requests.exceptions.RequestException as exc:
                raise CloseAPIError(
                    f"{method} {path} failed: {exc.__class__.__name__}"
                ) from exc
            if resp.status_code == 429 and attempt < _RATELIMIT_MAX_RETRIES:
                wait = _retry_after_seconds(resp)
                # Bound the cumulative backoff so a rate-limited org can't hang
                # a synchronous request (e.g. login) past the worker timeout.
                if total_waited + wait > _RATELIMIT_MAX_TOTAL_WAIT:
                    current_app.logger.warning(
                        "Close rate-limited %s %s — giving up after %.1fs total backoff",
                        method, path, total_waited,
                    )
                else:
                    attempt += 1
                    total_waited += wait
                    current_app.logger.warning(
                        "Close rate-limited %s %s — waiting %.1fs before retry %d/%d",
                        method, path, wait, attempt, _RATELIMIT_MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
            if not resp.ok:
                raise CloseAPIError(
                    _format_http_error(f"{method} {path}", resp),
                    status_code=resp.status_code,
                )
            return _parse_json(f"{method} {path}", resp)

    def _post(self, path: str, json: dict = None) -> dict:
        return self._request("POST", path, json=json)

    def _put(self, path: str, json: dict = None) -> dict:
        return self._request("PUT", path, json=json)

    def _get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def get_me(self) -> dict:
        """Fetch the authenticated user's profile."""
        return self._get("/me/")

    def get_org(self) -> dict:
        """Fetch the organization record (includes name, memberships, etc.)."""
        return self._get(f"/organization/{self.user.close_org_id}/")

    def get_active_org_members(self) -> List[dict]:
        """
        Return all active members of the organization using the org endpoint.
        The org endpoint separates active (memberships) from inactive
        (inactive_memberships), so we only return currently active users.
        Fields are prefixed with 'user_' in the API response; we normalize
        them here so callers get plain id/email/first_name/last_name dicts.
        """
        data = self._get(f"/organization/{self.user.close_org_id}/")
        members = []
        for m in data.get("memberships", []):
            members.append({
                "id": m.get("user_id"),
                "email": m.get("user_email"),
                "first_name": m.get("user_first_name", ""),
                "last_name": m.get("user_last_name", ""),
            })
        return sorted(
            members,
            key=lambda u: f"{u['first_name']} {u['last_name']}".strip().lower()
        )

    def get_user_custom_fields(self) -> List[dict]:
        """
        Return all User-type custom fields defined on leads in this org.
        These are the fields Robin can write an assigned user ID into.
        """
        data = self._get("/custom_field/lead/")
        fields = [
            {"id": f["id"], "name": f["name"]}
            for f in data.get("data", [])
            if f.get("type") == "user"
        ]
        return sorted(fields, key=lambda f: f["name"].lower())

    def get_lead(self, lead_id: str) -> dict:
        """Fetch a single lead by ID."""
        return self._get(f"/lead/{lead_id}/")

    def search_leads(self, query: dict, fields: Optional[List[str]] = None) -> List[dict]:
        """
        Run a search against the Close Advanced Filtering API and return all
        matching leads, handling cursor-based pagination automatically.

        `query` is the Close filter JSON (e.g. {"type": "and", "queries": [...]}).
        `fields` is an optional list of lead fields to include in results.
        """
        requested_fields = fields or ["id", "display_name", "custom"]
        body = {
            "object_type": "lead",
            "query": query,
            "_fields": {"lead": requested_fields},
            "results_limit": 200,
            "cursor": None,
        }

        leads = []
        while True:
            data = self._post("/data/search/", json=body)
            leads.extend(data.get("data", []))
            cursor = data.get("cursor")
            if not cursor:
                break
            body["cursor"] = cursor

        return leads

    def assign_lead(self, lead_id: str, custom_field_id: str, user_id: str) -> dict:
        """
        Write a user ID into a custom field on a lead.
        `custom_field_id` should be the raw field ID (e.g. 'cf_abc123').
        The Close API expects the key as 'custom.{field_id}'.
        """
        return self._put(f"/lead/{lead_id}/", json={f"custom.{custom_field_id}": user_id})

    def get_workflows(self) -> List[dict]:
        """
        Return Close Workflows (Sequences) that Robin can trigger on Leads.
        Close still exposes Workflows under the /sequence/ endpoint — only
        sequences with status='active' and no attached schedule are eligible
        (scheduled ones are triggered by Close itself, not by Robin).
        """
        data = self._get("/sequence/")
        workflows = []
        for s in data.get("data", []):
            if s.get("status") != "active":
                continue
            # Manually-triggerable workflows have no schedule attached.
            if s.get("schedule_id"):
                continue
            workflows.append({"id": s["id"], "name": s.get("name", s["id"])})
        return sorted(workflows, key=lambda w: w["name"].lower())

    def get_user_email_accounts(self, close_user_id: str) -> List[dict]:
        """
        Return the active email connected accounts the given Close user can
        send from. Each entry has id/email/display_name keys.
        """
        data = self._get("/connected_account/", params={"user_id": close_user_id})
        accounts = []
        for a in data.get("data", []):
            if a.get("status") and a["status"] != "active":
                continue
            email = a.get("email") or a.get("identities", [{}])[0].get("email")
            if not email:
                continue
            accounts.append({
                "id": a["id"],
                "email": email,
                "display_name": a.get("display_name") or "",
            })
        return accounts

    def subscribe_lead_to_workflow(
        self,
        lead_id: str,
        workflow_id: str,
        sender_account_id: str,
        sender_name: str,
        sender_email: str,
    ) -> dict:
        """
        Trigger a Close Workflow (Sequence) on a Lead. Close subscribes a
        contact (not a lead) to a sequence, so we fetch the lead and pick
        its first contact. The sender fields tell Close which email account
        to send email steps from; they are required for email-step workflows
        and harmless for SMS/call-only workflows.
        """
        lead = self.get_lead(lead_id)
        contacts = lead.get("contacts") or []
        if not contacts:
            raise CloseAPIError(
                f"Lead {lead_id} has no contacts; cannot trigger workflow."
            )
        contact_id = contacts[0].get("id")
        if not contact_id:
            raise CloseAPIError(
                f"Lead {lead_id}'s first contact has no id; cannot trigger workflow."
            )
        return self._post(
            "/sequence_subscription/",
            json={
                "sequence_id": workflow_id,
                "contact_id": contact_id,
                "sender_account_id": sender_account_id,
                "sender_name": sender_name,
                "sender_email": sender_email,
            },
        )
