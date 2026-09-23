"""Desktop recall logic; window-server interaction is validated in a guest."""

from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from latticeshadow.desktop_recall import DesktopRecall
from latticeshadow.menu import (
    ShadowMenuApp, _desktop_vault, _open_kind, _preview, _row_label, _scope,
    _shortcut_matches, setup_spotlight,
)


@pytest.fixture(autouse=True)
def no_personal_shortcut_config(monkeypatch):
    monkeypatch.setattr("latticeshadow.menu.config.get", lambda key: None)


def event(doc_id="note_1", text="remember this"):
    return {"id": doc_id, "text": text, "timestamp": "2026-09-17T14:03:00Z",
            "captured_at": "2026-09-17T14:03:02Z", "project": "ops",
            "source": "manual", "metadata": {}}


def test_scope_uses_exact_project_source_unassigned_and_local_day():
    now = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    assert _scope("ops", False, "manual", 0) == {
        "projects": ("ops",), "sources": ("manual",)}
    assert _scope("ops", True, "", 1, now=now) == {
        "projects": (None,), "since": "2026-09-22T00:00:00Z"}
    assert _scope("", False, "", 0) == {}
    with pytest.raises(KeyError):
        _scope("", False, "", 99)


def test_provenance_visible_without_score_as_confidence():
    item = event()
    item["score"] = 0.99
    assert "ops" in _row_label(item)
    assert "manual" in _row_label(item)
    assert "Reference: latticeshadow://event/note_1" in _preview(item)
    assert "0.99" not in _preview(item)
    item["metadata"]["url"] = "https://example.org/incident"
    assert "Target: https://example.org/incident" in _preview(item)
    item["text"] = "x" * 21_000
    assert "preview shortened" in _preview(item)


@pytest.mark.parametrize("target, expected", [
    ("https://example.org/post", ("web", "https://example.org/post")),
    ("file:///tmp/a%20b", ("file", "/tmp/a b")),
    ("/tmp/a b", ("file", "/tmp/a b")),
    ("javascript:alert(1)", None),
    ("ssh://example.org/path", None),
    ("relative/path", None),
    ("https://example.org/\nmalicious", None),
    ("https://[broken", None),
])
def test_only_web_and_local_file_open_targets(target, expected):
    assert _open_kind(target) == expected


def test_shortcut_modes_and_disabled_fallback():
    option = 1 << 19
    control = 1 << 18
    space = SimpleNamespace(keyCode=lambda: 49, modifierFlags=lambda: option | control)
    assert _shortcut_matches("control-option-space", space)
    assert not _shortcut_matches("option-space", space)
    assert not _shortcut_matches("off", space)


def test_recall_uses_recent_and_scoped_search_and_discards_stale_result():
    started = Event()
    finish = Event()
    received = []
    dispatched = []

    def search(vault, query, *, scope, limit):
        assert scope == {"projects": ("ops",)}
        started.set()
        finish.wait(2)
        return [event("old")]

    def dispatch(fn, *args):
        dispatched.append((fn, args))

    recall = DesktopRecall(lambda: object(), dispatch,
                           lambda generation, rows, error: received.append((generation, rows, error)))
    with patch("latticeshadow.desktop_recall.search_events", side_effect=search), \
         patch("latticeshadow.desktop_recall.fetch_events", return_value={"events": [event()], "next_cursor": None}) as fetch:
        recall.request("deploy", {"projects": ("ops",)})
        assert started.wait(2)
        recall.request("", {"sources": ("manual",)})
        finish.set()
        for _ in range(100):
            if len(dispatched) == 2:
                break
            Event().wait(0.01)
        assert len(dispatched) == 2
        for fn, args in dispatched:
            fn(*args)
        assert len(received) == 1
        assert received[0][1][0]["id"] == "note_1"
        fetch.assert_called_once()
        assert fetch.call_args.kwargs == {"scope": {"sources": ("manual",)}, "limit": 20}
    recall.close()


def test_recall_exposes_storage_errors_without_returning_old_rows():
    delivered = Event()
    results = []

    def fail():
        raise OSError("database unavailable")

    def receive(generation, rows, error):
        results.append((rows, error))
        delivered.set()

    recall = DesktopRecall(fail, lambda fn, *args: fn(*args), receive)
    recall.request("", {})
    assert delivered.wait(2)
    assert results == [([], "OSError: database unavailable")]
    recall.close()


def test_cli_fatal_vault_exit_becomes_desktop_error():
    with patch("latticeshadow.menu.get_vault", side_effect=SystemExit(1)):
        with pytest.raises(RuntimeError, match="Vault unavailable"):
            _desktop_vault()


def test_menu_selection_actions_bind_to_live_id_and_scope():
    app = ShadowMenuApp.alloc().init()
    app.table = MagicMock()
    app.table.selectedRow.return_value = 0
    app._displayed = [event()]
    app.project_field = MagicMock()
    app.project_field.stringValue.return_value = "ops"
    app.source_field = MagicMock()
    app.source_field.stringValue.return_value = ""
    app.unassigned = MagicMock()
    app.unassigned.state.return_value = 0
    app.period = MagicMock()
    app.period.indexOfSelectedItem.return_value = 0
    app.result_field = MagicMock()
    app._request_search = MagicMock()
    app._message = MagicMock()

    with patch("latticeshadow.menu.get_vault", return_value=object()), \
         patch("latticeshadow.menu.get_events", return_value=[]) as get, \
         patch("latticeshadow.menu.AppKit.NSPasteboard") as board:
        app.copySelected_(None)
        get.assert_called_once()
        assert get.call_args.args[1] == ["note_1"]
        assert get.call_args.kwargs["scope"] == {"projects": ("ops",)}
        board.generalPasteboard.assert_not_called()
        app._request_search.assert_called_once()

    app._request_search.reset_mock()
    with patch("latticeshadow.menu.get_vault", return_value=object()), \
         patch("latticeshadow.menu.get_events", return_value=[event()]), \
         patch("latticeshadow.menu.AppKit.NSPasteboard") as board:
        app.copySelected_(None)
        board.generalPasteboard.return_value.setString_forType_.assert_called_once()
        assert board.generalPasteboard.return_value.setString_forType_.call_args.args[0] == "remember this"
        app._request_search.assert_not_called()


