"""The desk, journal and session-summary reads against real Postgres (§23 Tier 2).

These cannot be Tier 1. Every one of them is a query -- array aggregation over
``concepts_touched``, an enum cast on a status filter, a window over
``mastery_events`` -- and the failures worth catching are the ones a mock cannot
have: a column that is not there, an enum value Postgres rejects, an
``array_agg`` that returns NULL where a list was expected.

They also check the two things the frontend cannot check for itself, because it
would need a server to find out: that what comes back matches the shapes in
``studium-web/lib/api/schemas.ts``, and that a learner cannot read another
learner's rows.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium import reads
from studium.api.app import app
from studium.api.reads import USER_HEADER

pytestmark = pytest.mark.postgres


@pytest.fixture
def learner(runtime_db: Session):
    """A learner with an enrollment, a closed session, and a journal.

    Built to be *read*, unlike the runtime fixture, which is built to be
    streamed against: a desk with no history renders every empty state and
    proves nothing about the queries.
    """
    from tests.fixtures import lambda_calculus

    fixture = lambda_calculus.build(
        runtime_db, email=f"reads-{uuid.uuid4().hex[:8]}@example.com"
    )
    runtime_db.flush()

    beta = fixture.concept_id("beta-reduction")
    alpha = fixture.concept_id("alpha-equivalence")

    # The plan's head, so "coming up" has somewhere to start from.
    runtime_db.execute(
        sql(
            """
            UPDATE learner_subjects
               SET current_focus_concept_id = :concept_id
             WHERE id = :lsid
            """
        ),
        {"concept_id": fixture.concept_id("syntax"), "lsid": fixture.enrollment.id},
    )

    closed = runtime_db.execute(
        sql(
            """
            INSERT INTO learning_sessions
                (user_id, learner_subject_id, mode, focus_concept_id,
                 target_duration_minutes, started_at, ended_at, end_reason,
                 total_cost_usd)
            VALUES (:user_id, :lsid, 'lecture', :concept_id, 90,
                    NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour',
                    'learner_stop', 0.4212)
            RETURNING id
            """
        ),
        {"user_id": fixture.user.id, "lsid": fixture.enrollment.id, "concept_id": beta},
    ).scalar_one()

    runtime_db.execute(
        sql(
            """
            INSERT INTO session_summaries
                (session_id, summary, key_points, open_threads, concepts_touched,
                 generated_by, cost_usd)
            VALUES (:sid, :summary, '[]'::jsonb,
                    CAST(:threads AS jsonb), CAST(:touched AS uuid[]), 'curator', 0.02)
            """
        ),
        {
            "sid": closed,
            "summary": "We worked through beta-reduction and normal forms.",
            "threads": '["Confluence is still not settled."]',
            "touched": [beta, alpha],
        },
    )

    # Evidence on one of the two touched concepts, so the summary exercises both
    # the delta path and the zero-delta fallback in one call.
    mastery_id = runtime_db.execute(
        sql(
            "SELECT id FROM concept_mastery "
            " WHERE learner_subject_id = :lsid AND concept_id = :cid"
        ),
        {"lsid": fixture.enrollment.id, "cid": beta},
    ).scalar_one()

    for before, after, offset in ((0.10, 0.42, 40), (0.42, 0.71, 20)):
        runtime_db.execute(
            sql(
                """
                INSERT INTO mastery_events
                    (concept_mastery_id, session_id, kind,
                     p_known_before, p_known_after, created_at)
                VALUES (:mid, :sid, 'practice_correct', :before, :after,
                        NOW() - make_interval(mins => :offset))
                """
            ),
            {
                "mid": mastery_id,
                "sid": closed,
                "before": before,
                "after": after,
                "offset": offset,
            },
        )

    runtime_db.execute(
        sql(
            """
            UPDATE concept_mastery
               SET p_known = 0.71, p_known_decayed = 0.71, last_evidence_at = NOW()
             WHERE id = :mid
            """
        ),
        {"mid": mastery_id},
    )

    entries: dict[str, uuid.UUID] = {}
    for slug, concept_id, status, summary, origin in (
        ("open", beta, "open", "Treats reduction order as significant.", "tracker_inferred"),
        ("mine", alpha, "partial", "Capture-avoidance still feels arbitrary.", "learner_flagged"),
        ("done", beta, "resolved", "Confused redex with normal form.", "tracker_inferred"),
    ):
        entry_id = runtime_db.execute(
            sql(
                """
                INSERT INTO journal_entries
                    (user_id, learner_subject_id, concept_id, status, summary,
                     hypothesis, learner_note, origin, last_touched_at)
                VALUES (:user_id, :lsid, :cid, CAST(:status AS journal_status),
                        :summary, :hypothesis, '', :origin, NOW())
                RETURNING id
                """
            ),
            {
                "user_id": fixture.user.id,
                "lsid": fixture.enrollment.id,
                "cid": concept_id,
                "status": status,
                "summary": summary,
                "hypothesis": "Has not internalised confluence.",
                "origin": origin,
            },
        ).scalar_one()
        entries[slug] = entry_id
        runtime_db.execute(
            sql(
                """
                INSERT INTO journal_events (entry_id, session_id, kind, note)
                VALUES (:eid, :sid, 'created', '')
                """
            ),
            {"eid": entry_id, "sid": closed},
        )

    runtime_db.commit()
    return {
        "fixture": fixture,
        "user_id": fixture.user.id,
        "closed_session_id": closed,
        "entries": entries,
        "beta": beta,
        "alpha": alpha,
        "db": runtime_db,
    }


@pytest.fixture
def client(learner):
    """A TestClient whose requests already carry the learner's identity."""
    with TestClient(app) as c:
        c.headers.update({USER_HEADER: str(learner["user_id"])})
        yield c


