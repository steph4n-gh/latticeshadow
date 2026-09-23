"""AppKit menu and recall panel for the local LatticeShadow vault."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import AppKit
import objc
from PyObjCTools import AppHelper

from latticeshadow import config, consent
from latticeshadow.capture_state import get_status
from latticeshadow.desktop_recall import DesktopRecall
from latticeshadow.shadow_cli import get_vault
from latticeshadow.timeline import assign_project, forget_events, get_events, open_target


SHORTCUTS = ("off", "option-space", "control-option-space", "command-option-space")
SHORTCUT_LABELS = ("Off", "Option–Space", "Control–Option–Space", "Command–Option–Space")
MODIFIER_FLAGS = (
    getattr(AppKit, "NSEventModifierFlagOption", 1 << 19),
    getattr(AppKit, "NSEventModifierFlagControl", 1 << 18),
    getattr(AppKit, "NSEventModifierFlagCommand", 1 << 20),
    getattr(AppKit, "NSEventModifierFlagShift", 1 << 17),
)
KEY_MASK = getattr(AppKit, "NSEventMaskKeyDown", 1 << 10)


def _desktop_vault():
    """The CLI's fatal exits become visible panel errors."""
    try:
        return get_vault()
    except SystemExit as exc:
        detail = exc.code if isinstance(exc.code, str) else "Vault unavailable. Run shadow install."
        raise RuntimeError(detail) from exc


def _shortcut_matches(mode, event):
    if mode == "off" or event.keyCode() != 49:
        return False
    option, control, command, shift = MODIFIER_FLAGS
    wanted = {"option-space": option, "control-option-space": control | option,
              "command-option-space": command | option}.get(mode)
    return wanted is not None and event.modifierFlags() & (option | control | command | shift) == wanted


def _scope(project: str, unassigned: bool, source: str, period: int, now=None):
    """Build one exact scope for recent results, search and selected-ID actions."""
    result = {}
    if unassigned:
        result["projects"] = (None,)
    elif project.strip():
        result["projects"] = (project.strip(),)
    if source.strip():
        result["sources"] = (source.strip(),)
    if period:
        now = now or datetime.now().astimezone()
        if period == 1:
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            since = now - timedelta(days={2: 7, 3: 30}[period])
        result["since"] = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def _row_label(event):
    date = str(event.get("timestamp", ""))[:16].replace("T", " ")
    source = str(event.get("source") or "unknown")
    project = str(event.get("project") or "Unassigned")
    snippet = " ".join(str(event.get("text") or "").split())[:110]
    return f"{date}  ·  {source}  ·  {project}  ·  {snippet}"


def _preview(event):
    text = str(event.get("text") or "")
    if len(text) > 20_000:
        text = text[:20_000] + "\n… [preview shortened; Copy uses the complete event]"
    target = open_target(event)
    location = f"Target: {target}\n" if _open_kind(target) else ""
    return (f"{text}\n\n"
            f"Source: {event.get('source') or 'unknown'}\n"
            f"Project: {event.get('project') or 'Unassigned'}\n"
            f"Occurred: {event.get('timestamp') or 'unknown'}\n"
            f"Captured: {event.get('captured_at') or 'unknown'}\n"
            f"{location}"
            f"Reference: latticeshadow://event/{quote(str(event.get('id') or ''), safe='')}")


def _open_kind(target):
    """Return a web URL or local file path; never produce an executable command."""
    if not isinstance(target, str) or not target or any(ord(ch) < 32 for ch in target):
        return None
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme in ("https", "http") and parsed.netloc:
        return "web", target
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
        return "file", unquote(parsed.path)
    if not parsed.scheme and Path(target).is_absolute():
        return "file", target
    return None


def _color(red, green, blue):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        red / 255, green / 255, blue / 255, 1)


NAVY = _color(17, 31, 43)
SLATE = _color(28, 47, 61)
EDGE = _color(61, 84, 100)
INK = _color(232, 239, 243)
MUTED = _color(172, 190, 202)
AMBER = _color(239, 187, 111)


