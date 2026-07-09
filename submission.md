# Mixtape Bug Hunt — Submission

## AI Usage

_This section is filled in at the end of the project — see the bottom of this document._

## Codebase Map

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`). Configures the SQLAlchemy database
  URI (sqlite by default), registers the four blueprints (`songs`, `playlists`, `users`, `feed`),
  and calls `db.create_all()`. `db = SQLAlchemy()` is instantiated at module level here and
  imported by `models.py` and every service module — this is the shared session/engine handle.

- **`models.py`** — All SQLAlchemy models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`,
  `Playlist`, `Notification`. Three association tables:
  - `friendships` — symmetric many-to-many self-join on `User` (friendship is inserted as two
    rows, one per direction, so `user.friends` is a simple one-directional lookup).
  - `song_tags` — plain many-to-many between `Song` and `Tag`, composite PK `(song_id, tag_id)`.
  - `playlist_entries` — many-to-many between `Playlist` and `Song`, but with **extra columns**
    (`position`, `added_by`, `added_at`) beyond the composite PK `(playlist_id, song_id)`. This
    is what gives playlists an explicit, orderable position instead of relying on insertion order.
  Every model has a `to_dict()` used directly by routes for JSON serialization — there's no
  separate schema/serializer layer.

- **`routes/`** — one blueprint per resource (`songs.py`, `playlists.py`, `users.py`, `feed.py`).
  Every route follows the same shape: parse/validate request input, call exactly one service
  function, translate a `ValueError` into a 400/404 JSON error, otherwise `jsonify` the result.
  No business logic lives in routes.

- **`services/`** — all business logic:
  - `streak_service.py` — `record_listening_event` (create a `ListeningEvent`, then update the
    streak) and `update_listening_streak` (the day-boundary math), plus `get_streak`.
  - `feed_service.py` — `get_friends_listening_now` (recency-filtered feed) and
    `get_activity_feed` (unfiltered, limit-N feed).
  - `search_service.py` — `search_songs` (title/artist `ILIKE` match) and `get_song`.
  - `notification_service.py` — `create_notification` (generic constructor), `add_to_playlist`
    (adds a song to a playlist **and** notifies the original sharer), `rate_song` (upserts a
    `Rating`), `get_notifications`, `mark_as_read`.
  - `playlist_service.py` — `create_playlist`, `get_playlist_songs` (position-ordered song list),
    `get_playlist`, `get_user_playlists`.

