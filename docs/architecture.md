# Architecture

How the system is put together, what each boundary is for, and where to change things.

---

## 1. Topology

```
┌───────────────────────────────────────────────────────────────────────┐
│  Browser                                                              │
│  React SPA  ──  chat pane  │  artifact viewer (sandboxed iframe)      │
└───────────────────────────┬───────────────────────────────────────────┘
                            │  JSON over HTTP, same origin
┌───────────────────────────▼───────────────────────────────────────────┐
│  FastAPI (app/)                                                       │
│                                                                       │
│   api/routes.py      validate → delegate → shape response             │
│        │                                                              │
│   agent/orchestrator.py    route → retrieve → generate → verify       │
│        ├── agent/router.py         deterministic skill selection      │
│        ├── rag/retriever.py        BM25 + pgvector, RRF fusion        │
│        ├── skills/{qa,ship30,artifact}.py   prompts + validators      │
│        └── agent/registry.py       provider selection + fallback      │
│                 ├── ollama_provider.py    (local, demo default)       │
│                 ├── openai_compat.py      (groq, openai)              │
│                 └── anthropic_agent.py    (Claude Agent SDK)          │
│                                                                       │
│   db/  SQLAlchemy 2.0 async                                           │
└───────────┬───────────────────────────────────┬───────────────────────┘
            │                                   │
┌───────────▼─────────────┐        ┌────────────▼──────────────┐
│  PostgreSQL 16          │        │  Ollama (on the host)     │
│  + pgvector             │        │  llama3.2                 │
│  sessions, messages,    │        │  nomic-embed-text         │
│  artifacts, documents,  │        └───────────────────────────┘
│  chunks(vector 768)     │
└─────────────────────────┘
```

Ollama runs on the **host**, not in a container: it is the user's local model runtime and often has GPU access a container would not. Containers reach it through `host.docker.internal`.

---

## 2. Component boundaries

The rule the codebase follows: **each layer knows only the layer directly beneath it.**

| Layer | Owns | Must not contain |
|---|---|---|
| `api/` | HTTP contracts, validation, response shaping | Prompt text, retrieval logic, provider names |
| `agent/orchestrator.py` | Turn sequencing, skill dispatch, citation resolution | HTTP, SQL, provider SDKs |
| `agent/router.py` | Skill selection | Anything I/O |
| `skills/` | Prompts, output validation, sanitisation | Provider or database knowledge |
| `agent/registry.py` + `providers/` | Model calls, fallback | Prompts, retrieval |
| `rag/` | Parse, chunk, embed, index, search | Prompts, HTTP |
| `db/` | Schema and sessions | Business logic |
| `core/` | Config, logging, errors | Everything else |

Practical consequence: the entire agent layer is testable without an HTTP client, and the retrieval layer without a database.

**`app/core/config.py` is the only module that reads the environment.** Nothing else calls `os.environ`. That is what makes the model toggle a single, auditable switch.

---

## 3. Database schema

Two independent groups. Re-ingesting the corpus never touches conversations; wiping conversations never forces a re-index.

### Conversation state

**`sessions`**

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | UUID4 |
| `title` | varchar(200) | Auto-set from the first message |
| `user_id` | varchar(120) | Caller-supplied. The seam for auth (PRD A1) |
| `user_agent` | varchar(400) null | Request metadata |
| `provider`, `model` | varchar | In force at creation |
| `extra` | JSON | Caller metadata |
| `created_at`, `updated_at` | timestamptz | |

Index: `(user_id, updated_at)` for the scoped, ordered sidebar query.

**`messages`**

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | |
| `session_id` | FK → sessions, **ON DELETE CASCADE** | Session isolation is enforced here, not in code |
| `role` | varchar(16) | user / assistant / system |
| `content` | text | |
| `provider`, `model`, `latency_ms` | | What actually served this turn, which can differ from the session default after a fallback |
| `usage` | JSON | Token counts plus the full provider attempt trail |
| `citations` | JSON | Resolved citations, denormalised so re-rendering history needs no re-query |
| `route` | varchar(40) | Which skill handled it |
| `created_at` | timestamptz | |

