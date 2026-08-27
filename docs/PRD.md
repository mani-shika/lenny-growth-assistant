# PRD: The Lenny Growth Assistant

**Status:** v1.0, delivered
**Author:** Forward Deployed Engineering
**Date:** 27 August 2026

---

## 1. Forward deployment brief

### 1.1 User and problem

**Primary user: the product manager or growth lead on a small product-and-growth team.** Typically 2 to 15 people, no dedicated research function, and a weekly cadence of decisions that each deserve more evidence than they get.

The job they are trying to complete is not "search a podcast". It is **"make this week's decision without re-deriving what the industry already knows."** That job shows up in three shapes:

| Shape | What they actually say | What they need back |
|---|---|---|
| Decide | "Are we chasing PMF signals that matter?" | An answer with sources they can check |
| Persuade | "I need to make this case to the exec team" | Written content they can send, not notes |
| Package | "Turn this into something I can share" | A rendered document, not a wall of code |

**The pain being removed.** Lenny's archive is roughly 750,000 words across 60 sources in this starter set, and over 300 episodes in the full one. It is the highest-signal corpus in product and growth, and it is effectively unreadable at decision time. Today the workaround is one of three bad options: search a transcript you half-remember, ask a general chatbot and get plausible advice with no provenance, or skip the evidence and go with instinct.

The middle option is the dangerous one. A general assistant answers *every* product question confidently, and its right answers look exactly like its wrong ones. **The value of this product is not that it answers. It is that its answers can be checked, and that it says "I don't know" when the corpus is silent.**

### 1.2 Success metrics

**Primary (product): grounded answer rate at or above 90%.**
The share of answered questions where every substantive claim carries a citation resolving to a real retrieved passage. Measured from the `citations_matched` flag and the `citations` array persisted on every assistant message, so it is computed from production data with no extra instrumentation.

Chosen because it is the metric that maps directly to whether the product is trustworthy, and because it is falsifiable. "User satisfaction" is not.

**Secondary (product): correct refusal rate at or above 90%** on out-of-corpus questions. Currently **13/14 on the labelled probe set** in `tests/test_retrieval.py`. This is the metric that protects the primary one: a system that answers everything cannot have a meaningful grounded answer rate.

**Operational: p50 time to first answer under 15 seconds on the local model.**
Currently ~11 seconds warm, ~67 seconds on the first turn after a cold start (model load). Measured from the `turn.complete` log line's `latency_ms`. Named explicitly because local-model latency is the single biggest threat to adoption, and hiding it in a demo would be dishonest about what shipping this actually costs.

