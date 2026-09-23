"""Build the Apple Silicon LatticeShadow application with py2app.

Invoke from the repository root with the Python 3.12 build environment::

    python packages/cli/packaging/setup_app.py py2app

The build environment must have both repository packages and py2app installed.
The app is intentionally unsigned; signing is a separate release operation.
"""

from __future__ import annotations

import sys
import shutil
import subprocess
import tomllib
from pathlib import Path

from setuptools import setup


ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "packages" / "cli"
APP_VERSION = tomllib.loads((CLI / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
MODEL_ID = "sentence-transformers/static-retrieval-mrl-en-v1"
MODEL_REVISION = "f60985c706f192d45d218078e49e5a8b6f15283a"

# modulegraph walks deeply nested conditional imports in the ML stack.
sys.setrecursionlimit(10000)
# Editable installs use import hooks that modulegraph does not follow.
sys.path[:0] = [str(ROOT / "packages" / "db"), str(CLI)]


def prepare_native_library() -> None:
    source = CLI / "latticeshadow" / "lwe.cpp"
    output = CLI / "latticeshadow" / "liblwe.dylib"
    subprocess.run(
        [
            "xcrun", "--sdk", "macosx", "clang++", "-std=c++17", "-O2",
            "-arch", "arm64", "-mmacosx-version-min=11.0", "-dynamiclib",
            str(source), "-o", str(output),
        ],
        check=True,
    )


def prepare_model() -> Path:
    from huggingface_hub import snapshot_download

    model_files = (
        "0_StaticEmbedding/model.safetensors",
        "0_StaticEmbedding/tokenizer.json",
        "config_sentence_transformers.json",
        "modules.json",
        "README.md",
    )
    snapshot = Path(snapshot_download(
        MODEL_ID, revision=MODEL_REVISION, allow_patterns=list(model_files)
    ))
    if snapshot.name != MODEL_REVISION:
        raise RuntimeError(f"Expected model revision {MODEL_REVISION}, got {snapshot.name}")
    destination = ROOT / "build" / "packaging" / "model"
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Hugging Face snapshots use symlinks into a machine-local blob cache.
    # Resolve only the files required by the PyTorch model. The repository also
    # contains optional ONNX variants that would add hundreds of megabytes.
    for relative in model_files:
        source = snapshot / relative
        if not source.is_file():
            raise RuntimeError(f"Pinned model file is missing: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    expected = destination / "0_StaticEmbedding" / "model.safetensors"
    if not expected.is_file() or expected.stat().st_size < 1_000_000:
        raise RuntimeError("Pinned model weights are missing or incomplete")
    (destination / "LATTICESHADOW_MODEL_SOURCE.txt").write_text(
        f"Source: https://huggingface.co/{MODEL_ID}\n"
        f"Revision: {MODEL_REVISION}\n"
        "License declared by model card: Apache-2.0\n"
        "License: https://www.apache.org/licenses/LICENSE-2.0\n",
        encoding="utf-8",
    )
    return destination


prepare_native_library()
MODEL_DIR = prepare_model()

setup(
    app=[str(CLI / "packaging" / "app_main.py")],
    name="LatticeShadow",
    version=APP_VERSION,
    options={
        "py2app": {
            "argv_emulation": False,
            "iconfile": None,
            "packages": ["latticeshadow", "latticeshadow_db", "torch", "sentence_transformers"],
            "includes": ["AppKit", "Quartz", "Metal", "MetalKit", "Vision", "ServiceManagement"],
            "resources": [
                str(MODEL_DIR),
                ("bin", [str(CLI / "packaging" / "shadow")]),
                ("licenses", [
                    str(ROOT / "LICENSE"),
                    str(CLI / "packaging" / "licenses" / "Apache-2.0.txt"),
                ]),
            ],
            "plist": {
                "CFBundleIdentifier": "io.github.steph4n-gh.latticeshadow",
                "CFBundleName": "LatticeShadow",
                "CFBundleDisplayName": "LatticeShadow",
                "CFBundleShortVersionString": APP_VERSION,
                "CFBundleVersion": "1",
                "LSUIElement": True,
                "NSAppleEventsUsageDescription": "Open a source item you choose from your memories.",
            },
        }
    },
    setup_requires=["py2app"],
)
