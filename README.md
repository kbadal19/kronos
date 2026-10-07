# Second Mind

A voice-first personal knowledge graph. You open the app, talk, and forget
about it. Everything else happens server-side.

Two parts, both in this repo:

- `backend/` — Django + Celery + Postgres/pgvector. Transcription and
  (from Phase 1) the extraction pipeline.
- `mobile/` — the Expo app. Opens with the mic already live; an on-device
  SQLite outbox guarantees a thought survives a dead connection. See
  `mobile/README.md`.

---

## The one thing to understand first

**The raw transcript is the immutable floor of this system.** Audio is
transient — it exists only between upload and a committed transcript, then
it is deleted. Everything *below* the transcript can be regenerated
forever: normalization, segmentation, extraction, entities, edges,
embeddings. Everything can be re-run under a better prompt or a better
model, because no derived row is ever the source of truth.

The consequence, which is deliberate and was chosen with eyes open: you can
re-run extraction over two years of thoughts, but you can never
re-transcribe. If the recognizer mangled a word, it is mangled forever.
That's why §"Accuracy" below matters more than it looks.

---

## Current state — Phase 0

Implemented, and verified end to end against live Deepgram over real HTTP:

```
POST /api/captures/  ->  spool audio  ->  transcribe  ->  raw transcript  ->  delete audio
```

A 28-second capture went upload → `transcribed` in **2.7s**, a duplicate
upload of the same id returned 200 without re-billing transcription, and
seeded entities measurably corrected proper nouns on a re-run of identical
audio (see "Accuracy").

- Idempotent upload keyed on a **client-generated** UUID
- Deepgram Nova-3 batch transcription behind a swappable provider interface
- Hindi/English code-switching: Nova-3 `language=multi`, then a Gemini
  **normalize** pass that re-listens to the audio with your vocabulary,
  corrections and recent notes, and writes Hinglish (Roman script). Raw
  stays untouched. On once `GEMINI_API_KEY` is set; re-run and compare
  with `manage.py normalize_captures --all-with-audio --show`
- Multi-user: keyterms, corrections and a per-user `TranscriptionProfile`
  (languages, script, speaker notes; set in the admin) never cross users
- Keyterm biasing from your own entity names
- Audio deletion with a configurable retention window
- Stall detection and re-queueing
- `manage.py seed_entities` to prime the keyterm flywheel before day one
- Full graph schema (nodes, edges, embeddings, review queue) migrated and
  indexed — so the shape is settled before Phase 1 fills it
- `GET /api/graph/` returns circles-and-lines data, sized by mention count

Not implemented, deliberately: **normalize, segment, extract, resolve,
link, embed**. Those stages need to be written against two weeks of *your
real transcripts*, not imagined ones. Their prompts are the part of this
system most likely to be wrong if guessed at. See "Build order".

---

## Running it locally

```bash
cd backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in DEEPGRAM_API_KEY at minimum

createdb secondmind           # needs Postgres with pgvector:
                              #   brew install postgresql@17 pgvector
./venv/bin/python manage.py migrate
./venv/bin/python manage.py createsuperuser
./venv/bin/python manage.py runserver
```

Or the whole stack, which is what production runs:

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

Tests (28, no network or API keys needed — the provider is stubbed):

```bash
cd backend && ./venv/bin/python manage.py test
```

And the app:

```bash
cd mobile && npm install && npx expo run:android
```

---

## API

| Method | Path | |
|---|---|---|
| POST | `/api/auth/token/` | username + password -> JWT pair |
| POST | `/api/auth/token/refresh/` | |
| POST | `/api/captures/` | multipart upload: `id`, `audio`, `captured_at`, `duration_seconds`, `device` |
| GET | `/api/captures/list/` | the timeline, paginated 50/page |
| GET | `/api/captures/health/` | pending / failed counts — poll this |
| GET | `/api/captures/<uuid>/` | one capture with transcripts and segments |
| GET | `/api/graph/?limit=500` | nodes + edges for the circles view |
| GET | `/api/nodes/<uuid>/` | one node and everything edged to it |

`POST /api/captures/` returns **202** for a new capture and **200** for one
already seen. The app's outbox should treat both as success and stop
retrying — that distinction is what makes a lost ack at 4am harmless.

---

## Accuracy

The lever is not the STT model. For pure English the top batch models are
within noise of each other. What breaks is *your* proper nouns: "Project
CB", "Credence Marketings", "Navjyoti", "RCA".

This was measured, not assumed. Same 28-second recording, run through
Nova-3 twice — once cold, once with entity keyterms:

| Without keyterms | With keyterms |
|---|---|
| "project CB" | **"Project CB"** |
| "Credence **marketing's** team" | "Credence **Marketing's** team" *(partial)* |
| "**master flow** proposal module" | "**masterflow** proposal module" |

**Deepgram reported `confidence=0.999` on both runs.** The recognizer is
exactly as certain when it mangles your proper nouns as when it gets them
right, so confidence score will never flag these. Only the entity loop
catches them — and, as "Marketing's" shows, keyterms get you most of the
way but not all of it. That's what layer 2 is for.

Three things, in order of payoff:

1. **Keyterm biasing** (`pipeline/keyterms.py`) — your top ~200 semantic
   node names and aliases are passed to the recognizer on every call. This
   is a flywheel: more entities -> better transcription -> better entities.
2. **A normalization pass** (`pipeline/normalize.py`) — Gemini re-listens
   to the audio with the same list, your corrections and recent notes, fixes
   proper nouns and Hindi words, and adds punctuation. Writes a
   `normalized` transcript; never touches `raw`.