class TestDesk:
    """§6.1, and the shape `deskResponse` in the client parses."""

    def test_it_answers_the_three_questions_the_desk_asks(self, client, learner):
        body = client.get("/api/user/me/desk").json()

        assert body["learner"]["id"] == str(learner["user_id"])
        assert body["learner"]["display_name"] == "Test Learner"

        # What did I do last.
        [recent] = body["recent_sessions"]
        assert recent["mode"] == "lecture"
        assert recent["duration_minutes"] == pytest.approx(60, abs=1)
        assert sorted(recent["concepts_touched"]) == [
            "Alpha-equivalence and variable capture",
            "Beta-reduction",
        ]

        # What is still open.
        statuses = {e["status"] for e in body["open_journal_entries"]}
        assert statuses == {"open", "partial"}, "a resolved entry is not still open"

        # Where I stand.
        assert body["mastery"], "mastery rows exist and should be summarised"
        assert all(0.0 <= m["p_known_decayed"] <= 1.0 for m in body["mastery"])

    def test_coming_up_starts_after_the_current_focus(self, client):
        """§6.1's "next 3 concepts" is next, not first.

        The plan is ordered and the learner is somewhere in it; showing its head
        would tell a learner three concepts in that they are about to start.
        """
        body = client.get("/api/user/me/desk").json()
        names = [c["name"] for c in body["syllabus_next"]]

        assert len(names) == 3
        assert "Lambda terms: variables, abstraction, application" not in names, (
            "the current focus is not 'coming up'"
        )
        assert names[0] == "Alpha-equivalence and variable capture"

    def test_an_open_session_is_reported_separately_from_the_closed_ones(
        self, client, learner
    ):
        """The continue-card and the history list are different questions."""
        learner["db"].execute(
            sql(
                """
                INSERT INTO learning_sessions
                    (user_id, learner_subject_id, mode, focus_concept_id,
                     target_duration_minutes)
                VALUES (:user_id, :lsid, 'tutorial', :cid, 60)
                """
            ),
            {
                "user_id": learner["user_id"],
                "lsid": learner["fixture"].enrollment.id,
                "cid": learner["beta"],
            },
        )
        learner["db"].commit()

        body = client.get("/api/user/me/desk").json()

        assert body["open_session"] is not None
        assert body["open_session"]["ended_at"] is None
        assert body["open_session"]["mode"] == "tutorial"
        assert all(s["ended_at"] for s in body["recent_sessions"])

    def test_a_desk_with_no_history_still_answers(self, client, runtime_db):
        """Every empty state at once. §3: no surface may fail to render."""
        from tests.fixtures import lambda_calculus

        fresh = lambda_calculus.build(
            runtime_db, email=f"fresh-{uuid.uuid4().hex[:8]}@example.com"
        )
        runtime_db.commit()

        body = client.get(
            "/api/user/me/desk", headers={USER_HEADER: str(fresh.user.id)}
        ).json()

        assert body["open_session"] is None
        assert body["recent_sessions"] == []
        assert body["open_journal_entries"] == []
        assert len(body["syllabus_next"]) == 3, "no focus yet means the plan's head"

    def test_an_unknown_learner_is_a_404_not_an_empty_desk(self, client):
        assert client.get(
            "/api/user/me/desk", headers={USER_HEADER: str(uuid.uuid4())}
        ).status_code == 404


