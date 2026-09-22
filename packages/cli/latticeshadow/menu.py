"""
Native macOS Menu Bar GUI & Spotlight UI for LatticeShadow.

Uses Python-Cocoa AppKit bindings to create a status item with a dropdown menu
and a modern, interactive Spotlight-style floating panel with live search results.
"""

import os
import sys
import AppKit
import objc
import subprocess
import threading
from PyObjCTools import AppHelper

from latticeshadow.shadow_cli import get_vault, LOG_DIR

# Compatibility constants for different PyObjC versions
NSEventMaskKeyDown = getattr(AppKit, "NSEventMaskKeyDown", getattr(AppKit, "NSKeyDownMask", 1024))
NSEventModifierFlagOption = getattr(AppKit, "NSEventModifierFlagOption", getattr(AppKit, "NSAlternateKeyMask", 524288))

# Dynamically load Metal and MetalKit frameworks
HAS_METAL = False
metal_device = None

try:
    metal_bundle = objc.loadBundle(
        'Metal',
        globals(),
        bundle_path='/System/Library/Frameworks/Metal.framework'
    )
    metalkit_bundle = objc.loadBundle(
        'MetalKit',
        globals(),
        bundle_path='/System/Library/Frameworks/MetalKit.framework'
    )
    objc.loadBundleFunctions(
        metal_bundle,
        globals(),
        [('MTLCreateSystemDefaultDevice', b'@')]
    )
    if 'MTLCreateSystemDefaultDevice' in globals():
        metal_device = MTLCreateSystemDefaultDevice()
        if metal_device is not None:
            HAS_METAL = True
except Exception as e:
    print(f"Warning: Metal/MetalKit not fully supported or loaded: {e}", file=sys.stderr)

if HAS_METAL:
    try:
        MTKViewBase = objc.lookUpClass('MTKView')
    except Exception:
        MTKViewBase = AppKit.NSView
        HAS_METAL = False
else:
    MTKViewBase = AppKit.NSView


class GridMetalView(MTKViewBase):
    def initWithFrame_(self, frame):
        self = objc.super(GridMetalView, self).initWithFrame_(frame)
        if self is not None:
            self._fallback = False
            if HAS_METAL:
                self.initMetal()
        return self

    def initMetal(self):
        try:
            self.setDevice_(metal_device)
            self.setOpaque_(False)
            self.setClearColor_((0.0, 0.0, 0.0, 0.0))
            
            # Load and compile shaders
            current_dir = os.path.dirname(os.path.abspath(__file__))
            shader_path = os.path.join(current_dir, "Shaders.metal")
            with open(shader_path, "r", encoding="utf-8") as f:
                source_code = f.read()
            
            # Compile library
            self.library = metal_device.newLibraryWithSource_options_error_(source_code, None, None)
            if self.library is None:
                raise RuntimeError("Failed to compile Metal library from source.")
            
            # Load functions
            self.vertex_func = self.library.newFunctionWithName_("vertex_main")
            self.fragment_func = self.library.newFunctionWithName_("fragment_main")
            
            # Create Render Pipeline Descriptor
            MTLRenderPipelineDescriptor = objc.lookUpClass('MTLRenderPipelineDescriptor')
            pipeline_desc = MTLRenderPipelineDescriptor.alloc().init()
            pipeline_desc.setVertexFunction_(self.vertex_func)
            pipeline_desc.setFragmentFunction_(self.fragment_func)
            pipeline_desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(self.colorPixelFormat())
            
            # Set up alpha blending on color attachments
            attachment = pipeline_desc.colorAttachments().objectAtIndexedSubscript_(0)
            attachment.setBlendingEnabled_(True)
            attachment.setSourceRGBBlendFactor_(4) # MTLBlendFactorSourceAlpha
            attachment.setDestinationRGBBlendFactor_(5) # MTLBlendFactorOneMinusSourceAlpha
            attachment.setRgbBlendOperation_(0) # MTLBlendOperationAdd
            attachment.setSourceAlphaBlendFactor_(4)
            attachment.setDestinationAlphaBlendFactor_(5)
            attachment.setAlphaBlendOperation_(0)
            
            # Build render pipeline state
            self.pipeline_state = metal_device.newRenderPipelineStateWithDescriptor_error_(pipeline_desc, None)
            
            # Command queue
            self.command_queue = metal_device.newCommandQueue()
            
            # Time tracking
            import time
            self.start_time = time.time()
            
            # Set up draw notification/callback
            self.setPaused_(False)
            self.setEnableSetNeedsDisplay_(False) # Draw continuously
            
        except Exception as e:
            print(f"Warning: GridMetalView failed to initialize Metal: {e}", file=sys.stderr)
            self._fallback = True

    def drawRect_(self, rect):
        if self._fallback or not HAS_METAL:
            return
            
        try:
            import time
            current_time = float(time.time() - self.start_time)
            
            # Create command buffer
            command_buffer = self.command_queue.commandBuffer()
            if command_buffer is None:
                return
            
            # Get render pass descriptor from MTKView
            render_pass_desc = self.currentRenderPassDescriptor()
            if render_pass_desc is None:
                return
                
            # Create command encoder
            encoder = command_buffer.renderCommandEncoderWithDescriptor_(render_pass_desc)
            if encoder is None:
                return
            
            # Set pipeline state
            encoder.setRenderPipelineState_(self.pipeline_state)
            
            # Send time float variable to fragment shader via setFragmentBytes_length_atIndex_
            import struct
            import ctypes
            time_bytes = struct.pack('f', current_time)
            buf = ctypes.create_string_buffer(time_bytes)
            encoder.setFragmentBytes_length_atIndex_(ctypes.addressof(buf), len(time_bytes), 0)
            
            # Draw screen-aligned quad (4 vertices for triangle strip)
            encoder.drawPrimitives_vertexStart_vertexCount_(5, 0, 4)
            
            # End encoding
            encoder.endEncoding()
            
            # Present and commit
            drawable = self.currentDrawable()
            if drawable is not None:
                command_buffer.presentDrawable_(drawable)
            command_buffer.commit()
            
        except Exception as e:
            print(f"Warning: Error in GridMetalView drawRect_: {e}", file=sys.stderr)
            self._fallback = True


