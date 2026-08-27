# Demo video script

**Target: 2:45.** Requirements covered: the problem, the product, local Ollama demonstrated, one technical trade-off explained. Camera on throughout (picture-in-picture over the screen recording is fine).

---

## Pre-flight (do this before you hit record)

1. **Docker running**, stack up:
   ```bash
   docker compose -f C:\Users\bhusa\Downloads\lenny-growth-assistant\docker-compose.yml up -d
   ```
2. **Warm the model.** Ask one throwaway question and delete that chat. Cold start is ~40s; warm is ~15s. Skipping this puts a 40-second hole in take one.
3. **Clear the sidebar** so old chats don't clutter the frame.
4. **Open two tabs**: the app on `localhost:8000`, and a terminal for the Ollama proof.
5. **Browser at ~1280px** so the three-pane layout shows without squashing.

**The one thing that will bite you:** every answer takes ~15 seconds. Do not wait in silence. Every beat below is written so you **ask, then keep talking** while it generates.

---

## 0:00 – 0:25 · The problem (camera)

> "Lenny's Podcast is the highest-signal corpus in product and growth. It's also about three quarters of a million words, which makes it completely unusable at the moment you actually need it: Tuesday afternoon, mid-decision.
>
> The workaround most teams use is asking a general chatbot. That's the dangerous one, because it answers *every* product question confidently, and its wrong answers look exactly like its right ones.
>
> So I built an assistant where you can check the answer."

---

## 0:25 – 1:10 · Grounded answer with real provenance

**Do:** Type `How do you know when you have product/market fit?` and hit Enter **immediately**, then talk over the wait.

> "Behind this, every turn does the same thing: it searches 1,420 passages from 60 sources using hybrid retrieval, BM25 plus vector search, and only then calls the model, with the evidence already in hand."

**Do:** When it lands, click **Show sources**, then click a source title.

> "Every claim carries a marker, and every marker resolves to a real passage. And these aren't decorative: clicking one opens the episode **at the timestamp the quote came from.**"

**Do:** Let the YouTube tab load at the timestamp. Come back.

> "That's the difference between a citation and a link."

---

## 1:10 – 1:32 · The refusal (your strongest moment)

**Do:** Ask `How do I replace the timing belt on a Honda Civic?`

> "Now watch what happens when the corpus doesn't cover something."

**Do:** It returns almost instantly.

> "That came back in under a second, because it never called the model at all. Retrieval scores how much of your question actually exists in the corpus, and below a measured threshold it stops and says so.
>
> A RAG system that always answers can't be trusted on the answers it *should* give. This is the feature, not the limitation."

---

## 1:32 – 1:55 · Artifacts, and treating model output as untrusted

**Do:** Click the **Artifact** pill, ask `Make an HTML one-pager on running effective user interviews`. Talk while it works.

> "It also generates documents. This is model-written HTML, which means it's untrusted input: the model wrote it after reading my message and passages retrieved for me."

**Do:** When the panel opens, click the **Blocked** tab.

> "So it renders in a sandboxed iframe with zero permissions, behind a server-side sanitiser and a locked-down CSP. Three independent layers, and this tab shows you exactly what was stripped. A security control you can't see is one nobody trusts."

---

## 1:55 – 2:15 · Local Ollama, proven

**Do:** Point at the provenance line under an answer (`ollama · llama3.2 · 14.3s`), then open the status panel.

> "Everything you've seen ran entirely on my laptop. Llama 3.2 through Ollama, generating; nomic-embed-text doing the vector search; Postgres with pgvector storing it. No API key touched this demo."

**Do:** Cut to the terminal:

```bash
ollama ps
```

> "There's the model, resident in memory. Switching to Groq or Claude is one environment variable, no code change, and there's a fallback chain if a provider goes down."

---

## 2:15 – 2:45 · The trade-off (camera)

> "One trade-off worth naming. The obvious design is to give the model a search tool and let it decide when to use it. I tried that, and this model won't do it reliably: it skips the search and answers from memory, which is exactly the failure the product exists to prevent.
>
> So retrieval isn't the model's decision. Every grounded turn searches first, always.
>
> What I gave up is multi-hop reasoning: a question needing two chained lookups only gets one round of evidence. What I got back is grounding that behaves identically on a two-gigabyte local model and a frontier one.
>
> For an assistant whose entire value is trustworthy citations, predictable beats clever. That's the call I'd defend."

**End.** Don't add a sign-off; the trade-off is a stronger last line.

---

## If you need to cut to 2:00

Drop the artifact beat (1:32–1:55) entirely. Keep the refusal and the trade-off, which are the two things that differentiate this submission.

## If you have room for 3:00

After the first answer, add: *"And it holds context"* → ask `What about for B2B?` → show it stays on topic. Fifteen seconds, and it demonstrates the session requirement explicitly.

---

## Delivery notes

- **Say numbers.** "1,420 passages", "60 sources", "under a second", "14 seconds". Specifics read as someone who measured; adjectives read as someone who didn't.
- **Don't apologise for the local model.** Thin prose from llama3.2 is expected and you've documented it. If you feel the urge to excuse it, say instead: *"the architecture is model-independent, this is a 2 GB model on a laptop."*
- **Let the refusal land.** Pause a beat after it appears. Most submissions can't show one.
- **Camera framing:** picture-in-picture in a corner, screen recording as the main frame. Face on for the opening and closing beats especially.
- **One take is fine.** Don't over-produce; the assignment is judging judgment, not editing.
