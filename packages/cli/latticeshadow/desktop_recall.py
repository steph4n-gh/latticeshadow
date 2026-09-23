"""Bounded, generation-checked recall work for the AppKit panel.

This module deliberately has no AppKit dependency so the UI state can be tested
without a window server. The dispatcher must run callbacks on the UI thread.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from latticeshadow.timeline import fetch_events, search_events


class DesktopRecall:
    def __init__(self, vault_factory: Callable[[], Any],
                 dispatcher: Callable[..., None],
                 on_result: Callable[[int, list[dict[str, Any]], str | None], None]):
        self._vault_factory = vault_factory
        self._dispatcher = dispatcher
        self._on_result = on_result
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._generation = 0
        self._pending: tuple[int, str, dict[str, Any]] | None = None
        self._closed = False
        self._worker: threading.Thread | None = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def invalidate(self) -> int:
        with self._lock:
            self._generation += 1
            self._pending = None
            return self._generation

    def request(self, query: str, scope: dict[str, Any]) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("Recall panel is closed")
            self._generation += 1
            generation = self._generation
            self._pending = (generation, query.strip(), dict(scope))
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="latticeshadow-recall", daemon=True)
                self._worker.start()
            self._wake.set()
            return generation

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            self._pending = None
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if self._closed:
                    return
                item = self._pending
                self._pending = None
                if item is None:
                    self._wake.clear()
                    continue
            generation, query, scope = item
            events: list[dict[str, Any]] = []
            error: str | None = None
            try:
                vault = self._vault_factory()
                if vault is None:
                    raise RuntimeError("Vault unavailable")
                if query:
                    events = search_events(vault, query, scope=scope, limit=20)
                else:
                    events = fetch_events(vault, scope=scope, limit=20)["events"]
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            self._dispatcher(self._deliver, generation, events, error)
            with self._lock:
                if self._pending is None:
                    self._wake.clear()

    def _deliver(self, generation: int, events: list[dict[str, Any]],
                 error: str | None) -> None:
        with self._lock:
            if self._closed or generation != self._generation:
                return
        self._on_result(generation, events, error)