class SearchResultsDataSource(AppKit.NSObject):
    def init(self):
        self = objc.super(SearchResultsDataSource, self).init()
        self.results = []  # List of tuples: (doc_string, score)
        return self

    def numberOfRowsInTableView_(self, tableView):
        return len(self.results)

    def tableView_objectValueForTableColumn_row_(self, tableView, column, row):
        if row < len(self.results):
            doc, score = self.results[row]
            # Clean up newlines for a clean single-line table view representation
            preview = doc.strip().replace("\n", " ")
            if len(preview) > 75:
                preview = preview[:75] + "..."
            return f"[{score:.2f}] {preview}"
        return ""


class SpotlightTextField(AppKit.NSTextField):
    def performKeyEquivalent_(self, event):
        if event.keyCode() == 53:  # ESC
            self.window().orderOut_(None)
            return True
        return objc.super(SpotlightTextField, self).performKeyEquivalent_(event)


class SpotlightWindow(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def selectNextRow(self):
        if not hasattr(self, 'table_view'):
            return
        row_count = self.table_view.numberOfRows()
        if row_count == 0:
            return
        current = self.table_view.selectedRow()
        new_row = min(row_count - 1, current + 1) if current >= 0 else 0
        self.table_view.selectRowIndexes_byExtendingSelection_(
            AppKit.NSIndexSet.indexSetWithIndex_(new_row), False
        )
        self.table_view.scrollRowToVisible_(new_row)

    def selectPreviousRow(self):
        if not hasattr(self, 'table_view'):
            return
        row_count = self.table_view.numberOfRows()
        if row_count == 0:
            return
        current = self.table_view.selectedRow()
        new_row = max(0, current - 1) if current >= 0 else 0
        self.table_view.selectRowIndexes_byExtendingSelection_(
            AppKit.NSIndexSet.indexSetWithIndex_(new_row), False
        )
        self.table_view.scrollRowToVisible_(new_row)

    def confirmSelection(self):
        if not hasattr(self, 'table_view') or not hasattr(self, 'delegate_app'):
            return
        row = self.table_view.selectedRow()
        if row >= 0 and row < len(self.table_view.dataSource().results):
            doc, score = self.table_view.dataSource().results[row]
            self.delegate_app.copyToClipboardAndNotify_score_(doc, score)
            self.orderOut_(None)

    def updateResults_(self, results):
        self.table_view.dataSource().results = results
        self.table_view.reloadData()
        
        # Select first result by default if available
        if len(results) > 0:
            self.table_view.selectRowIndexes_byExtendingSelection_(
                AppKit.NSIndexSet.indexSetWithIndex_(0), False
            )
        
        # Dynamically calculate window height
        new_height = 60 + len(results) * 35
        frame = self.frame()
        diff = new_height - frame.size.height
        new_y = frame.origin.y - diff
        new_frame = AppKit.NSMakeRect(frame.origin.x, new_y, frame.size.width, new_height)
        self.setFrame_display_animate_(new_frame, True, True)


class ShadowMenuApp(AppKit.NSObject):
    def init(self):
        self = objc.super(ShadowMenuApp, self).init()
        return self

    def applicationDidFinishLaunching_(self, notification):
        # 1. Create System Status Bar Item
        self.statusItem = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        self.statusItem.button().setTitle_("⏣")  # Secure index symbol
        
        # 2. Build Menu
        self.menu = AppKit.NSMenu.alloc().init()
        
        statusTitle = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "LatticeShadow: ● Active", None, ""
        )
        statusTitle.setEnabled_(False)
        self.menu.addItem_(statusTitle)
        self.menu.addItem_(AppKit.NSMenuItem.separatorItem())
        
        recallItem = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Recall Semantically...", "showRecallDialog:", "r"
        )
        recallItem.setTarget_(self)
        self.menu.addItem_(recallItem)

        sleepItem = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Trigger REM Sleep", "triggerSleep:", "s"
        )
        sleepItem.setTarget_(self)
        self.menu.addItem_(sleepItem)

        docItem = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Run Diagnostics (Doctor)", "runDoctor:", "d"
        )
        docItem.setTarget_(self)
        self.menu.addItem_(docItem)

        self.menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quitItem = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "q"
        )
        self.menu.addItem_(quitItem)
        
        self.statusItem.setMenu_(self.menu)
        
        # 3. Setup global key intercept monitors
        self.registerHotkeys()

    def registerHotkeys(self):
        def handle_event(event):
            flags = event.modifierFlags()
            is_option = bool(flags & NSEventModifierFlagOption)
            if is_option and event.keyCode() == 49:  # Option + Space
                AppHelper.callAfter(self.toggleSpotlight)
                return None
            return event

        # Local monitor (when our app is key)
        self._local_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, handle_event
        )
        # Global monitor (when other apps are key)
        self._global_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, handle_event
        )

    def showRecallDialog_(self, sender):
        """Open a native AppleScript dialog to query history."""
        script = (
            'tell application "System Events"\n'
            'activate\n'
            'set theResult to display dialog "Enter semantic recall query:" default answer "" with title "LatticeShadow Recall"\n'
            'text returned of theResult\n'
            'end tell'
        )
        try:
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            query = proc.stdout.strip()
            if query:
                self.executeRecall(query)
        except Exception as e:
            self.notify("Error", f"Failed to run dialog: {e}")

    @objc.python_method
    def executeRecall(self, query: str):
        from latticeshadow import config
        from latticeshadow.holographic_index import HolographicIndex
        save_path = os.path.join(config.get_data_dir(), "holographic_today.bin")
        if not os.path.exists(save_path):
            self.notify("Error", "No holographic index found. Run REM Sleep first.")
            return

        try:
            vault = get_vault()
            if not vault:
                self.notify("Error", "Vault not found.")
                return
            
            # Retrieve embedding representation of query
            query_emb = vault._embedder.embed(query)
            
            # Load index and recall
            index = HolographicIndex.load(save_path)
            result, score = index.recall(query_emb)
            
            if result:
                self.copyToClipboardAndNotify_score_(result, score)
            else:
                self.notify("Recall Failed", "No matching memories found.")
        except Exception as e:
            self.notify("Error", str(e))

    def copyToClipboardAndNotify_score_(self, doc: str, score: float):
        AppKit.NSPasteboard.generalPasteboard().clearContents()
        AppKit.NSPasteboard.generalPasteboard().setString_forType_(doc, AppKit.NSPasteboardTypeString)
        display_text = doc[:60] + "..." if len(doc) > 60 else doc
        self.notify("✓ Recalled to Clipboard", f"Match: '{display_text}' (Score: {score:.2f})")

    def triggerSleep_(self, sender):
        self.notify("REM Sleep", "Consolidation & Dream cycle started...")
        subprocess.Popen([sys.executable, "-c", "from latticeshadow.shadow_cli import do_sleep; do_sleep()"])

    def runDoctor_(self, sender):
        script = 'tell application "Terminal" to do script "shadow doctor"'
        subprocess.run(["osascript", "-e", script])

    @objc.python_method
    def notify(self, title: str, msg: str):
        safe_msg = msg.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def toggleSpotlight(self):
        if hasattr(self, 'spotlight_window'):
            if self.spotlight_window.isVisible():
                self.spotlight_window.orderOut_(None)
            else:
                # Reset state
                self.spotlight_tf.setStringValue_("")
                self.spotlight_window.updateResults_([])
                self.spotlight_window.center()
                self.spotlight_window.makeKeyAndOrderFront_(None)
                AppKit.NSApp.activateIgnoringOtherApps_(True)

    # ── Text Field & Live Search Delegates ─────────────────────────────────

    def controlTextDidChange_(self, notification):
        tf = notification.object()
        query = tf.stringValue()
        self.performLiveSearch_(query)

    def performLiveSearch_(self, query: str):
        if len(query.strip()) < 2:
            AppHelper.callAfter(self.updateSearchResults_, [])
            return

        def run_search():
            try:
                vault = get_vault()
                if not vault:
                    return
                # Semantic search
                res = vault.search(query, n_results=5)
                results = []
                if res and res.documents:
                    for doc, score in zip(res.documents, res.scores):
                        results.append((doc, score))
                AppHelper.callAfter(self.updateSearchResults_, results)
            except Exception as e:
                print(f"Background live search error: {e}")

        threading.Thread(target=run_search, daemon=True).start()

    def updateSearchResults_(self, results):
        if hasattr(self, 'spotlight_window'):
            self.spotlight_window.updateResults_(results)

    def control_textView_doCommandBySelector_(self, control, textView, selector):
        sel_name = selector.description() if hasattr(selector, 'description') else str(selector)
        if sel_name == "moveDown:":
            if hasattr(self, 'spotlight_window'):
                self.spotlight_window.selectNextRow()
            return True
        elif sel_name == "moveUp:":
            if hasattr(self, 'spotlight_window'):
                self.spotlight_window.selectPreviousRow()
            return True
        elif sel_name == "insertNewline:":
            if hasattr(self, 'spotlight_window'):
                self.spotlight_window.confirmSelection()
            return True
        return False

    def doubleClickRow_(self, sender):
        if hasattr(self, 'spotlight_window'):
            self.spotlight_window.confirmSelection()


