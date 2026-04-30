"""EvalDataset: container and loader for EvalCase collections.

Datasets are loaded from YAML files (a flat list of case dicts per file).
Multiple files in a directory are merged; duplicate ``id`` values raise a
``ValueError``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import yaml
from pydantic import BaseModel, ConfigDict

from src.evaluation.case import EvalCase

logger = logging.getLogger(__name__)


class EvalDataset(BaseModel):
    """An immutable, iterable collection of EvalCase objects.

    Attributes:
        cases: Ordered list of evaluation cases in this dataset.
    """

    cases: list[EvalCase]

    model_config = ConfigDict(frozen=True)

    def filter_by_tag(self, tag: str) -> EvalDataset:
        """Return a new dataset containing only cases that include ``tag``.

        Args:
            tag: Tag string to filter on.

        Returns:
            Subset dataset whose cases all carry the given tag.
        """
        return EvalDataset(cases=[c for c in self.cases if tag in c.tags])

    def filter_by_domain(self, domain: str) -> EvalDataset:
        """Return a new dataset containing only cases for ``domain``.

        Args:
            domain: Domain name (e.g. ``"ecommerce"``).

        Returns:
            Subset dataset whose cases all belong to the given domain.
        """
        return EvalDataset(cases=[c for c in self.cases if c.domain == domain])

    def filter_by_suite(self, suite: str) -> EvalDataset:
        """Sugar for :meth:`filter_by_tag`.

        Args:
            suite: Suite name (treated as a tag).

        Returns:
            Subset dataset matching the suite tag.
        """
        return self.filter_by_tag(suite)

    def case_by_id(self, case_id: str) -> EvalCase | None:
        """Look up a single case by its unique id.

        Args:
            case_id: The ``id`` field of the desired case.

        Returns:
            The matching ``EvalCase`` or ``None`` if not found.
        """
        for case in self.cases:
            if case.id == case_id:
                return case
        return None

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[EvalCase]:
        return iter(self.cases)


def _load_yaml_file(path: Path) -> list[EvalCase]:
    """Parse a single YAML file into a list of EvalCase instances.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        List of validated EvalCase objects.

    Raises:
        ValueError: If the top-level YAML value is not a list.
    """
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, list):
        raise ValueError(
            f"Expected a top-level YAML list in {path!s}, got {type(raw).__name__}"
        )
    return [EvalCase.model_validate(item) for item in raw]


def load_dataset(path: str | Path) -> EvalDataset:
    """Load an EvalDataset from a single YAML file or a directory of YAML files.

    When ``path`` is a directory all ``*.yaml`` / ``*.yml`` files are
    loaded recursively and merged into one dataset.

    Args:
        path: Path to a ``.yaml`` / ``.yml`` file or a directory.

    Returns:
        Merged, validated EvalDataset.

    Raises:
        ValueError: If any ``id`` appears more than once across files.
    """
    p = Path(path)
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()

    if p.is_dir():
        files: list[Path] = sorted(
            f for f in p.rglob("*") if f.suffix in {".yaml", ".yml"}
        )
    else:
        files = [p]

    for file in files:
        logger.debug("Loading eval cases from %s", file)
        for case in _load_yaml_file(file):
            if case.id in seen_ids:
                raise ValueError(f"Duplicate EvalCase id: {case.id!r}")
            seen_ids.add(case.id)
            cases.append(case)

    return EvalDataset(cases=cases)


def list_suites(path: str | Path) -> list[str]:
    """Return a sorted list of unique suite tags found across all cases in ``path``.

    Args:
        path: Path passed to :func:`load_dataset`.

    Returns:
        Sorted list of tag strings.
    """
    dataset = load_dataset(path)
    suites: set[str] = set()
    for case in dataset:
        suites.update(case.tags)
    return sorted(suites)
