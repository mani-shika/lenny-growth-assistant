/** Mirrors app/api/schemas.py. Kept hand-written and small so the contract is
 *  readable in one screen; if it grows, generate it from the OpenAPI schema. */

export type SkillName = "qa" | "ship30_essay" | "artifact";
export type ProviderName = "ollama" | "groq" | "openai" | "anthropic";

export interface Citation {
  chunk_id: string;
  document_title: string;
  doc_type: string;
  guest: string | null;
  published_at: string | null;
  source_url: string | null;
  speakers: string;
  timestamp: string | null;
  score: number;
  excerpt: string;
  marker: number | null;
}

export interface SanitiserReport {
  modified?: boolean;
  removed_tags?: string[];
  removed_attributes?: string[];
  removed_urls?: string[];
}

export interface Artifact {
  id: string;
  session_id: string;
  message_id: string | null;
  kind: "markdown" | "html";
  title: string;
  content: string;
  sanitiser_report: SanitiserReport;
  created_at: string;
}

export interface Message {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  provider: string | null;
  model: string | null;
  latency_ms: number | null;
  usage: Record<string, unknown>;
  citations: Citation[];
  route: string | null;
  artifact_id: string | null;
  /** Client-side only: mirrors ChatResponse.citations_matched for this turn. */
  citationsMatched?: boolean;
}

export interface RouteDecision {
  skill: SkillName;
  confidence: number;
  reason: string;
  artifact_kind: string | null;
}

export interface EssayCritique {
  word_count: number;
  passed: boolean;
  failures: string[];
  warnings: string[];
}

export interface ChatResponse {
  session_id: string;
  user_message: Message;
  assistant_message: Message;
  artifact: Artifact | null;
  route: RouteDecision;
  retrieval_strategy: string;
  retrieved_chunks: number;
  provider_attempts: Array<Record<string, unknown>>;
  grounded: boolean;
  /** False => `citations` are passages retrieved, not markers the answer used. */
  citations_matched: boolean;
  essay_critique: EssayCritique | null;
}

export interface Session {
  id: string;
  title: string;
  user_id: string;
  provider: string;
  model: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface SessionDetail extends Session {
  messages: Message[];
  artifacts: Artifact[];
}

export interface ProviderStatus {
  name: string;
  configured: boolean;
  reachable: boolean;
  model: string;
  detail: string;
  active: boolean;
}

export interface CorpusStatus {
  documents: number;
  chunks: number;
  embedded_chunks: number;
  indexed: boolean;
  podcasts: number;
  newsletters: number;
}

export interface Health {
  status: "ok" | "degraded" | "down";
  version: string;
  database: boolean;
  corpus: CorpusStatus;
  providers: ProviderStatus[];
  active_provider: string;
  fallback_chain: string[];
  embeddings: Record<string, unknown>;
  checks: string[];
}

/** The structured error envelope every failing endpoint returns. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    hint: string;
    details: Record<string, unknown>;
    request_id: string;
  };
}
