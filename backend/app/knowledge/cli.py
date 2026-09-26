from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.knowledge.service import KnowledgeService
from backend.app.storage import ObjectStorage


async def import_directory(path: Path) -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    storage = ObjectStorage(settings)
    service = KnowledgeService(
        database=database,
        storage=storage,
        embeddings=create_embedding_provider(settings),
        settings=settings,
    )
    try:
        await storage.ensure_bucket()
        files = sorted(
            item for item in path.iterdir() if item.is_file() and item.suffix.casefold() in {".md", ".markdown", ".txt"}
        )
        if not files:
            raise RuntimeError(f"No supported policy files found in {path}")
        for file_path in files:
            result = await service.ingest(
                filename=file_path.name,
                data=file_path.read_bytes(),
                content_type="text/markdown" if file_path.suffix.casefold() != ".txt" else "text/plain",
            )
            state = "unchanged" if result.unchanged else "imported"
            print(f"{state}: {result.title} ({result.chunk_count} chunks)")
    finally:
        await service.close()
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import policy documents into pgvector")
    parser.add_argument("directory", nargs="?", default="knowledge_docs")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        raise SystemExit(f"Knowledge directory not found: {directory}")
    asyncio.run(import_directory(directory))


if __name__ == "__main__":
    main()
