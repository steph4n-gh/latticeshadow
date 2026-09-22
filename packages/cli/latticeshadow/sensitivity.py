"""
Sensitivity classifier for LatticeShadow.

Runs BEFORE any clipboard text is sent to a remote LLM.
Pure regex + heuristics — no model, no dependencies, no network calls.
"""

import re

# Patterns that indicate sensitive/secret content
_SENSITIVE_PATTERNS = [
    # API Keys / Tokens (generic)
    re.compile(r"(?:api[_\-]?key|api[_\-]?secret|auth[_\-]?token|access[_\-]?token|bearer)\s*[=:]\s*\S{8,}", re.IGNORECASE),
    # AWS credentials
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?:aws_secret_access_key|AWS_SECRET)\s*[=:]\s*\S+", re.IGNORECASE),
    # Private keys (PEM format)
    re.compile(r"-----BEGIN\s+(?:RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE KEY-----"),
    # Connection strings with embedded passwords
    re.compile(r"(?:postgres|postgresql|mysql|redis|mongodb|amqp|mssql)://\S+:\S+@\S+"),
    # JWT tokens (header.payload.signature)
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
    # SSH keys
    re.compile(r"ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{40,}"),
    # Generic password assignments
    re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*\S{4,}", re.IGNORECASE),
    # GitHub / GitLab tokens
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),
    re.compile(r"glpat-[A-Za-z0-9\-]{20,}"),
    # Slack tokens
    re.compile(r"xox[bpors]-[A-Za-z0-9\-]{10,}"),
    # Stripe keys
    re.compile(r"(?:sk|pk)_(?:test|live)_[A-Za-z0-9]{20,}"),
    # OpenAI keys
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # Google API keys
    re.compile(r"AIza[A-Za-z0-9\-_]{35}"),
    # Hex secrets (64+ char hex strings that look like keys)
    re.compile(r"(?:secret|key|token)\s*[=:]\s*[0-9a-fA-F]{32,}", re.IGNORECASE),
]

# Pattern to match sensitive substrings for redaction
_REDACT_PATTERN = re.compile(
    r"("
    r"AKIA[0-9A-Z]{16}"
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"
    r"|glpat-[A-Za-z0-9\-]{20,}"
    r"|xox[bpors]-[A-Za-z0-9\-]{10,}"
    r"|(?:sk|pk)_(?:test|live)_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9\-_]{35}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"
    r"|ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{40,}"
    r"|-----BEGIN\s+(?:RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE KEY-----[\s\S]*?-----END\s+\w+\s*PRIVATE KEY-----"
    r")"
)


def classify(text: str) -> str:
    """
    Classify clipboard text as 'safe', 'sensitive', or 'unknown'.

    - 'sensitive': Contains known secret patterns. Never send to remote API.
    - 'safe': Looks like normal text (code, URLs, prose). OK to send.
    - 'unknown': Can't determine. Send with redaction applied.

    Returns:
        'safe', 'sensitive', or 'unknown'
    """
    if not text or len(text) < 3:
        return "safe"

    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return "sensitive"

    # Heuristic: if it looks like a long random string with no spaces, be cautious
    if len(text) > 32 and " " not in text and re.match(r"^[A-Za-z0-9+/=_\-]+$", text):
        return "unknown"

    return "safe"


def redact(text: str) -> str:
    """
    Replace sensitive patterns with [REDACTED] for safe LLM processing.

    Used for 'unknown' classified text where we want the LLM to understand
    the context but not see the actual secrets.
    """
    result = _REDACT_PATTERN.sub("[REDACTED]", text)

    # Also redact password values in key=value patterns
    result = re.sub(
        r"((?:api[_\-]?key|password|passwd|pwd|secret|token)\s*[=:]\s*)\S+",
        r"\1[REDACTED]",
        result,
        flags=re.IGNORECASE,
    )

    return result
