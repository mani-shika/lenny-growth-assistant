/** The artifact panel - a rendered document beside the chat.
 *
 *  Isolation model, which is the whole point of this component:
 *
 *  - **HTML** renders in an `<iframe srcdoc sandbox="">`. An empty `sandbox`
 *    grants *no* capabilities: no scripts, no same-origin, no forms, no
 *    top-level navigation, no popups. The document was already allowlist-
 *    sanitised server-side and carries a `default-src 'none'` CSP, so this is
 *    the third of three independent layers.
 *  - **Markdown** renders inline through DOMPurify. Its output surface is
 *    narrow and it needs the app's typography, so an iframe would cost styling
 *    for no meaningful security gain.
 *
 *  The "Blocked" tab exists because a security control nobody can see is a
 *  security control nobody trusts. It shows exactly what the sanitiser removed.
 */

import { useMemo, useState } from "react";
import type { Artifact } from "../types";
import { renderMarkdown } from "../lib/markdown";

interface Props {
  artifact: Artifact;
  onClose: () => void;
}

type Tab = "preview" | "source" | "blocked";

export function ArtifactViewer({ artifact, onClose }: Props) {
  const [tab, setTab] = useState<Tab>("preview");
  const [copied, setCopied] = useState(false);

  const report = artifact.sanitiser_report ?? {};
  const blockedCount =
    (report.removed_tags?.length ?? 0) +
    (report.removed_attributes?.length ?? 0) +
    (report.removed_urls?.length ?? 0);

  const markdownHtml = useMemo(
    () => (artifact.kind === "markdown" ? renderMarkdown(artifact.content) : ""),
    [artifact.kind, artifact.content],
  );

  async function copySource() {
    try {
      await navigator.clipboard.writeText(artifact.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  return (
    <aside className="artifact" aria-label="Artifact viewer">
      <header className="artifact__head">
        <div className="artifact__titles">
          <span className="artifact__kind" data-kind={artifact.kind}>
            {artifact.kind === "html" ? "HTML" : "Markdown"}
          </span>
          <h2 className="artifact__title" title={artifact.title}>
            {artifact.title}
          </h2>
        </div>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="Close artifact panel"
          title="Close"
        >
          ✕
        </button>
      </header>

      <div className="artifact__tabs" role="tablist" aria-label="Artifact views">
        {(["preview", "source", "blocked"] as Tab[]).map((name) => (
          <button
            key={name}
            role="tab"
            id={`artifact-tab-${name}`}
            aria-selected={tab === name}
            aria-controls={`artifact-panel-${name}`}
            className={`artifact__tab ${tab === name ? "is-active" : ""}`}
            onClick={() => setTab(name)}
          >
            {name === "preview" && "Preview"}
            {name === "source" && "Source"}
            {name === "blocked" && (
              <>
                Blocked
                {blockedCount > 0 && (
                  <span className="badge badge--warn">{blockedCount}</span>
                )}
              </>
            )}
          </button>
        ))}
        <button className="artifact__copy" onClick={copySource}>
          {copied ? "Copied" : "Copy source"}
        </button>
      </div>

      {tab === "preview" && (
        <div
          className="artifact__body"
          role="tabpanel"
          id="artifact-panel-preview"
          aria-labelledby="artifact-tab-preview"
        >
          {artifact.kind === "html" ? (
            <iframe
              className="artifact__frame"
              title={`Rendered artifact: ${artifact.title}`}
              srcDoc={artifact.content}
              // Empty sandbox = every restriction on. Do not add tokens here:
              // allow-scripts together with allow-same-origin would let the
              // frame remove its own sandbox.
              sandbox=""
              referrerPolicy="no-referrer"
              loading="lazy"
            />
          ) : (
            <article
              className="artifact__markdown markdown"
              dangerouslySetInnerHTML={{ __html: markdownHtml }}
            />
          )}
        </div>
      )}

      {tab === "source" && (
        <div
          className="artifact__body"
          role="tabpanel"
          id="artifact-panel-source"
          aria-labelledby="artifact-tab-source"
        >
          <pre className="artifact__source">
            <code>{artifact.content}</code>
          </pre>
        </div>
      )}

      {tab === "blocked" && (
        <div
          className="artifact__body artifact__body--pad"
          role="tabpanel"
          id="artifact-panel-blocked"
          aria-labelledby="artifact-tab-blocked"
        >
          <h3 className="artifact__section">What the viewer permits</h3>
          <ul className="artifact__list">
            <li>Structural markup, text formatting, tables and inline styles.</li>
            <li>Links, which open in a new tab with the opener severed.</li>
            <li>Images embedded as inline <code>data:</code> URIs.</li>
          </ul>

          <h3 className="artifact__section">What it blocks, always</h3>
          <ul className="artifact__list">
            <li>
              <strong>Scripts and event handlers.</strong> Stripped server-side,
              and the frame has no script permission regardless.
            </li>
            <li>
              <strong>Network access.</strong> No remote images, fonts,
              stylesheets or fetches - a beacon cannot report that you opened
              this, and cannot exfiltrate its contents.
            </li>
            <li>
              <strong>Frames, forms and embeds.</strong> Removed with their
              contents, so no credential prompt can be staged.
            </li>
            <li>
              <strong>Same-origin access.</strong> The frame runs in an opaque
              origin and cannot read this page, its storage or its cookies.
            </li>
          </ul>

          <h3 className="artifact__section">Removed from this artifact</h3>
          {blockedCount === 0 ? (
            <p className="artifact__muted">
              Nothing. This artifact was already within policy.
            </p>
          ) : (
            <dl className="artifact__report">
              {report.removed_tags?.length ? (
                <>
                  <dt>Tags</dt>
                  <dd>{report.removed_tags.join(", ")}</dd>
                </>
              ) : null}
              {report.removed_attributes?.length ? (
                <>
                  <dt>Attributes</dt>
                  <dd>{report.removed_attributes.join(", ")}</dd>
                </>
              ) : null}
              {report.removed_urls?.length ? (
                <>
                  <dt>URLs</dt>
                  <dd>
                    {report.removed_urls.map((url) => (
                      <code key={url} className="artifact__url">
                        {url}
                      </code>
                    ))}
                  </dd>
                </>
              ) : null}
            </dl>
          )}
        </div>
      )}
    </aside>
  );
}
