#!/usr/bin/env python
"""Fetch the corpus and build the retrieval index.

    python scripts/ingest.py                 # incremental: only changed docs
    python scripts/ingest.py --force         # re-chunk and re-embed everything
    python scripts/ingest.py --refresh       # git pull the corpus first
    python scripts/ingest.py --skip-embeddings

Safe to run repeatedly. Documents whose sha256 has not changed are skipped, so
a second run over an unchanged corpus does almost no work.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/ingest.py` from the repository root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_db  # noqa: E402
from app.rag.ingest import ensure_corpus, ingest  # noqa: E402

log = get_logger("ingest")


async def main(args: argparse.Namespace) -> int:
    configure_logging()

    if args.skip_embeddings:
        settings.embeddings_enabled = False

    try:
        corpus_dir = ensure_corpus(refresh=args.refresh)
    except Exception as exc:  # noqa: BLE001 - surface a readable setup failure
        log.error(
            "ingest.corpus_unavailable",
            error=str(exc)[:400],
            hint=(
                "Clone it manually: git clone --depth 1 "
                f"{settings.corpus_repo_url} {settings.corpus_dir}"
            ),
        )
        return 2

    try:
        await init_db()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "ingest.database_unavailable",
            error=str(exc)[:400],
            hint="Is Postgres up? Check DATABASE_URL. `docker compose up db -d`",
        )
        return 3

    async with get_sessionmaker()() as session:
        report = await ingest(session, corpus_dir=corpus_dir, force=args.force)

    await dispose_engine()

    print("\n--- Ingestion summary ---")
    for key, value in report.to_dict().items():
        if key != "errors":
            print(f"  {key:22s} {value}")
    if report.errors:
        print(f"  errors                 {len(report.errors)}")
        for err in report.errors[:10]:
            print(f"    - {err}")

    if not report.embeddings_available and settings.embeddings_enabled:
        print(
            "\n  Note: no embeddings were written. Retrieval will run lexical-only,\n"
            f"  which works. For hybrid search: ollama pull {settings.embedding_model}\n"
            "  then re-run this script."
        )

    # An empty index is the one outcome that must fail the container.
    indexed_anything = report.chunks_written > 0 or report.documents_skipped > 0
    return 0 if indexed_anything else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="re-index every document"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="git pull the corpus before indexing"
    )
    parser.add_argument(
        "--skip-embeddings",
        action="store_true",
        help="lexical index only (fast, no Ollama needed)",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