**Leading indicator worth watching:** citation click-through. If users never open a source, they are either fully trusting the output (risk) or ignoring the citations (the product's core differentiator is not landing). Not instrumented in v1; see Out of scope.

### 1.3 Assumptions

The client brief was deliberately incomplete. These are the calls made, and what happens if each is wrong.

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| A1 | Internal tool, trusted users, no auth needed | "internal assistant" in the brief | Auth is a clean addition: `sessions.user_id` already exists and list queries are already scoped by it |
| A2 | Read-only corpus, refreshed on a human's schedule, not continuously | Podcast episodes ship weekly at most | `scripts/ingest.py --refresh` is idempotent and incremental; wire it to cron |
| A3 | Tens of users, not thousands | Small product team | The in-memory BM25 index is per-process. Multi-worker deployment needs the index moved to Postgres FTS. Documented in `app/rag/lexical.py` |
| A4 | The starter corpus (60 sources) is representative enough to evaluate | It is what is publicly licensed | The full archive is a config change: `CORPUS_REPO_URL` |
| A5 | Evaluators will run the local model, so quality must degrade gracefully rather than assume a frontier model | The brief mandates an Ollama demo | Everything grounding-critical is deterministic and provider-independent |
| A6 | Answer quality is bounded by the *retrieval*, not the model, for factual questions | Observed: llama3.2 answers correctly when given the right passages, and hallucinates when not | Justifies spending the effort on the retrieval gate rather than prompt tuning |

**The assumption that most shaped the build (A5).** The mandated local model is a 2 GB llama3.2. It cannot reliably drive a tool-calling loop, and it will confidently answer from parametric memory if allowed to. So retrieval is not the model's decision. See §5.1.

### 1.4 Scope

**In scope, and delivered:**

- Grounded conversational Q&A over the corpus, with citations that deep-link to the episode and timestamp
- Multi-turn sessions with independent context, persisted in PostgreSQL
- Follow-up handling via deterministic query rewriting
- Explicit refusal when the corpus does not support an answer
- A Ship 30 for 30 essay skill with the writing principles encoded as data and a validator that measures output against them
- Markdown and HTML artifact generation with an in-app viewer
- Three-layer artifact isolation, with a UI panel showing what was blocked
- Four interchangeable model providers behind one config switch, with a fallback chain
- Docker Compose one-command startup, structured logging, deep health endpoint
- 138 automated tests

**Deliberately excluded, and why:**

| Excluded | Reason |
|---|---|
| Authentication and multi-tenancy | A1. Internal tool. Adding a login screen would consume build time that grounding quality needed more, and the schema already accommodates it. |
| Token-by-token streaming | Real cost: a local model's 11 to 67 second turn feels bad without it. Real reason for cutting it: streaming an answer whose citations are resolved *after* generation would show markers before their sources exist. Fixing that properly means restructuring citation resolution, which was worth less than the retrieval gate. This is the top item in §7. |
| Cross-session memory / user profiles | Not needed for the job to be done, and it introduces a data-retention question the client has not answered. |
| Multi-hop / agentic retrieval | Rejected on evidence, not taste. See §5.1. |
| Answer-quality evals (LLM-as-judge) | The infrastructure exists (labelled probe sets in tests) but a judge harness needs a frontier model, which the demo does not assume. |
| Citation analytics | Would require a click-tracking endpoint. Deferred with the metric named above so it is a known gap, not an oversight. |
| Reranking model | A cross-encoder would improve precision, but it is a second model to run locally. The coverage gate delivered most of the benefit for none of the cost. |

### 1.5 Risks and trade-offs

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| **Hallucination** | High | Retrieval is mandatory and deterministic; coverage gate refuses out-of-corpus questions; citations resolved against actually-retrieved chunks; `citations_matched` flags unlabelled answers | A small model can still misattribute a quote *within* correct sources. Observed once in testing: llama3.2 attributed a passage to Gloria Mark that came from Tom Verrilli's episode. The citation pointed at the right chunk; the name was wrong. **Not fully solved.** |
| **Local model quality** | High | Deterministic orchestration; format retry; essay validator with repair pass; provider switch is one env var | llama3.2 output is thin and occasionally mis-attributes. Groq or Anthropic fixes this immediately. The architecture is sound; the local model is the constraint. |
| **Latency** | Medium | `keep_alive` holds the model in memory; honest "this can take a while" copy; warm p50 ~11s | First turn after a cold start is ~67s. No streaming yet. |
| **Unsafe artifact rendering** | High | Three independent layers: server-side allowlist sanitiser, `sandbox=""` iframe with zero tokens, `default-src 'none'` CSP. Verified empirically: parent cannot reach the frame document | Residual risk is a browser sandbox escape, which is outside this application's control |
| **Data leakage** | Medium | Local-only path needs no API keys at all; `.env` gitignored; corpus not redistributed; DSNs redacted in logs | Cloud providers see prompt content by definition. The Ollama default exists precisely so that is opt-in. |
| **Corpus licence** | Medium | Raw files are **never committed**; fetched at setup from the upstream repository; `data/corpus/` gitignored | Depends on the upstream repo staying available |
| **Cost** | Low | Local default is free; Groq's free tier covers evaluation | Cloud usage at scale is unmeasured |
| **Retrieval blind spot** | Medium | Hybrid search covers morphology via embeddings | BM25 does not stem, so lexical-only mode (no Ollama embeddings) misses inflections. Documented and tested in `test_lexical_search_does_not_stem_which_is_why_retrieval_is_hybrid` |

---

## 2. User flows

**Flow 1: Grounded question**
Open app → type question → router selects `qa` → query rewritten if it is a follow-up → hybrid retrieval → coverage gate → model answers over numbered excerpts → citations resolved against retrieved chunks → answer renders with collapsible sources, each deep-linking to a timestamp.

**Flow 2: Corpus does not cover it**
Same path until the gate. Every candidate falls below the coverage threshold → **no model call is made** → the assistant states plainly that it found nothing, and describes what the corpus does cover. Costs nothing and takes milliseconds.

**Flow 3: Ship 30 essay**
"Write an essay on X" (or the Essay button) → retrieval → encoded principles composed into the prompt → draft → `critique()` measures word count, H1, section count, bullets, bold, opener length, citations → if it fails, one repair pass naming the specific defects → the critique is shown to the user either way.

**Flow 4: Artifact**
"Make an HTML one-pager" (or the Artifact button) → retrieval → generation with a literal output template → extraction (four fallback strategies) → if the model ignored the format, one strict retry → sanitiser → stored with its report → renders in the viewer beside the chat, with a Blocked tab listing exactly what was removed.

---

## 3. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| AC1 | A new chat maintains context independent of every other chat | Met, `test_sessions_keep_independent_context` |
| AC2 | Sessions, messages, timestamps, user metadata persist in PostgreSQL | Met |
| AC3 | Every grounded answer identifies its transcript sources | Met, with episode, guest and timestamp deep link |
| AC4 | The assistant refuses when the corpus does not support an answer | Met, 13/14 on the labelled probe set |
| AC5 | The provider is switchable without code changes | Met, `LLM_PROVIDER` alone; `test_switching_provider_needs_no_code_change` |
| AC6 | The demo runs on Ollama | Met, llama3.2 + nomic-embed-text, verified end to end |
| AC7 | Essays are ~1,250 words with hook, structure, skimmable formatting, takeaway | Met structurally and enforced by the validator; prose quality is model-bound |
| AC8 | Artifacts render in-app beside the chat | Met |
| AC9 | Generated HTML is treated as untrusted | Met, three layers, empirically verified |
| AC10 | One-command startup | Met, `docker compose up --build` |
| AC11 | Graceful handling of missing keys, absent Ollama, timeouts, empty retrieval, DB failure | Met, each with its own test |
| AC12 | Meaningful automated tests | Met, 138 passing |

---

## 4. What went wrong during the build

Recorded because the failures are more informative than the successes, and because an evaluator should know which parts were hard. Full logs in `agent-transcripts/`.

1. **The relevance gate was in a place it could never work.** The first implementation thresholded the *fused RRF score*. RRF encodes rank agreement, not relevance: a single-list top hit always scores 1/(60+1) = 0.0164, which sat below the 0.02 floor, so **every query returned zero results** while retrieval reported 30 healthy lexical hits. Fixed by moving the gate onto each list's own signal.

2. **BM25 magnitude does not separate relevant from irrelevant.** After moving the gate, the obvious threshold (raw BM25 score) still failed: `"zzzxqv nonsense token"` scored 7.8 because "token" is everywhere in an AI-heavy corpus, against 10 to 14 for real questions. Replaced with IDF-weighted query coverage, which is near-separable (in-corpus floor 0.497, out-of-corpus ceiling 0.519) and tuned to 0.45 against 25 labelled queries.

3. **Half the citations had no link.** `post_url` was treated as the only URL key. 34 of 60 real files use `youtube_url`. The failure was invisible: citations rendered, they just were not clickable. Also fixed the deep-link syntax, since YouTube ignores `#t=` and needs `?t=Ns`.

4. **The prompt caused the citation bug it was meant to prevent.** The user prompt ended `"citing them as [n]"`, and llama3.2 copied the placeholder literally, emitting `[n]` instead of `[1]`. Removed every literal placeholder in favour of worked examples. Naming the token you do not want is what teaches a small model to produce it.

5. **A validation error returned 500 instead of 422.** Pydantic error dicts embed the raw exception object under `ctx`, which is not JSON-serialisable, so the error handler crashed. Caught by a test, not by manual clicking.

6. **Two sanitiser bypasses.** Protocol-relative `url(//evil)` and the argument of a stripped `@import` both survived the first CSS cleaner. The CSP would have blocked them at render time, which is exactly the argument for defence in depth, but the sanitiser should not lean on it.

---

## 5. Key technical decisions

### 5.1 Deterministic orchestration over model-driven tool use

**Decision.** Every grounded turn retrieves first. The model never decides whether to search.

**Why.** The brief mandates an Ollama demo. llama3.2 does not reliably drive a tool loop: it skips the search and answers from parametric memory, which is the exact failure this product exists to prevent. Model-driven retrieval would make grounding a property of *which model you happen to be running*.

**Cost, stated plainly.** No multi-hop retrieval. A question requiring two chained lookups gets one round of evidence.

**Why it is still right.** Grounding behaviour is now identical on a laptop and in the cloud. For an assistant whose entire value is trustworthy citations, predictable beats clever. On the Anthropic provider the same skills are additionally exposed as in-process MCP tools, where model-driven selection *is* reliable.

### 5.2 The agent layer, honestly

The brief asks for the Claude Agent SDK or Pi. The client directed that this engagement use Groq and Ollama.

Resolution: the Claude Agent SDK path is **implemented and reviewable** (`app/agent/providers/anthropic_agent.py`), using `query()` with `ClaudeAgentOptions`, every built-in tool denied, and `setting_sources=[]` so it cannot inherit host configuration. It was verified as far as no API key allows, which means imports, configuration and health reporting, but not a live call. The demo runs on Ollama and Groq.

This is flagged rather than glossed because an evaluator will check.

### 5.3 In-memory BM25 rather than Postgres full-text search

The component that must **always** work. Dense retrieval needs Ollama reachable and the embedding model pulled; Postgres FTS needs the database. BM25 over an in-memory index needs neither, so the failure mode is "still answers, still cites" rather than "returns nothing". At 1,420 chunks the index builds in under a second. Past ~10^5 chunks, move it to Postgres FTS; the interface is designed for that swap.

### 5.4 Sanitiser plus sandbox plus CSP, not one of the three

Each layer fails differently. The sanitiser is an allowlist, so an unknown tag fails closed, but allowlists have bugs (two were found here). The sandbox with zero tokens cannot execute script even if the sanitiser misses something. The CSP blocks network egress even if both fail. Layer two was verified empirically: `iframe.contentDocument` throws from the parent.

---

## 6. Implementation plan (as executed)

| Phase | Work | Outcome |
|---|---|---|
| 0 | Corpus survey, licence review | Found the no-redistribution clause that shaped ingestion |
| 1 | Config, logging, errors, schema | Foundations |
| 2 | Parsing, chunking, BM25, embeddings, hybrid retrieval | Two gate bugs found and fixed |
| 3 | Providers, fallback chain, router, skills | Model toggle working |
| 4 | FastAPI routes, error envelope | 500-instead-of-422 bug found |
| 5 | React SPA, artifact viewer | Isolation verified in-browser |
| 6 | Docker Compose, health, resilience | One-command startup |
| 7 | 138 tests, lint | Three production bugs caught by tests |
| 8 | Documentation and handoff | This document |

## 7. What I would do next, in priority order

1. **Streaming**, with citation resolution restructured to run before generation rather than after. Biggest single UX win.
2. **An eval harness** over a labelled question set, so answer quality becomes a tracked number rather than an impression. The probe sets in the tests are the seed.
3. **Citation click instrumentation**, to close the loop on the leading indicator named in §1.2.
4. **Stemming or a lemmatiser** for BM25, with the coverage threshold re-tuned afterwards.
5. **Move the lexical index to Postgres FTS** when horizontal scaling is needed (A3).
6. **Speaker-attribution verification**: check that a name the model attributes a quote to actually appears in the cited chunk's `speakers`. This would have caught the Gloria Mark misattribution automatically.
