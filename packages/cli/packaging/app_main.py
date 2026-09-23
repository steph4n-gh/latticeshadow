"""The macOS app entry point, including explicit headless subcommands.

The installed app uses one bundled Python runtime for its menu, CLI, and daemon.
``--cli`` and ``--daemon`` are internal entry points for the launchers in the
app archive; they are not a replacement for the public ``shadow`` interface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    model_dir = Path(sys.executable).resolve().parents[1] / "Resources" / "model"
    if model_dir.is_dir():
        os.environ.setdefault("LATTICESHADOW_BUNDLED_MODEL", str(model_dir))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from latticeshadow.shadow_cli import main as cli_main

        sys.argv = ["shadow", *sys.argv[2:]]
        cli_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        from latticeshadow.shadowd import run_daemon

        run_daemon()
    elif len(sys.argv) > 1 and sys.argv[1] == "--bundle-check":
        from importlib import resources

        import AppKit  # noqa: F401
        import objc  # noqa: F401
        import torch  # noqa: F401
        from sentence_transformers import SentenceTransformer
        import latticeshadow_db  # noqa: F401

        shader = resources.files("latticeshadow").joinpath("Shaders.metal")
        if not shader.is_file():
            raise RuntimeError("Metal shader is missing from the app bundle")
        from latticeshadow import homomorphic  # noqa: F401

        if not model_dir.is_dir():
            raise RuntimeError("Pinned embedding model is missing from the app bundle")
        model = SentenceTransformer(str(model_dir), truncate_dim=128, device="cpu")
        if len(model.encode("synthetic deployment note")) != 128:
            raise RuntimeError("Bundled embedding model returned the wrong shape")
        print("bundle imports and resources: OK")
    else:
        from latticeshadow.menu import run_menu_app

        run_menu_app()


if __name__ == "__main__":
    main()
