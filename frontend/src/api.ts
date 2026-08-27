/** Typed API client.
 *
 *  Every failure is normalised into `ApiError`, which carries the server's
 *  machine-readable code, its operator hint, and the request id. That is what
 *  lets the UI show "Ollama isn't running - here's the command" instead of
 *  "Something went wrong", and lets a user quote an id in a bug report.
 */

import type {
  ApiErrorBody,
  Artifact,
  ChatResponse,
  Health,
  ProviderName,
  Session,
  SessionDetail,
  SkillName,
} from "./types";

export class ApiError extends Error {
  code: string;
  hint: string;
  requestId: string;
  status: number;

  constructor(
    message: string,
    opts: { code?: string; hint?: string; requestId?: string; status?: number } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.code = opts.code ?? "unknown_error";
    this.hint = opts.hint ?? "";
    this.requestId = opts.requestId ?? "";
    this.status = opts.status ?? 0;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    // fetch only rejects on network-level failure, so this is unambiguous.
    throw new ApiError("Cannot reach the API server.", {
      code: "network_error",
      hint: "Is the backend running on port 8000?",
    });
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }

  if (!response.ok) {
    const envelope = body as ApiErrorBody | null;
    if (envelope?.error) {
      throw new ApiError(envelope.error.message, {
        code: envelope.error.code,
        hint: envelope.error.hint,
        requestId: envelope.error.request_id,
        status: response.status,
      });
    }
    throw new ApiError(`Request failed (${response.status}).`, {
      status: response.status,
    });
  }

  return body as T;
}

export const api = {
  health: () => request<Health>("/api/health"),

  listSessions: () => request<Session[]>("/api/sessions"),

  createSession: (title?: string) =>
    request<Session>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null }),
    }),

  getSession: (id: string) => request<SessionDetail>(`/api/sessions/${id}`),

  deleteSession: (id: string) =>
    request<void>(`/api/sessions/${id}`, { method: "DELETE" }),

  sendMessage: (
    sessionId: string,
    message: string,
    opts: { skill?: SkillName | null; provider?: ProviderName | null } = {},
  ) =>
    request<ChatResponse>(`/api/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        message,
        skill: opts.skill ?? null,
        provider: opts.provider ?? null,
      }),
    }),

  getArtifact: (id: string) => request<Artifact>(`/api/artifacts/${id}`),
};
