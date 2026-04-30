"""CLI entrypoint for the evaluation framework.

Usage::

    # Run smoke suite
    python -m src.evaluation.run --suite smoke --output reports/smoke.json

    # Run a single case (debug mode)
    python -m src.evaluation.run --case ecommerce-001 --verbose

    # Diff two reports
    python -m src.evaluation.run --diff baseline.json head.json --output diff.md

    # Show help
    python -m src.evaluation.run --help

This module constructs all runtime dependencies (LLM, executor, ontology,
observability) and hands them to ``EvalRunner``.  Tests never import this
module; they construct the runner directly with fakes.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DATASETS_DIR = "tests/eval/datasets"
_DEFAULT_ONTOLOGIES_DIR = "ontologies"


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for the eval CLI.

    Returns:
        Configured ``ArgumentParser``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.evaluation.run",
        description=(
            "Tier-1 evaluation runner for ontology-aip-agent.\n\n"
            "Runs evaluation cases against the real LLM and reports accuracy.\n"
            "pytest (FakeLLM) and eval (real LLM) must never be mixed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--suite",
        metavar="NAME",
        help=(
            "Filter dataset by this suite tag (e.g. 'smoke', 'full'). "
            "Mutually exclusive with --case and --diff."
        ),
    )
    mode_group.add_argument(
        "--case",
        metavar="CASE_ID",
        help="Run a single case by ID (overrides --suite). Debug mode.",
    )
    mode_group.add_argument(
        "--diff",
        nargs=2,
        metavar=("BASELINE", "HEAD"),
        help=(
            "Diff two JSON report files. Mutually exclusive with --suite/--case. "
            "Output is Markdown unless --output ends with .json."
        ),
    )

    parser.add_argument(
        "--datasets-dir",
        metavar="PATH",
        default=_DEFAULT_DATASETS_DIR,
        help=f"Directory containing YAML dataset files. Default: {_DEFAULT_DATASETS_DIR}",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "Output path. If path ends with .json, writes JSON; otherwise Markdown. "
            "Default: print to stdout."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging and dump full agent state on failures.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of concurrent workers. Default: 1 (sequential). "
            "NOTE: concurrency > 1 is reserved for B-4; this flag is accepted "
            "but ignored for values > 1 in the current release."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------


def _build_llm(config: dict) -> Any:
    """Construct an LLMClient from *config*.

    Args:
        config: Fully-merged configuration dict from ``load_config()``.

    Returns:
        An LLMClient instance matching the configured provider.
    """
    provider = config["llm"].get("provider", "vertex")

    if provider == "ollama":
        from src.llm.ollama import OllamaClient

        return OllamaClient(
            host=config["ollama"]["host"],
            model_name=config["ollama"]["model"],
            timeout=config["ollama"]["timeout"],
        )
    if provider == "openai":
        from src.llm.openai_compat import OpenAICompatClient

        cfg = config["openai"]
        return OpenAICompatClient(
            api_key=cfg["api_key"],
            model_name=cfg.get("model", "gpt-4o"),
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            provider_name="OpenAI",
        )
    if provider == "openrouter":
        from src.llm.openai_compat import OpenAICompatClient

        cfg = config["openrouter"]
        extra_headers: dict[str, str] = {}
        if cfg.get("site_url"):
            extra_headers["HTTP-Referer"] = cfg["site_url"]
        if cfg.get("app_name"):
            extra_headers["X-Title"] = cfg["app_name"]
        return OpenAICompatClient(
            api_key=cfg["api_key"],
            model_name=cfg.get("model", "anthropic/claude-3.5-sonnet"),
            base_url=cfg.get("base_url", "https://openrouter.ai/api/v1"),
            provider_name="OpenRouter",
            extra_headers=extra_headers,
        )

    # Default: Vertex AI Gemini
    from src.llm.vertex import VertexGeminiClient

    return VertexGeminiClient(
        project=config["vertex"]["project"],
        location=config["vertex"]["location"],
        model_name=config["llm"]["model"],
        credentials_path=config["vertex"].get("credentials", ""),
    )


# ---------------------------------------------------------------------------
# Domain graph factory
# ---------------------------------------------------------------------------


def _make_graph_factory(config: dict, ontologies_dir: str, obs: Any) -> Any:
    """Return a ``graph_factory(domain) -> compiled_graph`` closure.

    The closure captures config, ontology dir, and observability client so
    the runner can rebuild the graph per case (different domains).

    Args:
        config: Loaded configuration dict.
        ontologies_dir: Directory containing ``<domain>.rdf`` files.
        obs: ObservabilityClient (may be disabled).

    Returns:
        A callable ``(domain: str) -> compiled LangGraph``.
    """
    import sqlite3
    from src.database.executor import SQLiteExecutor
    from src.database.schema import create_tables
    from src.database.mock_data import generate_mock_data
    from src.ontology.rdf_provider import RDFOntologyProvider
    from src.ontology.parser import parse_ontology
    from src.agent.graph import build_graph

    permissions = config.get("permissions", {
        "read": "auto",
        "write": "confirm",
        "delete": "confirm",
        "admin": "deny",
    })
    db_dir = Path(config.get("database", {}).get("path", "./data/eval"))
    db_dir.mkdir(parents=True, exist_ok=True)
    rows_per_table = config.get("database", {}).get("mock_rows_per_table", 20)

    _graph_cache: dict[str, Any] = {}

    def _factory(domain: str) -> Any:
        if domain in _graph_cache:
            return _graph_cache[domain]

        rdf_path = str(Path(ontologies_dir) / f"{domain}.rdf")
        if not Path(rdf_path).exists():
            raise FileNotFoundError(f"No ontology file found: {rdf_path}")

        db_path = str(db_dir / f"{domain}_eval.db")
        if not Path(db_path).exists():
            schema = parse_ontology(rdf_path)
            create_tables(db_path, schema)
            generate_mock_data(db_path, schema, rows_per_table=rows_per_table)

        executor = SQLiteExecutor(db_path, permissions)
        ontology = RDFOntologyProvider([rdf_path], executor_dialect=executor.dialect)

        llm = _build_llm(config)
        llm = obs.wrap_llm(llm) if obs and obs.enabled else llm

        graph = build_graph(
            llm=llm,
            executors=executor,
            ontology=ontology,
            obs=obs if (obs and obs.enabled) else None,
        )
        _graph_cache[domain] = graph
        logger.info("Built graph for domain=%s (db=%s)", domain, db_path)
        return graph

    return _factory


# ---------------------------------------------------------------------------
# Run mode handlers
# ---------------------------------------------------------------------------


def _run_eval(args: argparse.Namespace, config: dict) -> None:
    """Execute evaluation cases and write a report.

    Args:
        args: Parsed CLI arguments.
        config: Loaded configuration dict.
    """
    from src.evaluation.dataset import load_dataset
    from src.evaluation.runner import EvalRunner
    from src.observability import ObservabilityClient

    obs = ObservabilityClient(config.get("langfuse", {}))
    logger.info("Observability enabled: %s", obs.enabled)

    dataset = load_dataset(args.datasets_dir)

    if args.case:
        case = dataset.case_by_id(args.case)
        if case is None:
            logger.error("Case not found: %s", args.case)
            sys.exit(1)
        from src.evaluation.dataset import EvalDataset
        dataset = EvalDataset(cases=[case])
        suite_name = args.case
    elif args.suite:
        dataset = dataset.filter_by_suite(args.suite)
        suite_name = args.suite
        if len(dataset) == 0:
            logger.warning("No cases found for suite=%s", args.suite)
    else:
        suite_name = "all"

    logger.info("Running %d cases (suite=%s)", len(dataset), suite_name)

    graph_factory = _make_graph_factory(config, _DEFAULT_ONTOLOGIES_DIR, obs)
    runner = EvalRunner(graph_factory=graph_factory, obs=obs)

    report = runner.run(dataset)
    # Patch suite_name into the report (runner doesn't know it)
    from dataclasses import replace
    report = replace(report, suite_name=suite_name)

    llm_model = config["llm"].get("model", "unknown")
    report = replace(report, llm_model=llm_model)

    _write_report(args.output, report, verbose=args.verbose)

    if args.verbose:
        for cr in report.case_results:
            from src.evaluation.runner import _combine_outcome
            from src.evaluation.judges import JudgeOutcome
            outcome = _combine_outcome(cr.intent_result.outcome, cr.primary_result.outcome)
            if outcome == JudgeOutcome.FAIL:
                logger.debug(
                    "FAIL case=%s | intent=%s | primary=%s | agent_output=%s",
                    cr.case.id,
                    cr.intent_result.reason,
                    cr.primary_result.reason,
                    cr.agent_output,
                )

    logger.info(
        "Done. accuracy=%.1f%% (%d/%d) in %.1fs",
        report.accuracy * 100,
        report.summary.passed,
        report.summary.passed + report.summary.failed,
        report.summary.duration_ms / 1000,
    )


def _run_diff(args: argparse.Namespace) -> None:
    """Compute and display a diff between two JSON report files.

    Args:
        args: Parsed CLI arguments.  ``args.diff`` is a 2-item list of paths.
    """
    from src.evaluation.report import EvalReport, diff_reports

    baseline_path, head_path = args.diff
    logger.info("Diffing %s vs %s", baseline_path, head_path)

    with open(baseline_path) as f:
        baseline = EvalReport.from_json(json.load(f))
    with open(head_path) as f:
        head = EvalReport.from_json(json.load(f))

    diff = diff_reports(baseline, head)

    output = diff.to_markdown()
    _write_output(args.output, output)


def _write_report(output_path: str | None, report: "EvalReport", verbose: bool = False) -> None:  # type: ignore[name-defined]
    """Write *report* to *output_path* or stdout.

    Args:
        output_path: Destination path.  If ends with ``.json``, writes JSON;
            otherwise Markdown.  ``None`` means stdout.
        report: The EvalReport to write.
        verbose: When True, logs additional diagnostics.
    """
    if output_path and output_path.endswith(".json"):
        content = json.dumps(report.to_json(), indent=2, ensure_ascii=False)
    else:
        content = report.to_markdown()

    _write_output(output_path, content)


def _write_output(output_path: str | None, content: str) -> None:
    """Write *content* to *output_path* or stdout.

    Args:
        output_path: File path or ``None`` for stdout.
        content: String content to write.
    """
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("Report written to %s", output_path)
    else:
        print(content)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for the evaluation framework.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.concurrency > 1:
        logger.warning(
            "--concurrency %d requested; concurrent execution deferred to B-4. "
            "Running sequentially.",
            args.concurrency,
        )

    if args.diff:
        _run_diff(args)
        return

    from src.config import load_config

    try:
        config = load_config()
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    _run_eval(args, config)


if __name__ == "__main__":  # pragma: no cover
    main()
