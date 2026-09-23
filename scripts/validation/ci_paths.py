"""Select expensive macOS CI steps from a NUL-delimited Git path list.

Unknown paths select every check. A release candidate always selects every
check. This script is intentionally small enough to audit alongside ci.yml.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Checks:
    macos: bool = False
    model: bool = False
    swift: bool = False
    bundle: bool = False

    def merge(self, other: "Checks") -> "Checks":
        return Checks(
            self.macos or other.macos,
            self.model or other.model,
            self.swift or other.swift,
            self.bundle or other.bundle,
        )


ALL = Checks(True, True, True, True)
NONE = Checks()
MAC = Checks(macos=True)
MODEL = Checks(macos=True, model=True)
SWIFT = Checks(macos=True, swift=True)
BUNDLE = Checks(macos=True, bundle=True)


def classify(path: str) -> Checks:
    if not path or path.startswith("/") or ".." in path.split("/"):
        return ALL
    if path.endswith(".md") or path.startswith("docs/") and path.endswith(
        (".png", ".svg", ".webp", ".jpg", ".html", ".css")
    ):
        return NONE
    if path == "scripts/render_user_manual.sh":
        return NONE
    if path in {"LICENSE", "SECURITY.md", "CONTRIBUTING.md", ".gitignore"}:
        return NONE
    if path.startswith("packages/db/tests/"):
        return NONE  # These run in the Linux database job.
    if path.startswith("packages/db/scripts/"):
        return NONE
    if path.startswith("packages/db/"):
        return MODEL
    if path.startswith("packages/cli/native/"):
        return SWIFT
    if path.startswith("packages/cli/packaging/"):
        return BUNDLE
    if path == "packages/cli/scripts/verify_docs.py":
        return NONE
    if path == "packages/cli/scripts/evaluate_recall.py":
        return MODEL
    if path in {"packages/cli/scripts/validate_lifecycle.py",
                "packages/cli/scripts/validate_mcp_client.py"}:
        return MAC
    if path.startswith("packages/cli/evaluation/") or path.startswith("docs/validation/"):
        return NONE
    if path.startswith("packages/cli/tests/"):
        if path.endswith("test_real_model.py"):
            return MODEL
        return MAC
    if path.startswith("packages/cli/latticeshadow/"):
        if path.rsplit("/", 1)[-1] in {"vaults.py", "timeline.py", "retrieval.py", "rebuild.py"}:
            return MODEL
        if path.rsplit("/", 1)[-1] in {"menu.py", "shadowd.py", "security.py"}:
            return BUNDLE
        return MAC
    if path in {"packages/cli/pyproject.toml", "requirements-dev.txt", "Makefile"}:
        return ALL
    if path.startswith("scripts/validation/"):
        return MAC
    return ALL


def select(paths: list[str], *, candidate: bool = False) -> Checks:
    if candidate or not paths:
        return ALL
    result = NONE
    for path in paths:
        result = result.merge(classify(path))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read()
    if raw and not raw.endswith(b"\0"):
        raise SystemExit("Expected NUL-delimited paths from git diff -z")
    try:
        paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise SystemExit("Git path is not valid UTF-8") from exc
    checks = select(paths, candidate=args.candidate)
    for name in ("macos", "model", "swift", "bundle"):
        print(f"run_{name}={str(getattr(checks, name)).lower()}")


if __name__ == "__main__":
    main()
