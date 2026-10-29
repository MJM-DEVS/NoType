"""
NoType – Whisper Transcription Engine
Uses faster-whisper with CUDA for local speech-to-text.
"""

import gc
import logging
import os
import threading
import numpy as np
from faster_whisper import WhisperModel, BatchedInferencePipeline
from faster_whisper.utils import download_model
from faster_whisper.vad import VadOptions

import hallucinations

logger = logging.getLogger("NoType.Transcriber")

SAMPLE_RATE = 16000

# ── Language policy ──────────────────────────────────────────────────
# The user lists the languages they speak, optionally with shares ("80 %
# German, 15 % English, 5 % Polish"). Whisper's own detection picks the most
# probable of all 99 languages, and on short or noisy audio that regularly is
# one the user never speaks (Russian, French, Portuguese …). Instead, its
# probabilities are weighed:
# - between the user's languages only lightly (share ** 0.3), so the shares
#   decide close calls but a clearly English sentence stays English
# - every other language counts five times less, or not at all when the user
#   turned "also recognize other languages" off
SHARE_EXPONENT = 0.3
OTHER_WEIGHT = 0.2
# Whisper decides the language from the first 30 s of speech – no need to run
# the VAD over a five-minute dictation twice for that.
DETECT_SECONDS = 40


def normalize_languages(entries, fallback: str = "de") -> list:
    """Config entries → [(code, share)] with shares summing to 1, highest
    first. Shares are percentages and optional: the ones left empty split
    what's left of 100 (80 + 15 + empty → 5), at least 1 each. They don't
    have to add up to 100. Plain codes ("de") are accepted, too. The
    settings window mirrors this in effectiveShares()."""
    shares = {}
    for e in entries or []:
        code, share = (e, None) if isinstance(e, str) else (
            (e.get("code"), e.get("share")) if isinstance(e, dict) else (None, None))
        code = str(code or "").strip().lower()
        if not code or code in shares:
            continue
        try:
            shares[code] = max(float(share), 1.0)
        except (TypeError, ValueError):
            shares[code] = None
    if not shares:
        return [(fallback or "de", 1.0)]
    given = [s for s in shares.values() if s is not None]
    missing = len(shares) - len(given)
    default = max((100.0 - sum(given)) / missing, 1.0) if missing else 0.0
    shares = {c: (s if s is not None else default) for c, s in shares.items()}
    total = sum(shares.values())
    return sorted(((c, s / total) for c, s in shares.items()), key=lambda x: -x[1])


def pick_language(probs: dict, languages: list, allow_other: bool) -> str:
    """The language to decode with: Whisper's probabilities weighed by the
    user's languages (see above)."""
    weights = {code: share ** SHARE_EXPONENT for code, share in languages}
    best, best_score = languages[0][0], -1.0
    for code in (set(probs) | set(weights)) if allow_other else weights:
        score = probs.get(code, 0.0) * weights.get(code, OTHER_WEIGHT)
        if score > best_score:
            best, best_score = code, score
    return best


# Long dictations are decoded in parallel batches (BatchedInferencePipeline,
# ~1.7× faster on 80 s). Below this length a single 30 s window does the job.
BATCH_MIN_SECONDS = 30
BATCH_SIZE = 8
# The batched decoder skips Whisper's anti-hallucination guards. They are
# re-applied per chunk afterwards with the same thresholds as the normal path.
NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4

# Curated catalog of local open-weight models – all served by the same
# faster-whisper/CTranslate2 engine (no extra runtimes). `repo` is what
# WhisperModel() loads (built-in name or Hugging Face repo id). `lang` pins
# single-language models to their language regardless of the UI setting.
# Benchmarked against alternatives (NVIDIA Parakeet TDT v3, Canary 1B v2 /
# Flash via onnx-asr, CrisperWhisper): none beat large-v3-turbo on German or
# mixed DE/EN dictation on consumer GPUs, so they are deliberately absent.
MODEL_CATALOG = {
    "tiny":              {"repo": "tiny",           "lang": None},
    "base":              {"repo": "base",           "lang": None},
    "small":             {"repo": "small",          "lang": None},
    "medium":            {"repo": "medium",         "lang": None},
    "large-v2":          {"repo": "large-v2",       "lang": None},
    "large-v3":          {"repo": "large-v3",       "lang": None},
    "large-v3-turbo":    {"repo": "large-v3-turbo", "lang": None},
    "distil-large-v3":   {"repo": "distil-large-v3", "lang": "en"},
    "distil-large-v3.5": {"repo": "distil-whisper/distil-large-v3.5-ct2", "lang": "en"},
    "german-turbo":      {"repo": "jimmymeister/whisper-large-v3-turbo-german-ct2", "lang": "de"},
}

MODEL_SIZES = list(MODEL_CATALOG)

