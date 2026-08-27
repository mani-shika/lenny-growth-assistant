/** Application shell: sessions | conversation | artifact.
 *
 *  Layout follows the work. The conversation is the spine and is always
 *  present. The artifact panel is not chrome: it appears only when a turn
 *  produced a document, and it can be dismissed, because two thirds of the
 *  screen is too high a price for an empty placeholder.
 *
 *  On narrow screens the two panes become one, with a toggle - a side-by-side
 *  reading experience below ~900px is worse than either pane alone.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { ArtifactViewer } from "./components/ArtifactViewer";
import { Composer } from "./components/Composer";
import { MessageBubble } from "./components/MessageBubble";
import { StatusBar } from "./components/StatusBar";
import type {
  Artifact,
  EssayCritique,
  Health,
  Message,
  Session,
  SkillName,
} from "./types";

const SUGGESTIONS = [
  "How do you know when you have product/market fit?",
  "What separates a great PM from a good one?",
  "Write a Ship 30 essay on how AI is changing product teams",
  "Make an HTML one-pager on running effective user interviews",
];

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [openArtifact, setOpenArtifact] = useState<Artifact | null>(null);
  const [critiques, setCritiques] = useState<Record<string, EssayCritique>>({});
  const [health, setHealth] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [mobilePane, setMobilePane] = useState<"chat" | "artifact">("chat");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const scrollAnchor = useRef<HTMLDivElement>(null);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      setHealth(null);
    }
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
    } catch {
      /* the status bar already reports connectivity */
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    void refreshSessions();
    // Health is cheap and the answer changes when a user starts Ollama mid-demo.
    const timer = window.setInterval(refreshHealth, 20_000);
    return () => window.clearInterval(timer);
  }, [refreshHealth, refreshSessions]);

  useEffect(() => {
    scrollAnchor.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, busy]);

  async function newChat() {
    setError(null);
    try {
      const session = await api.createSession();
      setSessions((prev) => [session, ...prev]);
      setActiveId(session.id);
      setMessages([]);
      setArtifacts([]);
      setOpenArtifact(null);
      setCritiques({});
      setSidebarOpen(false);
    } catch (err) {
      setError(err as ApiError);
    }
  }

  async function selectSession(id: string) {
    setError(null);
    setSidebarOpen(false);
    try {
      const detail = await api.getSession(id);
      setActiveId(detail.id);
      setMessages(detail.messages);
      setArtifacts(detail.artifacts);
      setOpenArtifact(detail.artifacts.at(-1) ?? null);
      setCritiques({});
    } catch (err) {
      setError(err as ApiError);
    }
  }

  async function removeSession(id: string, event: React.MouseEvent) {
    event.stopPropagation();
    try {
      await api.deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (activeId === id) {
        setActiveId(null);
        setMessages([]);
        setArtifacts([]);
        setOpenArtifact(null);
      }
    } catch (err) {
      setError(err as ApiError);
    }
  }

  async function send(text: string, skill: SkillName | null) {
    let sessionId = activeId;
    setError(null);

    // Sending without an open chat should just work rather than scolding.
    if (!sessionId) {
      try {
        const session = await api.createSession();
        setSessions((prev) => [session, ...prev]);
        sessionId = session.id;
        setActiveId(session.id);
      } catch (err) {
        setError(err as ApiError);
        return;
      }
    }

    // Optimistic echo: the user's own words appear instantly, because a local
    // model can take many seconds and a frozen input feels broken.
    const optimistic: Message = {
      id: `pending-${Date.now()}`,
      session_id: sessionId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
      provider: null,
      model: null,
      latency_ms: null,
      usage: {},
      citations: [],
      route: null,
      artifact_id: null,
    };
    setMessages((prev) => [...prev, optimistic]);
    setBusy(true);

    try {
      const response = await api.sendMessage(sessionId, text, { skill });
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== optimistic.id),
        response.user_message,
        { ...response.assistant_message, citationsMatched: response.citations_matched },
      ]);
      if (response.essay_critique) {
        setCritiques((prev) => ({
          ...prev,
          [response.assistant_message.id]: response.essay_critique!,
        }));
      }
      if (response.artifact) {
        setArtifacts((prev) => [...prev, response.artifact!]);
        setOpenArtifact(response.artifact);
        setMobilePane("artifact");
      }
      void refreshSessions();
    } catch (err) {
      // Keep the user's text on screen - retyping a long brief after a
      // transient model failure is the most annoying possible outcome.
      setError(err as ApiError);
    } finally {
      setBusy(false);
    }
  }

  function openArtifactById(id: string) {
    const found = artifacts.find((a) => a.id === id);
    if (found) {
      setOpenArtifact(found);
      setMobilePane("artifact");
    }
  }

  const empty = messages.length === 0;

  return (
    <div className={`shell ${openArtifact ? "shell--split" : ""}`} data-pane={mobilePane}>
      <a href="#main" className="skip-link">
        Skip to conversation
      </a>

      <aside className={`sidebar ${sidebarOpen ? "is-open" : ""}`}>
        <div className="sidebar__head">
          <h1 className="brand">
            <span className="brand__mark" aria-hidden="true">
              L
            </span>
            <span className="brand__text">Lenny Growth Assistant</span>
          </h1>
        </div>
        <button className="new-chat" onClick={newChat}>
          + New chat
        </button>
        <nav className="sessions" aria-label="Previous chats">
          {sessions.length === 0 && (
            <p className="sessions__empty">No chats yet.</p>
          )}
          {sessions.map((session) => (
            <div
              key={session.id}
              className={`session ${session.id === activeId ? "is-active" : ""}`}
            >
              <button
                className="session__open"
                onClick={() => selectSession(session.id)}
                aria-current={session.id === activeId}
                aria-label={`Open chat: ${session.title} (${session.message_count} messages)`}
              >
                <span className="session__title">{session.title}</span>
                <span className="session__count">{session.message_count}</span>
              </button>
              <button
                className="session__delete"
                aria-label={`Delete chat: ${session.title}`}
                onClick={(e) => removeSession(session.id, e)}
              >
                ✕
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar__foot">
          <StatusBar health={health} onRefresh={refreshHealth} />
        </div>
      </aside>

      <main className="main" id="main">
        <header className="topbar">
          <button
            className="icon-button topbar__menu"
            onClick={() => setSidebarOpen((v) => !v)}
            aria-label="Toggle chat list"
          >
            ☰
          </button>
          <div className="topbar__title">
            {sessions.find((s) => s.id === activeId)?.title ?? "New chat"}
          </div>
          {openArtifact && (
            <div className="topbar__panes" role="tablist" aria-label="Pane">
              <button
                role="tab"
                aria-selected={mobilePane === "chat"}
                className={mobilePane === "chat" ? "is-active" : ""}
                onClick={() => setMobilePane("chat")}
              >
                Chat
              </button>
              <button
                role="tab"
                aria-selected={mobilePane === "artifact"}
                className={mobilePane === "artifact" ? "is-active" : ""}
                onClick={() => setMobilePane("artifact")}
              >
                Artifact
              </button>
            </div>
          )}
        </header>

        <div className="thread" role="log" aria-live="polite" aria-busy={busy}>
          {empty && !busy && (
            <div className="welcome">
              <h2>Ask anything from Lenny&rsquo;s Podcast</h2>
              <p>
                Every answer is grounded in the transcripts and cites the episode
                and timestamp it came from. When the corpus doesn&rsquo;t cover
                something, the assistant says so instead of guessing.
              </p>
              <div className="welcome__grid">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    className="suggestion"
                    onClick={() => send(suggestion, null)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((message) => (
            <MessageBubble
              key={message.id}
              message={message}
              critique={critiques[message.id]}
              onOpenArtifact={openArtifactById}
            />
          ))}

          {busy && (
            <div className="thinking" aria-label="Assistant is working">
              <span className="thinking__dot" />
              <span className="thinking__dot" />
              <span className="thinking__dot" />
              <span className="thinking__text">
                Searching transcripts and drafting an answer
                {health?.active_provider === "ollama" &&
                  " — a local model can take a while on the first turn"}
              </span>
            </div>
          )}

          {error && (
            <div className="error" role="alert">
              <strong>{error.message}</strong>
              {error.hint && <p>{error.hint}</p>}
              {error.requestId && (
                <p className="error__id">Request id: {error.requestId}</p>
              )}
            </div>
          )}

          <div ref={scrollAnchor} />
        </div>

        <Composer disabled={false} busy={busy} onSend={send} />
      </main>

      {openArtifact && (
        <ArtifactViewer
          artifact={openArtifact}
          onClose={() => {
            setOpenArtifact(null);
            setMobilePane("chat");
          }}
        />
      )}
    </div>
  );
}
