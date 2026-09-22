import os
import socket
import ssl
import stat
import tempfile
import threading
import json
import subprocess
from datetime import datetime, timedelta
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class StickyTLSProxy:
    def __init__(self, port=443, upstream_host="api.openai.com", upstream_port=443, cert_dir=None):
        self.port = port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        default_cert_dir = Path.home() / ".latticeshadow_db" / "mitm"
        self.cert_dir = Path(cert_dir or os.environ.get("LATTICESHADOW_MITM_CERT_DIR", default_cert_dir)).expanduser()
        self.cert_file = str(self.cert_dir / "shadow_mitm_cert.pem")
        self.key_file = str(self.cert_dir / "shadow_mitm_key.pem")
        self._running = False
        self._server_socket = None

    def _ensure_private_cert_dir(self):
        if self.cert_dir.exists() and self.cert_dir.is_symlink():
            raise PermissionError("MITM certificate directory must not be a symlink.")
        existed = self.cert_dir.exists()
        if not existed:
            self.cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.cert_dir, 0o700)
        st = self.cert_dir.stat()
        if not stat.S_ISDIR(st.st_mode):
            raise PermissionError("MITM certificate path is not a directory.")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise PermissionError("MITM certificate directory must be owned by the current user.")
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("MITM certificate directory must not be group/other accessible.")

    def _validate_private_file(self, path: str):
        file_path = Path(path)
        st = file_path.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise PermissionError(f"MITM certificate file must not be a symlink: {file_path}")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise PermissionError(f"MITM certificate file must be owned by the current user: {file_path}")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(f"MITM certificate file must not be group/other writable: {file_path}")
        os.chmod(file_path, 0o600)

    def _generate_self_signed_cert(self):
        """Generates a self-signed root CA for MITM (requires OpenSSL)."""
        self._ensure_private_cert_dir()
        if os.path.exists(self.cert_file) and os.path.exists(self.key_file):
            self._validate_private_file(self.cert_file)
            self._validate_private_file(self.key_file)
            return

        logger.info("Generating self-signed certificate for MITM proxy...")
        cert_tmp = None
        key_tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cert_dir, prefix=".shadow_cert_", suffix=".pem", delete=False) as cert_handle:
                cert_tmp = cert_handle.name
            with tempfile.NamedTemporaryFile(dir=self.cert_dir, prefix=".shadow_key_", suffix=".pem", delete=False) as key_handle:
                key_tmp = key_handle.name

            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
                "-keyout", key_tmp, "-out", cert_tmp, "-days", "365",
                "-subj", f"/CN={self.upstream_host}"
            ], check=True, stderr=subprocess.DEVNULL)

            os.chmod(cert_tmp, 0o600)
            os.chmod(key_tmp, 0o600)
            os.replace(cert_tmp, self.cert_file)
            os.replace(key_tmp, self.key_file)
            cert_tmp = None
            key_tmp = None
            self._validate_private_file(self.cert_file)
            self._validate_private_file(self.key_file)
        finally:
            for tmp_path in (cert_tmp, key_tmp):
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            
    def _trust_cert_macos(self):
        """Adds the cert to macOS Keychain as trusted (requires sudo/opt-in)."""
        self._validate_private_file(self.cert_file)
        logger.warning(f"Adding {self.cert_file} to trusted root certificates. This may prompt for admin password.")
        try:
            subprocess.run([
                "sudo", "security", "add-trusted-cert", "-d", "-r", "trustRoot",
                "-k", "/Library/Keychains/System.keychain", self.cert_file
            ], check=True)
        except subprocess.CalledProcessError as e:
            logger.error("Failed to trust MITM certificate. Sticky Proxy requires sudo opt-in: %s", e)
            raise PermissionError("Sudo authorization failed for Sticky Proxy.")

    def _handle_client(self, client_socket):
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        
        try:
            secure_client_socket = context.wrap_socket(client_socket, server_side=True)
            request_data = secure_client_socket.recv(8192)
            
            # Simple HTTP parsing to extract JSON payload if any
            # In a real implementation this would stream and parse chunks, but for the MVP:
            try:
                headers, body = request_data.split(b"\r\n\r\n", 1)
                if b"application/json" in headers and body:
                    # Send payload to PrivacyEngine/Vault (simulated here)
                    logger.info("Intercepted API payload length: %d", len(body))
                    # TODO: Pass to PrivacyEngine for Cayley Rotation / indexing
            except Exception:
                pass

            # Forward to real upstream
            upstream_context = ssl.create_default_context()
            with socket.create_connection((self.upstream_host, self.upstream_port)) as upstream_sock:
                with upstream_context.wrap_socket(upstream_sock, server_hostname=self.upstream_host) as secure_upstream:
                    secure_upstream.sendall(request_data)
                    
                    # Read response and forward back
                    while True:
                        resp_data = secure_upstream.recv(8192)
                        if not resp_data:
                            break
                        secure_client_socket.sendall(resp_data)
                        
        except Exception as e:
            logger.debug("MITM connection handling error: %s", e)
        finally:
            client_socket.close()

    def start(self):
        self._generate_self_signed_cert()
        self._trust_cert_macos()
        
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server_socket.bind(("127.0.0.1", self.port))
            self._server_socket.listen(5)
            self._running = True
            logger.info("Sticky Proxy listening on 127.0.0.1:%d", self.port)
            
            def accept_loop():
                while self._running:
                    try:
                        client_sock, addr = self._server_socket.accept()
                        threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
                    except Exception:
                        break
                        
            threading.Thread(target=accept_loop, daemon=True).start()
        except PermissionError:
            logger.error(f"Cannot bind to port {self.port} without root privileges.")
            raise

    def stop(self):
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
