# Design

UI and UX decisions, and the reasoning behind them.

---

## 1. Principles

**1. Evidence is part of the answer, not a footnote.**
The product's differentiator is not that it answers, it is that its answers can be checked. So citations live directly under each answer, inline `[n]` markers are clickable and scroll to their source, and every source shows the episode, guest, timestamp and a deep link to that moment. Trust is a function of how *cheap* it is to verify.

**2. Never imply more precision than exists.**
When the model produces no resolvable citation markers, the UI does not renumber the retrieved passages and present them as citations. It says "sources consulted" and explains the difference. A citation that looks authoritative but was never actually made is worse than no citation.

**3. The system explains itself.**
Provider, model and latency appear under every answer. The status panel shows the corpus size, every provider's state, the fallback order, and a plain-language list of what is wrong and how to fix it. An evaluator should never need the logs to understand what they are looking at.

**4. Determinism on demand.**
Routing infers intent from phrasing, but a user who knows what they want should not have to phrase their way into it. The skill selector makes the choice explicit, and explicit always wins.

**5. Surfaces appear when they are earned.**
The artifact panel is not chrome. It exists only when a turn produced a document, and it can be dismissed. Two thirds of the screen is too high a price for an empty placeholder.

---

## 2. Information architecture

```
┌────────────┬───────────────────────────┬──────────────────┐
│  Sessions  │  Conversation             │  Artifact        │
│            │                           │  (conditional)   │
│  brand     │  topbar: title, panes     │  title + kind    │
│  new chat  │                           │  Preview │       │
│  ────────  │  thread                   │  Source  │ tabs  │
│  chat 1    │    message                │  Blocked │       │
│  chat 2    │      body                 │  ──────────────  │
│  ────────  │      critique             │  sandboxed       │
│  status    │      sources (collapsed)  │  iframe          │
│            │      provenance           │                  │
│            │  composer: skills + input │                  │
└────────────┴───────────────────────────┴──────────────────┘
```

Three levels of persistence, matching three timescales: **sessions** (across visits), **conversation** (this session), **artifact** (this turn).

**System status sits at the bottom of the sidebar**, not in a settings page. It is the first thing an evaluator needs and the first thing that breaks, so it is permanently visible and one click from full detail.

**Sources are collapsed by default.** Expanded, they would bury the prose they support. Collapsed, the count is still visible ("Show 3 sources"), so the evidence is advertised without being imposed.

---

## 3. Key interaction states

Every state below is implemented, because the states are where a demo differs from a product.

| State | Treatment | Why |
|---|---|---|
| **Empty** | Centred welcome, one sentence on what makes this different, four one-click starters covering all three skills | A blank chat box is a worse question than any of the four |
| **Typing** | Composer grows to a 220px ceiling | Long briefs stay visible; the conversation never disappears |
| **Sending** | User's message appears instantly (optimistic), input clears, Send disables | A local model takes 11 to 67 seconds. A frozen input reads as broken |
| **Working** | Animated dots plus "Searching transcripts and drafting an answer", and on Ollama, "a local model can take a while on the first turn" | Setting the expectation is cheaper than making it fast, and more honest |
| **Answered** | Markdown, clickable markers, collapsed sources, provenance line | |
| **Refused** | Normal assistant message stating nothing was found, what the corpus covers, and how to ask better | A refusal is a legitimate answer, not an error. Styling it as a failure would train users to distrust the honest case |
| **Unmatched citations** | Amber note distinguishing "consulted" from "cited" | Principle 2 |
| **Essay critique** | Green when the draft meets the format, amber with the specific failures when not | The validator's opinion belongs to the user, not just the logs |
| **Error** | Red card with the message, the operator hint, and the request id | The id is what turns a complaint into a bug report |
| **Degraded** | Amber status dot plus a `checks[]` list of fixes | Half-working must look different from working |
| **Artifact ready** | Panel opens automatically, chat keeps the model's prose | The document is the deliverable; the chat stays a conversation |

The user's text is **never** discarded on error. Retyping a long brief after a transient model failure is the most annoying possible outcome.

---

## 4. The artifact viewer

