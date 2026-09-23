"""
Terminal history watcher for LatticeShadow.

Reads new commands from ~/.zsh_history (or HISTFILE) and returns them.
Handles both standard and extended zsh history formats.
"""

import os
import re
import time

TRIVIAL_COMMANDS = {
    "ls", "cd", "pwd", "clear", "exit", "logout", "q",
    "fg", "bg", "jobs", "history", "which", "whoami", "shadow"
}

TRIVIAL_PREFIXES = ("cd ", "ls ", "echo ", "cat ", "shadow ")


class HistoryWatcher:
    """Watches a shell history file (e.g. ~/.zsh_history) for new commands."""

    def __init__(self, histfile: str = None):
        if not histfile:
            histfile = os.environ.get("HISTFILE")
            if not histfile:
                histfile = os.path.expanduser("~/.zsh_history")
        self.histfile = histfile
        self._offset = 0
        self._last_commands = []  # rolling window of last 20 commands
        self._initialized = False

    def _initialize_offset(self):
        """Seek to the end of the history file to avoid replaying old history."""
        self.skip_to_end()

    def skip_to_end(self):
        """Discard commands written before capture became active again."""
        try:
            with open(self.histfile, "rb") as history:
                self._offset = os.fstat(history.fileno()).st_size
        except FileNotFoundError:
            self._offset = 0
        self._initialized = True

    def poll(self) -> list:
        """
        Poll the history file for new entries since the last check.
        Returns a list of dicts: [{"text": str, "timestamp": float, "source": "terminal"}]
        """
        if not self._initialized:
            self._initialize_offset()

        if not os.path.exists(self.histfile):
            return []

        try:
            current_size = os.path.getsize(self.histfile)
        except Exception:
            return []

        # If file was truncated or rotated, reset offset to start
        if current_size < self._offset:
            self._offset = 0

        if current_size == self._offset:
            return []

        new_entries = []
        try:
            with open(self.histfile, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                content = f.read()
                self._offset = f.tell()

            # Parse lines (handling zsh multiline commands with backslash, though simple line splitting is fine for now)
            lines = content.splitlines()
            for line in lines:
                if not line.strip():
                    continue
                cmd_text, ts = self._parse_line(line)
                if not cmd_text:
                    continue

                # Skip trivial commands
                if self._is_trivial(cmd_text):
                    continue

                # Deduplicate against rolling window
                if cmd_text in self._last_commands:
                    continue

                self._last_commands.append(cmd_text)
                if len(self._last_commands) > 20:
                    self._last_commands.pop(0)

                new_entries.append({
                    "text": cmd_text,
                    "timestamp": ts or time.time(),
                    "source": "terminal"
                })
        except Exception:
            pass

        return new_entries

    def _parse_line(self, line: str) -> tuple:
        """
        Parse zsh history line.
        Extended format: ': 1719500000:0;git status' -> ('git status', 1719500000.0)
        Plain format:    'git status' -> ('git status', None)
        """
        line = line.strip()
        # Regex for extended format: starts with : followed by timestamp, :0; (or other number)
        # e.g., : 1719500000:0;my_command
        m = re.match(r"^:\s*(\d+):\d+;(.*)$", line)
        if m:
            ts = float(m.group(1))
            cmd = m.group(2).strip()
            return cmd, ts
        return line, None

    def _is_trivial(self, cmd: str) -> bool:
        """Check if command is trivial or too short."""
        cmd = cmd.strip()
        if len(cmd) < 3:
            return True
        if cmd in TRIVIAL_COMMANDS:
            return True
        if cmd.startswith(TRIVIAL_PREFIXES):
            return True
        return False


TerminalHistoryWatcher = HistoryWatcher