def _card(frame):
    return CardView.alloc().initWithFrame_(frame)


def _label(text, frame, *, color=INK, size=13, weight=AppKit.NSFontWeightRegular):
    field = AppKit.NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setTextColor_(color)
    field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
    return field


def _button(title, action, target, frame):
    button = AppKit.NSButton.alloc().initWithFrame_(frame)
    button.setTitle_(title)
    button.setTarget_(target)
    button.setAction_(action)
    button.setFont_(AppKit.NSFont.systemFontOfSize_(13))
    return button


class LatticeMark(AppKit.NSView):
    """A small, decorative mark; the actual controls remain native AppKit."""

    def drawRect_(self, dirty_rect):
        points = ((5, 6), (19, 6), (5, 20), (19, 20), (12, 34))
        AMBER.setStroke()
        for left, right in ((0, 1), (0, 2), (1, 3), (2, 3), (2, 4), (3, 4)):
            line = AppKit.NSBezierPath.bezierPath()
            line.moveToPoint_(points[left])
            line.lineToPoint_(points[right])
            line.setLineWidth_(1.2)
            line.stroke()
        AMBER.setFill()
        for x, y in points:
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                AppKit.NSMakeRect(x - 2.2, y - 2.2, 4.4, 4.4)).fill()


class CardView(AppKit.NSView):
    def drawRect_(self, dirty_rect):
        border = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), 12, 12)
        SLATE.setFill()
        border.fill()
        EDGE.setStroke()
        border.setLineWidth_(1)
        border.stroke()


class ResultsDataSource(AppKit.NSObject):
    def init(self):
        self = objc.super(ResultsDataSource, self).init()
        if self is not None:
            self.events = []
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self.events)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return _row_label(self.events[row]) if 0 <= row < len(self.events) else ""


class ResultsTable(AppKit.NSTableView):
    def keyDown_(self, event):
        if event.keyCode() in (36, 76):  # Return and keypad Enter
            self.target().copySelected_(self)
            return
        objc.super(ResultsTable, self).keyDown_(event)


class RecallPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


