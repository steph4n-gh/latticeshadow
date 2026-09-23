"""
Dream Cycle — LLM enrichment during REM Sleep consolidation.

The dreamer is ephemeral: it wakes, processes a batch of clips through
the LLM, stores enrichments (tags, categories, summaries) in metadata,
and then exits. The LLM is never resident in memory.
"""

import json
import logging

from latticeshadow import config
from latticeshadow.llm import ShadowLLM, LLMError
from latticeshadow.sensitivity import classify, redact

logger = logging.getLogger("shadowd")

ENRICHMENT_SYSTEM_PROMPT = """You are a clipboard analysis agent. For each clipboard entry, return a JSON object with:
- "tags": list of 1-3 short descriptive tags
- "category": one of ["code", "config", "url", "message", "credential", "data", "command", "other"]
- "summary": one concise sentence describing what this clipboard entry is

Respond with a JSON array containing one object per entry. Only return the JSON array, nothing else."""


def dream_cycle(vault, log=None):
    """
    Process unprocessed clips through the LLM.

    Called during REM Sleep consolidation. The LLM client is created,
    used for one batch, and then garbage collected — zero persistent RAM.

    Args:
        vault: A Collection object with get_undreamed/update_metadata methods.
        log: Optional logger. Falls back to module logger.
    """
    log = log or logger

    cfg = config.load_config()
    memory_cfg = cfg.get("memory", {})

    # LLM enrichment requires an explicitly selected provider.
    provider = memory_cfg.get("provider", "none")
    llm = ShadowLLM.from_config()
    if llm is None:
        if provider != "none":
            log.warning("Dream cycle: LLM not available (provider=%s)", provider)
        return

    batch_size = memory_cfg.get("dream_batch_size", 50)
    use_sensitivity_filter = memory_cfg.get("sensitivity_filter", True)

    # 1. Get un-dreamed clips
    unprocessed = vault.get_undreamed(limit=batch_size)
    if not unprocessed:
        log.debug("Dream cycle: no unprocessed clips.")
        return

    log.info("Dream cycle: processing %d clips...", len(unprocessed))

    # 2. Classify and filter
    safe_clips = []  # (clip_dict, text_for_llm)
    sensitive_count = 0

    for clip in unprocessed:
        text = clip["document"]
        if not text or len(text.strip()) < 3:
            # Mark trivial clips as dreamed without LLM
            vault.update_metadata(clip["doc_id"], {"dreamed": True})
            continue

        if use_sensitivity_filter:
            sensitivity = classify(text)
            if sensitivity == "sensitive":
                # Mark as dreamed with minimal metadata (no LLM contact)
                vault.update_metadata(clip["doc_id"], {
                    "dreamed": True,
                    "tags": ["sensitive"],
                    "category": "credential",
                    "summary": "(sensitive content — not sent to LLM)",
                })
                sensitive_count += 1
                continue
            elif sensitivity == "unknown":
                text = redact(text)

        safe_clips.append((clip, text))

    if sensitive_count > 0:
        log.info("Dream cycle: skipped %d sensitive clips.", sensitive_count)

    if not safe_clips:
        log.info("Dream cycle: all clips were sensitive or trivial.")
        return

    # 3. Build batch prompt
    prompt_lines = []
    for i, (clip, text) in enumerate(safe_clips, 1):
        # Truncate very long clips for the prompt
        display_text = text[:500] + "..." if len(text) > 500 else text
        prompt_lines.append(f"Entry {i}: {display_text}")

    prompt = "\n\n".join(prompt_lines)

    # 4. Single LLM call for the entire batch
    try:
        response = llm.complete(
            system=ENRICHMENT_SYSTEM_PROMPT,
            user=prompt,
            temperature=0.2,
        )
        enrichments = _parse_enrichment_response(response, len(safe_clips))
    except LLMError as e:
        log.warning("Dream cycle LLM call failed: %s", e)
        # Mark all as dreamed anyway so we don't retry forever
        for clip, _ in safe_clips:
            vault.update_metadata(clip["doc_id"], {
                "dreamed": True,
                "tags": [],
                "category": "other",
                "summary": "(LLM enrichment failed)",
            })
        return

    # 5. Store enrichments in metadata
    for (clip, _), enrichment in zip(safe_clips, enrichments):
        vault.update_metadata(clip["doc_id"], {
            "dreamed": True,
            "tags": enrichment.get("tags", []),
            "category": enrichment.get("category", "other"),
            "summary": enrichment.get("summary", ""),
        })

    log.info(
        "Dream cycle complete: enriched %d clips (%d sensitive skipped).",
        len(safe_clips), sensitive_count,
    )
    # LLM goes out of scope here → garbage collected → zero RAM


def _parse_enrichment_response(response: str, expected_count: int) -> list:
    """
    Parse the LLM's JSON response into a list of enrichment dicts.

    Handles common LLM response quirks:
    - Markdown code fences (```json ... ```)
    - Partial responses
    - Invalid JSON
    """
    # Strip markdown code fences if present
    text = response.strip()
    if text.startswith("```"):
        # Remove first line (```json) and last line (```)
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            # Pad with empty enrichments if LLM returned fewer than expected
            while len(parsed) < expected_count:
                parsed.append({"tags": [], "category": "other", "summary": ""})
            return parsed[:expected_count]
        elif isinstance(parsed, dict):
            # Single object — wrap in list
            return [parsed] + [{"tags": [], "category": "other", "summary": ""}] * (expected_count - 1)
    except json.JSONDecodeError:
        pass

    # Fallback: return empty enrichments
    return [{"tags": [], "category": "other", "summary": ""}] * expected_count