Index: `(session_id, created_at)`.

**`artifacts`**

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | |
| `session_id` | FK CASCADE | |
| `message_id` | FK SET NULL | |
| `kind` | varchar(16) | markdown / html |
| `content` | text | **Sanitised**, render-ready |
| `raw_content` | text | Exactly what the model produced |
| `sanitiser_report` | JSON | What was stripped and why |

Both `content` and `raw_content` are stored on purpose: a sanitiser decision can be audited after the fact without re-running the model.

### Knowledge base

**`documents`** — `source_path` (unique), `title`, `doc_type`, `guest`, `published_at`, `source_url`, `word_count`, `checksum`, `ingested_at`.

`checksum` is the sha256 of the file and drives incremental refresh.

**`chunks`** — `document_id` (FK CASCADE), `ordinal`, `text`, `speakers`, `start_timestamp`, `token_estimate`, `embedding vector(768)`.

`embedding` is **nullable**, and that is load-bearing: it lets ingestion succeed when Ollama is unreachable, leaving a fully usable lexical index that a later run fills in. Unique on `(document_id, ordinal)`.

Schema is created with `Base.metadata.create_all` at startup plus `CREATE EXTENSION IF NOT EXISTS vector`. A missing pgvector extension logs a warning and degrades to lexical-only rather than refusing to boot. For a team that needs versioned migrations, this is where Alembic goes; it was left out rather than shipped unused.

---

## 4. API

All responses carry `x-request-id`. All errors share one envelope.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health/live` | Liveness. Touches nothing. |
| GET | `/api/health` | Deep health: DB, corpus, every provider, embeddings, plus a `checks[]` array of human-readable next steps |
| GET | `/api/config` | Non-secret runtime config for the UI badge |
| POST | `/api/sessions` | Start a chat → 201 |
| GET | `/api/sessions` | List, `user_id` and `limit` filters |
| GET | `/api/sessions/{id}` | Chat with full history and artifacts |
| DELETE | `/api/sessions/{id}` | → 204, cascades |
| POST | `/api/sessions/{id}/messages` | **The main endpoint.** Route, retrieve, generate, persist, return |
| GET | `/api/artifacts/{id}` | Fetch a rendered artifact |
| GET | `/api/corpus` | Index statistics |
| POST | `/api/corpus/reindex` | Re-ingest. Synchronous: seconds at this scale, and a result beats a job id you then have to poll |

### Error envelope

```json
{
  "error": {
    "code": "provider_unavailable",
    "message": "Cannot reach Ollama at http://localhost:11434...",
    "hint": "For Ollama, confirm `ollama serve` is running and the model is pulled.",
    "details": { "provider": "ollama" },
    "request_id": "a1b2c3d4e5f6"
  }
}
```

`code` is stable and machine-readable. `hint` is written for whoever has to fix it. Codes: `session_not_found`, `artifact_not_found`, `corpus_not_indexed`, `provider_unavailable`, `provider_timeout`, `provider_not_configured`, `all_providers_failed`, `database_unavailable`, `validation_failed`, `unsafe_artifact`.

### The main response

`POST /api/sessions/{id}/messages` returns the user message, the assistant message, any artifact, and the diagnostics the UI needs to explain itself: `route`, `retrieval_strategy`, `retrieved_chunks`, `provider_attempts`, `grounded`, `citations_matched`, `essay_critique`.

`citations_matched` deserves a note. When it is `false`, the model produced no resolvable `[n]` markers, so `citations` are the passages the answer was written *from* rather than markers it actually used. The UI labels the two differently. Presenting the second as the first would be exactly the kind of false precision this product must not ship.

---

## 5. Ingestion and retrieval

### Corpus licensing (read this before changing ingestion)

Lenny's Data licenses the starter dataset for personal, non-commercial use and **explicitly forbids redistributing the raw files**. So:

- `data/corpus/` is in `.gitignore` and **no transcript is committed**
- `scripts/ingest.py` clones the upstream repository at setup time
- Docker keeps the corpus in a named volume

Changing this would make the repository non-compliant.

### Pipeline

```
git clone ─→ parse ─→ chunk ─→ store ─→ embed ─→ index
             │        │        │        │        │
             │        │        │        │        └─ in-memory BM25
             │        │        │        └─ Ollama, batched, optional
             │        │        └─ Postgres, replace-per-document
             │        └─ group turns to ~1000 tokens
             └─ frontmatter + speaker turns
