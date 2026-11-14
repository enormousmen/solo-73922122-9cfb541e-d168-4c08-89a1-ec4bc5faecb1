"""Functional tests for the read-only old/new meter-id matching API."""

from __future__ import annotations

from tests.common import at, reading


def profile(
    meter: str,
    prefix: str,
    usages: list[int],
    start: int = 0,
    step: int = 60,
    base: int = 1000,
    replaced_at: int | None = None,
) -> list[dict]:
    """Readings whose consecutive intervals produce exactly `usages`.

    ``replaced_at`` marks the reading with that index (1-based sample number)
    as a meter replacement, turning the interval ending there into a break.
    """
    readings = [reading(meter, f"{prefix}-0", start, base)]
    value = base
    for index, usage in enumerate(usages):
        value += usage
        readings.append(
            reading(
                meter,
                f"{prefix}-{index + 1}",
                start + (index + 1) * step,
                value,
                replaced=(replaced_at == index + 1),
            )
        )
    return readings


def post(client, readings) -> None:
    resp = client.post("/api/v1/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text


def match(client, old, new, start=0, end=360):
    return client.post(
        "/api/v1/meters/match",
        json={
            "old_meter_ids": old,
            "new_meter_ids": new,
            "window": {"from": at(start), "to": at(end)},
        },
    )


def seed_two_pairs(client, usages_a=(10, 20, 30), usages_b=(1, 2, 3)) -> None:
    post(client, profile("O1", "o1", list(usages_a)))
    post(client, profile("O2", "o2", list(usages_b)))
    post(client, profile("N1", "n1", list(usages_a)))
    post(client, profile("N2", "n2", list(usages_b)))


def test_unique_matching_returns_all_pairs_with_evidence(client):
    post(client, profile("O1", "o1", [10, 20, 30, 40]))
    post(client, profile("O2", "o2", [3, 1, 4, 1], base=500))
    # New ids report the same usage profile from a different counter base.
    post(client, profile("N1", "n1", [3, 1, 4, 1], base=700))  # mirrors O2
    post(client, profile("N2", "n2", [10, 20, 30, 40], base=2000))  # mirrors O1

    resp = match(client, ["O1", "O2"], ["N1", "N2"], end=240)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "matched"
    assert body["reason"] is None
    assert body["commit_seq"] == client.get("/health").json()["commit_seq"]
    assert body["window"] == {"from": at(0), "to": at(240), "applied": True}

    pairs = {pair["old_meter_id"]: pair for pair in body["pairs"]}
    assert set(pairs) == {"O1", "O2"}
    assert pairs["O1"]["new_meter_id"] == "N2"
    assert pairs["O2"]["new_meter_id"] == "N1"

    evidence = pairs["O1"]["evidence"]
    assert len(evidence) == 3
    assert [seg["usage"] for seg in evidence] == [10, 20, 30]
    # Consecutive: each segment starts where the previous one ended.
    assert evidence[0]["to_read_at"] == evidence[1]["from_read_at"]
    assert evidence[1]["to_read_at"] == evidence[2]["from_read_at"]
    # Source record ids point at the stored readings of both sides.
    first = evidence[0]
    assert first["old_from_source_record_id"] == "o1-0"
    assert first["old_to_source_record_id"] == "o1-1"
    assert first["new_from_source_record_id"] == "n2-0"
    assert first["new_to_source_record_id"] == "n2-1"


def test_evidence_matches_interval_source_records(client):
    seed_two_pairs(client)
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "matched"
    pair = next(p for p in body["pairs"] if p["old_meter_id"] == "O1")
    intervals = client.get("/api/v1/meters/O1").json()["intervals"]
    assert len(pair["evidence"]) == 3
    for segment, interval in zip(pair["evidence"], intervals):
        assert segment["from_read_at"] == interval["from_read_at"]
        assert segment["to_read_at"] == interval["to_read_at"]
        assert segment["usage"] == interval["usage"]
        assert segment["old_from_source_record_id"] == interval["from_source_record_id"]
        assert segment["old_to_source_record_id"] == interval["to_source_record_id"]


def test_matching_is_read_only(client):
    seed_two_pairs(client)
    before = client.get("/health").json()["commit_seq"]
    for _ in range(3):
        body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
        assert body["status"] == "matched"
    assert client.get("/health").json()["commit_seq"] == before


def test_maximum_of_six_pairs(client):
    readings = []
    for i in range(6):
        usages = [i + 1, i + 2, i + 3]
        readings += profile(f"O{i}", f"o{i}", usages)
        readings += profile(f"N{i}", f"n{i}", usages)
    post(client, readings)
    body = match(
        client, [f"O{i}" for i in range(6)], [f"N{i}" for i in range(6)]
    ).json()
    assert body["status"] == "matched"
    assert len(body["pairs"]) == 6
    for pair in body["pairs"]:
        assert pair["new_meter_id"] == "N" + pair["old_meter_id"][1:]


def test_ambiguous_matching_reports_reason_without_pairs(client):
    post(client, profile("O1", "o1", [10, 20, 30]))
    post(client, profile("O2", "o2", [10, 20, 30]))
    post(client, profile("N1", "n1", [10, 20, 30]))
    post(client, profile("N2", "n2", [10, 20, 30]))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "ambiguous"
    assert body["pairs"] == []
    assert body["reason"]
    assert body["details"]["possible_matching_count"] == 2


def test_no_complete_matching_when_candidates_cannot_cover_all(client):
    # O1 and O2 both only match N1; O3 matches N2/N3.  Every meter has at
    # least one candidate, yet no one-to-one assignment covers all three.
    post(client, profile("O1", "o1", [10, 20, 30]))
    post(client, profile("O2", "o2", [10, 20, 30]))
    post(client, profile("O3", "o3", [1, 2, 3]))
    post(client, profile("N1", "n1", [10, 20, 30]))
    post(client, profile("N2", "n2", [1, 2, 3]))
    post(client, profile("N3", "n3", [1, 2, 3]))
    body = match(client, ["O1", "O2", "O3"], ["N1", "N2", "N3"]).json()
    assert body["status"] == "no_complete_matching"
    assert body["pairs"] == []
    assert body["reason"]


def test_insufficient_evidence_when_meter_has_no_readings(client):
    post(client, profile("O1", "o1", [10, 20, 30]))
    post(client, profile("N1", "n1", [10, 20, 30]))
    post(client, profile("N2", "n2", [1, 2, 3]))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "insufficient_evidence"
    assert body["pairs"] == []
    assert "O2" in body["reason"]
    assert body["details"]["old_meters_without_candidate"] == ["O2"]


def test_two_consecutive_segments_are_not_enough(client):
    seed_two_pairs(client, usages_a=(10, 20), usages_b=(1, 2, 3))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "insufficient_evidence"
    assert body["pairs"] == []
    assert "O1" in body["reason"]


def test_constant_usage_does_not_count_as_evidence(client):
    seed_two_pairs(client, usages_a=(5, 5, 5, 5), usages_b=(1, 2, 3))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "insufficient_evidence"
    assert "O1" in body["reason"]


def test_single_positive_value_is_not_enough(client):
    seed_two_pairs(client, usages_a=(0, 0, 5), usages_b=(1, 2, 3))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "insufficient_evidence"
    assert "O1" in body["reason"]


def test_zero_usage_segment_can_complete_a_triple(client):
    seed_two_pairs(client, usages_a=(0, 5, 7), usages_b=(1, 2, 3))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "matched"


def test_usage_conflict_forbids_pair(client):
    post(client, profile("O1", "o1", [10, 20, 30]))
    post(client, profile("O2", "o2", [1, 2, 3]))
    post(client, profile("N1", "n1", [10, 99, 30]))  # conflicts with O1
    post(client, profile("N2", "n2", [1, 2, 3]))
    body = match(client, ["O1", "O2"], ["N1", "N2"]).json()
    assert body["status"] == "insufficient_evidence"
    assert "O1" in body["reason"]
    assert "N1" in body["reason"]


def test_break_interrupts_consecutive_evidence(client):
    # O1's replacement break splits what would be four equal segments into
    # runs of one and two common valid intervals — never three in a row.
    post(client, profile("O1", "o1", [10, 20, 30, 40], replaced_at=2))
    post(client, profile("O2", "o2", [1, 2, 3]))
    post(client, profile("N1", "n1", [10, 20, 30, 40]))
    post(client, profile("N2", "n2", [1, 2, 3]))
    body = match(client, ["O1", "O2"], ["N1", "N2"], end=240).json()
    assert body["status"] == "insufficient_evidence"
    assert "O1" in body["reason"]


def test_missing_sample_interrupts_consecutive_evidence(client):
    post(client, profile("O1", "o1", [10, 20, 30, 40]))
    post(client, profile("O2", "o2", [1, 2, 3]))
    n1 = profile("N1", "n1", [10, 20, 30, 40])
    del n1[2]  # N1 skips the sample at minute 120
    post(client, n1)
    post(client, profile("N2", "n2", [1, 2, 3]))
    body = match(client, ["O1", "O2"], ["N1", "N2"], end=240).json()
    assert body["status"] == "insufficient_evidence"
    assert "N1" in body["reason"]


def test_window_restricts_evidence(client):
    post(client, profile("O1", "o1", [10, 20, 30, 40]))
    post(client, profile("O2", "o2", [1, 2, 3]))
    post(client, profile("N1", "n1", [10, 20, 30, 40]))
    post(client, profile("N2", "n2", [1, 2, 3]))
    assert match(client, ["O1", "O2"], ["N1", "N2"], 0, 240).json()["status"] == "matched"
    # Only two intervals are fully inside the narrower window.
    body = match(client, ["O1", "O2"], ["N1", "N2"], 60, 180).json()
    assert body["status"] == "insufficient_evidence"


def test_window_accepts_offset_and_naive_timestamps(client):
    seed_two_pairs(client)
    resp = client.post(
        "/api/v1/meters/match",
        json={
            "old_meter_ids": ["O1", "O2"],
            "new_meter_ids": ["N1", "N2"],
            "window": {
                "from": "2026-01-01T08:00:00+08:00",  # == 2026-01-01T00:00:00Z
                "to": "2026-01-01T06:00:00",  # naive -> UTC
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "matched"
    assert body["window"]["from"] == at(0)
    assert body["window"]["to"] == at(360)


def test_request_validation(client):
    post(client, profile("O1", "o1", [10, 20, 30]))
    ok = {
        "old_meter_ids": ["O1", "O2"],
        "new_meter_ids": ["N1", "N2"],
        "window": {"from": at(0), "to": at(180)},
    }
    bad = [
        {**ok, "new_meter_ids": ["N1"]},  # count mismatch
        {**ok, "old_meter_ids": ["O1"]},  # fewer than 2
        {**ok, "old_meter_ids": [f"O{i}" for i in range(7)]},  # more than 6
        {**ok, "old_meter_ids": ["O1", "O1"]},  # duplicates
        {**ok, "new_meter_ids": ["O1", "N2"]},  # old id reused as new id
        {**ok, "old_meter_ids": ["O1", "  "]},  # blank id
        {**ok, "window": {"from": at(180), "to": at(0)}},  # inverted window
        {**ok, "window": {"from": at(0), "to": at(24 * 60 + 1)}},  # > 24h
        {**ok, "window": {"from": "not-a-date", "to": at(60)}},
    ]
    for payload in bad:
        resp = client.post("/api/v1/meters/match", json=payload)
        assert resp.status_code == 422, payload

    # Exactly 24 hours is accepted.
    resp = client.post(
        "/api/v1/meters/match",
        json={**ok, "window": {"from": at(0), "to": at(24 * 60)}},
    )
    assert resp.status_code == 200


def test_query_window_echoes_from_boundary(client):
    """Regression: a windowed query must echo the applied `from` boundary."""
    post(client, profile("M1", "m1", [10, 20, 30]))
    body = client.get(
        "/api/v1/meters/M1", params={"from": at(0), "to": at(180)}
    ).json()
    assert body["window"] == {"from": at(0), "to": at(180), "applied": True}
