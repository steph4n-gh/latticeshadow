from __future__ import annotations

import re


class PiiScrubber:
    """
    Local, edge-side PII (Personally Identifiable Information) Scrubber.
    Uses robust regular expressions to sanitize text on the local device
    before it is sent to public cloud APIs.
    """

    def __init__(self):
        # Dictionary of compiled regex patterns and their replacement tokens
        self.patterns = {
            "EMAIL": re.compile(
                r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
            ),
            "PHONE": re.compile(
                r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
            ),
            "SSN": re.compile(
                r"\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b"
            ),
            "DATE": re.compile(
                r"\b(?:\d{4}[-.\s]\d{2}[-.\s]\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b"
                r"|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b",
                re.IGNORECASE
            ),
            "NAME": re.compile(
                r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.|Prof\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"
            ),
            "IP_ADDRESS": re.compile(
                r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
                r"|\b[0-9a-fA-F]{1,4}:(?:[0-9a-fA-F]{1,4}:){1,6}[0-9a-fA-F]{1,4}\b"
            ),
            "CREDIT_CARD": re.compile(
                r"\b(?:\d{4}[-.\s]?){3}\d{4}\b"
            ),
        }

    def scrub(self, text: str) -> str:
        """
        Scrubs all matching PII patterns in the text, replacing them with
        placeholder tokens (e.g. [REDACTED_EMAIL]).

        Args:
            text: The raw text string to sanitize.

        Returns:
            The sanitized text string.
        """
        if not text:
            return text

        scrubbed = text
        for label, pattern in self.patterns.items():
            replacement = f"[REDACTED_{label}]"
            scrubbed = pattern.sub(replacement, scrubbed)

        return scrubbed

    def add_pattern(self, label: str, pattern_str: str, flags: int = 0) -> None:
        """
        Allows developers to add custom PII patterns dynamically.

        Args:
            label: The name/label of the PII category (e.g. 'PATIENT_ID').
            pattern_str: The regex pattern string.
            flags: Regex compilation flags (e.g. re.IGNORECASE).
        """
        self.patterns[label.upper()] = re.compile(pattern_str, flags)