```

**Parsing** (`rag/corpus.py`). Two shapes: podcasts are `**Speaker** (HH:MM:SS):` turns; newsletters are prose with headings. Both become `Segment` objects carrying speaker and timestamp where they exist. That metadata is what turns a citation from "somewhere in a three-hour episode" into a deep link.

The URL comes from the first populated key of `post_url`, `youtube_url`, `url`, `link`, `episode_url`. The corpus is not uniform: 34 of 60 files use `youtube_url`. Reading only `post_url` silently produced unlinked citations for over half the knowledge base.

**Chunking** (`rag/chunker.py`). Group whole segments to ~1,000 tokens, never splitting a speaker turn, with one segment of overlap. A turn is the smallest self-contained unit of meaning in an interview; splitting one produces a chunk that quotes a subject without its verb. The chunk inherits the **first** timestamp in its group, because that is where the passage starts.

**Refresh.** Content-addressed. Unchanged sha256 means the document is skipped entirely. Verified: a second run over an unchanged corpus reported 60 skipped, 0 re-chunked.

**Embedding.** Batches of 32 via Ollama. If it fails, ingestion logs what remains and completes anyway.

### Retrieval

```
query ─┬─→ BM25            → gate: IDF-weighted coverage ≥ 0.45 ─┐
       └─→ pgvector <=>    → gate: cosine distance ≤ 0.45 ───────┴─→ RRF → top_k
```

**Gating happens before fusion, and this is the single most important design point in the retrieval stack.** RRF scores encode rank agreement, not relevance: a single-list top hit always scores 1/(60+1) regardless of match quality. Thresholding the fused score therefore *cannot* detect "the corpus does not cover this". The first implementation did exactly that and returned zero results for every query.

**Why coverage rather than BM25 score.** BM25 magnitude does not separate relevant from irrelevant on this corpus. `"zzzxqv nonsense token"` scores 7.8 because "token" appears everywhere, against 10 to 14 for real questions: overlapping ranges. IDF-weighted coverage asks a different question, "what share of this query's *information* is actually present?", and a term the corpus has never seen carries maximum IDF, dragging coverage down.

Measured over 25 labelled queries: in-corpus floor 0.497, out-of-corpus ceiling 0.519. Threshold 0.45. The single overlap is `"translate good morning into Japanese"`, which the Duolingo content arguably does cover.

**Dense threshold** measured against nomic-embed-text: in-corpus top distance ≤ 0.396, out-of-corpus ≥ 0.505. Threshold 0.45. **This is model-specific**: re-measure if you change `EMBEDDING_MODEL`.

**Why RRF over weighted blending.** BM25 scores are unbounded and corpus-dependent; cosine distances sit in a fixed range. Blending them needs constants that must be re-tuned whenever the corpus changes. RRF consumes only rank, so it is scale-free.

**Known limitation.** BM25 does not stem: `"price"` does not match `"Pricing"`. The dense half covers this in normal operation. Adding a stemmer would also shift the coverage distribution the 0.45 threshold is calibrated against, so it is a deliberate change with a re-tune, not a one-line patch. Tested explicitly.

---

## 6. Agent routing

```
message ─→ router.route(message, forced_skill)
              │
              ├─ forced_skill set?  → use it, confidence 1.0
              └─ else regex intent rules, biased toward QA
                     │
       ┌─────────────┼─────────────┐
     qa        ship30_essay    artifact
