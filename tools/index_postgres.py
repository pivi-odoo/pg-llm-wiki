#!/usr/bin/env python3
"""Index a PostgreSQL source tree and write source-index artifacts."""

import argparse
import csv
import json
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "versions.toml"
UPSTREAM_GIT = REPO_ROOT / "sources" / "postgresql" / "upstream.git"

INCLUDED_SUFFIXES = {".c", ".h", ".y", ".l", ".sql", ".sgml", ".md", ".txt"}


def load_config() -> dict:
    """Read and return the parsed contents of config/versions.toml."""
    if not CONFIG_FILE.exists():
        sys.exit(f"Config not found: {CONFIG_FILE}")
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def get_version_config(config: dict, version: str) -> dict:
    """Return the config block for a version key (e.g. PG_16_14).

    Exits with a clear message listing available versions if the key is absent.
    """
    try:
        return config["postgresql"][version]
    except KeyError:
        available = list(config.get("postgresql", {}).keys())
        sys.exit(f"Version {version!r} not in config. Available: {available}")


def _resolve_gitdir(worktree: Path) -> Path | None:
    """Return the resolved gitdir path for a linked worktree, or None.

    A linked worktree has a .git file (not directory) whose content is
    "gitdir: <absolute-path>". That path may be host-absolute and therefore
    unreachable inside Docker; if so we remap it using UPSTREAM_GIT.
    """
    git_file = worktree / ".git"
    if not git_file.is_file():
        return None
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content[len("gitdir:") :].strip())
    if gitdir.exists():
        return gitdir

    marker = UPSTREAM_GIT.name + "/"
    gitdir_str = str(gitdir)
    if marker in gitdir_str:
        suffix = gitdir_str.split(marker, 1)[1]
        remapped = UPSTREAM_GIT / suffix
        if remapped.exists():
            return remapped
    return None


def get_commit_hash(worktree: Path) -> str:
    """Return the HEAD commit SHA for a worktree.

    For linked worktrees, reads the SHA directly from the gitdir HEAD file.
    Falls back to running `git rev-parse HEAD` if the gitdir cannot be resolved.
    """
    gitdir = _resolve_gitdir(worktree)
    if gitdir is not None:
        head_file = gitdir / "HEAD"
        if head_file.exists():
            return head_file.read_text().strip()

    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to get git commit for {worktree}:\n{result.stderr.strip()}")
    return result.stdout.strip()


def classify_file(rel_path: str, rules: list[dict]) -> str:
    """Return the subsystem name for a source file path.

    Rules are tested in order; the first prefix match wins. Returns "other"
    if no rule matches.
    """
    for rule in rules:
        if rel_path.startswith(rule["prefix"]):
            return rule["subsystem"]
    return "other"


def collect_files(worktree: Path) -> list[dict]:
    """Walk the worktree and return metadata for all files with included suffixes.

    Each entry is a dict with keys: path (relative POSIX), suffix, size_bytes,
    line_count.
    """
    rows = []
    for path in sorted(worktree.rglob("*")):
        if not path.is_file() or path.suffix not in INCLUDED_SUFFIXES:
            continue
        rel = path.relative_to(worktree).as_posix()
        size = path.stat().st_size
        try:
            line_count = path.read_text(errors="replace").count("\n")
        except OSError:
            line_count = -1
        rows.append(
            {"path": rel, "suffix": path.suffix, "size_bytes": size, "line_count": line_count}
        )
    return rows


