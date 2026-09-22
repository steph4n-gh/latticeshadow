"""
Ambient Context Monitor for LatticeShadow.
Periodically polls the active app name using AppKit and gathers tab/document context.
"""

import os
import sys
import time
import json
import subprocess
import threading

try:
    import AppKit
except ImportError:
    AppKit = None

class AmbientContextMonitor(threading.Thread):
    def __init__(self, interval=5.0, pot_chain=None, vault=None):
        super().__init__()
        self.interval = interval
        self.daemon = True
        self._stop_event = threading.Event()
        self.context_path = os.path.expanduser("~/.latticeshadow/.ambient_context")
        self.pot_chain = pot_chain
        self.vault = vault

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.poll_and_save()
            except Exception:
                pass
            self._stop_event.wait(self.interval)

    def poll_and_save(self):
        if not AppKit:
            return
        
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        if not workspace:
            return
        frontmost_app = workspace.frontmostApplication()
        if not frontmost_app:
            return
        
        app_name = frontmost_app.localizedName()
        if not app_name:
            return
        
        if app_name not in ("Safari", "Google Chrome", "TextEdit", "Terminal", "iTerm2", "Code"):
            return
        
        content = None
        if app_name == "Safari":
            content = self.query_safari()
        elif app_name == "Google Chrome":
            content = self.query_chrome()
        elif app_name == "TextEdit":
            content = self.query_textedit()
        elif app_name in ("Terminal", "iTerm2"):
            content = self.query_terminal(app_name)
        elif app_name == "Code":
            content = self.query_vscode()
        
        if content:
            payload = {
                "app": app_name,
                "application": app_name,
                "title": content.get("title", ""),
                "url": content.get("url", ""),
                "text": content.get("text", ""),
                "buffer": content.get("text", ""),
                "timestamp": time.time()
            }
            serialized = json.dumps(payload)
            
            os.makedirs(os.path.dirname(self.context_path), exist_ok=True)
            
            # Write with owner-only 0o600 permissions
            fd = os.open(self.context_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            
            if self.pot_chain:
                self.pot_chain.append_event("ambient", serialized)
                
            self._check_for_jit_context(content.get("text", ""), app_name)

    def _check_for_jit_context(self, text, app_name):
        if not text or not self.vault:
            return
            
        # Very simple heuristic for terminal tracebacks
        if "Traceback (most recent call last):" in text or "Exception:" in text:
            # Avoid re-triggering constantly on the same text
            if hasattr(self, "_last_jit_text") and self._last_jit_text == text:
                return
            self._last_jit_text = text
            
            # Extract the last few lines to form the query
            lines = text.strip().split("\n")
            query_lines = lines[-10:]
            query_text = "\n".join(query_lines)
            
            # Search vault for this error
            try:
                results = self.vault.search(query_text, n_results=1)
                if results and getattr(results, "documents", None):
                    # If it's a strong match and it looks like a fix (e.g. not just the same error)
                    # We will show the tooltip
                    from latticeshadow.tooltip import show_tooltip
                    # For demo purposes, we show it if we got any result
                    msg = f"💡 Found relevant context in history. Press Ctrl+G to apply."
                    show_tooltip(msg, duration=6.0)
            except Exception as e:
                pass

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

    def query_safari(self):
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
        return self.run_applescript(script)

    def query_chrome(self):
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
        return self.run_applescript(script)

    def query_textedit(self):
        script = """tell application "TextEdit"
    if (count of documents) > 0 then
        set docName to name of document 1
        set docText to text of document 1
        return docName & "\\n---URL---\\n\\n---TEXT---\\n" & docText
    end if
end tell"""
        return self.run_applescript(script)

    def query_terminal(self, app_name):
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
        return self.run_applescript(script)

    def query_vscode(self):
        # VS Code does not support AppleScript for document text natively.
        # Placeholder for Accessibility API implementation.
        return None