def setup_spotlight(delegate):
    # Borderless floating panel
    rect = AppKit.NSMakeRect(0, 0, 600, 60)
    style = AppKit.NSWindowStyleMaskNonactivatingPanel | AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskResizable
    win = SpotlightWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, AppKit.NSBackingStoreBuffered, False
    )
    win.setLevel_(AppKit.NSFloatingWindowLevel)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.center()
    win.setMovableByWindowBackground_(True)
    win.setHasShadow_(True)

    # Blurred panel backdrop
    visual_effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(rect)
    visual_effect.setMaterial_(AppKit.NSVisualEffectMaterialPopover)
    visual_effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
    visual_effect.setState_(AppKit.NSVisualEffectStateActive)
    win.setContentView_(visual_effect)

    # Enable layer backing on visual_effect
    visual_effect.setWantsLayer_(True)

    # Instantiate GridMetalView
    grid_view = GridMetalView.alloc().initWithFrame_(rect)
    grid_view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    grid_view.setWantsLayer_(True)

    # Add GridMetalView at index 0 (as the first subview) so it acts as the background
    visual_effect.addSubview_positioned_relativeTo_(grid_view, AppKit.NSWindowBelow, None)

    # Input Text Field
    tf = SpotlightTextField.alloc().initWithFrame_(AppKit.NSMakeRect(20, 15, 560, 30))
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setFocusRingType_(AppKit.NSFocusRingTypeNone)
    tf.setFont_(AppKit.NSFont.systemFontOfSize_(20))
    tf.setPlaceholderString_("Search LatticeShadow...")
    tf.setTextColor_(AppKit.NSColor.textColor())
    tf.setDelegate_(delegate)
    tf.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin)
    tf.setWantsLayer_(True)
    visual_effect.addSubview_(tf)

    # Results Table Container (initially height 0)
    scroll_view = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(10, 10, 580, 0))
    scroll_view.setHasVerticalScroller_(True)
    scroll_view.setDrawsBackground_(False)
    scroll_view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    scroll_view.setWantsLayer_(True)
    visual_effect.addSubview_(scroll_view)

    # Autocomplete Matches Table View
    table_view = AppKit.NSTableView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 580, 0))
    col = AppKit.NSTableColumn.alloc().initWithIdentifier_("ResultColumn")
    col.setWidth_(560)
    col.setResizingMask_(AppKit.NSTableColumnNoResizing)
    
    cell = AppKit.NSTextFieldCell.alloc().init()
    cell.setFont_(AppKit.NSFont.fontWithName_size_("JetBrains Mono", 13.0) or AppKit.NSFont.systemFontOfSize_(13.0))
    cell.setTextColor_(AppKit.NSColor.textColor())
    col.setDataCell_(cell)
    
    table_view.addTableColumn_(col)
    table_view.setHeaderView_(None)
    table_view.setBackgroundColor_(AppKit.NSColor.clearColor())
    table_view.setRowHeight_(35.0)
    table_view.setDoubleAction_("doubleClickRow:")
    table_view.setTarget_(delegate)

    ds = SearchResultsDataSource.alloc().init()
    table_view.setDataSource_(ds)
    table_view.setDelegate_(ds)
    scroll_view.setDocumentView_(table_view)

    # Connect components
    win.table_view = table_view
    win.delegate_app = delegate
    
    delegate.spotlight_window = win
    delegate.spotlight_tf = tf


def run_menu_app():
    AppKit.NSApplicationLoad()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    
    delegate = ShadowMenuApp.alloc().init()
    app.setDelegate_(delegate)
    
    setup_spotlight(delegate)
    
    AppHelper.runEventLoop()
