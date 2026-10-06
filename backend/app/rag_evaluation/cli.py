from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from backend.app.config import get_settings
from backend.app.knowledge.embeddings import create_embedding_provider
from backend.app.rag_evaluation.dataset import load_rag_evaluation_cases
from backend.app.rag_evaluation.reporting import write_dataset, write_report
from backend.app.rag_evaluation.runner import RagEvaluationRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate policy RAG chunk/overlap and Top-K settings"
    )
    parser.add_argument("--knowledge-dir", type=Path, default=Path("knowledge_docs"))
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("artifacts/rag-evaluation"),
    )
    parser.add_argument(
        "--embedding-mode",
        choices=("bge", "hash", "openai"),
        help="Override EMBEDDING_MODE; use hash for a fast harness smoke test.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.embedding_mode:
        settings = settings.model_copy(update={"embedding_mode": args.embedding_mode})
    knowledge_dir = args.knowledge_dir.resolve()
    if not knowledge_dir.is_dir():
        raise SystemExit(f"Knowledge directory not found: {knowledge_dir}")

    cases = load_rag_evaluation_cases()
    embeddings = create_embedding_provider(settings)
    try:
        report = await RagEvaluationRunner(
            settings=settings,
            embeddings=embeddings,
            knowledge_dir=knowledge_dir,
        ).run(cases)
    finally:
        await embeddings.close()

    json_path, markdown_path = write_report(report, args.report_dir)
    dataset_path = write_dataset(cases, args.report_dir)
    print(
        f"Evaluated {report.case_count} cases, {len(report.configurations)} "
        f"chunk configurations, Top-K={report.top_k_values}"
    )
    print(f"JSON report: {json_path.resolve()}")
    print(f"Markdown report: {markdown_path.resolve()}")
    print(f"Dataset: {dataset_path.resolve()}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