def write_manifest(
    out_dir: Path, version: str, version_cfg: dict, commit: str, worktree: Path
) -> None:
    """Write manifest.json describing this index run."""
    manifest = {
        "version": version,
        "git_ref": version_cfg.get("git_ref", ""),
        "source_root": str(worktree),
        "commit": commit,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def write_files_csv(out_dir: Path, files: list[dict]) -> None:
    """Write files.csv with one row per indexed source file."""
    with (out_dir / "files.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["path", "suffix", "size_bytes", "line_count"])
        writer.writeheader()
        writer.writerows(files)


def write_directories_md(out_dir: Path, worktree: Path) -> None:
    """Write directories.md: a human-readable listing of top-level and src/ dirs."""
    lines = ["# Directory Overview", ""]

    lines += ["## /", ""]
    for d in sorted(worktree.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            lines.append(f"- `{d.name}/`")

    src = worktree / "src"
    if src.exists():
        lines += ["", "## /src", ""]
        for d in sorted(src.iterdir()):
            if d.is_dir():
                lines.append(f"- `src/{d.name}/`")

    backend = worktree / "src" / "backend"
    if backend.exists():
        lines += ["", "## /src/backend", ""]
        for d in sorted(backend.iterdir()):
            if d.is_dir():
                lines.append(f"- `src/backend/{d.name}/`")

    (out_dir / "directories.md").write_text("\n".join(lines) + "\n")


def write_symbols_csv(out_dir: Path, worktree: Path, rules: list[dict]) -> None:
    """Run ctags over the worktree and write symbols.csv.

    Extracts functions, macros, typedefs, structs, and unions from C source.
    Skips silently if ctags is not available or fails.
    """
    try:
        result = subprocess.run(
            [
                "ctags",
                "--output-format=json",
                "--fields=+n",
                "--languages=C",
                "--map-C=+.h",
                "--kinds-C=dftsugmvexp",
                "-f", "-",
                "-R", str(worktree),
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("Warning: ctags not found — skipping symbols.csv")
        return

    if result.returncode != 0:
        print(f"Warning: ctags exited {result.returncode} — skipping symbols.csv")
        return

    rows = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag":
            continue
        abs_path = tag.get("path", "")
        try:
            rel_path = Path(abs_path).relative_to(worktree).as_posix()
        except ValueError:
            continue
        rows.append({
            "name": tag.get("name", ""),
            "kind": tag.get("kind", ""),
            "path": rel_path,
            "line": tag.get("line", ""),
            "subsystem": classify_file(rel_path, rules),
        })

    with (out_dir / "symbols.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "kind", "path", "line", "subsystem"])
        writer.writeheader()
        writer.writerows(rows)


def write_subsystems_md(out_dir: Path, files: list[dict], subsystems_cfg: dict) -> None:
    """Write subsystems.md: files grouped by subsystem using rules from config."""
    rules: list[dict] = subsystems_cfg.get("rules", [])
    order: list[str] = subsystems_cfg.get("order", [])

    groups: dict[str, list[str]] = {}
    for f in files:
        subsystem = classify_file(f["path"], rules)
        groups.setdefault(subsystem, []).append(f["path"])

    lines = ["# Subsystem Overview", ""]
    for subsystem in order:
        if subsystem not in groups:
            continue
        file_list = groups[subsystem]
        lines += [f"## {subsystem} ({len(file_list)} files)", ""]
        for path in sorted(file_list):
            lines.append(f"- `{path}`")
        lines.append("")

    (out_dir / "subsystems.md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a PostgreSQL source tree.")
    parser.add_argument("version", help="Version key from config/versions.toml, e.g. PG_16_14")
    args = parser.parse_args()

    config = load_config()
    version_cfg = get_version_config(config, args.version)
    subsystems_cfg = config.get("postgresql", {}).get("subsystems", {})

    worktree = REPO_ROOT / version_cfg["worktree"]
    if not worktree.exists():
        sys.exit(f"Worktree does not exist: {worktree}\nAdd a worktree before indexing.")

    commit = get_commit_hash(worktree)

    out_dir = REPO_ROOT / version_cfg["index"]
    out_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files(worktree)

    rules: list[dict] = subsystems_cfg.get("rules", [])

    write_manifest(out_dir, args.version, version_cfg, commit, worktree)
    write_files_csv(out_dir, files)
    write_directories_md(out_dir, worktree)
    write_subsystems_md(out_dir, files, subsystems_cfg)
    write_symbols_csv(out_dir, worktree, rules)

    print(f"Indexed {len(files)} files → {out_dir}")


if __name__ == "__main__":
    main()
