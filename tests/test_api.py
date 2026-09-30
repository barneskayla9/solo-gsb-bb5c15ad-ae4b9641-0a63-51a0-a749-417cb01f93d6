"""End-to-end API tests against an in-file SQLite database (see conftest.py).

SQLite ignores ``SELECT ... FOR UPDATE``; the test client serialises
requests in-process, which is sufficient to verify revision logic (two
sequential approvals sharing one stale revision reproduce the concurrent
outcome).
"""
from fastapi.testclient import TestClient  # noqa: F401  (kept for type hints)


def body(
    circle=(0, 0, 10),
    freq=(100, 110),
    start="2026-10-01T08:00:00Z",
    end="2026-10-01T10:00:00Z",
    name="Event",
):
    from factories import make_payload

    return make_payload(circle, freq, start, end, name)


class TestValidation:
    def test_non_positive_radius_rejected(self, client):
        r = client.post("/api/applications", json=body(circle=(0, 0, 0)))
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "radius_not_positive" in codes

    def test_negative_radius_rejected(self, client):
        r = client.post("/api/applications", json=body(circle=(0, 0, -3)))
        assert r.status_code == 422

    def test_inverted_band_rejected(self, client):
        r = client.post("/api/applications", json=body(freq=(120, 100)))
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "frequency_low_above_high" in codes

    def test_empty_time_window_rejected(self, client):
        r = client.post(
            "/api/applications",
            json=body(
                start="2026-10-01T08:00:00Z", end="2026-10-01T08:00:00Z"
            ),
        )
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "time_start_not_before_end" in codes

    def test_multiple_violations_all_reported(self, client):
        r = client.post(
            "/api/applications",
            json=body(
                circle=(0, 0, -1),
                freq=(200, 100),
                start="2026-10-01T09:00:00Z",
                end="2026-10-01T08:00:00Z",
            ),
        )
        assert r.status_code == 422
        codes = {d["code"] for d in r.json()["error"]["details"]}
        assert {
            "radius_not_positive",
            "frequency_low_above_high",
            "time_start_not_before_end",
        } <= codes

    def test_naive_timestamp_rejected(self, client):
        r = client.post(
            "/api/applications",
            json=body(start="2026-10-01T08:00:00", end="2026-10-01T10:00:00Z"),
        )
        assert r.status_code == 422

    def test_boundary_values_accepted(self, client):
        # single-frequency closed band [100,100] is legal
        r = client.post("/api/applications", json=body(freq=(100, 100)))
        assert r.status_code == 201, r.text


