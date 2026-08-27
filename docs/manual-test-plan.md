# Manual UI test plan

For what automated tests cannot assert: whether the thing feels right, whether the failure states are legible, and whether the security claims hold in a real browser.

**Setup:** `docker compose up --build`, then http://localhost:8000. Expect `GET /api/health` to report `"status": "ok"` with an empty `checks` array.

Environment: Chrome or Firefox, 1440px wide unless stated. Where a step has a specific expected value, it is given so the result is unambiguous rather than a judgement call.

---

## 1. First run and status

| # | Step | Expected |
|---|---|---|
| 1.1 | Load the app | Welcome screen, four suggestion cards, no console errors |
| 1.2 | Read the sidebar status pill | Green dot, `ollama · llama3.2` |
| 1.3 | Click the status pill | Panel shows **60 sources (50 episodes, 10 posts)**, 1420 passages, "hybrid search", all four providers with state pills, fallback order |
| 1.4 | Check unconfigured providers | groq / openai / anthropic show a grey `no key` pill, not an error |
| 1.5 | Click Refresh in the panel | Values re-fetch without a page reload |

---

## 2. Grounded answer, and its evidence

| # | Step | Expected |
|---|---|---|
| 2.1 | Click "How do you know when you have product/market fit?" | Message appears **immediately**; working indicator with the local-model warning |
| 2.2 | Wait | First answer may take ~60s (cold model load). Later answers ~11s |
| 2.3 | Read the answer | Markdown prose; `[n]` markers rendered as small indigo superscripts |
| 2.4 | Read the line under the answer | `ollama · llama3.2 · N.Ns` |
| 2.5 | Click "Show N sources" | Sources expand, each with a numbered marker, episode title, guest, timestamp and excerpt |
| 2.6 | Click an inline `[n]` marker | Sources expand if collapsed and scroll to source *n* |
| 2.7 | Click a source title | Opens in a new tab **at the cited timestamp** (YouTube `&t=1183s`, Substack `#t=`) |
| 2.8 | Find a source with no link | Title shown as plain text, not a broken link. 14 of 60 files have no upstream URL |

**2.7 is the critical one.** If the link lands at 0:00, the citation is decorative rather than verifiable.

---

## 3. Session context and isolation

| # | Step | Expected |
|---|---|---|
| 3.1 | Ask "How do you know when you have product/market fit?" | Grounded answer |
| 3.2 | Follow up with "What about for B2B?" | Answer stays on the PMF topic. The follow-up inherits the previous question for retrieval |
| 3.3 | Click **+ New chat** | Thread clears; sidebar gains an entry |
| 3.4 | Ask "What did we just discuss?" | The assistant does **not** know. Contexts are independent |
| 3.5 | Click the first chat in the sidebar | Full history restores, including citations |
| 3.6 | Reload the browser, reopen it | History still intact (persisted in Postgres, not client state) |
| 3.7 | Hover a chat, click ✕ | Removed from the list; the thread clears if it was open |

---

## 4. Honest refusal

The behaviour that makes the rest of the product trustworthy.

| # | Step | Expected |
|---|---|---|
| 4.1 | Ask "What is the treatment protocol for acute pancreatitis?" | **Fast** reply (well under a second, no model call) stating nothing was found |
| 4.2 | Read it | Names the corpus size and suggests how to ask better |
| 4.3 | Check the styling | Rendered as a normal assistant message, **not** a red error |
| 4.4 | Ask "How do I replace the timing belt on a Honda Civic?" | Same refusal |
| 4.5 | Ask "What separates a great PM from a good one?" | Answers normally. The gate is not simply refusing everything |

If 4.1 is slow, the gate is not firing and a model call is being made unnecessarily.

---

## 5. Ship 30 essay

| # | Step | Expected |
|---|---|---|
| 5.1 | Select the **Essay** pill, ask for an essay on AI and product teams | Routes to the essay skill |
| 5.2 | Wait | Slower than Q&A; may include a repair pass |
| 5.3 | Check the structure | Single H1, three or more `##` sections, at least one bulleted list, selective bold, `[n]` citations |
| 5.4 | Check the opening line | One sentence, one of the six Ship 30 opener types |
| 5.5 | Read the critique block | Green "meets the format", or amber listing the **specific** failures |
| 5.6 | Compare word count against the critique | Matches the reported figure; target ~1,250, accepted 1,000 to 1,500 |

The critique appearing even on success is intentional: the validator's opinion belongs to the user.

---

## 6. Artifacts and the isolation claim

