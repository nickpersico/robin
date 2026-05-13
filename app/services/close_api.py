"""
Close CRM API client.

Handles OAuth token exchange/refresh and wraps common API calls.
All methods raise CloseAPIError on non-2xx responses.
"""

from datetime import datetime, timedelta
from typing import Optional, List
import requests
from flask import current_app


class CloseAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    resp = requests.post(
        current_app.config["CLOSE_TOKEN_URL"],
        data={
            "client_id": current_app.config["CLOSE_CLIENT_ID"],
            "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": current_app.config["CLOSE_REDIRECT_URI"],
        },
    )
    if not resp.ok:
        raise CloseAPIError(
            f"Token exchange failed: {resp.text}", status_code=resp.status_code
        )
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Use a refresh token to get a new access token."""
    resp = requests.post(
        current_app.config["CLOSE_TOKEN_URL"],
        data={
            "client_id": current_app.config["CLOSE_CLIENT_ID"],
            "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if not resp.ok:
        raise CloseAPIError(
            f"Token refresh failed: {resp.text}", status_code=resp.status_code
        )
    return resp.json()


def revoke_token(token: str):
    """Revoke an access or refresh token."""
    requests.post(
        current_app.config["CLOSE_REVOKE_URL"],
        data={
            "client_id": current_app.config["CLOSE_CLIENT_ID"],
            "client_secret": current_app.config["CLOSE_CLIENT_SECRET"],
            "token": token,
        },
    )


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

    def _post(self, path: str, json: dict = None) -> dict:
        self._ensure_fresh_token()
        resp = requests.post(
            f"{current_app.config['CLOSE_API_BASE']}{path}",
            headers={"Authorization": f"Bearer {self.user.access_token}"},
            json=json,
        )
        if not resp.ok:
            raise CloseAPIError(
                f"POST {path} failed: {resp.text}", status_code=resp.status_code
            )
        return resp.json()

    def _put(self, path: str, json: dict = None) -> dict:
        self._ensure_fresh_token()
        resp = requests.put(
            f"{current_app.config['CLOSE_API_BASE']}{path}",
            headers={"Authorization": f"Bearer {self.user.access_token}"},
            json=json,
        )
        if not resp.ok:
            raise CloseAPIError(
                f"PUT {path} failed: {resp.text}", status_code=resp.status_code
            )
        return resp.json()

    def _get(self, path: str, params: dict = None) -> dict:
        self._ensure_fresh_token()
        resp = requests.get(
            f"{current_app.config['CLOSE_API_BASE']}{path}",
            headers={"Authorization": f"Bearer {self.user.access_token}"},
            params=params,
        )
        if not resp.ok:
            raise CloseAPIError(
                f"GET {path} failed: {resp.text}", status_code=resp.status_code
            )
        return resp.json()

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