# Compute type options (user-facing labels → faster-whisper values)
COMPUTE_TYPES = {
    "cuda": ["int8_float16", "float16", "int8"],
    "cpu": ["int8", "float32"],
}


class Transcriber:
    # Class-level cache so reopening the settings panel or saving an unchanged
    # model_size doesn't reload the model from disk. Keyed by the trio that
    # uniquely identifies a loaded WhisperModel instance. Holds ONE model:
    # switching models drops the old one first, so RAM/VRAM never carries
    # two. The lock prevents two concurrent load_model() calls from racing
    # the dict.
    _model_cache: dict = {}
    _cache_lock = threading.Lock()
    # SERIALISES every call to `self._model.transcribe(...)`. faster-whisper's
    # underlying ctranslate2 model is NOT thread-safe – running the live-preview
    # pass and the final pass concurrently on the same CUDA model produces
    # 10-20× slowdowns (GPU stall) and occasional crashes. All inference paths
    # must acquire this lock before calling .transcribe().
    inference_lock = threading.Lock()

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "auto"):
        """
        Initialize the Whisper transcription engine.

        Args:
            model_size: Whisper model size (tiny/base/small/medium/large-v3)
            device: "cuda", "cpu", or "auto" (auto-detect)
            compute_type: "int8_float16" (fastest GPU), "float16", "int8", or "auto"
        """
        self.model_size = model_size
        entry = MODEL_CATALOG.get(model_size, {"repo": model_size, "lang": None})
        self._repo = entry["repo"]
        # Single-language models ignore the configured/auto language.
        self.forced_language = entry["lang"]
        self._model = None
        self._batched = None

        # Auto-detect device
        if device == "auto":
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    self.device = "cuda"
                    self.compute_type = "float16" if compute_type == "auto" else compute_type
                else:
                    self.device = "cpu"
                    self.compute_type = "int8" if compute_type == "auto" else compute_type
            except Exception:
                self.device = "cpu"
                self.compute_type = "int8" if compute_type == "auto" else compute_type
        else:
            self.device = device
            if compute_type == "auto":
                self.compute_type = "float16" if device == "cuda" else "int8"
            else:
                self.compute_type = compute_type

        logger.info(f"Transcriber: device={self.device}, compute_type={self.compute_type}")

    def _local_model_path(self):
        """Folder of the already downloaded model, or None.

        WhisperModel(repo_id) asks the Hugging Face Hub for the latest
        revision on every load, even when all files are cached. Loading the
        local snapshot folder instead keeps a downloaded model fully offline.
        A snapshot without model.bin is an interrupted download, which the
        normal repo_id path then finishes."""
        try:
            path = download_model(self._repo, local_files_only=True)
        except Exception:
            return None
        return path if os.path.isfile(os.path.join(path, "model.bin")) else None

    def load_model(self, on_progress=None):
        """Load the Whisper model. Downloads on first use, afterwards it
        loads from the local files without contacting the Hub.

        Cached at the class level: a subsequent call with the same
        (model_size, device, compute_type) reuses the existing WhisperModel
        instead of re-reading it from disk.
        """
        cache_key = (self._repo, self.device, self.compute_type)
        with Transcriber._cache_lock:
            cached = Transcriber._model_cache.get(cache_key)
            evict = cached is None and bool(Transcriber._model_cache)
            if evict:
                Transcriber._model_cache.clear()
        if evict:
            # Free the previous model before the new one loads (it is gone
            # once no Transcriber holds it any more).
            gc.collect()
        if cached is not None:
            self._model = cached
            logger.info(f"Model cache hit: {self.model_size} / {self.device} / {self.compute_type}")
            if on_progress:
                on_progress("Modell aus Cache geladen ✓")
            return

        if on_progress:
            on_progress(f"Lade Modell '{self.model_size}' ({self.device})...")

        source = self._local_model_path()
        logger.info(f"Loading model: {self.model_size} on {self.device} "
                    f"({'local files' if source else 'download from Hugging Face'})")
        source = source or self._repo

        try:
            self._model = WhisperModel(
                source,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as e:
            # int8_float16 not supported on older GPUs → fallback to float16
            if self.compute_type == "int8_float16" and self.device == "cuda":
                logger.warning(f"int8_float16 not supported, falling back to float16: {e}")
                self.compute_type = "float16"
                if on_progress:
                    on_progress("int8_float16 nicht unterstützt, nutze float16...")
                self._model = WhisperModel(
                    source,
                    device=self.device,
                    compute_type="float16",
                )
                cache_key = (self._repo, self.device, self.compute_type)
            else:
                raise

        with Transcriber._cache_lock:
            Transcriber._model_cache.clear()
            Transcriber._model_cache[cache_key] = self._model

        if on_progress:
            on_progress("Modell geladen ✓")

        logger.info(f"Model loaded successfully (compute_type={self.compute_type})")

    def _get_beam_size(self, audio_duration: float, beam_size_setting: str = "auto") -> int:
        """
        Determine beam size based on audio duration and user setting.

        Dynamic beam sizing:
        - Short (<10s): beam=1 (greedy, fastest, fine for short phrases)
        - Medium (10-30s): beam=3 (balanced)
        - Long (>30s): beam=5 (best quality for complex text)

        A number outside 1..10 or garbage in the config counts as "auto".
        """
        if beam_size_setting != "auto":
            try:
                beam = int(beam_size_setting)
            except (TypeError, ValueError):
                beam = 0
            if 1 <= beam <= 10:
                return beam
            logger.warning(f"Invalid beam_size {beam_size_setting!r}, using auto")
        if audio_duration < 10:
            return 1
        elif audio_duration < 30:
            return 3
        else:
            return 5

    def _choose_language(self, audio, languages, allow_other: bool,
                         vad_filter: bool, vad_sensitivity: int):
        """Language code to decode with (None = Whisper decides on its own).
        Must run under inference_lock – it uses the model."""
        if self.forced_language:
            return self.forced_language
        if not languages:
            return None
        if len(languages) == 1 and not allow_other:
            return languages[0][0]
        try:
            _, _, all_probs = self._model.detect_language(
                audio[:DETECT_SECONDS * SAMPLE_RATE], vad_filter=vad_filter,
                vad_parameters=VadOptions(min_silence_duration_ms=vad_sensitivity,
                                          speech_pad_ms=150),
            )
        except Exception as e:
            # Typically no speech left after the VAD – the decode comes back
            # empty anyway, any language will do.
            logger.info(f"Language detection skipped ({e.__class__.__name__})")
            return languages[0][0]
        probs = dict(all_probs)
        chosen = pick_language(probs, languages, allow_other)
        top = max(probs, key=probs.get)
        logger.info(f"Detected language: {chosen} (prob: {probs.get(chosen, 0.0):.2f})"
                    + (f", Whisper alone: {top} ({probs[top]:.2f})" if top != chosen else ""))
        return chosen

    def _decode(self, audio, language, beam: int, vad_filter: bool,
                vad_sensitivity: int, prompt):
        """The normal, sequential decode with Whisper's guards on."""
        segments, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=beam,
            vad_filter=vad_filter,
            vad_parameters=dict(
                min_silence_duration_ms=vad_sensitivity,
                speech_pad_ms=150,
            ),
            initial_prompt=prompt,
            # Anti-hallucination guards. Whisper invents text on noise and
            # loops on silence ("sehr sehr sehr...") – these thresholds drop
            # such segments instead of inserting them:
            # - no context carry-over, so one hallucinated segment can't
            #   poison every following segment (initial_prompt is unaffected)
            condition_on_previous_text=False,
            # - discard segments whose token stream is repetitive garbage
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            # - discard segments the model itself is unsure about / flags
            #   as non-speech
            log_prob_threshold=LOG_PROB_THRESHOLD,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
        )
        # Force segments to materialise inside the lock – the generator
        # does the actual model work lazily as we iterate it.
        return list(segments), info

    def _decode_batched(self, audio, language, beam: int, vad_sensitivity: int, prompt):
        """Long audio: decode the speech chunks (up to 30 s each) in parallel,
        then re-apply the guards the batched decoder skips. A chunk Whisper
        flags as silence is dropped; a repetitive or unsure one is decoded
        again on the normal path, whose temperature fallback handles it."""
        if self._batched is None or self._batched.model is not self._model:
            self._batched = BatchedInferencePipeline(self._model)
        segments, info = self._batched.transcribe(
            audio,
            language=language,
            beam_size=beam,
            batch_size=BATCH_SIZE,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=vad_sensitivity, speech_pad_ms=150),
            initial_prompt=prompt,
            # Sentence-level segments with timestamps (for the hallucination
            # filter) instead of one block per chunk.
            without_timestamps=False,
        )
        chunks = {}
        for seg in segments:
            chunks.setdefault(seg.seek, []).append(seg)

        kept = []
        for chunk in chunks.values():
            # avg_logprob and no_speech_prob are per chunk in batched mode.
            logprob, no_speech = chunk[0].avg_logprob, chunk[0].no_speech_prob
            if no_speech > NO_SPEECH_THRESHOLD and logprob < LOG_PROB_THRESHOLD:
                logger.info(f"Batched: dropped a {chunk[-1].end - chunk[0].start:.1f}s "
                            f"chunk as silence")
                continue
            if (logprob < LOG_PROB_THRESHOLD
                    or any(s.compression_ratio > COMPRESSION_RATIO_THRESHOLD for s in chunk)):
                start = max(0.0, chunk[0].start - 0.3)
                end = chunk[-1].end + 0.3
                logger.info(f"Batched: re-decoding a {end - start:.1f}s chunk "
                            f"(logprob {logprob:.2f})")
                redo, _ = self._decode(audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)],
                                       language, beam, True, vad_sensitivity, prompt)
                for seg in redo:
                    seg.start += start
                    seg.end += start
                kept.extend(redo)
                continue
            kept.extend(chunk)
        return kept, info

    def transcribe(self, audio: np.ndarray, languages=None, allow_other: bool = True,
                   beam_size: str = "auto", vad_sensitivity: int = 300,
                   initial_prompt: str = None, vad_filter: bool = True) -> dict:
        """
        Transcribe audio to text.

        Args:
            languages: [(code, share)] from normalize_languages(): the
                       languages the user speaks. None = Whisper decides alone.
            allow_other: also accept languages outside that list.

        Returns:
            Dict with 'text', 'beam', 'compute_type', 'duration', 'vad_ms',
            'language'.
        """
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        if len(audio) == 0:
            return {"text": "", "beam": 0, "compute_type": self.compute_type,
                    "duration": 0, "vad_ms": vad_sensitivity}

        # From the config: a string or garbage must not fail the dictation.
        try:
            vad_sensitivity = max(int(vad_sensitivity), 0)
        except (TypeError, ValueError):
            vad_sensitivity = 300

        audio_duration = len(audio) / SAMPLE_RATE
        actual_beam = self._get_beam_size(audio_duration, beam_size)
        batched = (vad_filter and self.device == "cuda"
                   and audio_duration >= BATCH_MIN_SECONDS)

        logger.info(f"Transcribing: {audio_duration:.1f}s audio, beam={actual_beam}, "
                    f"compute={self.compute_type}, vad_ms={vad_sensitivity}"
                    + (", batched" if batched else ""))

        # `initial_prompt` biases the model toward custom vocabulary (product
        # names, jargon, etc.) – the simplest form of vocabulary customization
        # without fine-tuning.
        prompt = initial_prompt.strip() if initial_prompt else None

        # Inference is serialised via the class-level lock – see comment above.
        def _run_inference():
            with Transcriber.inference_lock:
                language = self._choose_language(audio, languages, allow_other,
                                                 vad_filter, vad_sensitivity)
                if batched:
                    try:
                        return self._decode_batched(audio, language, actual_beam,
                                                    vad_sensitivity, prompt)
                    except RuntimeError as e:
                        # cuBLAS: handled below. Anything else (e.g. out of
                        # VRAM next to the LLM): the sequential path still works.
                        if "CUBLAS" in str(e).upper():
                            raise
                        logger.warning(f"Batched decode failed, decoding sequentially: {e}")
                return self._decode(audio, language, actual_beam, vad_filter,
                                    vad_sensitivity, prompt)

        compute_fallback = False
        try:
            segments, info = _run_inference()
        except RuntimeError as e:
            # int8 GEMM can be unsupported at INFERENCE time even though the
            # model LOADED fine (e.g. Blackwell GPUs + int8_float16 raise
            # CUBLAS_STATUS_NOT_SUPPORTED on the first matmul). Reload as
            # float16 and retry once instead of failing the dictation.
            if "CUBLAS" not in str(e).upper() or self.compute_type == "float16":
                raise
            logger.warning(f"cuBLAS rejected compute_type={self.compute_type}, "
                           f"falling back to float16: {e}")
            with Transcriber._cache_lock:
                Transcriber._model_cache.pop(
                    (self._repo, self.device, self.compute_type), None)
            self._model = None
            self._batched = None  # holds the old model, too
            self.compute_type = "float16"
            self.load_model()
            compute_fallback = True
            segments, info = _run_inference()

        # Known hallucinations (subtitle credits, video outros on noise) out.
        pieces = hallucinations.filter_segments(segments, audio)
        dropped = sum(1 for s in segments if s.text.strip()) - len(pieces)
        if dropped > 0:
            logger.info(f"Dropped {dropped} hallucinated segment(s)")
        result = " ".join(pieces).strip()

        if info and info.language and not languages:
            logger.info(f"Detected language: {info.language} "
                        f"(prob: {info.language_probability:.2f})")

        return {
            "text": result,
            "beam": actual_beam,
            "compute_type": self.compute_type,
            # True when this call had to downgrade to float16 – the caller
            # should persist that so future sessions skip the failed attempt.
            "compute_fallback": compute_fallback,
            "duration": round(audio_duration, 1),
            "vad_ms": vad_sensitivity,
            # Whisper's language code for this audio (detected, or the one
            # it was told to use), None if it didn't say.
            "language": (info.language if info else None) or None,
        }

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def get_device_info(self) -> str:
        """Return a string describing the current device setup."""
        return f"{self.device.upper()} ({self.compute_type})"

