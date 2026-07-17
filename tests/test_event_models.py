from datetime import datetime, timedelta, timezone

from sqlalchemy import UniqueConstraint

from thenetwork.db.models import Event, EventRecommendation, EventSuppression


def test_one_off_and_recurring_events_keep_stable_ids_across_edits():
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    one_off = Event(
        submitter_id="person-1",
        text="A one-off gathering on Friday",
        gist="A gathering on Friday",
        expires_at=expires_at,
    )
    recurring = Event(
        submitter_id="person-1",
        text="A monthly builders gathering",
        gist="A monthly builders gathering",
        recurrence="monthly on the first Friday",
        expires_at=expires_at,
    )

    original_id = recurring.id
    original_version = recurring.version
    recurring.text = "A monthly builders gathering, now at 6pm"
    recurring.gist = "A monthly builders gathering at 6pm"
    recurring.version += 1
    recurring.updated_at = datetime.now(timezone.utc)

    assert one_off.recurrence is None
    assert recurring.recurrence == "monthly on the first Friday"
    assert recurring.id == original_id
    assert recurring.version == original_version + 1
    assert recurring.cancelled_at is None


def test_event_recommendation_is_unique_per_event_and_person():
    table = EventRecommendation.__table__
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("event_id", "person_id") in unique_columns
    assert table.c.notified_at.nullable is True
    assert table.c.considered_at.nullable is False
    assert table.c.event_version.nullable is False
    assert (
        EventRecommendation(event_id="event-1", person_id="person-1").event_version == 1
    )


def test_event_suppression_is_independent_person_level_state():
    table = EventSuppression.__table__

    assert set(table.c.keys()) == {"person_id", "suppressed_at"}
    assert table.c.person_id.primary_key is True
    assert "event_id" not in table.c
    assert "introduction_consents" != table.name
    assert "proactive_surfaces" != table.name
