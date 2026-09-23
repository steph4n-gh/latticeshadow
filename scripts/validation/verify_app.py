"""Check a py2app candidate before archiving it for guest validation.

This is a build-host smoke check. The separate guest profile is still required
for an install claim because this host has development tools installed.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args: str, timeout: int = 180, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True,
                            timeout=timeout, env=env)
    return result.stdout


def verify(path: Path) -> dict[str, object]:
    path = path.resolve()
    contents = path / "Contents"
    executable = contents / "MacOS" / "LatticeShadow"
    model = contents / "Resources" / "model"
    weight = model / "0_StaticEmbedding" / "model.safetensors"
    library = contents / "Resources" / "lib" / "python3.12" / "latticeshadow" / "liblwe.dylib"
    shader = library.parent / "Shaders.metal"
    cli = contents / "Resources" / "bin" / "shadow"
    for required in (
        executable, weight, library, shader, cli,
        model / "LATTICESHADOW_MODEL_SOURCE.txt",
        contents / "Resources" / "licenses" / "Apache-2.0.txt",
    ):
        if not required.is_file():
            raise RuntimeError(f"Bundle is missing {required.relative_to(path)}")
    if weight.stat().st_size < 1_000_000:
        raise RuntimeError("Model weights are incomplete")
    with (contents / "Info.plist").open("rb") as file:
        info = plistlib.load(file)
    if info.get("CFBundleIdentifier") != "io.github.steph4n-gh.latticeshadow":
        raise RuntimeError("Unexpected bundle identifier")
    if "arm64" not in run("/usr/bin/lipo", "-archs", str(executable)).split():
        raise RuntimeError("The app launcher is not arm64")
    if "arm64" not in run("/usr/bin/lipo", "-archs", str(library)).split():
        raise RuntimeError("The native library is not arm64")
    binaries = [executable, *(
        item for item in contents.rglob("*")
        if item.is_file() and item.suffix in {".so", ".dylib"}
    )]
    for binary in binaries:
        for line in run("/usr/bin/otool", "-L", str(binary)).splitlines():
            if not line.startswith(("\t", "    ")):
                continue  # Universal binaries print a heading for each architecture.
            linked = line.strip().split(" (", 1)[0]
            if linked.startswith(("/Users/", "/opt/homebrew/", "/usr/local/")):
                raise RuntimeError(f"External build-machine library: {binary}: {linked}")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(path))

    with tempfile.TemporaryDirectory(prefix="latticeshadow-bundle-") as home:
        env = {
            "HOME": home,
            "PATH": "/usr/bin:/bin",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        smoke = run(str(executable), "--bundle-check", env=env)
        if "bundle imports and resources: OK" not in smoke:
            raise RuntimeError("Bundle smoke check did not finish")
        help_text = run(str(executable), "--cli", "--help", env=env)
        if "remember" not in help_text or "timeline" not in help_text:
            raise RuntimeError("Bundled CLI entry point is unavailable")
        wrapper_help = run(str(cli), "--help", env=env)
        if "remember" not in wrapper_help:
            raise RuntimeError("Bundled shadow launcher is unavailable")

    size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    result = {
        "bundle": str(path),
        "bundle_identifier": info["CFBundleIdentifier"],
        "version": info["CFBundleShortVersionString"],
        "launcher_architecture": "arm64",
        "model_revision": "f60985c706f192d45d218078e49e5a8b6f15283a",
        "model_weight_sha256": hashlib.sha256(weight.read_bytes()).hexdigest(),
        "file_bytes": size,
        "bundle_smoke": "pass",
        "linked_binaries_checked": len(binaries),
    }
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: verify_app.py <LatticeShadow.app>")
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))