Three tabs, each answering a different question.

**Preview** — what the document looks like. HTML renders in a `sandbox=""` iframe; Markdown renders inline with the app's typography.

**Source** — what the model actually wrote, with a copy button.

**Blocked** — what the viewer permits, what it blocks, and specifically what was removed from *this* artifact.

The Blocked tab is a deliberate product decision, not a debug panel. Users are being asked to trust that arbitrary model-generated HTML is safe to render. That trust should be inspectable. The tab states the policy in plain language ("no remote images, so a beacon cannot report that you opened this") and lists the exact tags, attributes and URLs stripped, with a count badge on the tab when anything was.

---

## 5. Responsive behaviour

Three breakpoints, each driven by a real failure of the layout above it.

| Width | Layout |
|---|---|
| **> 1100px** | Three columns. Artifact takes `minmax(360px, 0.85fr)` so it never squeezes the conversation below readable width |
| **820 to 1100px** | Sidebar stays; the artifact becomes a right-hand overlay with Chat/Artifact tabs in the topbar. Below ~360px of artifact, side-by-side reading is worse than either pane alone |
| **< 820px** | Single column. Sidebar becomes an off-canvas drawer behind a menu button; the artifact goes full width |

Content is capped at 780px in the conversation and 74ch in Markdown artifacts. Measure matters more than filling the viewport.

---

## 6. Accessibility

Treated as correctness, not decoration.

- **Semantics first.** `<article>` per message, `<nav>` for sessions, `role="log"` with `aria-live="polite"` on the thread so answers are announced, `aria-busy` while working.
- **No nested interactive controls.** A session row is a container with the open action and the delete action as *siblings*. Nesting them produced an empty accessible name, which was caught by reading the accessibility tree during the build.
- **Explicit labels** where content inference is ambiguous: `aria-label="Open chat: {title} ({n} messages)"`.
- **Keyboard**: a skip link to the conversation, visible `:focus-visible` rings everywhere, Enter to send and Shift+Enter for a newline, the delete button revealed on focus and not only on hover.
- **Tabs and radios** use real `role="tablist"`/`role="radiogroup"` with `aria-selected` / `aria-checked` and `aria-controls`.
- **Colour is never the only signal.** Provider state carries a text pill (`ready` / `unreachable` / `no key`) alongside the colour; the essay critique states its verdict in words.
- **Motion**: `prefers-reduced-motion` disables the thinking animation, smooth scrolling and transitions.
- **Contrast**: text tokens target WCAG AA against their surfaces in both themes.
- **The iframe has a descriptive `title`**, so screen-reader users know what the frame contains.

---

## 7. Visual language

Tokens plus flat component classes, no utility framework. The whole surface is about a dozen components, and a client engineer extending it should be able to read the styles top to bottom without first learning a build-time abstraction. Every colour is a token, so rebranding is one block.

**Light and dark are both first-class**, driven by `prefers-color-scheme`, with a separately tuned dark palette rather than an inverted light one. Artifacts carry their own dark-mode styling inside the iframe, since they cannot inherit the parent's.

Type is a single system stack. Indigo is the only accent, reserved for interactive and provenance elements, which is what lets citation markers read as clickable without extra affordance.

---

## 8. Decisions worth arguing with

**Sources collapsed by default.** Trades discoverability for readability. Mitigated by showing the count. If click-through instrumentation (PRD §7) showed users never expanding them, expanded-by-default would be worth testing.

**No streaming.** The honest reason is not that it was hard: citations are resolved *after* generation, so streaming would show `[3]` before source 3 exists. Fixing that properly means restructuring citation resolution. It is the top of the next-steps list.

**Optimistic user message, but no optimistic assistant placeholder.** A skeleton implies imminent content; 60 seconds of skeleton is a lie. Explicit "this can take a while" copy is better.

**The status panel is dense.** It is aimed at an evaluator or an on-call engineer, not a daily user, and it is one click away rather than always open.

**Delete has no confirmation dialog.** A chat is cheap and recreatable, and a modal on every delete is worse for the common case. If sessions later hold anything expensive, this should change to an undo toast.