```

**Rules, not a classifier.** Asking llama3.2 to emit a skill label costs a full extra round trip (seconds, locally) and still misfires. The signal is close to explicit anyway: users say "write an essay" or "make an HTML one-pager". Rules are faster, free, unit-testable, and legible: you can read *why* a message routed the way it did.

**The escape hatch matters more than the rules.** The UI exposes the three skills as buttons, and an explicit choice always wins. The rules only have to handle the conversational case.

**Biased toward QA on purpose.** A wrong QA route costs a slightly plain answer. A wrong essay route produces 1,250 words nobody asked for. A leading question word beats an incidental "document" or "page".

### Skills

| Skill | Output | Verification |
|---|---|---|
| `qa` | Cited prose | Citation markers resolved against retrieved chunks |
| `ship30_essay` | ~1,250 word Markdown essay | `ship30.critique()`, one repair pass on failure |
| `artifact` | Markdown or HTML document | Extraction, sanitiser, one strict retry if the format was ignored |

**The Ship 30 skill encodes its principles as data**, not prose in a prompt: the six single-sentence openers, the five headline elements, the formatting rules, the 1/3/1 rhythm, all sourced from the published material (URLs in `SOURCES`). They are used twice: composed deterministically into the prompt, and used by `critique()` to measure the result. A prompt can only ask; a validator can tell you whether you got it. When a draft fails, the repair instruction names the specific defects rather than saying "make it better", and the critique is returned to the UI either way, so a still-imperfect essay is visibly imperfect.

---

## 7. Model configuration and fallback

Switching provider is one environment variable. `app/core/config.py` is the only reader.

| Provider | Transport | Notes |
|---|---|---|
| `ollama` | `/api/chat` | Demo default. Keyless. Reports real token counts |
| `groq` | OpenAI-compatible | Same class as OpenAI, different base URL |
| `openai` | OpenAI-compatible | |
| `anthropic` | **Claude Agent SDK** `query()` | All built-in tools denied, `setting_sources=[]` |

`LLM_FALLBACK_CHAIN` lists who to try next. The chain advances on **infrastructure** failures: unreachable, timed out, rate limited, missing or rejected key, and empty completion (a real failure mode on small local models). It does **not** advance on a successful call whose answer we dislike, and it does **not** swallow genuine bugs: a `TypeError` propagates rather than being retried, because silently falling through would turn a defect into a latency problem and hide it.

Every attempt is recorded on the message row, so "why did this answer come from Groq when the UI said Ollama?" is answerable after the fact.

### The Claude Agent SDK path, stated honestly

The brief specifies the Claude Agent SDK or Pi for the agent layer; this engagement was directed to use Groq and Ollama. The SDK path is implemented and reviewable but **verified only as far as no API key allows**: imports, configuration, health reporting. Not a live call. Enable it with `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

The Python SDK bundles its own Claude Code binary, so no separate Node install is needed on supported platforms.

---

## 8. Artifact security

Generated HTML is **untrusted input**. The model writes it after reading a user message and passages retrieved by similarity to that message. The realistic attacks are stored XSS, exfiltration beacons, and UI redress.

### Three independent layers

**1. Server-side allowlist sanitiser** (`skills/sanitizer.py`). An allowlist, not a blocklist, so a tag nobody thought about fails closed. Removes every `on*` handler, non-`https`/`data:` URLs, and `script`/`iframe`/`form`/`object`/`embed`/`svg` **with their contents** (unwrapping `<script>` would just move the payload into the body). CSS is cleaned in three passes: `@import` at-rules, any `url()` that is not an inline image, and `expression()`/`javascript:`/`-moz-binding`. Unknown tags are *unwrapped* rather than dropped, so legitimate text survives.

**2. Sandboxed iframe.** `srcdoc` with `sandbox=""`: **zero tokens**, so no scripts, no same-origin, no forms, no navigation, no popups. Never add `allow-scripts` together with `allow-same-origin`, because that combination lets the frame remove its own sandbox.