| # | Step | Expected |
|---|---|---|
| 6.1 | Select **Artifact**, ask for an HTML one-pager on user interviews | Panel opens automatically beside the chat |
| 6.2 | Check the chat pane | Shows the model's prose, not a wall of HTML |
| 6.3 | Check the artifact title | A real title, never "Here is a possible…" |
| 6.4 | **Preview** tab | Renders as a styled document |
| 6.5 | **Source** tab | Full HTML source; Copy source works |
| 6.6 | **Blocked** tab | States what is permitted and blocked; lists what was removed from this artifact, or says nothing was |
| 6.7 | Ask for a Markdown checklist | Renders inline with app typography; kind badge reads `Markdown` |
| 6.8 | Close the panel with ✕ | Conversation reflows to full width |
| 6.9 | Click **Open artifact** on an earlier message | Panel reopens with that artifact |

### 6.10 Verify the sandbox yourself

With an HTML artifact open, run in DevTools:

```js
const f = document.querySelector('iframe.artifact__frame');
console.log(f.sandbox.length);            // 0  → no capabilities granted
console.log(/Content-Security-Policy/.test(f.srcdoc));  // true
try { f.contentDocument.body } catch (e) { console.log('blocked:', e.name); }
```

Expected: `0`, `true`, and `blocked: TypeError`. The parent cannot reach the frame, which is the isolation claim made concrete.

---

## 7. Failure paths

The states most demos skip.

| # | Step | Expected |
|---|---|---|
| 7.1 | Stop Ollama (`ollama stop llama3.2`, or quit the app), then ask a question | Red error card naming Ollama, with the fix and a request id |
| 7.2 | Check the status pill | Turns amber or red; the panel lists the problem under `checks` |
| 7.3 | Confirm the composer | Your text is **still there**, not discarded |
| 7.4 | Restart Ollama, wait ~20s | Status returns to green on its own (polled) |
| 7.5 | Resend | Works |
| 7.6 | Stop Postgres (`docker compose stop db`), reload | App still loads; the status panel reports the database as unreachable rather than a blank page |
| 7.7 | Restart it (`docker compose start db`) | Recovers without restarting the API (`pool_pre_ping`) |
| 7.8 | Set `LLM_FALLBACK_CHAIN=ollama,groq` with a Groq key, stop Ollama, ask | Answer arrives from Groq; the provenance line names `groq` |

---

## 8. Responsive and accessibility

| # | Step | Expected |
|---|---|---|
| 8.1 | Resize to 1000px with an artifact open | Artifact becomes an overlay; Chat/Artifact tabs appear in the topbar |
| 8.2 | Resize to 700px | Single column; sidebar becomes a drawer behind ☰ |
| 8.3 | Open the drawer, pick a chat | Drawer closes on selection |
| 8.4 | 375px (iPhone SE) | No horizontal scroll; composer and tabs usable |
| 8.5 | Tab from page load | First stop is the "Skip to conversation" link |
| 8.6 | Keep tabbing | Visible focus ring on every control; delete buttons appear on focus, not only on hover |
| 8.7 | Tab to a session row | Announces `Open chat: {title} ({n} messages)`; the delete button is a **separate** stop |
| 8.8 | Type a message, press Enter | Sends. Shift+Enter inserts a newline |
| 8.9 | Switch the OS to dark mode | Whole UI and artifact contents switch; contrast holds |
| 8.10 | Enable "reduce motion" | The thinking animation stops pulsing |
| 8.11 | Screen reader on the thread | New answers announced (`role="log"`, `aria-live="polite"`) |

---

## 9. Regression checks

Specific bugs found during the build. Each has an automated test; these confirm them in the product.

| # | Check | Expected |
|---|---|---|
| 9.1 | Ask several questions and read the markers | Real digits (`[1]`, `[2]`), never a literal `[n]` |
| 9.2 | If an answer's sources are unnumbered | An amber note explains they were "consulted", not cited. Never silently renumbered |
| 9.3 | Ask for an HTML artifact repeatedly | Reliably HTML, not Markdown prose. One strict retry recovers format failures |
| 9.4 | `POST /api/sessions/{id}/messages` with `{"message":"   "}` | **422** with `validation_failed`, not a 500 |
| 9.5 | Open a YouTube-sourced citation | `&t=Ns`, not `#t=`, so it lands at the right moment |

---

## Sign-off

A build is acceptable when sections 1 to 7 pass, 8.1 to 8.8 pass, and section 9 is clean.

Record the model and provider used, since answer quality is model-bound while everything else in this plan is not.