class TestFlow:
    def test_full_happy_path_and_history(self, client):
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/revision/current").json()["current_revision"] == 0

        # submit A and approve it at revision 1
        a = client.post("/api/applications", json=body(name="A")).json()
        prev = client.post(f"/api/applications/{a['id']}/preview").json()
        assert prev["current_revision"] == 0
        assert prev["has_conflicts"] is False

        ok = client.post(
            f"/api/applications/{a['id']}/approve",
            json={"expected_revision": 0},
        ).json()
        assert ok["approved"] is True and ok["revision"] == 1

        # B overlaps A in time/place/frequency
        b = client.post("/api/applications", json=body(name="B")).json()
        prev_b = client.post(f"/api/applications/{b['id']}/preview").json()
        assert prev_b["current_revision"] == 1
        assert prev_b["has_conflicts"] is True
        conflict = prev_b["conflicts"][0]
        assert conflict["application"]["id"] == a["id"]
        assert conflict["reason"] == "frequency_intersection"
        assert conflict["trigger"] == {
            "time_intersect": True,
            "area_intersect": True,
            "frequency_intersect": True,
            "minimum_frequency_gap": 0.0,
            "within_guard_band": True,
        }

        # stale revision is refused
        stale = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_revision"

        # current revision with interference is refused
        blocked = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 1},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "interference"
        assert blocked.json()["error"]["details"]["conflicts"][0]["application"][
            "id"
        ] == a["id"]

        # as-of revision 0 has no events; revision 1 shows A's approval
        snap0 = client.get("/api/revision/0").json()
        assert snap0["approvals"] == [] and snap0["revocations"] == []
        snap1 = client.get("/api/revision/1").json()
        assert len(snap1["approvals"]) == 1
        assert snap1["effective_application_ids"] == [a["id"]]

        # revoke A at revision 2; history at revision 1 is unchanged
        rev = client.post(
            f"/api/applications/{a['id']}/revoke", json={"reason": "event moved"}
        ).json()
        assert rev["revision"] == 2
        snap1_again = client.get("/api/revision/1").json()
        assert snap1_again["effective_application_ids"] == [a["id"]]

        # B now sees an empty effective set and can be approved (revision 3)
        prev_b2 = client.post(f"/api/applications/{b['id']}/preview").json()
        assert prev_b2["current_revision"] == 2
        assert prev_b2["has_conflicts"] is False
        ok_b = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 2},
        ).json()
        assert ok_b["revision"] == 3

        # snapshot at 2 records both the approval and the revocation
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == []
        assert [r["application_id"] for r in snap2["revocations"]] == [a["id"]]
        assert snap2["approvals"][0]["revoked_at_revision"] == 2

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [b["id"]]

        # cannot approve twice / revoke twice
        again = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 3},
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "not_pending"
        rev_again = client.post(f"/api/applications/{a['id']}/revoke", json={})
        assert rev_again.status_code == 409
        assert rev_again.json()["error"]["code"] == "already_revoked"

    def test_guard_band_rule_and_touching_time(self, client):
        a = client.post(
            "/api/applications", json=body(name="A", freq=(100, 110))
        ).json()
        client.post(
            f"/api/applications/{a['id']}/approve",
            json={"expected_revision": 0},
        ).raise_for_status()

        # gap 1.0 < guard 2.0 -> conflict even though closed bands are disjoint
        near = client.post(
            "/api/applications", json=body(name="near", freq=(111, 112))
        ).json()
        r = client.post(f"/api/applications/{near['id']}/preview").json()
        assert r["has_conflicts"] is True
        c = r["conflicts"][0]
        assert c["reason"] == "within_guard_band"
        assert c["trigger"]["frequency_intersect"] is False
        assert c["trigger"]["minimum_frequency_gap"] == 1.0

        # gap exactly 2.0 -> allowed; move its window adjacent to A's end
        safe = client.post(
            "/api/applications",
            json=body(
                name="safe",
                freq=(112, 120),
                start="2026-10-01T10:00:00Z",  # == A end, half-open => no overlap
                end="2026-10-01T12:00:00Z",
            ),
        ).json()
        prev = client.post(f"/api/applications/{safe['id']}/preview").json()
        assert prev["has_conflicts"] is False
        approved = client.post(
            f"/api/applications/{safe['id']}/approve",
            json={"expected_revision": 1},
        )
        assert approved.status_code == 200, approved.text

    def test_frequency_endpoint_touch_is_conflict(self, client):
        a = client.post(
            "/api/applications", json=body(freq=(100, 110))
        ).json()
        client.post(
            f"/api/applications/{a['id']}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        b = client.post(
            "/api/applications", json=body(freq=(110, 120))
        ).json()
        r = client.post(f"/api/applications/{b['id']}/preview").json()
        assert r["has_conflicts"] is True
        assert r["conflicts"][0]["reason"] == "frequency_intersection"

    def test_disjoint_area_no_conflict(self, client):
        a = client.post(
            "/api/applications", json=body(circle=(0, 0, 5))
        ).json()
        client.post(
            f"/api/applications/{a['id']}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        b = client.post(
            "/api/applications", json=body(circle=(100, 100, 5))
        ).json()
        r = client.post(f"/api/applications/{b['id']}/preview").json()
        assert r["has_conflicts"] is False

    def test_unknown_application(self, client):
        assert client.get("/api/applications/999").status_code == 404
        assert client.post("/api/applications/999/preview").status_code == 404
        assert (
            client.post(
                "/api/applications/999/approve",
                json={"expected_revision": 0},
            ).status_code
            == 404
        )

    def test_future_revision_rejected(self, client):
        r = client.get("/api/revision/7")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "revision_out_of_range"