3. **`CorrectionPair`** — every transcript you fix by hand becomes a
   (wrong -> right) pair fed into pass 2. Twenty of these fix most of what
   the recognizer routinely mangles about your vocabulary.

### Seed the vocabulary before your first capture

The flywheel cannot start itself: biasing is built from entities you
already have, and on day one there are none. That's backwards, because
your first weeks of transcripts are the ones that become the eval set and
the corpus you tune extraction against — accuracy matters most exactly
when the system is least able to help itself.

Ten minutes fixes it:

```bash
cp backend/entities.example.txt backend/entities.txt   # make it yours
python manage.py seed_entities --user <you> entities.txt
```

Write down the proper nouns you know you'll say: clients, people, places,
projects, recurring ideas. Re-running is safe — existing entities gain new
aliases rather than duplicating.

**Before you turn audio retention off, run the bake-off.** Record ~20
minutes of your actual voice — the technical rambling, the mumbling, the
4am half-sentences — and run it through two or three providers. Don't pick
from a benchmark table. `TRANSCRIPTION_PROVIDER` makes the swap an env
change; adding a provider is a ~60 line file in `pipeline/providers/`.

---

## Data model in one paragraph

**One `node` table, not two.** Separate `knowledge_items` and `entities`
tables would force polymorphic edges (from_type/from_id pairs), which
throws away foreign keys and makes every traversal a union. Instead there's
one table with a `kind` discriminator split into two families that behave
differently: *episodic* nodes (things you said — one per segment,
immutable) and *semantic* nodes (things that accumulate — aliases, merging,
and the circles in the graph view). Twelve relation types, and resist
adding a thirteenth for six months. Three timestamps — `occurred_at`,
`captured_at`, `ingested_at` — because "last Tuesday I realised..." said at
4am on a Friday has to resolve to Tuesday.

Nothing is ever hard-deleted. Reprocessing writes new rows against a new
`ExtractionRun` and flips the old ones to `superseded`.

---

## Build order

**Phase 0 — capture only.** Done. Now *use it for two weeks and build
nothing.*

That is not a pause, it's the highest-value step in the project. You get
real audio to benchmark providers against, real transcripts to write the
extractor against, your actual topics-per-session distribution, and honest
evidence about whether you'll really talk to it at 4am. Every hour of
extraction work done before this is work done against imagined data.

**Phase 1 — understanding.** `segment` -> `extract` -> `resolve` -> `link`
-> `embed`, the review inbox, node pages, and the eval harness (~40
hand-labelled real transcripts, scored on every prompt change — without it
the graph degrades silently and you will never know).

**Phase 2 — retrieval.** Hybrid: BM25 over `tsvector` + vector kNN + graph
expansion + temporal filter, then synthesis with citations. Never
vector-only — proper nouns and jargon are exactly what embeddings are worst
at, and exactly what you'll search for.

**Phase 3 — consolidation.** The nightly job: recurring themes,
contradictions with past decisions, and the highest-signal output of the
whole system — "you've raised this problem six times since March."

**Phase 4 — the graph UI.** Circles sized by `sqrt(mention_count)`, straight
lines, canvas not SVG, on a desktop browser. Worth building past ~200-300
semantic nodes; below that it's a hairball that tells you less than the
timeline. `mention_count` already ships, so it's a two-day build whenever
you decide it's time.

---

## Deploying

```bash
cp backend/.env.example backend/.env    # fill it in
./deploy/deploy.sh root@<server-ip>
```

Hetzner CPX22 (2 vCPU / 4GB / 80GB, ~EUR 8/mo) is the target. 4GB is
genuinely enough; only local embeddings would push you to 8GB, which is why
this uses API embeddings.

Then set up backups and **test the restore once**:

```bash
age-keygen -o ~/.age/secondmind.key      # keep the private half OFF the server
# put the public half in BACKUP_AGE_PUBLIC_KEY
crontab -e   # 0 3 * * * cd /opt/secondmind/deploy && ./backup.sh
./deploy/restore.sh <backup-name> ~/.age/secondmind.key
```

With audio discarded, that dump is the only copy of your transcripts that
exists anywhere. An untested backup is not a backup.

---

## Known gotchas

Each of these cost real debugging time. They are written down so they
cost it once.

- **Celery's prefork pool is broken under `spawn`.** `celery/app/trace.py`
  → `fast_trace_task` reads a module-global `_loc` that is only populated
  in the child by `fork()`. macOS uses spawn, so every task dies with
  `ValueError: not enough values to unpack (expected 3, got 0)` — the task
  is *received*, then the handler crashes, so the queue looks healthy
  while nothing is processed. Both compose files use
  `--pool=threads --concurrency=4`, which is the better choice anyway:
  these tasks are ~100% I/O wait on Deepgram and Gemini, so processes buy
  nothing and cost memory a 4GB box doesn't have.
- **pgvector 0.4.1's `VectorExtension` is broken on Django 6.0** — it
  overrides `__init__` without calling `super()`, so `self.hints` is never
  set and `database_forwards` raises `AttributeError`. `graph/migrations/0001_extensions.py`
  uses Django's own `CreateExtension("vector")` instead. Revisit if you
  upgrade pgvector.
- **The API is JWT-only** (no `SessionAuthentication`). In tests use DRF's
  `APIClient().force_authenticate()`; Django's `client.force_login()` will
  leave you with a 401.
- **The upload view enqueues via `transaction.on_commit`** so a worker
  can't grab the job before the row is visible. In `TestCase`, wrap calls
  in `self.captureOnCommitCallbacks(execute=True)` or the chain silently
  never runs.
