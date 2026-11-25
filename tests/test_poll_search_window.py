"""
Regression tests for the poll search window.

Background: the poller's "created since last check" condition used a shape
Close silently ignores, and the search capped *total* results at 200. Close
orders unsorted searches by date_updated desc, so every poll only ever saw
the 200 most recently updated matching leads. Bulk imports of more than 200
leads were partly never assigned, and the misses left no AssignmentLog row.

These tests pin:
  * the Close request shape (moment_range on_or_after, _limit paging, sort)
  * a >200-lead import is fully assigned across polls, with no duplicates
  * the overlap window catches leads created just before a poll
  * failed leads are retried instead of being skipped forever
  * seed_queue starts a list "from now" without acting on older leads

Run with:  python -m unittest discover -s tests
"""
import atexit
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("SECRET_KEY", "test-secret")

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".sqlite")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.assignment_log import AssignmentLog  # noqa: E402
from app.models.lead_list import LeadList, STATUS_ACTIVE, TYPE_ONGOING  # noqa: E402
from app.models.rotation import Rotation, RotationMember  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import assignment_engine as engine  # noqa: E402
from app.services.close_api import CloseAPIError, CloseClient  # noqa: E402

# Built at import so it binds to this module's temp DB; the app context is
# pushed only while this module's tests run, so it nests cleanly with other
# test modules that push their own context.
_app = create_app()
_ctx = _app.app_context()

ORG = "orga_test"
ASSIGN_FIELD = "cf_assigned_to"
MEMBER_IDS = ["user_a", "user_b", "user_c"]


def setUpModule():
    _ctx.push()
    db.create_all()


def _remove_db_file():
    os.close(_DB_FD)
    try:
        os.remove(_DB_PATH)
    except OSError:
        pass


# app.config reads DATABASE_URL once, on first import, so in a combined run
# every test module shares whichever temp DB was created first. Delete the file
# only at process exit, never mid-run under another module's tests.
atexit.register(_remove_db_file)


def tearDownModule():
    db.session.remove()
    _ctx.pop()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _find_date_condition(node):
    """Return the date_created condition dict inside a Close query, if any."""
    if isinstance(node, dict):
        field = node.get("field") or {}
        if field.get("field_name") == "date_created":
            return node.get("condition")
        for child in node.get("queries") or []:
            found = _find_date_condition(child)
            if found:
                return found
    return None


class FakeCloseClient:
    """
    In-memory stand-in for CloseClient. Every stored lead "matches" the list
    filter; the date condition and sort are applied the way Close applies
    them, and a condition Close wouldn't recognise is ignored, as Close does.
    """

    def __init__(self):
        self.leads = {}          # id -> {"id", "display_name", "date_created", "custom.<f>"}
        self.fail_assign = set() # lead ids whose assign_lead raises
        self.assign_calls = []
        self.search_calls = []

    def add_lead(self, lead_id, created: datetime, assigned_to=None):
        lead = {"id": lead_id, "display_name": lead_id, "date_created": _iso(created)}
        if assigned_to:
            lead[f"custom.{ASSIGN_FIELD}"] = assigned_to
        self.leads[lead_id] = lead

    def get_active_org_members(self):
        return [{"id": uid} for uid in MEMBER_IDS]

    def search_leads(self, query, fields=None, sort=None, max_results=None):
        self.search_calls.append({"query": query, "fields": fields, "sort": sort,
                                  "max_results": max_results})
        results = list(self.leads.values())
        cond = _find_date_condition(query) or {}
        bound = (cond.get("on_or_after") or {}).get("value")
        if cond.get("type") == "moment_range" and bound:
            results = [l for l in results if l["date_created"] >= bound]
        if sort:
            results.sort(key=lambda l: l["date_created"])
        if max_results:
            results = results[:max_results]
        return [dict(l) for l in results]

    def assign_lead(self, lead_id, custom_field_id, user_id):
        self.assign_calls.append((lead_id, user_id))
        if lead_id in self.fail_assign:
            raise CloseAPIError("boom", status_code=500)
        self.leads[lead_id][f"custom.{custom_field_id}"] = user_id
        return {}


