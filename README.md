# SANS — Spoken Archive & Neural System

A personal app, not a product. The repo, server paths and Android package
still say `secondmind`, its working name.

A voice-first personal knowledge graph. You open the app, talk, and forget
about it. Everything else happens server-side.

Two parts, both in this repo:

- `backend/` — Django + Celery + Postgres/pgvector. Transcription,
  normalization, the knowledge base, Ask and Teach. The extraction pipeline
  comes in Phase 1.
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

## Current state — Phase 0 done, Ask and Teach running ahead of Phase 1

The app ships as **SANS** (sideloaded APK). It's in daily use on the
shared server and the loop holds up: capture → transcript → fix → ask.
87 backend tests pass.

What a capture goes through today:

```
POST /api/captures/ -> spool audio -> transcribe (Deepgram) -> raw
                    -> normalize (Gemini re-listens)        -> normalized
                    -> embed note                           -> delete audio
```

Early measurement: a 28-second capture went upload → `transcribed` in **2.7s**,
a duplicate upload of the same id returned 200 without re-billing
transcription, and seeded entities measurably corrected proper nouns on a
re-run of identical audio (see "Accuracy").

### Capture and transcription

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

### Fixing notes (the learning loop)

- Three versions of every note: raw (Deepgram, immutable), normalized
  (Gemini, re-runnable via `/renormalize/`), and **yours**. Your edit
  always wins and nothing server-side overwrites it
  (`capture/editing.py`)
- Saving an edit proposes `CorrectionPair`s from the changed words. Names
  come pre-ticked, ordinary words don't. Kept pairs are applied to every
  future note, and their hit counts are tracked

### Knowledge base

- People, companies, places and projects as semantic nodes, with summary,
  body, aliases, "often misheard as", and `part_of` links. Browse, search
  and edit them in the app; search matches by sound ("starcom" finds Stracomm)
- `manage.py load_knowledge` loads a structured JSON file (entities,
  relations, speaker notes). It's the richer sibling of `seed_entities`.
  The file is personal, so pipe it over stdin on the server; deploy never
  ships it
- `retrieval.py` picks the few entities a note plausibly mentions
  (phonetic + fuzzy match on the draft, 1-hop neighbours, recently active,
  embedding-related) so the normalize prompt stays the same size as the
  knowledge base grows
- **Teach by voice** (`teach.py`): say "Rahul is the new designer at
  Stracomm", get back proposed changes, untick what's wrong, apply.
  Nothing is written before apply, because a misheard name saved here
  would become a keyterm. `Teaching` logs proposed against applied, which
  is how you evaluate the teach prompt

### Ask

- `ask.py`: plan (search terms in English + Hinglish, date range) →
  search four ways (Postgres full-text, note embeddings, entity sound-match,
  time filter) → reciprocal rank fusion → answer only from that context,
  citing notes as `[n]`. Multi-turn via `conversation_id`
- Asked by voice in the app (`spoken.py`): one synchronous Gemini listen
  briefed with your vocabulary and corrections, with Deepgram + romanize as
  the fallback. Clip audio is never stored
- Every question is logged (`Question`, with `spoken` flagged) as a free
  eval set
- Every question also records per-stage wall-clock `timings` (heard,
  plan, search, answer). On prod (2026-10-19, 15 real questions) the
  answer call was 82% of Ask time, almost entirely the model *thinking*:
  8.5s median, 18.5s worst, no better answers. Answers now think at
  `ASK_ANSWER_THINKING=low` (~2.1s median). The prompt also now lets
  SANS advise from its own expertise. Facts about your life still come
  only from your notes

### Not built yet

**Segment, extract, resolve, link.** Notes are embedded and searchable,
but nothing yet splits a session into separate thoughts or extracts
entities and edges from it automatically. Entities only come from you,
through teach, the editor or `load_knowledge`. Mention counts update when
a note names a known entity. `nightly_consolidation` is a stub. See
"Build order".

### Loose ends