class TestJournalList:
    def test_the_default_view_is_every_entry_across_subjects(self, client):
        """§6.4: "all open, partial, and resolved entries across the learner's
        subjects" -- so no enrollment id is required to ask."""
        entries = client.get("/api/user/me/journal").json()
        assert len(entries) == 3

    def test_status_filters(self, client):
        entries = client.get("/api/user/me/journal?status=open,partial").json()
        assert {e["status"] for e in entries} == {"open", "partial"}

    def test_an_unknown_status_is_rejected_rather_than_ignored(self, client):
        """A filter Postgres would reject on cast fails as a 422 here.

        Silently dropping it would answer a different question than the one the
        filter panel is showing.
        """
        assert client.get("/api/user/me/journal?status=urgent").status_code == 422

    def test_concept_and_date_filters(self, client, learner):
        by_concept = client.get(
            f"/api/user/me/journal?concept_ids={learner['alpha']}"
        ).json()
        assert len(by_concept) == 1
        assert by_concept[0]["concept_name"] == "Alpha-equivalence and variable capture"

        future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()
        assert client.get("/api/user/me/journal", params={"since": future}).json() == []

    def test_a_since_whose_plus_was_eaten_by_the_query_string_still_parses(
        self, client
    ):
        """An unencoded `+00:00` arrives as ` 00:00`, and means the same instant.

        Rejecting it would answer a filter the learner set with a 422 that
        blamed them for their client's encoding.
        """
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()
        response = client.get(f"/api/user/me/journal?since={future}")
        assert response.status_code == 200
        assert response.json() == []

    def test_a_since_that_is_not_a_date_is_still_rejected(self, client):
        assert client.get("/api/user/me/journal?since=yesterday").status_code == 422

    def test_search_is_a_substring_over_what_the_learner_can_see(self, client):
        """§6.4: "Not fuzzy search -- a substring match is sufficient"."""
        hits = client.get("/api/user/me/journal?q=capture").json()
        assert len(hits) == 1
        assert "Capture-avoidance" in hits[0]["summary"]

        assert client.get("/api/user/me/journal?q=confluence").json() == [], (
            "the hypothesis is not shown, so it must not be searched"
        )

    def test_summary_authorship_is_derived_from_origin(self, client):
        """§12.1 attributes the summary; the table records `origin` (§6.7)."""
        by_summary = {e["summary"]: e for e in client.get("/api/user/me/journal").json()}
        assert by_summary["Capture-avoidance still feels arbitrary."][
            "summary_author"
        ] == "learner"
        assert by_summary["Treats reduction order as significant."][
            "summary_author"
        ] == "tutor"

    def test_the_trackers_hypothesis_is_not_served(self, client):
        """Data layer §11 / C5: never serialised to a learner. See F16."""
        assert all(e["hypothesis"] is None for e in client.get("/api/user/me/journal").json())


