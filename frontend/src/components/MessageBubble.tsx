/** One turn in the conversation.
 *
 *  An assistant turn is not just text. It carries the evidence it used and the
 *  record of how it was produced, both reachable without leaving the thread:
 *  citations sit directly under the answer (collapsed by default so they never
 *  bury the prose), and the provenance line names the provider, model and
 *  latency. Trust in this product is a function of how cheap it is to check.
 */

import { useMemo, useState } from "react";
import type { EssayCritique, Message } from "../types";
import { linkifyCitations, renderMarkdown } from "../lib/markdown";

interface Props {
  message: Message;
  critique?: EssayCritique | null;
  onOpenArtifact?: (artifactId: string) => void;
}

const SKILL_LABEL: Record<string, string> = {
  qa: "Grounded answer",
  ship30_essay: "Ship 30 essay",
  artifact: "Artifact",
};

export function MessageBubble({ message, critique, onOpenArtifact }: Props) {
  const [showSources, setShowSources] = useState(false);
  const isUser = message.role === "user";

  // Only turn [n] into clickable markers when the model actually produced
  // resolvable ones. Otherwise the sources are provenance, not citations.
  const matched = message.citationsMatched !== false;

  const html = useMemo(() => {
    const rendered = renderMarkdown(message.content);
    return isUser || !matched
      ? rendered
      : linkifyCitations(rendered, message.citations.length);
  }, [message.content, message.citations.length, isUser, matched]);

  function handleMarkerClick(event: React.MouseEvent | React.KeyboardEvent) {
    const target = event.target as HTMLElement;
    if (!target.classList.contains("citation-marker")) return;
    setShowSources(true);
    const marker = target.dataset.marker;
    window.requestAnimationFrame(() => {
      document
        .getElementById(`source-${message.id}-${marker}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  return (
    <article className={`msg msg--${isUser ? "user" : "assistant"}`}>
      <div className="msg__meta">
        <span className="msg__author">{isUser ? "You" : "Assistant"}</span>
        {!isUser && message.route && (
          <span className="chip chip--skill">
            {SKILL_LABEL[message.route] ?? message.route}
          </span>
        )}
      </div>

      <div
        className="msg__body markdown"
        onClick={handleMarkerClick}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") handleMarkerClick(e);
        }}
        dangerouslySetInnerHTML={{ __html: html }}
      />

      {!isUser && message.artifact_id && onOpenArtifact && (
        <button
          className="msg__artifact-link"
          onClick={() => onOpenArtifact(message.artifact_id!)}
        >
          Open artifact
        </button>
      )}

      {critique && (
        <div
          className={`critique ${critique.passed ? "critique--ok" : "critique--warn"}`}
        >
          <strong>
            Ship 30 check: {critique.word_count} words -{" "}
            {critique.passed ? "meets the format" : "partially meets the format"}
          </strong>
          {!critique.passed && (
            <ul>
              {critique.failures.map((failure) => (
                <li key={failure}>{failure}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!isUser && message.citations.length > 0 && (
        <div className="sources">
          <button
            className="sources__toggle"
            onClick={() => setShowSources((v) => !v)}
            aria-expanded={showSources}
          >
            {showSources ? "Hide" : "Show"} {message.citations.length}{" "}
            {matched ? "source" : "source consulted"}
            {message.citations.length === 1 ? "" : "s"}
          </button>

          {showSources && !matched && (
            <p className="sources__note">
              This answer did not label its claims, so these are the passages it
              was written from rather than numbered citations.
            </p>
          )}

          {showSources && (
            <ol className="sources__list">
              {message.citations.map((citation, index) => {
                const marker = citation.marker ?? index + 1;
                return (
                  <li
                    key={`${citation.chunk_id}-${marker}`}
                    id={`source-${message.id}-${marker}`}
                    className="source"
                  >
                    <div className="source__head">
                      {matched && <span className="source__marker">{marker}</span>}
                      {citation.source_url ? (
                        <a
                          href={citation.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="source__title"
                        >
                          {citation.document_title}
                        </a>
                      ) : (
                        // 14 of 60 corpus files ship no canonical URL. Show the
                        // source anyway rather than hiding the evidence.
                        <span className="source__title source__title--plain">
                          {citation.document_title}
                        </span>
                      )}
                    </div>
                    <div className="source__sub">
                      {citation.guest && <span>{citation.guest}</span>}
                      {citation.timestamp && (
                        <span className="source__time">{citation.timestamp}</span>
                      )}
                      <span className="source__type">{citation.doc_type}</span>
                    </div>
                    <p className="source__excerpt">{citation.excerpt}</p>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}

      {!isUser && message.provider && message.provider !== "none" && (
        <p className="msg__provenance">
          {message.provider} · {message.model}
          {message.latency_ms != null &&
            ` · ${(message.latency_ms / 1000).toFixed(1)}s`}
        </p>
      )}
    </article>
  );
}