- `docker-compose.yml` (local) still runs gunicorn with the default 30s
  timeout and no threads. Prod and shared got `--threads 4 --timeout 90`
  for synchronous Ask/Teach, so a slow answer can be killed locally but
  not in production
- `expo-updates` is installed, but `app.json` has no EAS project or
  updates URL, so **OTA does nothing**. Every app change ships as a new
  APK (see "Deploying")
- No eval harness yet. The `Question` and `Teaching` logs are piling up
  data for one
- The app still has separate tabs, each with its own mic (Capture, Ask,
  Teach). The plan is a single voice button that works out from context
  whether you're making a note, asking something or teaching a fact.
  `spoken.py`, `ask.py` and `teach.py` are already UI-agnostic for that

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

Tests (87, no network or API keys needed — Deepgram and Gemini are stubbed):

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
| PUT / DELETE | `/api/captures/<uuid>/transcript/` | save your edited version (returns suggested corrections) / drop it |
| POST | `/api/captures/<uuid>/renormalize/` | re-run the Gemini pass |
| GET | `/api/graph/?limit=500` | nodes + edges for the circles view |
| GET | `/api/nodes/<uuid>/` | one node and everything edged to it |
| GET / POST | `/api/vocabulary/` | entities used for biasing; add one |
| PATCH / DELETE | `/api/vocabulary/<uuid>/` | |
| GET / POST | `/api/corrections/` | wrong -> right pairs |
| DELETE | `/api/corrections/<uuid>/` | |
| GET | `/api/knowledge/` | the knowledge base, searchable (by sound too) |
| GET / PATCH / DELETE | `/api/knowledge/<uuid>/` | one entry, its links and the notes mentioning it |
| POST / DELETE | `/api/knowledge/<uuid>/links/<uuid>/` | connect / disconnect two entries |
| POST | `/api/ask/` | `{question, conversation_id?}` -> answer with cited notes |
| POST | `/api/ask/spoken/` | multipart `audio` (+ `conversation_id`): heard and answered in one request; `question` is what was heard |
| POST | `/api/knowledge/teach/` | multipart `audio`: what was heard + proposed knowledge changes. Saves nothing |
| POST | `/api/knowledge/teach/<uuid>/apply/` | `{changes: [...]}`, the ones you kept -> written to the knowledge base |

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

**Phase 0 — capture.** Done, plus normalize, hand edits and corrections.
The "use it for two weeks before building extraction" rule still holds.
Real transcripts are what extraction gets written against, and every
hour of extraction work before that is work against imagined data.

**Phase 1 — understanding.** `segment` -> `extract` -> `resolve` -> `link`
-> `embed`, the review inbox, node pages, and the eval harness (~40
hand-labelled real transcripts, scored on every prompt change — without it
the graph degrades silently and you will never know).
*Partly in place:* note and entity embeddings, entity pages in the app, and
`retrieval.match_text`, which resolve will reuse for mention → candidates.
*Still to build:* segment, extract, resolve, link, the review inbox and
the eval harness.

**Phase 2 — retrieval.** Hybrid: BM25 over `tsvector` + vector kNN + graph
expansion + temporal filter, then synthesis with citations. Never
vector-only — proper nouns and jargon are exactly what embeddings are worst
at, and exactly what you'll search for.
*Mostly done, ahead of schedule:* Ask runs keyword, embedding, entity and
time search with RRF and cited answers, by voice. It improves for free
once extraction adds edges for graph expansion.

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

On a box that already runs another project's reverse proxy (how it runs
today), use the shared compose file. Take a backup first whenever a
migration ships:

```bash
ssh <server> 'cd /opt/secondmind/deploy && COMPOSE_FILE=docker-compose.shared.yml ./backup.sh'
COMPOSE_FILE=docker-compose.shared.yml ./deploy/deploy.sh root@<server-ip>
```

The app ships as an APK, because OTA isn't wired:

```bash
cd mobile/android && ./gradlew assembleRelease   # skip prebuild --clean unless native deps changed
./deploy/publish_apk.sh root@<server-ip>          # prints the private download link
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
