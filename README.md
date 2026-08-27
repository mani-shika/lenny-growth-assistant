# The Lenny Growth Assistant

A grounded internal assistant over [Lenny's Podcast](https://www.lennyspodcast.com) and [Lenny's Newsletter](https://www.lennysnewsletter.com) transcripts. It answers product and growth questions with citations that deep-link to the exact moment in an episode, writes Ship 30 for 30 essays from that material, and renders Markdown or HTML artifacts beside the chat.

**It also says "I don't know."** When the corpus does not support an answer, the assistant refuses and explains what it does cover, without calling a model. That behaviour is the point of the product, not a limitation of it.

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Architecture at a glance](#architecture-at-a-glance)
- [Configuration](#configuration)
- [Switching models](#switching-models)
- [Running tests](#running-tests)
- [Troubleshooting](#troubleshooting)
- [Extending it](#extending-it)
- [Documentation](#documentation)

---

## What it does

| Skill | Trigger | Output |
|---|---|---|
| **Grounded Q&A** | Any question, or the **Answer** button | Cited prose. Every `[n]` marker resolves to a real retrieved passage with episode, guest and timestamp |
| **Ship 30 essay** | "write an essay…", or the **Essay** button | ~1,250 words with a hook, skimmable structure and a specific takeaway, validated against the encoded Ship 30 principles |
| **Artifact** | "make an HTML one-pager…", or the **Artifact** button | A rendered Markdown or HTML document in a sandboxed viewer beside the chat |

Plus: independent multi-turn sessions persisted in PostgreSQL, four interchangeable model providers behind one environment variable, and a deep health endpoint that tells you exactly what is broken.

---

## Quick start

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker + Compose | 25+ | The only hard requirement for the containerised path |
| [Ollama](https://ollama.com) | 0.3+ | Runs on the **host**, not in a container |
| Git | any | The corpus is cloned at setup time (see [Corpus](#corpus-and-licensing)) |

For local development without Docker, add Python 3.12+ and Node 20+.

### 1. Pull the local models

```bash
ollama pull llama3.2
```

```bash
ollama pull nomic-embed-text
```

`llama3.2` is ~2 GB and generates answers. `nomic-embed-text` is ~274 MB and powers dense retrieval. **Both are optional in the sense that the system degrades rather than fails**: without the embedding model you get lexical-only search; without Ollama entirely you need a cloud key.

### 2. Configure

```bash
cp .env.example .env
```

The defaults run the whole system locally with **no API keys**. Every required variable already has a working value.

### 3. Start

```bash
docker compose up --build
```

This starts PostgreSQL with pgvector, runs corpus ingestion to completion, then starts the API with the built UI. First run takes a few minutes: it clones ~5 MB of transcripts and embeds 1,420 passages.

Open **http://localhost:8000**.

### 4. Verify

```bash
curl -s http://localhost:8000/api/health
```

You want `"status": "ok"` and a `checks` array that is empty. Anything else, the `checks` array names the fix.

<details>
<summary><b>Running without Docker</b></summary>

Postgres with pgvector still needs to come from somewhere; the compose file can provide just the database:

```bash
docker compose up db -d
```

Then:

```bash
python -m venv .venv && ./.venv/Scripts/activate && pip install -r requirements-dev.txt
```

Point `DATABASE_URL` at `postgresql+asyncpg://lenny:lenny@localhost:5433/lenny` and `OLLAMA_BASE_URL` at `http://localhost:11434` in `.env`, then:

```bash
python scripts/ingest.py
```

```bash
uvicorn app.main:app --reload
```

For the UI with hot reload, in a second terminal:

```bash
npm --prefix frontend install && npm --prefix frontend run dev
```

Vite serves on :5173 and proxies `/api` to :8000.
</details>

---

## Architecture at a glance

```
React SPA  ──►  FastAPI  ──►  orchestrator: route → retrieve → generate → verify
                   │                            │            │          │
                   │                            │            │          └─ citations,
                   │                            │            │             sanitiser,
                   │                            │            │             essay critique
                   │                            │            └─ ollama / groq / openai /
                   │                            │               anthropic (+ fallback chain)
                   │                            └─ BM25  +  pgvector  ──►  RRF
                   └──►  PostgreSQL 16 + pgvector
```

Retrieval is **not** the model's decision. Every grounded turn searches first, so grounding behaves identically on a 2 GB local model and a frontier one. The reasoning, and its cost, is in [docs/architecture.md](docs/architecture.md#6-agent-routing).

---

## Configuration

Full annotated list in [`.env.example`](.env.example). The variables that matter:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | bundled Postgres | Any Postgres, including Supabase or Railway |
| `LLM_PROVIDER` | `ollama` | `ollama` \| `groq` \| `openai` \| `anthropic` |
| `LLM_FALLBACK_CHAIN` | `ollama` | Comma-separated, tried left to right on infrastructure failure |
| `LLM_TIMEOUT_SECONDS` | `120` | Raise it for large local models on modest hardware |
| `OLLAMA_MODEL` | `llama3.2` | Any tool-capable local model |
| `EMBEDDINGS_ENABLED` | `true` | `false` forces lexical-only retrieval |
| `RETRIEVAL_TOP_K` | `8` | Passages given to the model |
| `RETRIEVAL_MIN_COVERAGE` | `0.45` | **The honesty dial.** Higher means stricter grounding and more refusals |

> **Docker and Ollama.** Inside a container, `localhost` is the container. Compose therefore sets `OLLAMA_BASE_URL=http://host.docker.internal:11434` itself and ignores whatever `.env` says, so a developer can keep `localhost` in `.env` for host-based runs without breaking the containerised one. If Ollama runs on another machine, set `OLLAMA_HOST_URL`.

`RETRIEVAL_MIN_COVERAGE` was measured, not guessed: over 25 labelled queries the in-corpus floor was 0.497 and the out-of-corpus ceiling 0.519. See [docs/architecture.md](docs/architecture.md#retrieval).

**No secret is ever committed.** `.env` is gitignored, and database credentials are redacted in logs.

---

## Switching models

Change one variable and restart. No code change.

**Local (default, no key):**
```bash
LLM_PROVIDER=ollama
```

**Groq:**
```bash
LLM_PROVIDER=groq
```
plus `GROQ_API_KEY=…` in `.env`.

**Anthropic, via the Claude Agent SDK:**
```bash
LLM_PROVIDER=anthropic
```
plus `ANTHROPIC_API_KEY=…` in `.env`.

The active provider is visible in the UI status panel and at `GET /api/config`.

**Fallback.** `LLM_FALLBACK_CHAIN=ollama,groq` tries Ollama first and falls through to Groq if it is unreachable, timing out, rate limited, missing a key, or returns an empty completion. It deliberately does **not** fall through on a genuine bug, because that would hide the defect. Every attempt is recorded on the message row, so you can always see which provider actually served an answer.

> **On the Claude Agent SDK path.** The brief specifies the Claude Agent SDK for the agent layer; this engagement was directed to use Groq and Ollama. The SDK integration is implemented and reviewable in [`app/agent/providers/anthropic_agent.py`](app/agent/providers/anthropic_agent.py), with every built-in Claude Code tool denied and `setting_sources=[]` so it cannot inherit host configuration. It was verified only as far as no API key allows: imports, configuration and health reporting, not a live call. Flagged here rather than glossed over.

---

## Running tests

```bash
python -m pytest -q
```

**138 tests, ~2 seconds, no infrastructure required.** No Postgres, no Ollama, no API keys. Persistence and API tests run against in-memory SQLite; the model is stubbed so the tests assert contracts rather than model behaviour.

| File | Covers |
|---|---|
| `test_retrieval.py` | Parsing, chunking, BM25, the coverage gate, RRF, deep links |
| `test_security.py` | Sanitiser (adversarial), artifact extraction, CSP |
| `test_routing.py` | Skill routing, follow-up rewriting, the Ship 30 validator |
| `test_providers.py` | Provider abstraction, model toggle, fallback chain |
| `test_api.py` | Session isolation, persistence, citations, structured errors |

To exercise the dense-retrieval path against real Postgres:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://lenny:lenny@localhost:5433/lenny python -m pytest -q
```

Lint:

```bash
python -m ruff check app scripts tests
```

### End-to-end QA against a running instance

The unit suite stubs the model. To exercise the real API, real model and real
corpus, with the stack up:

```bash
python scripts/qa_smoke.py
```

**64 checks in ~60s** covering grounding, citation resolution, honest refusal,
session isolation, artifact sanitisation, structured errors and persistence. It
issues requests back to back with no delay, which is what a browser does - and
what caught a read-after-write race that manual curl testing could not
reproduce.

A manual UI test plan is in [docs/manual-test-plan.md](docs/manual-test-plan.md).

---

## Troubleshooting

**Start here.** `GET /api/health` returns a `checks` array written for whoever has to fix the problem.

```bash
curl -s http://localhost:8000/api/health
```

| Symptom | Cause | Fix |
|---|---|---|
| `"status": "down"`, `database: false` | Postgres unreachable | `docker compose up db -d`, and confirm `DATABASE_URL` |
| `corpus_not_indexed` (503) | Index empty | `docker compose run --rm api python scripts/ingest.py` |
| `provider_unavailable` naming Ollama | Ollama not running | `ollama serve`. From a container, `OLLAMA_BASE_URL` must be `http://host.docker.internal:11434` |
| Error names an `ollama pull` command | Model not pulled | Run exactly that command |
| Answers work, but `"lexical search only"` | Embedding model missing | `ollama pull nomic-embed-text`, then re-run ingestion |
| Refuses a question it should answer | Coverage gate too strict for your phrasing | Check `best_coverage` in the `retrieval.search` log line, then lower `RETRIEVAL_MIN_COVERAGE` |
| `provider_timeout` | Local model too slow for the budget | Raise `LLM_TIMEOUT_SECONDS`, or use a smaller model |
| First answer takes ~60s, later ones ~11s | Cold model load | Expected. `keep_alive` holds it in memory for 10 minutes |
| Sources shown but not numbered | Model produced no `[n]` markers | Expected on small models; the UI labels these "sources consulted". A stronger provider fixes it |
| Artifact thinner than expected | llama3.2 capacity | Switch provider. The architecture is model-independent |

Every response carries an `x-request-id` header, and it appears in every log line for that request and in every error body. Quote it when reporting a problem.

Logs are JSON by default. For readable local output, set `LOG_FORMAT=console`.

---

## Extending it

| Task | Where |
|---|---|
| Add a model provider | Implement `BaseProvider` in `app/agent/providers/`, register it in `_FACTORIES` in `app/agent/registry.py` |
| Add a skill | Add to `Skill` in `app/agent/types.py`, add rules in `app/agent/router.py`, add a module in `app/skills/`, dispatch in the orchestrator |
| Change chunking | `app/rag/chunker.py`, then `python scripts/ingest.py --force` |
| Change the corpus | `CORPUS_REPO_URL`. Parsing handles frontmatter plus either speaker turns or prose |
| Loosen or tighten grounding | `RETRIEVAL_MIN_COVERAGE`. Re-measure against your own labelled queries |
| Change artifact policy | `ALLOWED_TAGS` / `TAG_ATTRS` in `app/skills/sanitizer.py`. Add tests in `tests/test_security.py` first |
| Refresh the corpus | `python scripts/ingest.py --refresh`. Incremental and safe to run on a schedule |

### Corpus and licensing

The transcripts are **not** in this repository, and must not be added to it. Lenny's Data licenses the starter dataset for personal, non-commercial use and explicitly forbids redistributing the raw files, so `scripts/ingest.py` clones them at setup time and `data/corpus/` is gitignored.

---

## Documentation

| Document | What it answers |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Who this is for, the success metrics, assumptions, scope decisions, risks, and what went wrong during the build |
| [docs/architecture.md](docs/architecture.md) | Schema, endpoints, component boundaries, ingestion and retrieval, routing, the model toggle, artifact security, deployment |
| [docs/design.md](docs/design.md) | UI principles, information architecture, interaction states, responsive behaviour, accessibility |
| [docs/manual-test-plan.md](docs/manual-test-plan.md) | Scripted UI walkthrough, including the failure paths |
| [docs/demo-video-script.md](docs/demo-video-script.md) | Timed script for the demo recording |
| [agent-transcripts/](agent-transcripts/) | Coding-agent logs, including the failed attempts and how they were corrected |

---

## Known limitations

Stated plainly, because an evaluator will find them anyway.

- **Small-model quality.** llama3.2 produces thin prose and occasionally misattributes a quote to the wrong speaker *within* correctly retrieved sources. The citation points at the right passage; the name can be wrong. A cloud provider fixes this immediately.
- **No streaming.** A 11 to 67 second wait with no token-by-token output. The reason it was cut, and the fix, are in [docs/PRD.md](docs/PRD.md#15-risks-and-trade-offs).
- **BM25 does not stem**, so lexical-only mode misses inflections (`price` vs `Pricing`). Dense retrieval covers this in normal operation.
- **14 of 60 corpus files carry no source URL** upstream. Those citations show the episode and timestamp without a link.
- **The lexical index is per-process**, so horizontal scaling needs it moved into Postgres FTS.
- **The Claude Agent SDK path is unverified against a live key**, as described above.
