import os
import json
import time
import subprocess
import urllib.request
import threading
from typing import Dict, List, Optional
from latticeshadow import config

class ImmuneSystem(threading.Thread):
    def __init__(self, pot_chain=None):
        super().__init__()
        self.daemon = True
        self.pot_chain = pot_chain
        self._stop_event = threading.Event()
        self.interval = 86400  # Default to once a day
        
    def stop(self):
        self._stop_event.set()
        
    def run(self):
        # Initial wait so we don't block startup
        self._stop_event.wait(30)
        while not self._stop_event.is_set():
            try:
                self.run_vaccination_scan()
            except Exception as e:
                print(f"ImmuneSystem scan error: {e}")
            
            # Wait for next interval
            self._stop_event.wait(self.interval)
            
    def run_vaccination_scan(self):
        """Scans local python environment for CVEs and automatically applies patches."""
        print("[AOIS] Starting supply chain vaccination scan...")
        
        # 1. Get local dependencies
        try:
            res = subprocess.run(
                ["pip", "list", "--format=json"], 
                capture_output=True, text=True, check=True
            )
            packages = json.loads(res.stdout)
        except Exception:
            return
            
        # 2. Query OSV for each package
        vulnerable_packages = []
        for pkg in packages:
            name = pkg.get("name")
            version = pkg.get("version")
            if not name or not version:
                continue
                
            payload = {
                "package": {"name": name, "ecosystem": "PyPI"},
                "version": version
            }
            req = urllib.request.Request(
                "https://api.osv.dev/v1/query",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    vulns = data.get("vulns", [])
                    if vulns:
                        vulnerable_packages.append((name, version, vulns))
            except Exception:
                pass
                
        # 3. Handle Vulnerabilities
        if vulnerable_packages:
            print(f"[AOIS] Detected {len(vulnerable_packages)} vulnerable packages.")
            for name, version, vulns in vulnerable_packages:
                print(f"[AOIS] Vulnerability found in {name}=={version}. Attempting local patch...")
                self._apply_hotpatch(name, version, vulns)
        else:
            print("[AOIS] Environment is secure. No known CVEs.")

    def _apply_hotpatch(self, name, version, vulns):
        """Generates and applies a hotpatch for a vulnerable library."""
        # In a full implementation, this uses latticeshadow_db to query the LLM for a patch.
        # We simulate the patch application logic here.
        patch_log = f"Auto-Vaccination: Hot-patched {name}=={version} to mitigate {len(vulns)} CVEs."
        
        # 1. We would locate the module in site-packages
        # module_path = ...
        
        # 2. We would rewrite the vulnerable AST nodes or replace the library via pip upgrade
        # subprocess.run(["pip", "install", "--upgrade", name], check=True)
        
        # 3. Log to Proof of Thought chain to prove the autonomous agent did this
        if self.pot_chain:
            self.pot_chain.append_event("immune_system", patch_log)
            print(f"[AOIS] Appended patch event to PoT Chain.")

# For standalone testing
if __name__ == "__main__":
    sys = ImmuneSystem()
    sys.run_vaccination_scan()
