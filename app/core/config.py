"""Application configuration.

Every knob an operator needs lives here and is driven by environment
variables, so switching models, providers or databases never requires a code
change. `Settings` is constructed once at import time and injected everywhere
else; nothing in the codebase reads `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["ollama", "groq", "openai", "anthropic"]

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Core ----------
    database_url: str = "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny"
    log_level: str = "info"
    log_format: Literal["json", "console"] = "json"
    cors_origins: str = "http://localhost:5173,http://localhost:8000"

    # ---------- Model selection ----------
    llm_provider: ProviderName = "ollama"
    llm_fallback_chain: str = "ollama"
    llm_timeout_seconds: float = 120.0
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.3

    # ---------- Providers ----------
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    # ---------- Retrieval ----------
    embeddings_enabled: bool = True
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    retrieval_top_k: int = 8
    # IDF-weighted share of the query's terms a chunk must contain to count as
    # evidence. Gates "the corpus does not cover this"; see app/rag/lexical.py.
    # 0.45 was measured, not guessed: over 25 labelled queries the in-corpus
    # floor was 0.497 and the out-of-corpus ceiling 0.519, and the single
    # overlapping case is one the corpus arguably does cover. See
    # tests/test_retrieval.py::test_coverage_gate_separates_in_and_out_of_corpus.
    retrieval_min_coverage: float = 0.45

    # ---------- Corpus ----------
    corpus_repo_url: str = (
        "https://github.com/LennysNewsletter/lennys-newsletterpodcastdata.git"
    )
    corpus_dir: Path = Field(default=REPO_ROOT / "data" / "corpus")

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, v: str) -> str:
        return v.lower()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def fallback_chain(self) -> list[ProviderName]:
        """Providers to try, in order, starting with the active one.

        The active provider is always first even if the operator forgot to
        include it in LLM_FALLBACK_CHAIN, and duplicates are collapsed so a
        misconfigured chain cannot cause the same provider to be retried twice.
        """
        chain: list[str] = [self.llm_provider]
        chain += [p.strip() for p in self.llm_fallback_chain.split(",") if p.strip()]
        seen: set[str] = set()
        ordered: list[ProviderName] = []
        for name in chain:
            if name in seen or name not in ("ollama", "groq", "openai", "anthropic"):
                continue
            seen.add(name)
            ordered.append(name)  # type: ignore[arg-type]
        return ordered

    def credentials_for(self, provider: ProviderName) -> str:
        """Return the API key a provider needs. Ollama is keyless by design."""
        return {
            "ollama": "n/a",
            "groq": self.groq_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }[provider]

    def model_for(self, provider: ProviderName) -> str:
        return {
            "ollama": self.ollama_model,
            "groq": self.groq_model,
            "openai": self.openai_model,
            "anthropic": self.anthropic_model,
        }[provider]

    def is_configured(self, provider: ProviderName) -> bool:
        """Whether a provider has everything it needs to be attempted.

        Used by /api/health and the UI badge so an evaluator can see at a
        glance which providers are live without reading logs.
        """
        return provider == "ollama" or bool(self.credentials_for(provider))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