- **`seed_data.py`** — resets the DB and populates 5 users/friendships, 13 songs with varying tag
  counts (0, 1, 3+ tags — deliberately structured to expose Issue #3), 3 playlists, listening
  events at different recencies (deliberately structured to expose Issue #2), and per-user
  `last_listened_at`/`listening_streak` values.

- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`. Each uses an in-memory
  sqlite DB via a `create_app({"TESTING": True, ...})` fixture. Several tests already encode the
  *expected* (bug-free) behavior in their assertions/comments — running the suite against the
  starter code is itself a reproduction step for Issues #1 and #5 (see below).

### Data flow — a friend rates a shared song and a notification (should) appear

1. Client calls `POST /songs/<song_id>/rate` with `{user_id, score}`.
2. `routes/songs.py::rate()` validates input and calls `notification_service.rate_song(user_id, song_id, score)`.
3. `rate_song()` validates the score range, loads the `Song` and rating `User`, upserts a `Rating`
   row (unique on `(user_id, song_id)` — rating twice updates the existing row instead of creating
   a second one), commits, and returns the `Rating`.
4. The route returns the serialized rating as the HTTP response.

Compare this to the *working* notification path, `add_to_playlist()` in the same file: after
appending the song to the playlist, it explicitly calls `create_notification(user_id=song.shared_by, ...)`
so the original sharer gets a `Notification` row. `rate_song()` has no equivalent call — it saves
the `Rating` and returns, so no `Notification` is ever created for a rating event (Issue #4).

### Patterns noticed

- **Routes are thin.** Every route does input parsing → one service call → response formatting.
  All logic (including the logic that's buggy) lives in `services/`.
- **No schema/serializer layer.** Models serialize themselves via `to_dict()`; services return
  plain dicts built from those.
- **`ValueError` is the one error-signaling convention** — every service raises `ValueError` for
  "not found" / "invalid input," and every route catches exactly that to produce a 4xx response.
- **Notifications are opt-in per code path**, not automatic — every action that should notify
  someone needs its own explicit `create_notification()` call. There's no event bus or hook;
  this is exactly why Issue #4 is architectural rather than a typo — the pattern was simply never
  applied to `rate_song()`.
- **"Most recent per person" dedup pattern** appears in `feed_service.get_friends_listening_now`
  (keep first-seen event per friend from a `desc`-ordered query) — a reasonable pattern, but its
  correctness depends entirely on the recency window used to select candidate events in the first
  place (see Issue #2).

## Root Cause Analyses

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** Before touching any code, I wrote a small script (run with the app's
own `create_app`/`db` against an in-memory sqlite DB) that mirrors kenji's exact report: create a
user with `listening_streak=12` and `last_listened_at` set to a Saturday evening, then call
`update_listening_streak()` with a Sunday-morning timestamp — one calendar day later, i.e. a
genuinely consecutive day. Result: streak dropped from 12 to 1, exactly matching the report.
Running the existing `pytest tests/test_streaks.py` also showed one pre-existing failing test,
`test_streak_increments_on_sunday`, which independently encodes the same expectation and failed
before any change (`assert 1 == 2`).

**How I found the root cause.** `routes/users.py::streak()` calls `streak_service.get_streak()`,
which just reads `user.listening_streak` — not where the bug is, since it doesn't do any date
math. The route that actually updates the streak is `POST /songs/<id>/listen` →
`streak_service.record_listening_event()` → `update_listening_streak()`. Reading
`update_listening_streak()` top to bottom: it computes `days_since_last = (today - last_date).days`
and branches on that value. The branch for "one day has passed" was:
`elif days_since_last == 1 and today.weekday() != 6:`. That extra `and` clause was the moment I
was confident I'd found it — `days_since_last == 1` already fully and correctly captures "listened
on the very next calendar day" (the docstring's own rule). Adding a same-week/weekday condition on
top of a day-delta condition doesn't correspond to any rule in the docstring and doesn't need to
exist for the streak logic to be correct.

**The root cause.** Python's `datetime.weekday()` returns `6` for Sunday. The increment branch
required both "exactly one day passed" **and** "today is not Sunday" before it would increment the
streak. Any listen that happened to land on a Sunday — even one that was perfectly consecutive —
failed the `today.weekday() != 6` check and fell through to the `else` branch, which unconditionally
resets `listening_streak` to `1`. This is exactly why the bug was Sunday-specific and intermittent:
it has nothing to do with the streak length or how many days were skipped, only with whether `now`
happens to fall on a Sunday.

**My fix and side-effect check.** Removed the `and today.weekday() != 6` clause, so the branch is
just `elif days_since_last == 1: user.listening_streak += 1`. This is the smallest possible change —
one condition deleted, no other logic touched. I verified: (1) the exact kenji repro script now
returns 13 after the Sunday listen and 14 after the following Monday listen; (2) a 7-day simulation
from a Monday start (`update_listening_streak` called once per consecutive calendar day) produces
1,2,3,4,5,6,7,8 with no drop across the Saturday→Sunday or Sunday→Monday boundaries; (3) the full
`pytest tests/` suite — including `test_streak_does_not_double_count_same_day` and
`test_streak_resets_after_skipped_day`, which exercise the same-day and skipped-day branches this
change didn't touch — all still pass, confirming the fix is scoped to the Sunday case only.
