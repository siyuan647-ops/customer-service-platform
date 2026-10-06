from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from backend.app.config import get_settings
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.knowledge.reranker import create_reranker
from backend.app.rag_evaluation.dataset import load_rag_evaluation_cases
from backend.app.rag_evaluation.reporting import write_threshold_report
from backend.app.rag_evaluation.reranker_runner import RerankerThresholdRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate the policy RAG reranker threshold")
    parser.add_argument("--knowledge-dir", type=Path, default=Path("knowledge_docs"))
    parser.add_argument("--report-dir", type=Path, default=Path("artifacts/rag-evaluation"))
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings().model_copy(
        update={"reranker_enabled": True, "embedding_mode": "bge"}
    )
    embeddings = create_embedding_provider(settings)
    reranker = create_reranker(settings)
    if reranker is None:
        raise SystemExit("Reranker could not be created")
    try:
        report = await RerankerThresholdRunner(
            settings=settings,
            embeddings=embeddings,
            reranker=reranker,
            knowledge_dir=args.knowledge_dir.resolve(),
        ).run(load_rag_evaluation_cases())
    finally:
        await reranker.close()
        await embeddings.close()
    json_path, markdown_path = write_threshold_report(report, args.report_dir)
    print(f"Selected threshold: {report.selected_threshold:.4f}")
    print(f"JSON report: {json_path.resolve()}")
    print(f"Markdown report: {markdown_path.resolve()}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
