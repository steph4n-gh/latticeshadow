"""Exercise real Tab completion in an isolated interactive shell."""

import os
import pty
import select
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("source_count", [1, 2])
def test_tab_completes_after_loading_plugin(tmp_path, source_count):
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh is required for the shell integration test")
    plugin = Path(__file__).resolve().parents[1] / "latticeshadow" / "latticeshadow.zsh"
    target = "latticeshadow-completion-target"
    (tmp_path / target).touch()
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [zsh, "-dfi"], stdin=slave, stdout=slave, stderr=slave,
        cwd=tmp_path, start_new_session=True,
    )
    os.close(slave)
    output = bytearray()

    def read_until(marker):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if marker in output:
                return
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
        pytest.fail(f"Shell did not produce {marker!r}: {output.decode(errors='replace')}")

    try:
        # An empty history file avoids reading the user's shell history.
        setup = "HISTFILE=/dev/null; PS1='LS-READY> '; "
        setup += "tab_before=$(bindkey '^I'); ghost_before=$(bindkey '^G'); "
        setup += "; ".join(f"source {shlex.quote(str(plugin))}" for _ in range(source_count))
        setup += "; if [[ \"$tab_before\" == \"$(bindkey '^I')\" && \"$ghost_before\" == \"$(bindkey '^G')\" ]]; then printf '%s%s\\n' BINDINGS -OK; else printf '%s%s\\n' BINDINGS -BAD; fi"
        setup += "; printf '\\n%s%s\\n' LS- SETUP-DONE\n"
        os.write(master, setup.encode())
        read_until(b"LS-SETUP-DONE")
        assert b"BINDINGS-OK" in output
        output.clear()
        os.write(master, b"printf '\\n%s%s\\n' COMPLETED: ./latticeshadow-comp\t\n")
        read_until(b"\r\nCOMPLETED:./latticeshadow-completion-target\r\n")
        assert b"recursion limit exceeded" not in output
        assert b"job table full" not in output
    finally:
        proc.kill()  # Interactive zsh deliberately ignores SIGTERM.
        proc.wait(timeout=5)
        os.close(master)