class ShadowMenuApp(AppKit.NSObject):
    def init(self):
        self = objc.super(ShadowMenuApp, self).init()
        if self is not None:
            self.recall = DesktopRecall(_desktop_vault, AppHelper.callAfter, self._show_results)
            self._shortcut_mode = config.get("ui.shortcut") or "option-space"
            if self._shortcut_mode not in SHORTCUTS:
                self._shortcut_mode = "off"
            self._status_loading = False
            self._search_generation = 0
            self._displayed = []
        return self

    def applicationDidFinishLaunching_(self, notification):
        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("⏣")
        menu = AppKit.NSMenu.alloc().init()
        menu.setDelegate_(self)
        self.capture_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Capture status: checking…", None, "")
        self.capture_item.setEnabled_(False)
        menu.addItem_(self.capture_item)
        self.pause_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Pause capture", "pauseResume:", "")
        self.pause_item.setTarget_(self)
        menu.addItem_(self.pause_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        recall = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open Recall…", "openRecall:", "r")
        recall.setTarget_(self)
        menu.addItem_(recall)
        shortcut_menu = AppKit.NSMenu.alloc().init()
        for index, label in enumerate(SHORTCUT_LABELS):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "chooseShortcut:", "")
            item.setTarget_(self)
            item.setTag_(index)
            shortcut_menu.addItem_(item)
        shortcut = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Keyboard shortcut", None, "")
        shortcut.setSubmenu_(shortcut_menu)
        menu.addItem_(shortcut)
        self.shortcut_menu = shortcut_menu
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit LatticeShadow", "terminate:", "q")
        menu.addItem_(quit_item)
        self.status_item.setMenu_(menu)
        self._local_monitor = None
        self._global_monitor = None
        self._register_hotkey()
        self.refreshStatus()

    def menuNeedsUpdate_(self, menu):
        self.refreshStatus()
        for index, item in enumerate(self.shortcut_menu.itemArray()):
            item.setState_(1 if SHORTCUTS[index] == self._shortcut_mode else 0)

    def _register_hotkey(self):
        def local(event):
            if _shortcut_matches(self._shortcut_mode, event):
                AppHelper.callAfter(self.toggleRecall)
                return None
            return event

        def global_event(event):
            if _shortcut_matches(self._shortcut_mode, event):
                AppHelper.callAfter(self.toggleRecall)

        try:
            self._local_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                KEY_MASK, local)
            self._global_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                KEY_MASK, global_event)
        except Exception:
            self._global_monitor = None
            # The menu action remains available when Input Monitoring is denied.
            self.capture_item.setTitle_("Shortcut unavailable; use Open Recall…")

    def chooseShortcut_(self, sender):
        mode = SHORTCUTS[sender.tag()]
        config.set("ui.shortcut", mode)
        self._shortcut_mode = mode

    def openRecall_(self, sender):
        self.showRecall()

    @objc.python_method
    def toggleRecall(self):
        if self.panel.isVisible():
            self.hideRecall()
        else:
            self.showRecall()

    @objc.python_method
    def showRecall(self):
        self.panel.center()
        self.panel.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeFirstResponder_(self.search_field)
        self.refreshStatus()
        self._request_search()

    @objc.python_method
    def hideRecall(self):
        self.recall.invalidate()
        self.panel.orderOut_(None)

    def applicationWillTerminate_(self, notification):
        self.recall.close()
        for monitor in (self._local_monitor, self._global_monitor):
            if monitor is not None:
                AppKit.NSEvent.removeMonitor_(monitor)

    @objc.python_method
    def refreshStatus(self):
        if self._status_loading:
            return
        self._status_loading = True

        def work():
            try:
                status = get_status()
            except Exception as exc:
                status = {"state": "unavailable", "error": str(exc), "paused": False,
                          "sources": {}, "consent_needed": []}
            AppHelper.callAfter(self._show_status, status)

        import threading
        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def _show_status(self, status):
        self._status_loading = False
        state = status["state"]
        labels = {"capturing": "Capture active", "idle": "No capture sources enabled",
                  "paused": "Capture paused", "stopped": "Capture stopped",
                  "consent_needed": "Capture needs consent", "unavailable": "Capture status unavailable"}
        title = labels.get(state, f"Capture: {state}")
        active = [name for name, data in status.get("sources", {}).items() if data.get("capturing")]
        if active:
            title += " (" + ", ".join(active) + ")"
        if status.get("error"):
            title += f" — {status['error']}"
        self.capture_item.setTitle_(title)
        self.status_field.setStringValue_(title)
        self.status_field.setToolTip_(title)
        self.pause_item.setTitle_("Resume capture" if status.get("paused") else "Pause capture")
        self.panel_pause.setTitle_("Resume capture" if status.get("paused") else "Pause capture")
        self._paused = bool(status.get("paused"))

    def pauseResume_(self, sender):
        try:
            consent.set_paused(not self._paused)
        except Exception as exc:
            self._message("Capture state", str(exc))
        self.refreshStatus()

    def controlTextDidChange_(self, notification):
        if notification.object() is self.project_field and self.unassigned.state() == 1:
            self.unassigned.setState_(0)
        self._schedule_search()

    def filterChanged_(self, sender):
        if sender is self.unassigned and self.unassigned.state() == 1:
            self.project_field.setStringValue_("")
        self._schedule_search()

    @objc.python_method
    def _current_scope(self):
        return _scope(self.project_field.stringValue(),
                      self.unassigned.state() == 1,
                      self.source_field.stringValue(),
                      self.period.indexOfSelectedItem())

    @objc.python_method
    def _schedule_search(self):
        self._search_generation += 1
        generation = self._search_generation
        self.recall.invalidate()
        self._displayed = []
        self.data_source.events = []
        self.table.reloadData()
        self.preview.setString_("")
        self.result_field.setStringValue_("Searching…")
        AppHelper.callLater(0.18, self._run_search_if_current, generation)

    @objc.python_method
    def _run_search_if_current(self, generation):
        if generation == self._search_generation and self.panel.isVisible():
            self._request_search()

    @objc.python_method
    def _request_search(self):
        self.recall.invalidate()
        self._displayed = []
        self.data_source.events = []
        self.table.reloadData()
        self.preview.setString_("")
        self.result_field.setStringValue_("Searching…")
        self.recall.request(self.search_field.stringValue(), self._current_scope())

    @objc.python_method
    def _show_results(self, generation, events, error):
        if not self.panel.isVisible():
            return
        self._displayed = events
        self.data_source.events = events
        self.table.reloadData()
        if error:
            self.result_field.setStringValue_(f"Search error: {error}")
        elif events:
            count = len(events)
            if self.search_field.stringValue().strip():
                noun = "result" if count == 1 else "results"
            else:
                noun = "recent event" if count == 1 else "recent events"
            self.result_field.setStringValue_(f"{count} {noun}")
            self.table.selectRowIndexes_byExtendingSelection_(
                AppKit.NSIndexSet.indexSetWithIndex_(0), False)
            self._show_selection()
        else:
            self.result_field.setStringValue_("No matches" if self.search_field.stringValue().strip()
                                              else "No recent events")

    @objc.python_method
    def _selected_id(self):
        row = self.table.selectedRow()
        return self._displayed[row]["id"] if 0 <= row < len(self._displayed) else None

    def tableViewSelectionDidChange_(self, notification):
        self._show_selection()

    @objc.python_method
    def _show_selection(self):
        row = self.table.selectedRow()
        self.preview.setString_(_preview(self._displayed[row]) if 0 <= row < len(self._displayed) else "")

    @objc.python_method
    def _live_selected(self):
        event_id = self._selected_id()
        if event_id is None:
            return None
        try:
            found = get_events(_desktop_vault(), [event_id], scope=self._current_scope())
        except Exception as exc:
            self._message("Memory unavailable", str(exc))
            return None
        if not found:
            self._request_search()
            self._message("Memory changed", "That event is no longer available in this view.")
            return None
        return found[0]

    def copySelected_(self, sender):
        event = self._live_selected()
        if event is None:
            return
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(event["text"], AppKit.NSPasteboardTypeString)
        self.result_field.setStringValue_("Copied selected event to clipboard")

    def openSelected_(self, sender):
        event = self._live_selected()
        if event is None:
            return
        target = _open_kind(open_target(event))
        if target is None:
            self._message("No supported target", "This event has no web link or local file to open.")
            return
        kind, value = target
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        if kind == "web":
            workspace.openURL_(AppKit.NSURL.URLWithString_(value))
        else:
            if not Path(value).exists():
                self._message("Source file missing", "The file no longer exists at its recorded path.")
                return
            workspace.activateFileViewerSelectingURLs_([AppKit.NSURL.fileURLWithPath_(value)])

    def assignSelected_(self, sender):
        event = self._live_selected()
        if event is None:
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Assign a project")
        alert.setInformativeText_("Enter a project name. Leave blank for Unassigned.")
        entry = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 300, 24))
        entry.setStringValue_(event.get("project") or "")
        alert.setAccessoryView_(entry)
        alert.addButtonWithTitle_("Assign")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        try:
            assign_project(_desktop_vault(), [event["id"]], entry.stringValue().strip() or None)
        except Exception as exc:
            self._message("Assignment failed", str(exc))
        self._request_search()

    def forgetSelected_(self, sender):
        event = self._live_selected()
        if event is None:
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Forget this event?")
        alert.setInformativeText_("It will disappear from live LatticeShadow views. Older backups and the original source may still contain it.")
        alert.addButtonWithTitle_("Forget")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        self.recall.invalidate()
        self._displayed = []
        self.data_source.events = []
        self.table.reloadData()
        self.preview.setString_("")
        try:
            outcome = forget_events(_desktop_vault(), [event["id"]])
            if outcome["cleanup_errors"]:
                self._message("Cleanup needs attention", "\n".join(outcome["cleanup_errors"]))
        except Exception as exc:
            self._message("Forget failed", str(exc))
        self._request_search()

    def control_textView_doCommandBySelector_(self, control, text_view, selector):
        name = selector.description() if hasattr(selector, "description") else str(selector)
        if name == "insertNewline:":
            self.copySelected_(None)
            return True
        if name == "cancelOperation:":
            self.hideRecall()
            return True
        if name in ("moveDown:", "moveUp:"):
            row = self.table.selectedRow()
            row += 1 if name == "moveDown:" else -1
            row = max(0, min(row, len(self._displayed) - 1))
            if self._displayed:
                self.table.selectRowIndexes_byExtendingSelection_(
                    AppKit.NSIndexSet.indexSetWithIndex_(row), False)
            return True
        return False

    def doubleClickRow_(self, sender):
        self.copySelected_(sender)

    def windowShouldClose_(self, sender):
        self.hideRecall()
        return False

    @objc.python_method
    def _message(self, title, detail):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(detail)
        alert.addButtonWithTitle_("OK")
        alert.runModal()


