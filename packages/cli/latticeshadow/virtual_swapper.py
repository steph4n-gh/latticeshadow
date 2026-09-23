import os
import sys
import time
import json
import subprocess
import threading
import logging

try:
    import AppKit
except ImportError:
    AppKit = None

logger = logging.getLogger("shadowd")

class SemanticSwapperDaemon(threading.Thread):
    def __init__(self, vault=None, pot_chain=None):
        super().__init__()
        self.daemon = True
        self.vault = vault
        self.pot_chain = pot_chain
        self._stop_event = threading.Event()
        self.last_active_app = None
        self.last_active_app_name = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        if not AppKit:
            logger.warning("AppKit not available, SemanticSwapperDaemon starting in idle mode.")
            return

        logger.info("SemanticSwapperDaemon starting...")
        while not self._stop_event.is_set():
            try:
                from latticeshadow import consent
                if not consent.surface_enabled("semantic_swapper"):
                    self._stop_event.wait(5.0)
                    continue
                # 1. Check memory swap usage to scale the polling interval
                swap_used_mb = self.get_swap_used_mb()
                
                # Dynamic polling interval
                if swap_used_mb > 2048:      # > 2GB swap used (high pressure)
                    interval = 3.0
                elif swap_used_mb > 512:     # > 512MB swap used (warn)
                    interval = 10.0
                else:                        # Low pressure
                    interval = 30.0

                # 2. Check active app transitions
                self.check_app_transition()
                
            except Exception as e:
                logger.warning("Error in SemanticSwapperDaemon loop: %s", e)
                interval = 10.0

            self._stop_event.wait(interval)

    def get_swap_used_mb(self) -> float:
        try:
            out = subprocess.check_output(["sysctl", "-n", "vm.swapusage"], text=True).strip()
            # total = 3072.00M  used = 1536.00M  free = 1536.00M  (encrypted)
            parts = out.split()
            for i, part in enumerate(parts):
                if part == "used" and i + 2 < len(parts):
                    val_str = parts[i + 2].rstrip("M").rstrip("G")
                    val = float(val_str)
                    if "G" in parts[i + 2]:
                        val *= 1024
                    return val
        except Exception:
            pass
        return 0.0

    def check_app_transition(self):
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        if not workspace:
            return
        frontmost_app = workspace.frontmostApplication()
        if not frontmost_app:
            return

        app_name = frontmost_app.localizedName()
        if not app_name:
            return

        # If the active app changed
        if app_name != self.last_active_app_name:
            prev_app_name = self.last_active_app_name
            self.last_active_app_name = app_name

            # Snapshot the application that just went into the background
            if prev_app_name and prev_app_name in ("Safari", "Google Chrome", "TextEdit", "Terminal", "iTerm2", "iTerm"):
                logger.info("Application transitioned to background: %s. Performing semantic page-out...", prev_app_name)
                self.snapshot_app_to_vault(prev_app_name)

    def run_applescript(self, script):
        try:
            res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
            output = res.stdout.strip()
            title = ""
            url = ""
            text = ""
            if "---URL---" in output:
                parts = output.split("---URL---")
                title = parts[0].strip()
                rest = parts[1]
                if "---TEXT---" in rest:
                    parts2 = rest.split("---TEXT---")
                    url = parts2[0].strip()
                    text = parts2[1].strip()
                else:
                    url = rest.strip()
            else:
                text = output
            return {"title": title, "url": url, "text": text}
        except Exception:
            return None

    def query_app_text(self, app_name):
        if app_name == "Safari":
            script = """tell application "Safari"
    if (count of windows) > 0 then
        tell window 1
            set activeTab to current tab
            set tabTitle to name of activeTab
            set tabURL to URL of activeTab
            set tabText to ""
            try
                set tabText to do JavaScript "document.body.innerText" in activeTab
            on error
                try
                    set tabText to source of activeTab
                on error
                    set tabText to ""
                end try
            end try
            return tabTitle & "\\n---URL---\\n" & tabURL & "\\n---TEXT---\\n" & tabText
        end tell
    end if
end tell"""
        elif app_name == "Google Chrome":
            script = """tell application "Google Chrome"
    if (count of windows) > 0 then
        tell window 1
            set activeTab to active tab
            set tabTitle to title of activeTab
            set tabURL to URL of activeTab
            set tabText to ""
            try
                set tabText to execute activeTab javascript "document.body.innerText"
            on error
                set tabText to ""
            end try
            return tabTitle & "\\n---URL---\\n" & tabURL & "\\n---TEXT---\\n" & tabText
        end tell
    end if
end tell"""
        elif app_name == "TextEdit":
            script = """tell application "TextEdit"
    if (count of documents) > 0 then
        set docName to name of document 1
        set docText to text of document 1
        return docName & "\\n---URL---\\n\\n---TEXT---\\n" & docText
    end if
end tell"""
        elif app_name in ("Terminal", "iTerm2", "iTerm"):
            if app_name == "Terminal":
                script = """tell application "Terminal"
    if (count of windows) > 0 then
        tell window 1
            set docName to name
            set docText to contents of selected tab
            return docName & "\\n---URL---\\n\\n---TEXT---\\n" & docText
        end tell
    end if
end tell"""
            else:
                script = """tell application "iTerm"
    if (count of windows) > 0 then
        tell current window
            set docName to name
            set docText to text of current session
            return docName & "\\n---URL---\\n\\n---TEXT---\\n" & docText
        end tell
    end if
end tell"""
        else:
            return None

        return self.run_applescript(script)

    def snapshot_app_to_vault(self, app_name):
        from latticeshadow import consent
        if not consent.surface_enabled("semantic_swapper"):
            return
        if not self.vault:
            return

        content = self.query_app_text(app_name)
        if not content or not content.get("text"):
            return

        text = content["text"].strip()
        if not text:
            return
        if not consent.surface_enabled("semantic_swapper"):
            return

        # Store in vault
        metadata = {
            "source": "virtual_swapper",
            "app": app_name,
            "title": content.get("title", ""),
            "url": content.get("url", ""),
            "type": "swap_page",
            "timestamp": time.time()
        }
        
        try:
            doc_id = f"swap_{app_name}_{int(time.time())}"
            self.vault.add(
                documents=[text],
                metadatas=[metadata],
                ids=[doc_id]
            )
            logger.info("Semantic page-out successful for %s (id: %s)", app_name, doc_id)
            if self.pot_chain:
                self.pot_chain.append_event("virtual_swapper", json.dumps(metadata))
        except Exception as e:
            logger.warning("Failed to save swap page to vault: %s", e)