class PollSearchWindowTestCase(unittest.TestCase):
    def setUp(self):
        for model in (AssignmentLog, LeadList, RotationMember, Rotation, User):
            db.session.query(model).delete()
        db.session.commit()

        owner = User(close_user_id="user_owner", close_org_id=ORG,
                     email="owner@example.com", access_token="tok")
        db.session.add(owner)
        db.session.flush()
        rotation = Rotation(name="Team", owner_id=owner.id, close_org_id=ORG)
        db.session.add(rotation)
        db.session.flush()
        for pos, uid in enumerate(MEMBER_IDS):
            db.session.add(RotationMember(rotation_id=rotation.id, close_user_id=uid,
                                          close_user_name=uid, position=pos))
        self.now = datetime.utcnow()
        self.lead_list = LeadList(
            name="USA leads", queue_type=TYPE_ONGOING, status=STATUS_ACTIVE,
            close_org_id=ORG, rotation_id=rotation.id,
            filters_json={"limit": None, "query": {"type": "and", "queries": []},
                          "results_limit": None, "sort": []},
            assign_enabled=True, custom_field_id=ASSIGN_FIELD, overwrite_existing=False,
            last_checked_at=self.now - timedelta(minutes=5),
        )
        db.session.add(self.lead_list)
        db.session.commit()

        self.client = FakeCloseClient()
        patcher = mock.patch.object(engine, "_get_org_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _poll(self):
        result = engine.poll_queue(self.lead_list.id)
        db.session.expire_all()
        return result

    def _logged_lead_ids(self):
        return [r.close_lead_id for r in
                AssignmentLog.query.filter_by(queue_id=self.lead_list.id).all()]

    # -- request shape --------------------------------------------------------

    def test_date_condition_uses_close_moment_shape(self):
        q = engine._inject_date_filter({"type": "and", "queries": []}, datetime(2026, 11, 23, 11, 35))
        cond = _find_date_condition(q)
        self.assertEqual(cond["type"], "moment_range")
        self.assertNotIn("after", cond)
        self.assertEqual(cond["on_or_after"]["type"], "fixed_utc")
        self.assertTrue(cond["on_or_after"]["value"].startswith("2026-11-23T11:35:00"))

    def test_search_leads_pages_without_a_total_cap(self):
        client = CloseClient.__new__(CloseClient)
        pages = [
            {"data": [{"id": f"lead_{i}"} for i in range(200)], "cursor": "c1"},
            {"data": [{"id": f"lead_{i}"} for i in range(200, 400)], "cursor": "c2"},
            {"data": [{"id": "lead_400"}], "cursor": None},
        ]
        bodies = []

        def fake_post(path, json=None):
            bodies.append(dict(json))
            return pages[len(bodies) - 1]

        client._post = fake_post
        sort = engine.SORT_OLDEST_FIRST
        leads = client.search_leads({"type": "and", "queries": []}, fields=["id"], sort=sort)

        self.assertEqual(len(leads), 401)
        self.assertNotIn("results_limit", bodies[0])
        self.assertEqual(bodies[0]["_limit"], 200)
        self.assertEqual(bodies[0]["sort"], sort)
        self.assertEqual([b["cursor"] for b in bodies], [None, "c1", "c2"])

    # -- bulk import ----------------------------------------------------------

    def test_import_larger_than_200_is_fully_assigned_across_polls(self):
        # Mirrors a real import: 977 leads created over ~2.5 minutes, then polled.
        start = self.now - timedelta(minutes=4)
        ids = [f"lead_{i:04d}" for i in range(977)]
        for i, lead_id in enumerate(ids):
            self.client.add_lead(lead_id, start + timedelta(milliseconds=150 * i))
        # An older, already-handled lead outside the window must never be touched.
        self.client.add_lead("lead_old", self.now - timedelta(days=3))

        first = self._poll()
        self.assertEqual(first["assigned"], engine.MAX_ACTIONS_PER_POLL)
        self.assertTrue(first["capped"])

        second = self._poll()
        self.assertEqual(second["assigned"], 977 - engine.MAX_ACTIONS_PER_POLL)
        self.assertFalse(second["capped"])

        logged = self._logged_lead_ids()
        self.assertEqual(sorted(logged), ids)               # every lead, once
        self.assertEqual(len(self.client.assign_calls), 977)  # no double writes
        self.assertNotIn("lead_old", logged)

        third = self._poll()
        self.assertEqual(third["assigned"], 0)

    def test_round_robin_spreads_import_evenly(self):
        start = self.now - timedelta(minutes=1)
        for i in range(300):
            self.client.add_lead(f"lead_{i:03d}", start + timedelta(milliseconds=10 * i))
        self._poll()
        counts = {}
        for _, uid in self.client.assign_calls:
            counts[uid] = counts.get(uid, 0) + 1
        self.assertEqual(counts, {uid: 100 for uid in MEMBER_IDS})

    # -- overlap window -------------------------------------------------------

    def test_overlap_catches_lead_created_just_before_last_poll(self):
        checkpoint = self.lead_list.last_checked_at
        # Created 20s before the last poll but only started matching after it.
        self.client.add_lead("lead_late_match", checkpoint - timedelta(seconds=20))
        # Well outside the overlap: pre-existing, must stay untouched.
        self.client.add_lead("lead_ancient", checkpoint - engine.POLL_OVERLAP - timedelta(minutes=1))

        self._poll()
        self.assertEqual(self._logged_lead_ids(), ["lead_late_match"])

    def test_overlap_never_reassigns_on_overwrite_lists(self):
        self.lead_list.overwrite_existing = True
        db.session.commit()
        self.client.add_lead("lead_1", self.now - timedelta(minutes=2))

        self._poll()
        self._poll()  # lead_1 is still inside the overlap window
        self.assertEqual(len(self.client.assign_calls), 1)
        self.assertEqual(self._logged_lead_ids(), ["lead_1"])

    # -- failures -------------------------------------------------------------

    def test_failed_lead_is_retried_on_next_poll(self):
        self.client.add_lead("lead_fails_once", self.now - timedelta(minutes=30))
        self.client.add_lead("lead_ok", self.now - timedelta(minutes=1))
        # Put the checkpoint far enough back that lead_fails_once is in the window.
        self.lead_list.last_checked_at = self.now - timedelta(hours=1)
        db.session.commit()
        self.client.fail_assign.add("lead_fails_once")

        first = self._poll()
        self.assertEqual(first["errors"], 1)
        self.assertEqual(self._logged_lead_ids(), ["lead_ok"])
        # Checkpoint held so the next window starts at the failed lead,
        # instead of jumping to "now".
        self.assertEqual(engine._poll_window_start(self.lead_list).replace(microsecond=0),
                         (self.now - timedelta(minutes=30)).replace(microsecond=0))

        self.client.fail_assign.clear()
        second = self._poll()
        self.assertEqual(second["assigned"], 1)
        self.assertEqual(sorted(self._logged_lead_ids()), ["lead_fails_once", "lead_ok"])

    def test_held_checkpoint_starts_next_window_at_the_held_lead(self):
        now = datetime(2026, 11, 24, 12, 0)
        overlap = engine.POLL_OVERLAP
        self.assertEqual(engine._next_checkpoint(now, None, None), now)
        # Retries are held back at most MAX_RETRY_HOLDBACK.
        failed_long_ago = now - timedelta(days=2)
        self.assertEqual(
            engine._next_checkpoint(now, None, failed_long_ago) - overlap,
            now - engine.MAX_RETRY_HOLDBACK,
        )
        # Backlog is never clamped; the window starts exactly at it.
        backlog = now - timedelta(days=2)
        self.assertEqual(engine._next_checkpoint(now, backlog, None) - overlap, backlog)
        # The earliest hold wins, and the checkpoint never goes past now.
        self.assertEqual(
            engine._next_checkpoint(now, now - timedelta(hours=1), now - timedelta(hours=2)) - overlap,
            now - timedelta(hours=2),
        )
        self.assertEqual(engine._next_checkpoint(now, now - timedelta(minutes=1), None), now)

    # -- chunked search (Close's cursor stops at 10,000 results) ---------------

    def test_search_walks_past_a_chunk_of_already_actioned_leads(self):
        start = self.now - timedelta(minutes=4)
        ids = [f"lead_{i:03d}" for i in range(200)]
        for i, lead_id in enumerate(ids):
            self.client.add_lead(lead_id, start + timedelta(milliseconds=500 * i))
        # The oldest 120 were already handled by this list (e.g. a held window).
        for lead_id in ids[:120]:
            db.session.add(AssignmentLog(queue_id=self.lead_list.id, close_lead_id=lead_id))
        db.session.commit()

        with mock.patch.object(engine, "MAX_SEARCH_RESULTS", 50):
            result = self._poll()

        self.assertEqual(result["assigned"], 80)
        self.assertEqual(sorted(a for a, _ in self.client.assign_calls), ids[120:])
        self.assertTrue(all(c["max_results"] == 50 for c in self.client.search_calls))

    def test_window_larger_than_one_poll_can_search_resumes_next_poll(self):
        # A big backlog from two hours ago (e.g. Robin was down), larger than
        # one poll's chunk budget.
        self.lead_list.last_checked_at = self.now - timedelta(hours=3)
        db.session.commit()
        start = self.now - timedelta(hours=2)
        ids = [f"lead_{i:03d}" for i in range(260)]
        for i, lead_id in enumerate(ids):
            self.client.add_lead(lead_id, start + timedelta(seconds=i))

        with mock.patch.object(engine, "MAX_SEARCH_RESULTS", 50), \
                mock.patch.object(engine, "MAX_SEARCH_CHUNKS", 2):
            totals = [self._poll()["assigned"] for _ in range(6)]

        self.assertEqual(sum(totals), 260)
        self.assertEqual(totals[-1], 0)
        self.assertEqual(sorted(self._logged_lead_ids()), ids)
        self.assertEqual(len(self.client.assign_calls), 260)

    # -- seeding --------------------------------------------------------------

    def test_seed_starts_list_from_now_without_touching_existing_leads(self):
        self.client.add_lead("lead_old", self.now - timedelta(days=5))
        self.client.add_lead("lead_recent", self.now - timedelta(minutes=3))  # inside overlap

        engine.seed_queue(self.lead_list.id)
        db.session.expire_all()
        self.assertEqual(self.lead_list.seeded_lead_ids, ["lead_recent"])
        self.assertGreaterEqual(self.lead_list.last_checked_at, self.now)

        self.assertEqual(self._poll()["assigned"], 0)

        self.client.add_lead("lead_new", datetime.utcnow() + timedelta(seconds=1))
        self._poll()
        self.assertEqual(self._logged_lead_ids(), ["lead_new"])


if __name__ == "__main__":
    unittest.main()
