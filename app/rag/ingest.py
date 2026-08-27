"""Corpus ingestion: fetch -> parse -> chunk -> embed -> index.

The corpus is **not committed to this repository**. Lenny's Data licenses the
starter dataset for personal, non-commercial use and explicitly forbids
redistributing the raw files, so we fetch from the upstream repository at setup
time and keep `data/corpus/` in `.gitignore`. See docs/architecture.md.

Refresh is incremental and content-addressed: a document whose sha256 is
unchanged is skipped entirely, so re-running ingestion after an upstream update
only re-embeds what actually moved.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger, timed
from app.db.models import Chunk, Document
from app.rag.chunker import chunk_segments
from app.rag.corpus import iter_corpus_files, parse_document
from app.rag.embeddings import embed_texts
from app.rag.retriever import retriever

log = get_logger(__name__)

EMBED_BATCH = 32


@dataclass(slots=True)
class IngestReport:
    documents_seen: int = 0
    documents_indexed: int = 0
    documents_skipped: int = 0
    chunks_written: int = 0
    chunks_embedded: int = 0
    embeddings_available: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "documents_seen": self.documents_seen,
            "documents_indexed": self.documents_indexed,
            "documents_skipped": self.documents_skipped,
            "chunks_written": self.chunks_written,
            "chunks_embedded": self.chunks_embedded,
            "embeddings_available": self.embeddings_available,
            "errors": self.errors,
        }


def ensure_corpus(corpus_dir: Path | None = None, *, refresh: bool = False) -> Path:
    """Make sure the corpus is on disk, cloning or pulling as needed.

    A network failure on refresh is non-fatal when we already have a copy -
    ingesting a slightly stale corpus beats failing setup entirely.
    """
    target = Path(corpus_dir or settings.corpus_dir)
    git = shutil.which("git")

    if target.exists() and any(target.glob("**/*.md")):
        if refresh and git and (target / ".git").exists():
            try:
                subprocess.run(
                    [git, "-C", str(target), "pull", "--ff-only"],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
                log.info("corpus.refreshed", path=str(target))
            except (subprocess.SubprocessError, OSError) as exc:
                log.warning("corpus.refresh_failed", error=str(exc)[:300])
        return target

    if not git:
        raise RuntimeError(
            "git is required to fetch the corpus. Install git, or download the "
            f"repository manually into {target}."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("corpus.cloning", url=settings.corpus_repo_url, path=str(target))
    subprocess.run(
        [git, "clone", "--depth", "1", settings.corpus_repo_url, str(target)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return target


async def ingest(
    session: AsyncSession,
    *,
    corpus_dir: Path | None = None,
    force: bool = False,
) -> IngestReport:
    """Ingest every corpus file, skipping documents whose checksum is unchanged."""
    report = IngestReport()
    root = Path(corpus_dir or settings.corpus_dir)
    files = iter_corpus_files(root)

    if not files:
        report.errors.append(
            f"No markdown found under {root}. Run ensure_corpus() first."
        )
        return report

    existing = {
        row[0]: row[1]
        for row in (
            await session.execute(select(Document.source_path, Document.checksum))
        ).all()
    }

    for path in files:
        report.documents_seen += 1
        relative = path.relative_to(root).as_posix()
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{relative}: unreadable ({exc})")
            continue

        parsed = parse_document(relative, raw)
        if not force and existing.get(relative) == parsed.checksum:
            report.documents_skipped += 1
            continue

        chunks = chunk_segments(parsed.segments)
        if not chunks:
            report.errors.append(f"{relative}: produced no chunks")
            continue

        document = (
            await session.execute(
                select(Document).where(Document.source_path == relative)
            )
        ).scalar_one_or_none()

        if document is None:
            document = Document(source_path=relative)
            session.add(document)

        document.title = parsed.title
        document.doc_type = parsed.doc_type
        document.guest = parsed.guest
        document.published_at = parsed.published_at
        document.source_url = parsed.source_url
        document.word_count = parsed.word_count
        document.checksum = parsed.checksum
        await session.flush()

        # Replace wholesale: chunk boundaries move when a document changes, so
        # a diff-based update would leave orphaned passages behind.
        await session.execute(delete(Chunk).where(Chunk.document_id == document.id))

        rows = [
            Chunk(
                document_id=document.id,
                ordinal=c.ordinal,
                text=c.text,
                speakers=c.speakers,
                start_timestamp=c.start_timestamp,
                token_estimate=c.token_estimate,
            )
            for c in chunks
        ]
        session.add_all(rows)
        await session.flush()

        report.documents_indexed += 1
        report.chunks_written += len(rows)
        log.info(
            "ingest.document",
            source=relative,
            doc_type=parsed.doc_type,
            chunks=len(rows),
        )

    await session.commit()

    embedded = await _embed_missing(session)
    report.chunks_embedded = embedded
    report.embeddings_available = embedded > 0

    await retriever.load(session)
    log.info("ingest.complete", **report.to_dict())
    return report


async def _embed_missing(session: AsyncSession) -> int:
    """Embed chunks that have no vector yet.

    Runs after the text is already committed, which is what makes embeddings
    optional: if this returns 0 because Ollama is down, the corpus is still
    fully searchable lexically and a later run can fill the vectors in.
    """
    if not settings.embeddings_enabled:
        log.info("ingest.embeddings_disabled")
        return 0

    pending = (
        await session.execute(
            select(Chunk.id, Chunk.text).where(Chunk.embedding.is_(None))
        )
    ).all()
    if not pending:
        return 0

    written = 0
    with timed(log, "ingest.embed", pending=len(pending)) as fields:
        for start in range(0, len(pending), EMBED_BATCH):
            batch = pending[start : start + EMBED_BATCH]
            vectors = await embed_texts([row[1] for row in batch])
            if not vectors:
                log.warning(
                    "ingest.embeddings_stopped",
                    completed=written,
                    remaining=len(pending) - written,
                    hint=(
                        f"pull the model with `ollama pull {settings.embedding_model}` "
                        "then re-run ingestion to fill in the vectors"
                    ),
                )
                break
            for (chunk_id, _), vector in zip(batch, vectors, strict=True):
                chunk = await session.get(Chunk, chunk_id)
                if chunk is not None:
                    chunk.embedding = vector
                    written += 1
            await session.commit()
        fields["embedded"] = written
    return written