def setup_spotlight(delegate):
    rect = AppKit.NSMakeRect(0, 0, 900, 650)
    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
    panel = RecallPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, AppKit.NSBackingStoreBuffered, False)
    panel.setTitle_("LatticeShadow Recall")
    panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
    panel.setBackgroundColor_(NAVY)
    panel.setLevel_(AppKit.NSFloatingWindowLevel)
    panel.setDelegate_(delegate)
    content = panel.contentView()

    mark = LatticeMark.alloc().initWithFrame_(AppKit.NSMakeRect(24, 575, 38, 42))
    mark.setAccessibilityElement_(False)
    content.addSubview_(mark)
    content.addSubview_(_label("LatticeShadow", AppKit.NSMakeRect(68, 597, 320, 34),
                               size=25, weight=AppKit.NSFontWeightSemibold))
    content.addSubview_(_label("Small things stay with you.",
                               AppKit.NSMakeRect(69, 574, 330, 20), color=MUTED))
    status = _label("Capture status: checking…", AppKit.NSMakeRect(490, 600, 280, 23),
                    color=AMBER, size=12, weight=AppKit.NSFontWeightMedium)
    content.addSubview_(status)
    pause = _button("Pause capture", "pauseResume:", delegate,
                    AppKit.NSMakeRect(770, 594, 108, 32))
    pause.setAccessibilityLabel_("Pause or resume capture")
    content.addSubview_(pause)
    search = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(24, 516, 852, 40))
    search.setPlaceholderString_("Search memories, or leave blank for recent events")
    search.setAccessibilityLabel_("Search local memories")
    search.setFont_(AppKit.NSFont.systemFontOfSize_(17))
    search.setDelegate_(delegate)
    content.addSubview_(search)

    content.addSubview_(_label("Project", AppKit.NSMakeRect(24, 480, 57, 20), color=MUTED))
    project = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(82, 477, 155, 26))
    project.setPlaceholderString_("All projects")
    project.setAccessibilityLabel_("Filter by project")
    project.setDelegate_(delegate)
    content.addSubview_(project)
    unassigned = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(245, 476, 146, 28))
    unassigned.setButtonType_(AppKit.NSSwitchButton)
    unassigned.setTitle_("Unassigned only")
    unassigned.setTarget_(delegate)
    unassigned.setAction_("filterChanged:")
    content.addSubview_(unassigned)
    content.addSubview_(_label("Source", AppKit.NSMakeRect(410, 480, 55, 20), color=MUTED))
    source = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(464, 477, 155, 26))
    source.setPlaceholderString_("All sources")
    source.setAccessibilityLabel_("Filter by source")
    source.setDelegate_(delegate)
    content.addSubview_(source)
    content.addSubview_(_label("When", AppKit.NSMakeRect(635, 480, 48, 20), color=MUTED))
    period = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
        AppKit.NSMakeRect(682, 477, 194, 26), False)
    for title in ("Any time", "Today", "Last 7 days", "Last 30 days"):
        period.addItemWithTitle_(title)
    period.setAccessibilityLabel_("Filter by time")
    period.setTarget_(delegate)
    period.setAction_("filterChanged:")
    content.addSubview_(period)

    result = _label("Recent events", AppKit.NSMakeRect(30, 435, 395, 22),
                    color=AMBER, size=14, weight=AppKit.NSFontWeightMedium)
    content.addSubview_(result)
    content.addSubview_(_label("Preview and provenance", AppKit.NSMakeRect(466, 435, 390, 22),
                               color=AMBER, size=14, weight=AppKit.NSFontWeightMedium))
    content.addSubview_(_card(AppKit.NSMakeRect(20, 80, 420, 345)))
    content.addSubview_(_card(AppKit.NSMakeRect(454, 80, 426, 345)))

    scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(30, 100, 400, 312))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(AppKit.NSNoBorder)
    scroll.setBackgroundColor_(SLATE)
    table = ResultsTable.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 400, 312))
    column = AppKit.NSTableColumn.alloc().initWithIdentifier_("event")
    column.setWidth_(380)
    table.addTableColumn_(column)
    table.setHeaderView_(None)
    table.setRowHeight_(34)
    table.setBackgroundColor_(SLATE)
    table.setAccessibilityLabel_("Recall results")
    data_source = ResultsDataSource.alloc().init()
    table.setDataSource_(data_source)
    table.setDelegate_(delegate)
    table.setTarget_(delegate)
    table.setDoubleAction_("doubleClickRow:")
    scroll.setDocumentView_(table)
    content.addSubview_(scroll)

    preview_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
        AppKit.NSMakeRect(466, 149, 402, 262))
    preview_scroll.setHasVerticalScroller_(True)
    preview_scroll.setBorderType_(AppKit.NSNoBorder)
    preview_scroll.setBackgroundColor_(SLATE)
    preview = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 402, 262))
    preview.setEditable_(False)
    preview.setSelectable_(True)
    preview.setDrawsBackground_(True)
    preview.setBackgroundColor_(SLATE)
    preview.setTextColor_(INK)
    preview.setFont_(AppKit.NSFont.systemFontOfSize_(14))
    preview.setAccessibilityLabel_("Selected event preview and provenance")
    preview_scroll.setDocumentView_(preview)
    content.addSubview_(preview_scroll)
    for title, action, x, width in (("Copy", "copySelected:", 466, 88),
                                    ("Open link/file", "openSelected:", 558, 105),
                                    ("Assign project", "assignSelected:", 667, 110),
                                    ("Forget…", "forgetSelected:", 781, 87)):
        button = _button(title, action, delegate, AppKit.NSMakeRect(x, 99, width, 34))
        if title == "Copy":
            button.setBezelColor_(AMBER)
            button.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                title, {AppKit.NSForegroundColorAttributeName: NAVY,
                        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(13)}))
        elif title == "Forget…":
            button.setBezelColor_(_color(115, 52, 52))
            button.setContentTintColor_(INK)
        else:
            button.setBezelColor_(_color(64, 86, 102))
        content.addSubview_(button)
    content.addSubview_(_label("Return copies · Escape closes · Shortcut can be changed in the menu",
                                AppKit.NSMakeRect(24, 34, 830, 22), color=MUTED, size=12))

    delegate.panel = panel
    delegate.search_field = search
    delegate.project_field = project
    delegate.unassigned = unassigned
    delegate.source_field = source
    delegate.period = period
    delegate.table = table
    delegate.data_source = data_source
    delegate.preview = preview
    delegate.result_field = result
    delegate.status_field = status
    delegate.panel_pause = pause
    delegate._paused = False
    return panel


def run_menu_app():
    AppKit.NSApplicationLoad()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = ShadowMenuApp.alloc().init()
    app.setDelegate_(delegate)
    setup_spotlight(delegate)
    AppHelper.runEventLoop()
