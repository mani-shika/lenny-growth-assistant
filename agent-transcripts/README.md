# Agent transcripts

Coding-agent logs from building this system, plus the narrative below explaining what went wrong and how each problem was corrected.

| File | Contents |
|---|---|
| [`session-01-build-log.md`](session-01-build-log.md) | Full session transcript, 234 turns, automatically redacted |
| This file | The failures, in order, with the reasoning |

**Redaction.** The transcript was exported with [`scripts/export_transcript.py`](../scripts/export_transcript.py), which strips API keys, bearer tokens, database credentials, email addresses and absolute user paths, then **refuses to write the file** if any known secret pattern survives. The check passed clean. Tool results are omitted (they are mostly file contents already in the repo); tool calls are retained.

Only this project's session was exported. The machine held ~58 MB of transcripts from an unrelated project, and those are deliberately excluded rather than bulk-copied.

---

## The failures, in order

Ten problems worth recording. Seven were real defects, two were caught by tests rather than by using the app, and one was a test that was wrong about the code.

### 1. The relevance gate was in a place where it could never work

**Symptom.** Every query returned zero results while logging 30 healthy lexical hits.

```
Q: How do you know when you have product/market fit?
   strategy=empty lexical=30 dense=0 returned=0
```

**Cause.** The gate thresholded the *fused* RRF score against `RETRIEVAL_MIN_SCORE = 0.02`. RRF encodes rank agreement, not relevance: a top hit appearing in one list scores `1/(60+1) = 0.0164`, which is permanently below `0.02`. The threshold was unreachable by construction.

**Why it mattered beyond the bug.** The comment above it claimed the threshold prevented hallucination. It could not have: RRF assigns roughly the same top score regardless of match quality. The code was wrong *and* the reasoning was wrong.

**Fix.** Move the gate off the fused score and onto each list's own signal, before fusion. RRF now only orders.

---

### 2. BM25 magnitude does not separate relevant from irrelevant

**Symptom.** With the gate correctly positioned, the obvious threshold still failed. Measured scores:

| Query | Top BM25 |
|---|---|
| "How do you close a six figure enterprise deal?" (in corpus) | 13.8 |
| "What is the biggest mistake new PMs make?" (in corpus) | 12.1 |
| **"zzzxqv nonsense token"** (nonsense) | **7.8** |
| "best recipe for sourdough starter hydration" (out of corpus) | 7.9 |

Overlapping ranges. `"zzzxqv nonsense token"` scored respectably because "token" appears constantly in an AI-heavy corpus.

**Fix.** Score and relevance are different questions. Added **IDF-weighted query coverage**: what share of the query's *information* is actually present in a passage. A term the corpus has never seen (`zzzxqv`, `sourdough`) carries maximum IDF, so failing to match it collapses coverage.

Re-measured over 25 labelled queries: in-corpus floor **0.497**, out-of-corpus ceiling **0.519**. Threshold set to **0.45**, with the single overlap (`"translate good morning into Japanese"`) being a query the Duolingo content arguably does cover.

The dense threshold was measured the same way: in-corpus distances topped out at 0.396, out-of-corpus bottomed out at 0.505, so 0.45 separates them. The initial guess of 0.62 would have let every out-of-corpus query through.

**Lesson.** Both thresholds in this system were measured against labelled data. Neither was guessable.

---

### 3. Half the citations had no link, and nothing looked broken

**Symptom.** Citations rendered with title, guest and timestamp, but `source_url` was `null` for 34 of 60 documents.

**Cause.** `post_url` was treated as the only URL key. The corpus is not uniform: roughly half the transcripts carry `youtube_url` instead.

**Why it was nearly missed.** Nothing errored. Citations displayed correctly, they just were not clickable. This is the failure mode that automated tests exist for, and the regression test now enumerates every URL key the corpus uses.

**Second bug found while fixing it.** The deep link was built as `#t=<seconds>` for every host. YouTube ignores media fragments and needs `?t=<seconds>s`, so every YouTube citation would have silently landed at 0:00 - a link that looks like it works.

---

### 4. The prompt caused the exact bug it was meant to prevent

**Symptom.** llama3.2 emitted literal `[n]` markers:

> Grant Lee mentions that they spent three to four months... **[n]**

Citation resolution found no digits, fell back to showing the top three retrieved passages, and the displayed sources named different guests than the prose did.

**Cause.** The user prompt ended: `"Answer using only the excerpts above, citing them as [n]."` The model copied the placeholder verbatim.

**Fix, and the second-order mistake.** The first fix added `"Never write a placeholder such as [n]"`. That is worse: naming the token you do not want is how you teach a small model to produce it. Replaced with positive worked examples only - `"Retention is the strongest signal [2]."` - and removed every literal `[n]` from every prompt.

**Also added.** A `citations_matched` flag. When the model produces no resolvable markers, the UI says "sources consulted" instead of renumbering retrieved passages and presenting them as citations. The original fallback quietly implied a precision that did not exist.

---

