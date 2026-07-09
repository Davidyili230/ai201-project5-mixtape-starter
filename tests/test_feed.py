"""
tests/test_feed.py — Mixtape

Regression test for Issue #2: "Friends Listening Now" must use a
calendar-day boundary, not a rolling 24-hour window.
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def friends(app):
    """Two friended users and two songs, for listening-now tests."""
    with app.app_context():
        nova = User(username="nova", email="nova@example.com")
        darius = User(username="darius", email="darius@example.com")
        db.session.add_all([nova, darius])
        db.session.flush()

        db.session.execute(friendships.insert().values(user_id=nova.id, friend_id=darius.id))
        db.session.execute(friendships.insert().values(user_id=darius.id, friend_id=nova.id))

        song_late_night = Song(title="Late Night Song", artist="A", shared_by=nova.id)
        song_this_morning = Song(title="This Morning Song", artist="B", shared_by=nova.id)
        db.session.add_all([song_late_night, song_this_morning])
        db.session.commit()

        yield {
            "nova": nova,
            "darius": darius,
            "song_late_night": song_late_night,
            "song_this_morning": song_this_morning,
        }


def test_yesterday_evening_listen_does_not_show_today(app, friends):
    """
    A friend's listen from yesterday evening must not appear as
    "listening now" this morning, even though it happened less than
    24 hours ago.
    """
    with app.app_context():
        now = datetime.now(timezone.utc)
        yesterday_11pm = now.replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=1)

        db.session.add(ListeningEvent(
            user_id=friends["darius"].id,
            song_id=friends["song_late_night"].id,
            listened_at=yesterday_11pm,
        ))
        db.session.commit()

        feed = get_friends_listening_now(friends["nova"].id)
        assert feed == []  # Bug causes darius to still show up here


def test_todays_early_morning_listen_shows_up(app, friends):
    """A friend's listen from earlier today (even just after midnight) should appear."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        today_1am = now.replace(hour=1, minute=0, second=0, microsecond=0)

        db.session.add(ListeningEvent(
            user_id=friends["darius"].id,
            song_id=friends["song_this_morning"].id,
            listened_at=today_1am,
        ))
        db.session.commit()

        feed = get_friends_listening_now(friends["nova"].id)
        assert len(feed) == 1
        assert feed[0]["friend"]["username"] == "darius"
        assert feed[0]["song"]["title"] == "This Morning Song"


def test_no_friends_returns_empty_list(app):
    """A user with no friends gets an empty feed, not an error."""
    with app.app_context():
        user = User(username="loner", email="loner@example.com")
        db.session.add(user)
        db.session.commit()

        assert get_friends_listening_now(user.id) == []