class TestJournalEntry:
    def test_the_detail_carries_the_history_timeline(self, client, learner):
        body = client.get(f"/api/journal/{learner['entries']['open']}").json()
        assert body["summary"].startswith("Treats reduction order")
        assert [event["kind"] for event in body["history"]] == ["created"]
        assert body["history"][0]["session_id"] == str(learner["closed_session_id"])

    def test_resolving_writes_the_event_that_explains_it(self, client, learner):
        """§6.4's timeline is what makes a resolved entry account for itself."""
        entry_id = learner["entries"]["open"]
        patched = client.patch(
            f"/api/journal/{entry_id}", json={"status": "resolved"}
        ).json()
        assert patched["status"] == "resolved"

        history = client.get(f"/api/journal/{entry_id}").json()["history"]
        assert [event["kind"] for event in history] == ["created", "resolved"]

    def test_reopening_clears_the_resolution_date(self, client, learner):
        entry_id = learner["entries"]["done"]
        client.patch(f"/api/journal/{entry_id}", json={"status": "resolved"})
        client.patch(f"/api/journal/{entry_id}", json={"status": "open"})

        resolved_at = learner["db"].execute(
            sql("SELECT resolved_at FROM journal_entries WHERE id = :id"),
            {"id": entry_id},
        ).scalar()
        assert resolved_at is None, "a reopened entry must not keep a resolution date"

    def test_the_note_saves_and_records_itself(self, client, learner):
        entry_id = learner["entries"]["mine"]
        body = client.patch(
            f"/api/journal/{entry_id}", json={"learner_note": "Try the capture example."}
        ).json()
        assert body["learner_note"] == "Try the capture example."

        kinds = [e["kind"] for e in client.get(f"/api/journal/{entry_id}").json()["history"]]
        assert "learner_note_added" in kinds

    def test_an_empty_patch_is_rejected(self, client, learner):
        response = client.patch(f"/api/journal/{learner['entries']['open']}", json={})
        assert response.status_code == 422

    def test_the_hypothesis_cannot_be_written_through_this_endpoint(
        self, client, learner
    ):
        """It is the Tracker's inference. No request shape reaches it."""
        entry_id = learner["entries"]["open"]
        client.patch(
            f"/api/journal/{entry_id}",
            json={"hypothesis": "Actually they understand it fine.", "status": "partial"},
        )
        stored = learner["db"].execute(
            sql("SELECT hypothesis FROM journal_entries WHERE id = :id"),
            {"id": entry_id},
        ).scalar()
        assert stored == "Has not internalised confluence."


class TestSessionSummary:
    def test_it_returns_what_the_close_modal_shows(self, client, learner):
        body = client.get(
            f"/api/session/{learner['closed_session_id']}/summary"
        ).json()

        assert body["summary"].startswith("We worked through")
        assert body["open_threads"] == ["Confluence is still not settled."]
        assert body["duration_minutes"] == pytest.approx(60, abs=1)
        assert body["cost_usd"] == pytest.approx(0.4212)

    def test_mastery_deltas_span_the_whole_session(self, client, learner):
        """§6.5's "beta-reduction: 0.65 → 0.82".

        Two events on one concept: the delta is the first event's *before* and
        the last event's *after*, not the last event alone.
        """
        body = client.get(
            f"/api/session/{learner['closed_session_id']}/summary"
        ).json()
        deltas = {d["concept_name"]: d for d in body["concepts_touched"]}

        beta = deltas["Beta-reduction"]
        assert beta["before"] == pytest.approx(0.10, abs=0.001)
        assert beta["after"] == pytest.approx(0.71, abs=0.001)

    def test_a_concept_touched_without_evidence_is_still_listed(self, client, learner):
        """"What we covered" is not "what you were graded on"."""
        body = client.get(
            f"/api/session/{learner['closed_session_id']}/summary"
        ).json()
        deltas = {d["concept_name"]: d for d in body["concepts_touched"]}

        alpha = deltas["Alpha-equivalence and variable capture"]
        assert alpha["before"] == alpha["after"]

    def test_the_next_focus_is_the_curators_choice(self, client, learner):
        body = client.get(
            f"/api/session/{learner['closed_session_id']}/summary"
        ).json()
        assert body["next_focus_concept_name"] == (
            "Lambda terms: variables, abstraction, application"
        )

    def test_an_unsummarised_session_is_a_404(self, client, learner):
        """Distinct from "no such session": the close endpoint already reports
        `summarised: false`, and the client has copy for it."""
        open_id = learner["db"].execute(
            sql(
                """
                INSERT INTO learning_sessions
                    (user_id, learner_subject_id, mode, target_duration_minutes)
                VALUES (:user_id, :lsid, 'tutorial', 60)
                RETURNING id
                """
            ),
            {"user_id": learner["user_id"], "lsid": learner["fixture"].enrollment.id},
        ).scalar_one()
        learner["db"].commit()

        assert client.get(f"/api/session/{open_id}/summary").status_code == 404