### 5. A validation error returned 500 instead of 422

**Caught by:** `test_invalid_requests_return_422_not_500`, not by manual clicking.

```
TypeError: Object of type ValueError is not JSON serializable
```

**Cause.** The handler put `exc.errors()` straight into the response. Pydantic embeds the originating exception object under `ctx`, which `JSONResponse` cannot serialise, so the error handler itself crashed. **Every** request tripping a custom field validator returned a 500.

**Fix.** Project each error onto the three fields a client can act on: field, message, type.

---

### 6. Two sanitiser bypasses

An adversarial run of 14 payloads against the first implementation returned 12/14.

| Payload | Result |
|---|---|
| `<div style="background:url(//evil/beacon)">` | **survived** |
| `<style>@import url(//evil/x.css);</style>` | **survived** (the `@import` was stripped, its argument was not) |

**Cause.** The CSS pattern required a scheme (`[a-z]+:`) after `url(`, so protocol-relative `//host` slipped through, and `@import` removal did not consume its argument.

**Fix.** Three separate passes with different removal shapes: at-rules swallow to the semicolon, `url()` swallows to the closing paren, and legacy execution vectors are substring matches. Any `url()` that is not an inline `data:image/` is replaced with `none`, which keeps the surrounding declaration valid. Re-ran: 7/7, with benign CSS untouched.

**Worth noting.** The CSP would have blocked both at render time. That is the argument for defence in depth, and also the reason not to lean on it: a sanitiser that relies on the layer below it is not a layer.

---

### 7. The model ignored the output format

**Symptom.** A request for an HTML artifact produced Markdown prose, and the artifact title became `"Here is a possible HTML one-pager summarizing what makes a great product manager:"`.

**Cause.** Two problems. The prompt described the required format in prose, which llama3.2 ignored. And title inference took the first non-empty line, which was the model's conversational preamble.

**Fix.**
- Replaced the prose rules with a **literal output template** the model can copy. Small models imitate a skeleton far more reliably than they follow a description.
- Title inference now skips preamble patterns (`Here is`, `Below is`, `Sure`) and lines ending in a colon.
- Added one strict retry when the model returns no fenced block, mirroring the essay repair pass.

Result: 2 attempts, correct HTML artifact, clean title.

---

### 8. A read-after-write race, found only by firing requests back to back

**Symptom.** The end-to-end QA script reported `DELETE -> 204` immediately
followed by `GET -> 200` on the same session, and turns that had just been
written coming back missing from history.

**Not reproducible by hand.** Running the identical sequence with curl always
passed, and the database always showed the correct final state. Shell latency
between commands was enough to hide it.

**Cause.** `get_db` committed after the `yield`. Code after a `yield` in a
FastAPI dependency runs during teardown, *after* the response has gone back to
the client. So a client could receive `204`, immediately issue a read, and race
the commit - seeing state from before its own write. The SPA does exactly this:
it refreshes the session list the moment a send returns.

**Fix.** The dependency no longer commits; every mutating route commits
explicitly before returning. Teardown only rolls back and closes, which is safe
to do after the response.

**Why it matters.** This is the one bug in the build that unit tests could not
have caught: they drive the app in-process through ASGI, where the timing
window does not exist. It took a client hitting a real server with no delay
between requests.

---

### 9. A test that was wrong about the code

`test_bm25_ranks_the_relevant_chunk_first` asserted that `"how do I price my product"` would rank the pricing chunk first. It ranked a different one.

The code was right and the test was wrong: **BM25 here does not stem**, so `"price"` does not match `"Pricing"`, and the common word "product" decided the ranking.

Rather than change the query until the test passed, the limitation was made explicit: the test now documents that lexical search is surface-form only, that the dense half of the hybrid covers morphological variation, and that adding a stemmer would shift the coverage distribution the 0.45 threshold is calibrated against - so it is a deliberate change with a re-tune, not a one-line patch.

---

### 10. An accessibility bug found by reading the accessibility tree

The session list rendered as `button [ref_7]` with **no accessible name**, because the row was a `<button>` containing a `role="button"` span for delete. Nested interactive controls are invalid HTML and break name computation.

Restructured into a container `<div>` with the open and delete actions as siblings, plus an explicit `aria-label`. Found by inspecting the a11y tree in the browser rather than by looking at the rendered pixels, which looked fine.

---

## What the tests actually caught

Three production defects were found by tests rather than by using the app: the 500-instead-of-422 handler crash, the sanitiser bypasses, and the citation-marker regression. Two more (the retrieval gate, the missing URLs) were found by measuring behaviour against labelled data rather than by reading code.

A fourth - the read-after-write race - was found only by the end-to-end QA
script, because it required a real client issuing requests over a socket with
no pause between them.

The pattern worth taking from this: **every bug that mattered was invisible from
the UI.** Citations rendered. Answers appeared. The sanitiser reported success.
The delete returned 204. Only measurement, adversarial input, back-to-back
request timing, and reading the accessibility tree surfaced them.
