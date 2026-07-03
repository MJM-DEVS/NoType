"""
NoType – Transcript Post-Processing
Cleans up the raw Whisper output before it is inserted.

Two tiers, selected via config `cleanup_mode`:
  "off"  – raw Whisper text, untouched
  "fast" – rule-based pass (filler words, stutter repeats), effectively 0 ms
  "ai"   – local LLM rewrite via Ollama (http://127.0.0.1:11434). Falls back
           to the "fast" result silently when Ollama is not running, the model
           is missing, or the request exceeds the latency budget. Speed always
           wins over polish.
"""

import json
import logging
import re
import time
import urllib.request
import urllib.error

logger = logging.getLogger("NoType.PostProcess")

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_LLM_MODEL = "qwen2.5:3b-instruct"

# Hard latency budget for the LLM pass. If Ollama can't answer in time we
# insert the rule-cleaned text instead – the user must never wait on the LLM.
LLM_TIMEOUT_S = 2.5
# How long a "is Ollama up / is the model pulled" verdict stays cached.
AVAILABILITY_TTL_S = 60.0

# Standalone filler words. Word-boundary matched, case-insensitive; an
# immediately following comma/period is removed with the filler.
_FILLER_RE = re.compile(
    r"\s*\b(?:ähm+|äh+|ehm+|öhm+|hmm+|mhm+|uhm+|umm+|uh|erm+)\b[,.]?",
    re.IGNORECASE,
)

# 3+ consecutive identical words ("sehr sehr sehr sehr") collapse to one.
# Exactly two repeats are kept – "sehr sehr gut" is legitimate German.
_STUTTER_RE = re.compile(r"\b(\w+)(?:\s+\1\b){2,}", re.IGNORECASE | re.UNICODE)

_LLM_SYSTEM_PROMPT = (
    "You clean up dictated text from speech recognition. "
    "Remove filler words (äh, ähm, um, uh, ...), fix obvious recognition "
    "errors and punctuation. Keep the original language and wording exactly, "
    "including mixed German/English technical terms. Never translate, "
    "summarize, answer, or add anything. Output ONLY the cleaned text."
)

# Cached Ollama availability: (checked_at, available: bool)
_availability = (0.0, False)


def reset_availability_cache() -> None:
    """Forget the cached Ollama verdict – call right after starting/stopping
    the server so the next cleanup re-probes immediately."""
    global _availability
    _availability = (0.0, False)


def rule_clean(text: str) -> str:
    """Instant rule-based cleanup: fillers, stutter loops, spacing."""
    cleaned = _FILLER_RE.sub("", text)
    cleaned = _STUTTER_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"^[,.;:\s]+", "", cleaned)
    cleaned = cleaned.strip()
    # Removing a leading filler ("Ähm, das...") must not leave the sentence
    # starting lowercase.
    if cleaned and text and text[0].isupper() and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _ollama_available(model: str) -> bool:
    """True if Ollama answers on localhost AND has `model` pulled. Cached."""
    global _availability
    checked_at, available = _availability
    if time.monotonic() - checked_at < AVAILABILITY_TTL_S:
        return available

    result = False
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=0.4) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in tags.get("models", [])]
        # "qwen2.5:3b-instruct" also matches "qwen2.5:3b-instruct-q4_K_M"
        result = any(n == model or n.startswith(model) for n in names)
        if not result and names:
            logger.info(f"Ollama up, but model '{model}' not pulled (have: {names})")
    except Exception:
        result = False

    _availability = (time.monotonic(), result)
    return result


def warm_up(model: str = DEFAULT_LLM_MODEL) -> None:
    """Preload the LLM into VRAM so the first real cleanup isn't a cold start.
    Call from a background thread at app start / when 'ai' mode is enabled."""
    if not _ollama_available(model):
        return
    try:
        payload = json.dumps({
            "model": model,
            "prompt": "",
            "keep_alive": "30m",
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_URL + "/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        logger.info(f"LLM '{model}' warmed up")
    except Exception as e:
        logger.warning(f"LLM warm-up failed: {e}")


def _llm_clean(text: str, model: str) -> str | None:
    """One-shot cleanup via Ollama. Returns None on any failure or if the
    output looks like the model rewrote/answered instead of cleaning."""
    payload = json.dumps({
        "model": model,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0,
            # Cleanup never legitimately produces more tokens than the input;
            # cap generation so a runaway answer can't blow the time budget.
            "num_predict": max(64, len(text) // 2),
        },
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.info(f"LLM cleanup skipped ({e.__class__.__name__}), using rules")
        return None

    out = (data.get("message") or {}).get("content", "").strip()
    # Models sometimes wrap the result in quotes – unwrap a single full wrap.
    if len(out) > 1 and out[0] == out[-1] and out[0] in "\"'„“":
        out = out[1:-1].strip()

    # Sanity gate: the cleaned text must stay close to the input length.
    # A big deviation means the model summarized, answered, or hallucinated.
    if not out or not (0.4 <= len(out) / max(len(text), 1) <= 1.5):
        logger.info("LLM output failed sanity check, using rules")
        return None
    return out


def clean_transcript(text: str, mode: str = "fast",
                     llm_model: str = DEFAULT_LLM_MODEL,
                     llm_min_words: int = 8) -> dict:
    """Clean a transcript according to `mode`.

    Returns {"text": str, "engine": "off"|"rules"|"llm", "ms": int}.
    Never raises and never returns empty text for non-empty input.
    """
    start = time.perf_counter()
    if mode == "off" or not text.strip():
        return {"text": text, "engine": "off", "ms": 0}

    ruled = rule_clean(text) or text
    engine, result = "rules", ruled

    # Short dictations don't benefit from an LLM pass – the rules already
    # handle fillers, and ~300 ms extra latency on a 5-word command is worse
    # than a marginally smoother sentence.
    wants_llm = mode == "ai" and len(ruled.split()) >= llm_min_words
    if wants_llm and _ollama_available(llm_model):
        llm_out = _llm_clean(ruled, llm_model)
        if llm_out:
            engine, result = "llm", llm_out

    ms = int((time.perf_counter() - start) * 1000)
    if result != text:
        logger.info(f"Cleanup ({engine}, {ms}ms): '{text[:60]}' -> '{result[:60]}'")
    return {"text": result, "engine": engine, "ms": ms}
