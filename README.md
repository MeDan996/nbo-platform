# NBO Platform

A platform for running a national biology olympiad in the IBO format, with two modes:

- **Creative mode** — authors build questions, using real IBO papers as templates, in Russian, Kyrgyz and English.
- **Survival mode** — participants sit timed exams, get ranked, and see statement-level statistics on where they are weak.

Built around the **IBO Theory Part A** format that the papers in `sample-questions/` use: a stem with figures, followed by four true/false statements scored on a partial-credit curve.

---

## Quick start

```bash
pip install -r requirements.txt
```

```bash
python scripts/import_ibo.py
```

```bash
python scripts/seed_demo.py
```

```bash
python run.py
```

Then open <http://localhost:8088>.

`import_ibo.py` loads the 2024 paper from `sample-questions/` — 50 questions, 200 statements, the answer key with per-statement explanations, and the figures extracted from the PDF. `seed_demo.py` creates demo accounts and simulates a cohort so the leaderboard, statistics and integrity queue have real data in them.

Demo accounts (password `nbo-demo-2024`) — **local demo only, do not expose this to the internet**:

| Role | Email |
| --- | --- |
| admin | `admin@nbo.kg` |
| reviewer | `reviewer@nbo.kg` |
| author | `author@nbo.kg` |
| participant | any `*@example.kg` address from the seed output |

---

## Scoring: the IBO curve

A four-statement block is worth 1.0 point, awarded on how many statements were identified correctly:

| correct | 0 | 1 | 2 | 3 | 4 |
| --- | --- | --- | --- | --- | --- |
| points | 0 | 0 | 0.2 | 0.6 | 1.0 |

An unanswered statement counts as wrong, matching a blank cell on the real answer sheet. Answering a block at random has an expected value of 0.2875 points; the author UI computes this for any custom curve and warns when guessing becomes too profitable.

Curves resolve question-first, then exam, then the built-in default, and are stored as `{"4": [0, 0, 0.2, 0.6, 1.0]}`. See `app/services/scoring.py`.

---

## Rating

Olympiads are many-player events, so a plain Elo update does not fit. The platform uses the **Codeforces rating algorithm** (`app/services/rating.py`), which was designed for exactly this shape: each participant's expected finishing position ("seed") is compared against their actual position, and the field is renormalised so the rating pool does not inflate over time.

Tiers: novice → apprentice (1350) → specialist (1600) → expert (1900) → master (2100) → legend (2400).

Ratings are applied when an organiser **finalises** an exam, not when a participant submits. Finalising is idempotent, and voided attempts are excluded from the field entirely so a confirmed cheat does not distort anybody else's rating change.

---

## Exam integrity

Two principles shape `app/services/anticheat.py`:

**Blocks are narrow, flags are wide.** Participants sit exams from a mix of school labs and home connections. A school lab shares one NAT'd public address, so a naive "one account per IP" rule would lock out thirty innocent classmates. Hard blocks therefore apply only to signals that survive that:

| Rule | Behaviour |
| --- | --- |
| Same account, attempts exhausted | blocked |
| Same **device** already sat this exam as another account | blocked (the main retake defence) |
| Many accounts from an **unvouched** address | blocked above a configurable threshold |
| Many accounts from an **allowlisted school** address | allowed, up to that school's own ceiling |
| Practice mode | device/network rules skipped entirely |

Register a school's public CIDR ranges under **Admin → Schools** and that lab stops tripping the shared-address rule.

**Nothing is voided automatically.** Rules fire, evidence accumulates into a risk score, and an admin decides. Every refusal is recorded in `exam_access_denials`, so a participant blocked in error can be found and granted an override rather than silently losing their sitting.

Signals collected: browser fingerprint (hashed with a server-side salt), address history per account, focus loss / tab hiding, paste events, fullscreen exit, mid-exam network changes, superhuman answering pace, and high scores in implausibly short time.

**Collusion detection** uses a Harpp-Hogan style index: two strong candidates agreeing on correct answers proves nothing, so the measure is *matching wrong answers* — `errors in common / number of differing answers`. A value at or above 1.0 with a meaningful count of shared errors is the published threshold for investigating by hand. It is evidence for a reviewer, never a verdict.

### What participants are told

The registration page states that network address and a browser fingerprint are recorded during exams to detect duplicate accounts. The fingerprint is built from stable browser properties, hashed client-side, then salted and re-hashed server-side, so the raw components are never stored in reversible form. Nothing is sent to any third party.

---

## Statistics

The unit that matters is the **statement**, not the question — a four-statement block scored 0.2 tells a student almost nothing, but "you get plant water transport statements right 41% of the time, against a cohort average of 68%" is actionable.

- Per-topic accuracy with a cohort baseline marker, lifetime and a recency-weighted moving average
- Strengths and weaknesses, with topics below a data threshold excluded from both
- Specific questions worth reviewing, ranked by statements missed
- Rating history, percentile, score distribution with the participant's own bucket highlighted
- Practice streak and an activity calendar

