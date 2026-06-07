#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Generate a source coverage report for the PostgreSQL internals wiki.

Reads the source-index for a given PG version and the wiki page frontmatter,
then writes reports/coverage/coverage_<timestamp>.md showing which backend .c
files are covered by at least one wiki page.

Usage: python3 tools/coverage_report.py PG_17_10
"""

import argparse
import csv
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "versions.toml"
COVERAGE_DIR = REPO_ROOT / "reports" / "coverage"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"Config not found: {CONFIG_FILE}")
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def backend_c_files(version: str) -> list[str]:
    """All .c files under src/backend/ from the given index version."""
    index_dir = REPO_ROOT / "source-index" / "postgresql" / version
    if not index_dir.exists():
        sys.exit(f"Index not found: {index_dir}\nRun tools/index_postgres.py {version} first.")
    with (index_dir / "files.csv").open() as f:
        paths = [row["path"] for row in csv.DictReader(f)]
    return [p for p in paths if p.startswith("src/backend/") and p.endswith(".c")]


def symbol_counts_per_file(version: str) -> dict[str, int]:
    """Number of functions/structs/typedefs/enums per source file."""
    index_dir = REPO_ROOT / "source-index" / "postgresql" / version
    counts: dict[str, int] = defaultdict(int)
    important = {"function", "typedef", "struct", "enum"}
    with (index_dir / "symbols.csv").open() as f:
        for row in csv.DictReader(f):
            if row["kind"] in important:
                counts[row["path"]] += 1
    return counts


def wiki_source_refs() -> set[str]:
    """All source file paths mentioned in any wiki page's source_files: frontmatter."""
    refs: set[str] = set()
    for md_file in (REPO_ROOT / "wiki").rglob("*.md"):
        content = md_file.read_text()
        match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not match:
            continue
        for src_path in re.findall(r"  - (src/[^\n]+)", match.group(1)):
            refs.add(src_path)
    return refs


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

@dataclass
class SubsystemStats:
    name: str
    total: int
    covered: int
    uncovered_by_weight: list[str]  # sorted by symbol count, descending

    @property
    def pct(self) -> int:
        return self.covered * 100 // self.total if self.total else 0


def subsystem_for(path: str, rules: list[dict]) -> str:
    for rule in rules:
        if path.startswith(rule["prefix"]):
            return rule["subsystem"]
    return "other"


def compute_stats(
    files: list[str],
    covered: set[str],
    symbol_counts: dict[str, int],
    rules: list[dict],
    order: list[str],
) -> list[SubsystemStats]:
    by_subsystem: dict[str, list[str]] = defaultdict(list)
    for f in files:
        by_subsystem[subsystem_for(f, rules)].append(f)

    stats = []
    seen: set[str] = set()
    for name in [s for s in order if s != "other"] + ["other"]:
        if name in seen or name not in by_subsystem:
            continue
        seen.add(name)
        sub_files = by_subsystem[name]
        uncovered = sorted(
            [f for f in sub_files if f not in covered],
            key=lambda f: -symbol_counts.get(f, 0),
        )
        stats.append(SubsystemStats(
            name=name,
            total=len(sub_files),
            covered=sum(1 for f in sub_files if f in covered),
            uncovered_by_weight=uncovered,
        ))
    return stats


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render(
    version: str,
    files: list[str],
    covered: set[str],
    symbol_counts: dict[str, int],
    rules: list[dict],
    stats: list[SubsystemStats],
) -> str:
    wiki_page_count = sum(1 for _ in (REPO_ROOT / "wiki").rglob("*.md"))
    total_covered = sum(1 for f in files if f in covered)

    lines = [
        "# Source Coverage Report",
        "",
        f"Version: `{version}`. "
        f"{len(files)} `.c` files in `src/backend/`; "
        f"{wiki_page_count} wiki pages referencing {len(covered)} source files.",
        "",
        "Coverage = % of backend `.c` files referenced in at least one wiki page's `source_files:`.",
        "",
        "## Coverage by Subsystem",
        "",
        "| Subsystem | Files | Covered | % | Top uncovered files |",
        "|---|---|---|---|---|",
    ]

    for s in stats:
        top3 = ", ".join(f"`{Path(f).name}`" for f in s.uncovered_by_weight[:3]) or "—"
        lines.append(f"| {s.name} | {s.total} | {s.covered} | {s.pct}% | {top3} |")
    lines.append(
        f"| **total** | **{len(files)}** | **{total_covered}** "
        f"| **{total_covered * 100 // len(files)}%** | |"
    )

    other = next((s for s in stats if s.name == "other"), None)
    if other:
        by_dir: dict[str, list[str]] = defaultdict(list)
        for f in files:
            if subsystem_for(f, rules) == "other":
                by_dir[Path(f).parts[2]].append(f)

        lines += [
            "",
            "## 'other' Subsystem Breakdown",
            "",
            "Directories not matched by any subsystem rule:",
            "",
            "| Directory | Files | Covered | % |",
            "|---|---|---|---|",
        ]
        for dir_name, dir_files in sorted(by_dir.items(), key=lambda x: -len(x[1])):
            n_covered = sum(1 for f in dir_files if f in covered)
            pct = n_covered * 100 // len(dir_files)
            lines.append(f"| `src/backend/{dir_name}/` | {len(dir_files)} | {n_covered} | {pct}% |")

    uncovered_ranked = sorted(
        [f for f in files if f not in covered],
        key=lambda f: -symbol_counts.get(f, 0),
    )
    lines += [
        "",
        "## Highest-Impact Uncovered Files",
        "",
        "Uncovered backend `.c` files ranked by symbol count (higher = more complex).",
        "",
        "| File | Subsystem | Symbols |",
        "|---|---|---|",
    ]
    for f in uncovered_ranked[:50]:
        lines.append(f"| `{f}` | {subsystem_for(f, rules)} | {symbol_counts.get(f, 0)} |")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    available = sorted(config["postgresql"].keys() - {"subsystems"})

    parser = argparse.ArgumentParser(description="Generate wiki source coverage report.")
    parser.add_argument("version", nargs="?", help="Index version key from config/versions.toml")
    args = parser.parse_args()
    if not args.version:
        parser.error(f"version is required. Available: {', '.join(available)}")

    sub_cfg = config["postgresql"]["subsystems"]
    rules: list[dict] = sub_cfg["rules"]
    order: list[str] = sub_cfg["order"]

    files = backend_c_files(args.version)
    symbol_counts = symbol_counts_per_file(args.version)
    covered = wiki_source_refs()
    stats = compute_stats(files, covered, symbol_counts, rules, order)
    report = render(args.version, files, covered, symbol_counts, rules, stats)

    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    out = COVERAGE_DIR / f"coverage_{timestamp}.md"
    out.write_text(report)

    total_covered = sum(1 for f in files if f in covered)
    print(f"Written {out}")
    print(f"Overall: {total_covered}/{len(files)} = {total_covered * 100 // len(files)}%")
    for s in sorted(stats, key=lambda s: s.pct):
        bar = "█" * (s.pct // 10) + "░" * (10 - s.pct // 10)
        print(f"  {s.name:20} {bar} {s.pct:3}%  ({s.covered}/{s.total})")


if __name__ == "__main__":
    main()
