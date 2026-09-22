import os
import json
import time
import hashlib
import shutil
import logging
from latticeshadow.config import get_data_dir
from latticeshadow import config
from latticeshadow.sensitivity import classify, redact

logger = logging.getLogger(__name__)

class EnvironmentSnapshotter:
    def __init__(self, time_travel_enabled: bool = False, include_environment: bool | None = None):
        self.enabled = time_travel_enabled
        self.include_environment = (
            bool(config.get("time_travel.include_environment"))
            if include_environment is None
            else include_environment
        )
        self.snapshot_dir = os.path.join(get_data_dir(), "snapshots")
        if self.enabled:
            os.makedirs(self.snapshot_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.snapshot_dir, 0o700)
            except Exception:
                pass
            
    def capture(self) -> str:
        """Captures a snapshot of the current environment if time travel is enabled."""
        if not self.enabled:
            return ""
            
        timestamp = int(time.time() * 1000)
        
        # Environment variables often contain secrets. Do not capture them
        # unless explicitly enabled, and redact likely credentials even then.
        env_vars = self._capture_environment() if self.include_environment else {}
        
        # Capture critical shell configs if they exist
        home = os.path.expanduser("~")
        configs_to_capture = [".bashrc", ".zshrc", ".bash_profile"]
        captured_files = {}
        for config_file in configs_to_capture:
            path = os.path.join(home, config_file)
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        captured_files[config_file] = f.read()
                except Exception:
                    pass
                    
        snapshot = {
            "timestamp": timestamp,
            "env_vars": env_vars,
            "files": captured_files
        }
        
        snapshot_json = json.dumps(snapshot, sort_keys=True)
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()[:12]
        
        # Save snapshot
        snapshot_path = os.path.join(self.snapshot_dir, f"snapshot_{snapshot_hash}.json")
        try:
            fd = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(snapshot_json)
            logger.info("Environment snapshot captured: %s", snapshot_hash)
            return snapshot_hash
        except Exception as e:
            logger.warning("Failed to save environment snapshot: %s", e)
            return ""

    def _capture_environment(self) -> dict:
        redacted = {}
        sensitive_key_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL", "AUTH")
        for key, value in os.environ.items():
            if any(marker in key.upper() for marker in sensitive_key_markers):
                redacted[key] = "[REDACTED]"
                continue
            sensitivity = classify(value)
            if sensitivity == "safe":
                redacted[key] = value
            else:
                redacted[key] = redact(value)
        return redacted

    def list_snapshots(self) -> list:
        if not self.enabled or not os.path.exists(self.snapshot_dir):
            return []
        snapshots = []
        for f in os.listdir(self.snapshot_dir):
            if f.startswith("snapshot_") and f.endswith(".json"):
                snapshots.append(f.replace("snapshot_", "").replace(".json", ""))
        return snapshots

    def rollback(self, snapshot_hash: str) -> bool:
        """Rolls back the environment to the specified snapshot."""
        if not self.enabled:
            print("Time travel is not enabled.")
            return False
            
        snapshot_path = os.path.join(self.snapshot_dir, f"snapshot_{snapshot_hash}.json")
        if not os.path.exists(snapshot_path):
            print(f"Snapshot {snapshot_hash} not found.")
            return False
            
        try:
            with open(snapshot_path, "r") as f:
                snapshot = json.load(f)
                
            # Restore files
            home = os.path.expanduser("~")
            for filename, content in snapshot.get("files", {}).items():
                file_path = os.path.join(home, filename)
                # Create backup of current state just in case
                if os.path.exists(file_path):
                    shutil.copy2(file_path, f"{file_path}.bak")
                with open(file_path, "w") as out_f:
                    out_f.write(content)
                    
            # (Note: we cannot reliably restore OS environment variables for the parent process, 
            # but we restore the config files that dictate them for new sessions)
            
            print(f"Rollback to {snapshot_hash} complete. Restart your shell to apply config changes.")
            return True
        except Exception as e:
            print(f"Rollback failed: {e}")
            return False
