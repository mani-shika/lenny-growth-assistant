"""Structured application errors.

Each error carries a stable machine-readable `code` plus a `hint` written for
whoever is operating the system. The API turns these into a consistent JSON
envelope, so a client never has to parse prose to know what went wrong or
whether retrying is worthwhile.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    code: str = "internal_error"
    status_code: int = 500
    hint: str = "An unexpected error occurred. Check the server logs."

    def __init__(self, message: str | None = None, **details: Any) -> None:
        super().__init__(message or self.hint)
        self.message = message or self.hint
        self.details = details

    def to_payload(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
                "details": self.details,
                "request_id": request_id,
            }
        }


class SessionNotFound(AppError):
    code = "session_not_found"
    status_code = 404
    hint = "Start a new chat, or list sessions via GET /api/sessions."


class ArtifactNotFound(AppError):
    code = "artifact_not_found"
    status_code = 404
    hint = "The artifact id does not exist or belongs to another session."


class CorpusNotIndexed(AppError):
    code = "corpus_not_indexed"
    status_code = 503
    hint = "Run `python scripts/ingest.py` (or `docker compose run --rm api python scripts/ingest.py`) to build the index."


class ProviderUnavailable(AppError):
    code = "provider_unavailable"
    status_code = 503
    hint = (
        "The model provider could not be reached. For Ollama, confirm `ollama serve` "
        "is running and the model is pulled. For cloud providers, confirm the API key."
    )


class ProviderTimeout(ProviderUnavailable):
    code = "provider_timeout"
    hint = "The model took too long to respond. Raise LLM_TIMEOUT_SECONDS or use a smaller model."


class ProviderNotConfigured(AppError):
    code = "provider_not_configured"
    status_code = 400
    hint = "This provider needs an API key. Set it in .env and restart, or switch LLM_PROVIDER."


class AllProvidersFailed(AppError):
    code = "all_providers_failed"
    status_code = 503
    hint = "Every provider in LLM_FALLBACK_CHAIN failed. See `details.attempts` for the per-provider reason."


class DatabaseUnavailable(AppError):
    code = "database_unavailable"
    status_code = 503
    hint = "Postgres is unreachable. Check DATABASE_URL and that the `db` container is healthy."


class ValidationFailed(AppError):
    code = "validation_failed"
    status_code = 422
    hint = "The request body did not match the expected contract."


class UnsafeArtifact(AppError):
    code = "unsafe_artifact"
    status_code = 422
    hint = "The generated artifact was rejected by the sanitiser. See details.reasons."
