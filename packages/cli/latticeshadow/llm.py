"""
Ephemeral LLM client for LatticeShadow.

Created, used, discarded. Never resident in memory.
Uses raw urllib.request — zero pip dependencies.
All providers use OpenAI-compatible /v1/chat/completions endpoints.
"""

import json
import urllib.request
import urllib.error
import logging
import hashlib

from latticeshadow import config, keychain

logger = logging.getLogger("shadowd")

# Keychain service name for API keys (separate from master key)
_APIKEY_SERVICE_PREFIX = "com.latticedb.shadow.apikey"


class LLMError(Exception):
    """Raised when an LLM call fails."""
    pass


class ShadowLLM:
    """
    Ephemeral LLM client. Created per-call or per-batch, then garbage collected.

    Usage:
        llm = ShadowLLM.from_config()
        if llm:
            answer = llm.complete("You are helpful.", "What is 2+2?")
            # llm goes out of scope → zero RAM
    """

    def __init__(self, endpoint: str, model: str, api_key: str | None = None,
                 timeout: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, system: str, user: str, temperature: float = 0.3) -> str:
        """
        Single chat completion. Returns the response text.

        Raises LLMError on failure.
        """
        url = f"{self.endpoint}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
                result = body["choices"][0]["message"]["content"].strip()
                try:
                    from latticeshadow.audit_log import append_audit_event

                    append_audit_event(
                        "model_call",
                        {
                            "model": self.model,
                            "endpoint": self.endpoint,
                            "system_hash": hashlib.sha256(system.encode("utf-8", errors="replace")).hexdigest(),
                            "user_hash": hashlib.sha256(user.encode("utf-8", errors="replace")).hexdigest(),
                            "response_hash": hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest(),
                        },
                        data_dir=config.get_data_dir(),
                    )
                except Exception:
                    pass
                return result
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise LLMError(
                f"LLM API returned HTTP {e.code}: {error_body[:200]}"
            ) from e
        except urllib.error.URLError as e:
            raise LLMError(f"Failed to connect to LLM at {url}: {e.reason}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"Unexpected LLM response format: {e}") from e

    def is_reachable(self) -> bool:
        """Quick check if the endpoint is reachable (for local providers)."""
        url = f"{self.endpoint}/models"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False

    @classmethod
    def from_config(cls) -> "ShadowLLM | None":
        """
        Create an LLM client from config.toml + Keychain.

        Returns None if provider is 'none' or not configured. A local provider
        must be selected explicitly, just like a remote provider.
        """
        cfg = config.load_config()
        memory = cfg.get("memory", {})
        provider = memory.get("provider", "none")

        if provider == "none":
            return None

        # Get endpoint URL
        endpoints = memory.get("endpoints", config.DEFAULTS["memory"]["endpoints"])
        endpoint = endpoints.get(provider)
        if not endpoint:
            logger.warning("No endpoint configured for provider '%s'", provider)
            return None

        # Ensure endpoint ends with /v1 or similar
        if not endpoint.rstrip("/").endswith("/v1"):
            endpoint = endpoint.rstrip("/")

        # Get model
        model = memory.get("model") or config.DEFAULT_MODELS.get(provider, "default")

        # Get API key from Keychain (remote providers only)
        api_key = None
        if provider in ("gemini", "openai"):
            try:
                api_key = _get_api_key(provider)
            except Exception:
                pass
            if not api_key:
                logger.warning(
                    "No API key found for '%s'. Run: shadow config set-key %s <your-key>",
                    provider, provider,
                )
                return None

        return cls(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout=30.0 if provider in ("gemini", "openai") else 60.0,
        )


def store_api_key(provider: str, api_key: str) -> None:
    """Store an API key in the macOS Keychain."""
    import subprocess
    import platform

    if platform.system() != "Darwin":
        raise LLMError("Keychain is only available on macOS")

    service = f"{_APIKEY_SERVICE_PREFIX}.{provider}"
    result = subprocess.run(
        [
            "/usr/bin/security", "add-generic-password",
            "-a", provider,
            "-s", service,
            "-w", api_key,
            "-U",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise LLMError(f"Failed to store API key: {result.stderr.strip()}")


def _get_api_key(provider: str) -> str | None:
    """Retrieve an API key from the macOS Keychain."""
    import subprocess
    import platform

    if platform.system() != "Darwin":
        return None

    service = f"{_APIKEY_SERVICE_PREFIX}.{provider}"
    result = subprocess.run(
        [
            "/usr/bin/security", "find-generic-password",
            "-a", provider,
            "-s", service,
            "-w",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()
