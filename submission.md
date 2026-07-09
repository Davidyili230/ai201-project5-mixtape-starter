# Mixtape Bug Hunt — Submission

## AI Usage

I used Claude Code (an AI pair-programming agent) for essentially this entire project, working
under my direction rather than writing the fixes myself and only spot-checking with AI — so I want
to be specific about what it actually did and where I made it prove its claims rather than take
them on faith.

- **Orientation.** It read `app.py`, `models.py`, every file in `routes/` and `services/`, and
  `seed_data.py` before looking at any issue, and used that to write the codebase map above
  (file responsibilities, the association-table design in `models.py`, and the rating→notification
  data flow trace).
- **Reproduction before fixing.** For every issue, it wrote a small standalone script against an
  in-memory (or real seeded) DB that reconstructed the reporter's exact scenario — e.g. a user at
  streak 12 listening on a Saturday then Sunday, or darius's 11pm listen checked the next morning —
  and ran it *before* touching the corresponding service file, per the brief's instructions. It
  also ran the existing `pytest` suite early, which turned out to already encode the expected
  (bug-free) behavior for Issues #1 and #5, so those failing tests doubled as reproductions.
- **Root-causing, verified rather than assumed.** For Issue #3 specifically, its first-pass
  hypothesis (the `outerjoin` on `song_tags` fans out rows for multi-tag songs) matched the
  starter code's own comments, but it didn't stop there — it actually tried to reproduce the
  duplicate over HTTP and via direct function calls, got a clean (non-duplicated) result every
  time, and then traced *why* into SQLAlchemy's own source (`orm/loading.py`) to find that legacy
  `Query.all()` auto-deduplicates full-entity results in the installed SQLAlchemy version. That's
  the one place I'd flag as "AI verified its own hypothesis and reported the honest, less-clean
  result" instead of declaring victory on the first plausible explanation — which is exactly the
  failure mode the brief warns about ("AI is... unreliable for guessing what's wrong before you've
  read the code"). I checked its SQLAlchemy-source claim myself by reading the same
  `orm/loading.py` snippet it quoted, rather than trusting the explanation outright.
- **Fixes kept minimal.** Each fix was a small, targeted diff (one condition removed in
  `streak_service.py`, one cutoff calculation changed in `feed_service.py`, one slice removed in
  `playlist_service.py`, one notification call added in `notification_service.py`, one join
  removed in `search_service.py`) — I reviewed each diff against the RCA before it was committed
  to make sure the fix matched the stated root cause and didn't touch unrelated code.
- **Where I redirected it.** Mid-session, a `git commit` produced a commit with the message
  "issue 1" instead of the conventional-format message that was passed to the command — the file
  changes were correct, only the message text was wrong, and no hook configuration could be found
  to explain it. Rather than silently amending history, it flagged the discrepancy to me and asked
  before rewriting the commit message. Separately, while reproducing Issue #5 it found that adding
  a song to a playlist over the real HTTP endpoint 500s (`add_to_playlist()` appends to
  `playlist.songs` without setting the NOT-NULL `position`/`added_by` columns) — a real, distinct
  bug from the five assigned issues. It disclosed this rather than silently fixing or silently
  ignoring it, and left it out of scope since it isn't one of the five reported issues.
- **Side-effect checks were run, not just claimed.** After each fix, it re-ran the full `pytest`
  suite and, where relevant, the *other* function sharing the same file (e.g. `get_activity_feed`
  alongside the `get_friends_listening_now` fix; `add_to_playlist` alongside the `rate_song` fix)
  to confirm the unrelated code path still worked — I checked the actual command output for each
  of these rather than trusting a description of what was run.

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
  counts (0, 1, 3+ tags per song), 3 playlists, listening events spanning a range of recencies
  (same day, previous evening, older), and per-user `last_listened_at`/`listening_streak` values.

- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`. Each uses an in-memory
  sqlite DB via a `create_app({"TESTING": True, ...})` fixture and encodes the expected behavior
  of its corresponding service in assertions/comments.

### Data flow — adding a song to a playlist notifies the original sharer

1. Client calls `POST /playlists/<playlist_id>/songs` with `{song_id, added_by}`.
2. `routes/playlists.py::add_song()` validates input and calls
   `notification_service.add_to_playlist(playlist_id, song_id, added_by_user_id)`.
3. `add_to_playlist()` loads the `Playlist` and `Song`, appends the song to `playlist.songs`, and
   commits — that's the primary side effect.
4. If the person adding the song isn't the one who originally shared it
   (`song.shared_by != added_by_user_id`), it calls `create_notification(user_id=song.shared_by,
   notification_type="song_added_to_playlist", body=...)`, so the original sharer gets a
   `Notification` row.
5. The route returns the serialized playlist as the HTTP response.

This is the general shape every notification-producing action in the codebase follows: commit
the primary side effect, then explicitly call `create_notification()` for whoever should be
alerted.

### Patterns noticed

- **Routes are thin.** Every route does input parsing → one service call → response formatting.
  All business logic lives in `services/`.
- **No schema/serializer layer.** Models serialize themselves via `to_dict()`; services return
  plain dicts built from those.
- **`ValueError` is the one error-signaling convention** — every service raises `ValueError` for
  "not found" / "invalid input," and every route catches exactly that to produce a 4xx response.
- **Notifications are opt-in per code path**, not automatic — every action that should notify
  someone needs its own explicit `create_notification()` call. There's no event bus or hook, so
  whether any given action produces a notification depends entirely on whether that specific
  function remembers to call it.
- **"Most recent per person" dedup pattern** appears in `feed_service.get_friends_listening_now`
  (keep first-seen event per friend from a `desc`-ordered query) — a reasonable pattern, but its
  correctness depends entirely on the recency window used to select candidate events in the first
  place.

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

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** I wrote a script that creates two friended users (nova, darius), a song,
and one `ListeningEvent` for darius at 11pm "yesterday" (computed as `now - 1 day` with the hour
forced to 23:00). I then called `feed_service.get_friends_listening_now(nova.id)` with the real
current time (no time-travel needed — this is nova's actual "check my feed this morning" moment,
just replayed a few hours later in wall-clock terms). Result: darius's stale event still appeared
in the feed, matching nova's exact report of seeing "darius listening now" to something he played
the night before.

**How I found the root cause.** `routes/feed.py::listening_now()` calls
`feed_service.get_friends_listening_now()` directly — a short, single-purpose function, so I read
it in full. It builds `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD` and filters
`ListeningEvent.listened_at >= cutoff`, with `RECENT_THRESHOLD = timedelta(hours=24)`. The moment
I was confident this was the actual bug (not just "something about recency filtering") was doing
the arithmetic on the report itself: darius listened at 11pm, nova checked at 9am — only 10 hours
apart, well inside a 24-hour window, so of course the filter kept it. The word "today" in the
feature's intent ("what my friends played today") and the word "24 hours" in the implementation
are two different definitions of "recent," and the code implements the wrong one.

**The root cause.** `RECENT_THRESHOLD` is a rolling 24-hour window measured backward from the
current instant, not a calendar-day boundary. An event from 11pm one night stays inside that
rolling window until 11pm the *next* night — which is exactly the symptom nova described
("hangs around until the same time the next day"). The feature is supposed to mean "listened
today," a boundary that resets at midnight, but the code implements "listened in the last 24
hours," a boundary that slides forward continuously with `now`.

**My fix and side-effect check.** Changed the cutoff computation from `now - timedelta(hours=24)`
to the start of the current UTC calendar day (`now.replace(hour=0, minute=0, second=0,
microsecond=0)`), and removed the now-unused `RECENT_THRESHOLD` constant/import. I verified both
sides of the boundary directly: an event at 11pm "yesterday" now returns 0 feed entries, and an
event at 1am "today" (only 2 hours after midnight, but still today) returns 1 — confirming the
cutoff is a calendar-day boundary and not simply a shorter rolling window. I also re-ran
`get_activity_feed()`, the other function in the same file, which intentionally does *not* filter
by recency at all (per its own docstring) — it was untouched by this change and still returns
events regardless of age, so I didn't regress the unfiltered activity feed while fixing the
filtered "listening now" feed. Full `pytest tests/` suite still passes (no test file exists yet
for `feed_service.py` in the starter — see the regression test I added below).

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** I ran the app against the real seed data and looked at the seeded
"Friday Energy" playlist (7 songs per `seed_data.py`). Querying `playlist_entries` directly
confirmed 7 rows exist for that playlist, but calling `playlist_service.get_playlist_songs()`
returned only 6 — missing "Harlem Renaissance," the song at the highest `position`. I then
reproduced darius's second observation (adding a song "frees" the previously-missing one): I
inserted a new `playlist_entries` row for a brand-new song at `position=8` and re-fetched — the
previously-missing "Harlem Renaissance" reappeared, and the newly-added song became the new
missing entry, exactly matching the report. (Note: attempting this over the real HTTP
`POST /playlists/<id>/songs` endpoint hits a separate, pre-existing 500 error unrelated to this
issue — `notification_service.add_to_playlist()` appends to `playlist.songs` via the bare ORM
relationship, which never populates the NOT-NULL `position`/`added_by` columns on
`playlist_entries`. That's a real bug but not one of the five reported issues, so I didn't fix it
as part of this task; I inserted the row directly via SQLAlchemy Core to isolate and verify the
read-side bug in `get_playlist_songs()`.)

**How I found the root cause.** `routes/playlists.py::get_songs()` calls
`playlist_service.get_playlist_songs()` directly. Reading the function, it builds a query joining
`Song` to `playlist_entries`, filters by `playlist_id`, and orders by `position` — all correct —
and assigns the result to `songs`. The very next line is
`return [song.to_dict() for song in songs[:-1]]`. That `[:-1]` slice was the moment I was
confident I'd found it: `songs` at that point is already the fully correct, ordered list from the
query above; the function then explicitly drops its own last element before returning. Nothing
upstream of that line needed to change.

**The root cause.** `get_playlist_songs()` sliced off the last element of the correctly-ordered
song list (`songs[:-1]`) before returning it. Since the list is ordered ascending by `position`,
"last element" always means "highest position," i.e. the most recently added song — which is
exactly the pattern darius described: whichever song was added most recently is always the one
hidden, and adding a new song just moves that title to a new most-recent slot.

**My fix and side-effect check.** Changed the return statement to `songs` (no slice). Verified:
(1) the 7-song seeded playlist now returns all 7, ending with "Harlem Renaissance"; (2) after
adding an 8th song directly, all 8 are returned including the new one, with no song hidden;
(3) ran the existing `tests/test_playlists.py::test_empty_playlist_returns_empty_list` to confirm
the boundary on the *other* side — an empty playlist still correctly returns `[]`. Full
`pytest tests/` suite passes, 13/13, with no other files touched.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**How I reproduced it.** Using the real seeded data, I found a song shared by darius, recorded
his notification count (0), then called `notification_service.rate_song(kenji.id, song.id, 5)` —
the same call `POST /songs/<id>/rate` makes. The `Rating` was saved correctly (`rating.score == 5`),
but darius's notification count stayed at 0. That matches aaliya's exact report: rating is saved
and visible on the song, but no notification ever appears.

**How I found the root cause.** The hint pointed at comparing the working notification pattern to
the missing one, so I read both functions in `notification_service.py` side by side.
`add_to_playlist()` ends with: check `if song.shared_by != added_by_user_id:` then call
`create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", body=...)`.
`rate_song()` upserts the `Rating`, commits, and `return`s — there is no call to
`create_notification` anywhere in the function. This wasn't a subtle typo to spot; it was the
absence of a step that every other notification-producing function has. That's what the brief
meant by "architectural, not a typo" — the codebase's convention is that each action that should
notify someone needs its own explicit `create_notification()` call, and that step was simply never
written for ratings.

**The root cause.** `rate_song()` never calls `create_notification()`. Every other user-facing
action that's supposed to produce a notification (`add_to_playlist`) explicitly constructs one
after committing its primary side effect; `rate_song()` stops after committing the `Rating` and
returns without doing the equivalent step. There's no shared hook, signal, or event system that
would have created the notification automatically — so simply saving the rating was never going
to produce one.

**My fix and side-effect check.** Added the same pattern used in `add_to_playlist()`: after
committing the rating, if `song.shared_by != user_id` (don't notify someone for rating their own
song), call `create_notification(user_id=song.shared_by, notification_type="song_rated", body=...)`.
Verified: (1) a friend rating another user's shared song now produces exactly one notification
with the expected body text and `type: "song_rated"`; (2) a user rating their *own* shared song
produces no notification (mirrors the existing self-add guard in `add_to_playlist`); (3) re-ran the
`add_to_playlist` flow against seeded data to confirm the pre-existing "song added to playlist"
notification path is untouched and still fires correctly; (4) full `pytest tests/` suite still
passes, 13/13.

### Issue #3 — The same song keeps showing up twice in search (bonus — see note on reproduction)

**How I reproduced it — and why I couldn't, over HTTP.** `seed_data.py` and `tests/test_search.py`
both leave comments saying songs with 3+ tags are meant to "expose Issue #3" and that the bug
"causes it to be 3." Following that lead, I searched "Anthem" against the real seeded DB
(`Crown Heights Anthem` has 3 tags) via the actual `GET /songs/search?q=Anthem` endpoint, and
separately via `search_songs()` directly, and via a synthetic case with two multi-tag songs. In
every case the result count was correct (1 and 2 respectively) — **no duplication was observable**.
Per the brief's own guidance ("if you can't reproduce a bug, try a different one"), I want to be
upfront that this one didn't reproduce through the app's actual behavior in this environment. I
kept investigating anyway because the mechanism was too specific to abandon on a first null
result.

**How I found the root cause (and why it's invisible right now).** `search_service.search_songs()`
does `db.session.query(Song).outerjoin(song_tags, ...).filter(...).all()` — joining to the
`song_tags` association table without ever selecting or filtering on a tag column. Compiling that
exact query to raw SQL and running it directly against the DB (bypassing the ORM's row
post-processing) confirmed the join fans out: a song with 3 tags produced **3 raw rows** from the
database. So the multiplication is real at the SQL level, exactly matching the "3 tags → 3
results" pattern in the bug report. But `db.session.query(Song)...all()` (SQLAlchemy's legacy
`Query` API) didn't return 3 — it returned 1. I traced why into SQLAlchemy's own source
(`sqlalchemy/orm/loading.py`): legacy `Query.all()` sets `filtered = compile_state._has_mapper_entities`,
which is `True` for *any* query whose result is a full mapped entity, and when `filtered` is
True, `Query._iter()` calls `result.unique()` automatically before returning — deduplicating by
primary key regardless of whether a join was involved. That's not a conditional safety net that
this bug could slip past; it's unconditional for this query shape in the installed SQLAlchemy
version (2.0.51, pinned by `requirements.txt`'s `sqlalchemy>=2.0.0`). That's the moment I understood
why my repro attempts kept coming back clean: the ORM API this code happens to use quietly
absorbs the exact defect the join would otherwise cause.

**The root cause.** The join itself is still a real defect, even though its consequence is
currently masked: `search_songs()` joins `Song` to `song_tags` for no reason — it never filters or
selects anything from that join, since tags are separately eager-loaded through the
`Song.tags` relationship (`lazy="subquery"`) for `to_dict()`. A join that exists for no purpose is
also a join with no reason to keep, and if this code were ever ported to SQLAlchemy 2.0's
`select()`/`session.execute()` style — the direction SQLAlchemy's own docs are steering people
toward, and something I verified directly produces un-deduplicated, duplicated rows for the exact
same query — the duplication would resurface immediately with no warning, because dedup here is
an artifact of the legacy `Query` wrapper rather than a property of the query itself.

**My fix and side-effect check.** Removed the unnecessary `.outerjoin(song_tags, ...)` (and the
now-unused `Tag`/`song_tags` imports) from `search_songs()`, since it filtered nothing and
selected nothing — the join existed for no purpose. This removes the row-multiplication hazard at
its source rather than papering over it with `.distinct()` on a join that shouldn't be there in
the first place. Verified: (1) tags still appear correctly in search results (`song.tags` is
loaded independently via the relationship, untouched by this change) — confirmed
`['rap', 'hip-hop', 'boom bap']` still shows for Crown Heights Anthem; (2) full `pytest tests/`
suite still passes, 13/13, including all three `test_search_no_duplicates_*` tests, which continue
to pass as they did before this change (they were passing due to the same ORM-level dedup
described above; they still pass now because the join is simply gone).

I'm flagging this one differently from the other four RCA entries: I did not observe the reported
symptom directly, so I'm not confident this "fix" addresses what a real user would have hit in
this exact environment. I'm including it because the underlying defect (an unnecessary join that
provably multiplies rows at the SQL level) is real and worth removing regardless, but I want to be
transparent that this is the one bug where "reproduce first" genuinely failed for me.

## Regression Test

Added `tests/test_feed.py` for Issue #2, since `feed_service.py` had no test file in the starter
(unlike streaks, search, and playlists, which already had one). It covers:

- `test_yesterday_evening_listen_does_not_show_today` — a friend's 11pm-yesterday listen must not
  appear in "listening now" this morning. I confirmed this test actually catches the original bug:
  temporarily restoring the pre-fix `feed_service.py` (rolling 24-hour window) and re-running the
  suite makes exactly this test fail with `assert [...] == []`, while it passes against the fixed
  version — i.e. this is a genuine regression test, not just an assertion that happens to match
  the current code.
- `test_todays_early_morning_listen_shows_up` — the other side of the boundary: a listen from
  1am today (less than 24 hours old *and* today) still appears.
- `test_no_friends_returns_empty_list` — an existing edge case (no friends) that the fix must not
  break.