**3. Content Security Policy** inside the document: `default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.

Verified in-browser, not merely asserted:

```js
iframe.sandbox.length      // 0
iframe.contentDocument     // throws TypeError - opaque origin
```

### What is permitted, and why that is not a compromise

Structural markup, text formatting, tables, inline styles, links (forced to `target="_blank" rel="noopener noreferrer nofollow"`), and `data:` URI images. Artifacts are *documents*. None of that needs script or network access, so blocking both costs the product nothing real.

**Markdown artifacts take a different path**: `marked` → DOMPurify → rendered inline. Markdown's output surface is narrow enough that a well-audited allowlist is proportionate, and it needs the app's typography. Chat answers are sanitised the same way, because model output is untrusted wherever it appears.

The viewer's **Blocked tab** shows exactly what was removed. A security control nobody can see is a security control nobody trusts.

---

## 9. Observability

Structured JSON via structlog. Every line carries `request_id`, bound per request and echoed in the `x-request-id` header and every error body.

Key events, chosen so each failure domain is diagnosable from logs alone:

| Event | Tells you |
|---|---|
| `http.request` | method, path, status, duration |
| `retrieval.search` | strategy, candidates vs. hits per list, **best_coverage**, returned |
| `turn.complete` | skill, provider, model, latency, citations, fallback_used |
| `turn.no_evidence` | the gate refused |
| `citations.unmatched` | the model produced no resolvable markers |
| `provider.failed` / `provider.fallback_used` | which provider, which error code |
| `artifact.created` | kind, whether sanitised, which tags were removed |
| `artifact.format_retry` | the model ignored the output format |
| `ship30.repair` | which validator checks failed |
| `embeddings.unavailable` | dense retrieval is off, and the impact |

`best_coverage` is the first field to look at when the assistant refuses something it should have answered.

Health polling is excluded from the request log so it does not drown everything else.

---

## 10. Resilience

Startup is **survivable**: a missing database, empty index or unreachable Ollama does not stop the process from booting. A server that refuses to start gives an operator nothing to debug with. It comes up, logs precisely what is wrong, and reports it on `/api/health`.

| Failure | Behaviour |
|---|---|
| Postgres down | Boot continues; requests return 503 `database_unavailable`; `pool_pre_ping` recovers without restart |
| pgvector missing | Warning; lexical-only |
| Ollama down | 503 naming `ollama serve`; fallback chain advances |
| Model not pulled | Error names the exact `ollama pull` command |
| Embedding model missing | Ingestion completes; lexical-only; health says how to fix it |
| Cloud key missing | Provider reports `configured: false`; never attempted |
| Rate limited | Treated as unavailable so the chain advances |
| Model timeout | `provider_timeout`, suggests raising `LLM_TIMEOUT_SECONDS` |
| Empty completion | Treated as failure; chain advances |
| Empty retrieval | Honest refusal, no model call |
| Index empty | 503 `corpus_not_indexed`, names the ingest command |
| Malformed model output | Four-strategy extraction, then one strict retry |
| Malformed HTML | Sanitiser never raises |
| Corpus refresh fails | Falls back to the existing copy |

---

## 11. Deployment

`docker compose up --build` starts three services: `db` (pgvector/pg16, healthchecked), `ingest` (runs to completion, `api` waits for `service_completed_successfully`), and `api` (multi-stage build; Node builds the SPA, the runtime image ships only Python plus the static bundle, running as uid 10001).

The API serves the built SPA, so the whole product is one origin and one port. In development, Vite on :5173 proxies `/api` to :8000.

Two named volumes: `pgdata` and `corpus`. The corpus volume means a rebuild does not re-download.

**To deploy elsewhere:** point `DATABASE_URL` at managed Postgres (Supabase or Railway; pgvector optional, lexical still works), set `LLM_PROVIDER` to a cloud provider since Ollama will not be reachable, and run ingestion once against the new database. Note A3 in the PRD before scaling to multiple workers: the BM25 index is per-process.