def test_open_selected_uses_workspace_only_for_supported_targets():
    app = ShadowMenuApp.alloc().init()
    item = event()
    item["metadata"]["url"] = "javascript:alert(1)"
    app._live_selected = MagicMock(return_value=item)
    app._message = MagicMock()
    with patch("latticeshadow.menu.AppKit.NSWorkspace") as workspace:
        app.openSelected_(None)
        workspace.sharedWorkspace.assert_not_called()
    app._message.assert_called_once()

    item["metadata"]["url"] = "https://example.org/incident"
    app._message.reset_mock()
    with patch("latticeshadow.menu.AppKit.NSWorkspace") as workspace, \
         patch("latticeshadow.menu.AppKit.NSURL") as url:
        app.openSelected_(None)
        url.URLWithString_.assert_called_once_with("https://example.org/incident")
        workspace.sharedWorkspace.return_value.openURL_.assert_called_once()
    app._message.assert_not_called()


def test_forget_invalidates_results_before_followup_search():
    app = ShadowMenuApp.alloc().init()
    app._live_selected = MagicMock(return_value=event())
    app.recall = MagicMock()
    app._displayed = [event()]
    app.data_source = MagicMock()
    app.table = MagicMock()
    app.preview = MagicMock()
    app._request_search = MagicMock()
    app._message = MagicMock()
    with patch("latticeshadow.menu.AppKit.NSAlert") as alert, \
         patch("latticeshadow.menu.get_vault", return_value=object()), \
         patch("latticeshadow.menu.forget_events", return_value={"canonical_deleted": 1,
                 "derived_invalidated": True, "cleanup_errors": []}) as forget:
        from latticeshadow.menu import AppKit as menu_appkit
        alert.alloc.return_value.init.return_value.runModal.return_value = menu_appkit.NSAlertFirstButtonReturn
        app.forgetSelected_(None)
    assert app._displayed == []
    assert app.recall.invalidate.called
    forget.assert_called_once()
    assert forget.call_args.args[1] == ["note_1"]
    app._request_search.assert_called_once()


def test_capture_state_labels_are_truthful():
    app = ShadowMenuApp.alloc().init()
    app.capture_item = MagicMock()
    app.status_field = MagicMock()
    app.pause_item = MagicMock()
    app.panel_pause = MagicMock()
    app._show_status({"state": "stopped", "paused": False, "sources": {},
                      "consent_needed": [], "error": None})
    app.capture_item.setTitle_.assert_called_once_with("Capture stopped")
    app._show_status({"state": "paused", "paused": True, "sources": {},
                      "consent_needed": [], "error": None})
    app.pause_item.setTitle_.assert_called_with("Resume capture")


def test_pause_control_uses_persistent_consent_state():
    app = ShadowMenuApp.alloc().init()
    app._paused = False
    app.refreshStatus = MagicMock()
    app._message = MagicMock()
    with patch("latticeshadow.menu.consent.set_paused") as pause:
        app.pauseResume_(None)
    pause.assert_called_once_with(True)
    app.refreshStatus.assert_called_once()


def test_native_panel_keeps_search_results_preview_and_actions_accessible():
    from latticeshadow.menu import AppKit

    class Delegate(AppKit.NSObject):
        pass

    AppKit.NSApplicationLoad()
    delegate = Delegate.alloc().init()
    panel = setup_spotlight(delegate)
    try:
        assert panel.appearance().name() == AppKit.NSAppearanceNameDarkAqua
        assert delegate.search_field.accessibilityLabel() == "Search local memories"
        assert delegate.table.accessibilityLabel() == "Recall results"
        assert delegate.preview.accessibilityLabel() == "Selected event preview and provenance"
        results = delegate.table.enclosingScrollView().frame()
        preview = delegate.preview.enclosingScrollView().frame()
        assert results.origin.x + results.size.width < preview.origin.x
        titles = {view.title() for view in panel.contentView().subviews()
                  if isinstance(view, AppKit.NSButton)}
        assert {"Copy", "Open link/file", "Assign project", "Forget…"} <= titles
        text = {view.stringValue() for view in panel.contentView().subviews()
                if isinstance(view, AppKit.NSTextField) and not view.isEditable()}
        assert "Small things stay with you." in text
        assert "Return copies · Escape closes · Shortcut can be changed in the menu" in text
    finally:
        panel.close()


def test_panel_result_count_uses_readable_singular_labels():
    app = ShadowMenuApp.alloc().init()
    app.panel = MagicMock()
    app.panel.isVisible.return_value = True
    app.data_source = MagicMock()
    app.table = MagicMock()
    app._show_selection = MagicMock()
    app.result_field = MagicMock()
    app.search_field = MagicMock()
    app.search_field.stringValue.return_value = "widget"
    app._show_results(1, [event()], None)
    app.result_field.setStringValue_.assert_called_with("1 result")
    app.search_field.stringValue.return_value = ""
    app._show_results(2, [event()], None)
    app.result_field.setStringValue_.assert_called_with("1 recent event")