class TestScoping:
    """One learner cannot read another's rows.

    There is no authentication behind the header (F7), so this is not a claim
    that the API is secure. It is the narrower, checkable claim that the queries
    filter -- which is what stops a mistyped id from returning someone else's
    journal to an honest caller, and what a route that forgot `user_id` would
    fail here rather than in production.
    """

    @pytest.fixture
    def stranger(self, runtime_db: Session):
        from tests.fixtures import lambda_calculus

        other = lambda_calculus.build(
            runtime_db, email=f"stranger-{uuid.uuid4().hex[:8]}@example.com"
        )
        runtime_db.commit()
        return other

    def test_another_learners_entry_is_a_404_not_a_403(self, client, learner, stranger):
        """A 403 would confirm the entry exists to someone who does not own it."""
        response = client.get(
            f"/api/journal/{learner['entries']['open']}",
            headers={USER_HEADER: str(stranger.user.id)},
        )
        assert response.status_code == 404

    def test_another_learners_entry_cannot_be_patched(self, client, learner, stranger):
        response = client.patch(
            f"/api/journal/{learner['entries']['open']}",
            json={"status": "archived"},
            headers={USER_HEADER: str(stranger.user.id)},
        )
        assert response.status_code == 404

    def test_another_learners_summary_is_a_404(self, client, learner, stranger):
        response = client.get(
            f"/api/session/{learner['closed_session_id']}/summary",
            headers={USER_HEADER: str(stranger.user.id)},
        )
        assert response.status_code == 404

    def test_the_journal_list_shows_only_your_own(self, client, learner, stranger):
        entries = client.get(
            "/api/user/me/journal", headers={USER_HEADER: str(stranger.user.id)}
        ).json()
        assert entries == []


class TestIdentityHeader:
    def test_a_missing_header_is_a_401_rather_than_a_default_learner(self):
        with TestClient(app) as bare:
            assert bare.get("/api/user/me/desk").status_code == 401

    def test_a_malformed_header_is_a_400(self):
        with TestClient(app) as bare:
            response = bare.get(
                "/api/user/me/desk", headers={USER_HEADER: "not-a-uuid"}
            )
            assert response.status_code == 400


class TestHypothesisPolicy:
    def test_the_policy_is_one_constant_and_the_projection_agrees_with_it(self):
        """F16 is a decision, and a reversible one.

        Data layer §11 says the hypothesis never reaches a learner; frontend
        §6.4 asks for it with framing. The conflict is resolved in favour of the
        projection that already shipped, and this asserts the resolution lives
        in exactly one place rather than being spread across the queries.
        """
        from studium.acl import Role, project_journal_entry

        assert reads.SERVE_HYPOTHESIS_TO_LEARNER is False
        assert "hypothesis" not in project_journal_entry(
            {"summary": "s", "hypothesis": "h"}, Role.LEARNER
        )
