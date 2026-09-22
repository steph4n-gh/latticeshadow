import os
import subprocess
import logging
import tempfile

logger = logging.getLogger(__name__)

class DNSOverride:
    def __init__(self, target_domains=["api.openai.com", "api.anthropic.com"], target_ip="127.0.0.1"):
        self.target_domains = target_domains
        self.target_ip = target_ip
        self.hosts_path = "/etc/hosts"
        self.marker_start = "# --- LATTICESHADOW PROXY START ---"
        self.marker_end = "# --- LATTICESHADOW PROXY END ---"

    def apply(self):
        """Applies the DNS overrides to /etc/hosts (requires sudo)."""
        logger.warning(f"Modifying {self.hosts_path} to intercept domains. This may prompt for admin password.")
        
        # Read current hosts
        with open(self.hosts_path, "r") as f:
            lines = f.readlines()
            
        # Clean up any existing proxy blocks
        cleaned_lines = []
        in_block = False
        for line in lines:
            if line.strip() == self.marker_start:
                in_block = True
                continue
            if line.strip() == self.marker_end:
                in_block = False
                continue
            if not in_block:
                cleaned_lines.append(line)
                
        # Append new block
        cleaned_lines.append(f"\n{self.marker_start}\n")
        for domain in self.target_domains:
            cleaned_lines.append(f"{self.target_ip} {domain}\n")
        cleaned_lines.append(f"{self.marker_end}\n")
        
        # Write to temp file and sudo copy it over
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
            tmp.writelines(cleaned_lines)
            tmp_path = tmp.name
            
        try:
            subprocess.run(["sudo", "cp", tmp_path, self.hosts_path], check=True)
            subprocess.run(["sudo", "chmod", "644", self.hosts_path], check=True)
            subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=True) # Flush DNS cache on macOS
            logger.info("DNS overrides applied successfully.")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to modify /etc/hosts. Sudo authorization failed: %s", e)
            raise PermissionError("Sudo authorization failed for DNS Override.")
        finally:
            os.remove(tmp_path)

    def remove(self):
        """Removes the DNS overrides from /etc/hosts."""
        logger.info("Removing DNS overrides from /etc/hosts...")
        
        with open(self.hosts_path, "r") as f:
            lines = f.readlines()
            
        cleaned_lines = []
        in_block = False
        for line in lines:
            if line.strip() == self.marker_start:
                in_block = True
                continue
            if line.strip() == self.marker_end:
                in_block = False
                continue
            if not in_block:
                cleaned_lines.append(line)
                
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
            tmp.writelines(cleaned_lines)
            tmp_path = tmp.name
            
        try:
            subprocess.run(["sudo", "cp", tmp_path, self.hosts_path], check=True)
            subprocess.run(["sudo", "chmod", "644", self.hosts_path], check=True)
            subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=True)
            logger.info("DNS overrides removed successfully.")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to remove DNS overrides: %s", e)
        finally:
            os.remove(tmp_path)
