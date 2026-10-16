"""
Who each stretch of a recording belongs to - the rules, scenario by scenario.

Every case here is one the clinic described: a wrong profile picked at the
start, a deliberate click on another file, a glance mid-consultation, and
follow-ups that need no prescription followed by the next patient.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))

import labelling as lab                          # noqa: E402
from labelling import Event, resolve, same_cut   # noqa: E402

T0 = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def v(pid):
    return lab.Visit(patient_id=pid, doctor_id="DR1", cmed_hospital_id="AALO",
                     start_time=f"2026-10-16T10:00:00+06:00#{pid}", visit_date="2026-10-16")


def ev(seconds, pid, kind=lab.PATIENT_OPENED, n=None):
    return Event(seconds=seconds, kind=kind, visit=v(pid),
                 at=T0 + timedelta(seconds=seconds), grant_jti=f"g-{pid}",
                 entry_no=n if n is not None else int(seconds * 10) + 1)


def opened(pid):
    return Event(seconds=0.0, kind=lab.OPEN, visit=v(pid), at=T0, grant_jti=f"g-{pid}")


def rx(seconds, pid):
    return ev(seconds, pid, lab.PRESCRIPTION_BUILT)


def shape(parts):
    return [(p.patient_id, p.start_seconds, p.end_seconds, p.rule, p.needs_review)
            for p in parts]


def test_one_patient_with_a_prescription():
    parts = resolve([opened("SAKIB"), rx(420, "SAKIB")], total_seconds=540)
    assert shape(parts) == [("SAKIB", 0.0, None, "R1", False)]
    assert parts[0].grant_jti == "g-SAKIB" and parts[0].end_at is None


def test_one_patient_follow_up_without_a_prescription():
    parts = resolve([opened("SAKIB")], total_seconds=300)
    assert shape(parts) == [("SAKIB", 0.0, None, "R4", False)]


def test_wrong_pick_corrected_in_ten_seconds():
    # Rahim opened by mistake; Karim ten seconds later; Karim's prescription.
    parts = resolve([opened("RAHIM"), ev(10, "KARIM"), rx(400, "KARIM")],
                    total_seconds=500)
    assert shape(parts) == [("KARIM", 0.0, None, "R3+R1", False)]
    assert parts[0].visit == v("KARIM") and parts[0].grant_jti == "g-KARIM"


def test_deliberate_click_then_back_to_the_patient():
    # Sakib is right; Salma clicked at 0:40; back to Sakib at 1:10; Sakib's rx.
    parts = resolve([opened("SAKIB"), ev(40, "SALMA"), ev(70, "SAKIB"), rx(500, "SAKIB")],
                    total_seconds=600)
    assert shape(parts) == [("SAKIB", 0.0, None, "R2+R1", False)]


def test_glance_mid_consultation_and_the_prescription_counts_as_going_back():
    parts = resolve([opened("SAKIB"), ev(240, "SALMA"), rx(270, "SAKIB")],
                    total_seconds=400)
    assert shape(parts) == [("SAKIB", 0.0, None, "R2+R1", False)]


def test_follow_up_then_the_next_patient_with_a_prescription():
    # Sakib 6 minutes, no prescription. Salma clicked at 6:00, her rx at 9:00,
    # the recording closed at 11:00 when Rina was opened.
    parts = resolve([opened("SAKIB"), ev(360, "SALMA"), rx(540, "SALMA")],
                    total_seconds=660, closed_at=T0 + timedelta(seconds=660))
    assert shape(parts) == [("SAKIB", 0.0, 360.0, "R4", False),
                            ("SALMA", 360.0, None, "R1", False)]
    assert parts[0].end_at == T0 + timedelta(seconds=360)
    assert parts[1].start_at == T0 + timedelta(seconds=360)
    assert parts[1].end_at == T0 + timedelta(seconds=660)


def test_three_follow_ups_in_a_row():
    parts = resolve([opened("A"), ev(300, "B"), ev(600, "C")], total_seconds=900)
    assert shape(parts) == [("A", 0.0, 300.0, "R4", False), ("B", 300.0, 600.0, "R4", False),
                            ("C", 600.0, None, "R4", False)]


def test_wrong_pick_then_a_follow_up_with_no_prescription_at_all():
    parts = resolve([opened("RAHIM"), ev(12, "KARIM")], total_seconds=400)
    assert shape(parts) == [("KARIM", 0.0, None, "R3+R4", False)]


def test_a_short_stretch_at_the_end_is_kept_and_flagged():
    parts = resolve([opened("SAKIB"), ev(600, "SALMA")], total_seconds=630)
    assert shape(parts) == [("SAKIB", 0.0, 600.0, "R4", False),
                            ("SALMA", 600.0, None, "R4+R5", True)]


def test_one_patient_either_side_of_a_prescribed_one_is_flagged():
    parts = resolve([opened("Z"), ev(120, "P"), rx(300, "P"), ev(400, "Z")],
                    total_seconds=600)
    assert shape(parts) == [("Z", 0.0, 120.0, "R4+R5", True), ("P", 120.0, 400.0, "R1", False),
                            ("Z", 400.0, None, "R4+R5", True)]


def test_rapid_clicks_at_the_start_all_join_the_real_patient():
    parts = resolve([opened("A"), ev(8, "B"), ev(20, "C"), rx(300, "C")], total_seconds=400)
    assert shape(parts) == [("C", 0.0, None, "R3+R1", False)]


def test_a_short_glance_between_two_patients_joins_the_next():
    parts = resolve([opened("SAKIB"), ev(360, "Y"), ev(380, "SALMA"), rx(500, "SALMA")],
                    total_seconds=600)
    assert shape(parts) == [("SAKIB", 0.0, 360.0, "R4", False),
                            ("SALMA", 360.0, None, "R3+R1", False)]


def test_a_prescription_from_the_server_counts_as_much_as_the_signal():
    # 3a was missed on the PC, but CMED's 3b for Salma reached the server.
    events = [opened("SAKIB"), ev(360, "SALMA")]
    before = resolve(events, total_seconds=660)
    after = resolve(events, total_seconds=660, prescribed={"SALMA"})
    assert [p.rule for p in before] == ["R4", "R4"]
    assert [p.rule for p in after] == ["R4", "R1"]
    assert same_cut(before, after)                     # the files still stand


def test_a_late_prescription_that_moves_the_cut():
    # Without a prescription Z's return makes Y a glance; Y's 3b says Y was seen.
    events = [opened("Z"), ev(200, "Y"), ev(500, "Z")]
    before = resolve(events, total_seconds=700)
    after = resolve(events, total_seconds=700, prescribed={"Y"})
    assert shape(before) == [("Z", 0.0, None, "R2+R4", False)]
    assert [p.patient_id for p in after] == ["Z", "Y", "Z"]
    assert not same_cut(before, after)


def test_a_double_click_is_not_a_change_of_patient():
    parts = resolve([opened("SAKIB"), ev(5, "SAKIB"), ev(9, "SAKIB"), rx(300, "SAKIB")],
                    total_seconds=400)
    assert shape(parts) == [("SAKIB", 0.0, None, "R1", False)]


def test_events_out_of_order_and_beyond_the_end_are_tolerated():
    parts = resolve([ev(360, "SALMA"), rx(900, "SALMA"), opened("SAKIB")],
                    total_seconds=660)
    assert shape(parts) == [("SAKIB", 0.0, 360.0, "R4", False),
                            ("SALMA", 360.0, None, "R1", False)]


def test_parts_are_contiguous_and_cover_the_recording():
    parts = resolve([opened("A"), ev(300, "B"), ev(340, "C"), rx(400, "C"), ev(700, "D")],
                    total_seconds=1000)
    starts = [p.start_seconds for p in parts]
    ends = [p.end_seconds for p in parts]
    assert starts[0] == 0.0 and ends[-1] is None
    assert starts[1:] == ends[:-1]