For organisers, **Admin → exam → analysis** gives classical item analysis: `p` (mean fraction of marks) and `d` (point-biserial correlation with total score). A negative `d` means strong candidates did worse than weak ones on that item — nearly always a flawed key or ambiguous wording.

---

## Architecture

```
app/
  main.py            FastAPI app factory
  config.py          settings from NBO_* environment variables
  db.py              engine, session, Base, UtcDateTime, EnumType
  templating.py      Jinja environment, PageContext, filters
  responses.py       redirect() — commits before redirecting
  models/            SQLAlchemy models (32 tables)
  services/
    scoring.py       IBO partial-credit curves and every grader
    rating.py        Codeforces contest rating
    anticheat.py     access rules, telemetry, flags, collusion
    stats.py         mastery ingestion, insights, item analysis
    exam_engine.py   start / autosave / deadline / submit / finalise
    markup.py        escape-first Markdown subset for question text
    auth.py, i18n.py, slugs.py
  routers/           public, auth, survival, practice, creative, userstats, admin
  templates/         37 Jinja templates
  static/            hand-written CSS, exam player JS, fingerprint JS
  locales/           en/ru/ky catalogues (242 keys each)
scripts/
  import_ibo.py      parse the IBO PDFs into the question bank
  seed_demo.py       demo users, schools and a simulated cohort
  build_locales.py   regenerate the three locale files from one table
alembic/             migrations
tests/               114 tests
```

**Stack:** FastAPI + SQLAlchemy 2.0 + Jinja2, SQLite by default and Postgres-ready. No Node build step and no CDN — all CSS and JS are self-hosted, so a school with a flaky connection cannot lose its styling or its exam player mid-sitting.

### The exam player

`app/static/js/exam.js`, in priority order:

1. **Never lose an answer.** Every change is queued and retried until the server confirms it; the queue survives a dropped connection and is flushed again on reconnect and on page hide.
2. **Never trust the client clock.** The countdown ticks locally for smoothness but re-synchronises against the server's authoritative deadline every 30 seconds.
3. **Navigation works offline.** Every question is already in the DOM; moving between them is a class toggle, not a network request.

The server decides the deadline, the question order, the correct answers and the score. The browser is treated as a display surface that may be lying.

---

## Internationalisation

Every question, statement, figure caption and section title has per-locale rows; UI strings live in `app/locales/*.json`. Locale resolution is `?lang=` → cookie → user preference → `Accept-Language` → default.

To change UI copy, edit the single table in `scripts/build_locales.py` and re-run it — a key missing any of the three languages fails the build, which is what stops the locales drifting apart.

> The Kyrgyz translations were written as part of this build and should be reviewed by a native speaker before a real sitting.

---

## Configuration

Copy `.env.example` to `.env`. Everything has a working development default.

| Variable | Purpose |
| --- | --- |
| `NBO_SECRET_KEY` | signs session cookies and salts fingerprints — **must** be changed for production |
| `NBO_DATABASE_URL` | `sqlite:///./data/nbo.db`, or `postgresql+psycopg://…` |
| `NBO_TRUSTED_PROXY_HOPS` | how far into `X-Forwarded-For` to read the client IP; `0` ignores the header entirely |
| `NBO_DEFAULT_LOCALE` | `ru`, `ky` or `en` |
| `NBO_IP_SHARED_ACCOUNT_THRESHOLD` | accounts per unvouched address before blocking |
| `NBO_COOKIE_SECURE` | set `true` behind HTTPS |

`X-Forwarded-For` is attacker-controlled except for the hops your own proxies append, which is why the header is ignored unless you say how many proxies are in front of the app.

### Migrations

```bash
python -m alembic upgrade head
```

```bash
python -m alembic revision --autogenerate -m "describe the change"
```

The URL comes from `NBO_DATABASE_URL` via `alembic/env.py`, so app and migrations always agree. SQLite runs in batch mode so column changes work there too.

---

## Tests

```bash
python -m pytest
```

114 tests covering the scoring curves and every grader, the rating algorithm, access-control rules, collusion detection, the exam runtime end to end, the Markdown renderer's XSS handling, HTTP flows for all three roles, and regressions for the bugs found while building this.

---

## Known limitations

- **Figure attribution is approximate.** The importer assigns images from the pages a question spans, which can mis-assign a figure that spills onto the page where the next question starts. Authors fix this in the editor.
- **Only the Russian stem is imported.** The 2024 paper is Russian; English and Kyrgyz translation rows are created empty for a translator to fill. Answer-key explanations are imported in English.
- **Browser fingerprinting is evadable** by a determined student with a second device or a fresh browser profile. It raises the cost of a casual retake; it is not a proctoring substitute.
- **Open-response questions** are modelled and can be sat, but the human grading UI is not built — they always score 0 until a grader sets a mark.
- **Email is not wired up.** Password-reset tokens are modelled but nothing sends them.
- **Rate limiting** on login is not implemented; failed attempts are logged to `login_events` but not throttled. Put this behind a reverse proxy that does it, or add it before a public sitting.