class TestBatchApprove:
    BATCH = "/api/applications/batch-approve"

    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def test_empty_list_rejected(self, client):
        r = client.post(
            self.BATCH, json={"expected_revision": 0, "application_ids": []}
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_failed"

    def test_duplicate_ids_rejected(self, client):
        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [1, 2, 1]},
        )
        assert r.status_code == 422
        details = r.json()["error"]["details"]
        assert details[0]["code"] == "duplicate_application_id"
        assert details[0]["duplicates"] == [1]
        # no revision consumed
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_unknown_id_rejects_whole_batch(self, client):
        a = self._submit(client, "A")
        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, 999]},
        )
        assert r.status_code == 404
        details = r.json()["error"]["details"]
        assert details["missing_application_ids"] == [999]
        # the valid member stays pending and no revision was consumed
        assert client.get(f"/api/applications/{a}").json()["status"] == "pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_happy_path_consumes_consecutive_revisions_in_order(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [c, a, b]},
        )
        assert r.status_code == 200, r.text
        body_ = r.json()
        assert body_["approved"] is True
        assert body_["from_revision"] == 0
        assert body_["revision"] == 3
        assert body_["to_revision"] == 3
        assert body_["application_ids"] == [c, a, b]
        assert [x["application_id"] for x in body_["results"]] == [c, a, b]
        assert [x["revision"] for x in body_["results"]] == [1, 2, 3]
        for result, expected in zip(body_["results"], ("approved",) * 3):
            assert result["application"]["status"] == expected

        # as-of each assigned revision restores exactly the members approved
        # up to and including that item
        snap1 = client.get("/api/revision/1").json()
        assert snap1["effective_application_ids"] == [c]
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == [c, a]
        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == [c, a, b]

    def test_stale_revision_rejects_whole_batch(self, client):
        a = self._submit(client, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [b, c]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 0, "current_revision": 1}
        # nothing partially approved, revision unchanged
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["effective_application_ids"] == [a]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert client.get(f"/api/applications/{c}").json()["status"] == "pending"

    def test_status_mismatch_rejects_whole_batch(self, client):
        a = self._submit(client, "A")
        b = self._submit(client, "B", freq=(200, 201))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()

        r = client.post(
            self.BATCH,
            json={"expected_revision": 1, "application_ids": [a, b]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "not_pending"
        assert err["details"]["applications"] == [
            {
                "application_id": a,
                "status": "approved",
                "approval_revision": 1,
            }
        ]
        # b is untouched
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 1

    def test_interference_against_effective_set_rejects_whole_batch(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        clashing = self._submit(client, "clash", freq=(105, 106))
        clean = self._submit(client, "clean", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 1, "application_ids": [clashing, clean]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        failed = err["details"]["applications"]
        assert [x["application_id"] for x in failed] == [clashing]
        (conflict,) = failed[0]["conflicts"]
        assert conflict["scope"] == "effective"
        assert conflict["application"]["id"] == a
        assert conflict["reason"] == "frequency_intersection"
        # neither member approved
        assert (
            client.get(f"/api/applications/{clashing}").json()["status"]
            == "pending"
        )
        assert client.get(f"/api/applications/{clean}").json()["status"] == "pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 1

    def test_intra_group_interference_reported_for_both_sides(self, client):
        a = self._submit(client, "A", freq=(600, 610))
        b = self._submit(client, "B", freq=(605, 615))
        c = self._submit(client, "C", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, b, c]},
        )
        assert r.status_code == 409
        failed = r.json()["error"]["details"]["applications"]
        assert [x["application_id"] for x in failed] == [a, b]
        sides = {
            x["application_id"]: [
                (k["scope"], k["application"]["id"]) for k in x["conflicts"]
            ]
            for x in failed
        }
        assert sides[a] == [("group", b)]
        assert sides[b] == [("group", a)]
        # all members still pending, no revisions consumed
        for app_id in (a, b, c):
            assert (
                client.get(f"/api/applications/{app_id}").json()["status"]
                == "pending"
            )
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_batch_and_single_share_revision_arbitration(self, client):
        # A single approval advances the revision after the batch was based on
        # it; the batch must then be stale, never partially approved.
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))

        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [b, c]},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"

        # re-deciding on the fresh revision succeeds and continues the sequence
        r2 = client.post(
            self.BATCH,
            json={"expected_revision": 1, "application_ids": [b, c]},
        )
        assert r2.status_code == 200, r2.text
        assert [x["revision"] for x in r2.json()["results"]] == [2, 3]

    def test_two_batches_same_revision_one_wins_one_stale(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))
        d = self._submit(client, "D", freq=(400, 401))

        first = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, b]},
        )
        assert first.status_code == 200
        # same expected revision, as if launched concurrently
        second = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [c, d]},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "stale_revision"
        assert second.json()["error"]["details"]["current_revision"] == 2
        for app_id in (c, d):
            assert (
                client.get(f"/api/applications/{app_id}").json()["status"]
                == "pending"
            )
        # retry on the new revision succeeds and keeps revisions consecutive
        retry = client.post(
            self.BATCH,
            json={"expected_revision": 2, "application_ids": [c, d]},
        )
        assert retry.status_code == 200
        assert [x["revision"] for x in retry.json()["results"]] == [3, 4]

    def test_revoke_interleaves_with_batch_revisions(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, b]},
        ).raise_for_status()
        # revoke consumes the next shared revision
        rev = client.post(
            f"/api/applications/{a}/revoke", json={"reason": "moved"}
        ).json()
        assert rev["revision"] == 3

        c = self._submit(client, "C", freq=(100, 101))
        r = client.post(
            f"/api/applications/{c}/approve", json={"expected_revision": 3}
        )
        assert r.status_code == 200 and r.json()["revision"] == 4

    def test_revoked_member_rejected_with_already_revoked(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        client.post(
            f"/api/applications/{a}/revoke", json={"reason": "moved"}
        ).raise_for_status()
        b = self._submit(client, "B", freq=(200, 201))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 2, "application_ids": [a, b]},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
