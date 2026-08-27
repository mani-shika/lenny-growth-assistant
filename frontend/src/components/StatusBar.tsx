/** System status, always visible.
 *
 *  The brief asks for the selected provider to be visible in the UI, but the
 *  more useful requirement is the one behind it: an evaluator should be able to
 *  tell *at a glance* whether the thing in front of them is fully working,
 *  running degraded, or broken - and if degraded, what to type to fix it.
 *  `/api/health` already computes that; this renders it.
 */

import { useState } from "react";
import type { Health } from "../types";

interface Props {
  health: Health | null;
  onRefresh: () => void;
}

export function StatusBar({ health, onRefresh }: Props) {
  const [open, setOpen] = useState(false);

  if (!health) {
    return (
      <div className="status status--unknown">
        <span className="status__dot" aria-hidden="true" />
        <span>Connecting…</span>
      </div>
    );
  }

  const active = health.providers.find((p) => p.active);
  const label =
    health.status === "ok"
      ? "All systems ready"
      : health.status === "degraded"
        ? "Running degraded"
        : "Not ready";

  return (
    <div className="status-wrap">
      <button
        className={`status status--${health.status}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`System status: ${label}. Show details.`}
      >
        <span className="status__dot" aria-hidden="true" />
        <span className="status__provider">
          {active ? `${active.name} · ${active.model}` : health.active_provider}
        </span>
        <span className="status__caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="status__panel" role="dialog" aria-label="System status">
          <div className="status__row">
            <strong>{label}</strong>
            <button className="link-button" onClick={onRefresh}>
              Refresh
            </button>
          </div>

          {health.checks.length > 0 && (
            <ul className="status__checks">
              {health.checks.map((check) => (
                <li key={check}>{check}</li>
              ))}
            </ul>
          )}

          <h4 className="status__heading">Knowledge base</h4>
          <p className="status__line">
            {health.corpus.documents} sources ({health.corpus.podcasts} episodes,{" "}
            {health.corpus.newsletters} posts) · {health.corpus.chunks} passages ·{" "}
            {health.corpus.embedded_chunks > 0
              ? "hybrid search"
              : "lexical search only"}
          </p>

          <h4 className="status__heading">Model providers</h4>
          <ul className="status__providers">
            {health.providers.map((provider) => (
              <li key={provider.name} className="status__provider-row">
                <span
                  className={`pill ${
                    provider.reachable
                      ? "pill--ok"
                      : provider.configured
                        ? "pill--warn"
                        : "pill--off"
                  }`}
                >
                  {provider.reachable
                    ? "ready"
                    : provider.configured
                      ? "unreachable"
                      : "no key"}
                </span>
                <span className="status__provider-name">
                  {provider.name}
                  {provider.active && <em> (active)</em>}
                </span>
                <span className="status__provider-model">{provider.model}</span>
                {provider.detail && (
                  <span className="status__detail">{provider.detail}</span>
                )}
              </li>
            ))}
          </ul>

          <p className="status__line status__line--muted">
            Fallback order: {health.fallback_chain.join(" → ")}
          </p>
          <p className="status__line status__line--muted">
            Database: {health.database ? "connected" : "unreachable"} · v
            {health.version}
          </p>
        </div>
      )}
    </div>
  );
}
