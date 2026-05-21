"""
NoType – Whisper Transcription Engine
Uses faster-whisper with CUDA for local speech-to-text.
"""

import logging
import threading
import numpy as np
from faster_whisper import WhisperModel

logger = logging.getLogger("NoType.Transcriber")

# Map of our language codes to Whisper language codes
LANGUAGE_MAP = {
    "de": "de",  # German
    "en": "en",  # English
    "pl": "pl",  # Polish
    "hr": "hr",  # Croatian
}

MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]

# Compute type options (user-facing labels → faster-whisper values)
COMPUTE_TYPES = {
    "cuda": ["int8_float16", "float16", "int8"],
    "cpu": ["int8", "float32"],
}


class Transcriber:
    # Class-level cache so reopening the settings panel or saving an unchanged
    # model_size doesn't reload the model from disk. Keyed by the trio that
    # uniquely identifies a loaded WhisperModel instance. The lock prevents two
    # concurrent load_model() calls from racing the dict.
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
        self._model = None

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

    def load_model(self, on_progress=None):
        """Load the Whisper model. Downloads on first use.

        Cached at the class level: a subsequent call with the same
        (model_size, device, compute_type) reuses the existing WhisperModel
        instead of re-reading it from disk.
        """
        cache_key = (self.model_size, self.device, self.compute_type)
        with Transcriber._cache_lock:
            cached = Transcriber._model_cache.get(cache_key)
        if cached is not None:
            self._model = cached
            logger.info(f"Model cache hit: {self.model_size} / {self.device} / {self.compute_type}")
            if on_progress:
                on_progress("Modell aus Cache geladen ✓")
            return

        if on_progress:
            on_progress(f"Lade Modell '{self.model_size}' ({self.device})...")

        logger.info(f"Loading model: {self.model_size} on {self.device}")

        try:
            self._model = WhisperModel(
                self.model_size,
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
                    self.model_size,
                    device=self.device,
                    compute_type="float16",
                )
                cache_key = (self.model_size, self.device, self.compute_type)
            else:
                raise

        with Transcriber._cache_lock:
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
        """
        if beam_size_setting == "auto":
            if audio_duration < 10:
                return 1
            elif audio_duration < 30:
                return 3
            else:
                return 5
        else:
            return int(beam_size_setting)

    def transcribe(self, audio: np.ndarray, language: str = None,
                   auto_detect: bool = True, beam_size: str = "auto",
                   vad_sensitivity: int = 300,
                   initial_prompt: str = None,
                   on_segment=None) -> dict:
        """
        Transcribe audio to text.

        Args:
            on_segment: Optional callback(text:str) invoked for each segment as
                        faster-whisper produces it. Used by the frontend for a
                        live-rolling tail preview while transcription runs.

        Returns:
            Dict with 'text', 'beam', 'compute_type', 'duration', 'vad_ms'.
        """
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        if len(audio) == 0:
            return {"text": "", "beam": 0, "compute_type": self.compute_type,
                    "duration": 0, "vad_ms": vad_sensitivity}

        # Calculate audio duration (16kHz sample rate)
        audio_duration = len(audio) / 16000.0
        actual_beam = self._get_beam_size(audio_duration, beam_size)

        logger.info(f"Transcribing: {audio_duration:.1f}s audio, beam={actual_beam}, "
                    f"compute={self.compute_type}, vad_ms={vad_sensitivity}")

        # Determine language parameter for whisper
        whisper_lang = None
        if language and not auto_detect:
            whisper_lang = LANGUAGE_MAP.get(language, language)

        # Transcribe with optimized parameters. `initial_prompt` biases the
        # model toward custom vocabulary (product names, jargon, etc.) – the
        # simplest form of vocabulary customization without fine-tuning.
        # Inference is serialised via the class-level lock – see comment above.
        with Transcriber.inference_lock:
            segments, info = self._model.transcribe(
                audio,
                language=whisper_lang,
                beam_size=actual_beam,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=vad_sensitivity,
                    speech_pad_ms=150,
                ),
                initial_prompt=(initial_prompt.strip() if initial_prompt else None),
            )
            # Force segments to materialise inside the lock – the generator
            # does the actual model work lazily as we iterate it.
            segments = list(segments)

        # Collect all segments. `segments` is a generator – iterating it is what
        # actually runs the model. We push each chunk through `on_segment` as it
        # arrives so the UI can roll out text without waiting for the full pass.
        text_parts = []
        for segment in segments:
            piece = segment.text.strip()
            if not piece:
                continue
            text_parts.append(piece)
            if on_segment is not None:
                try:
                    on_segment(piece)
                except Exception as e:
                    logger.warning(f"on_segment callback raised: {e}")

        result = " ".join(text_parts).strip()

        if info and info.language:
            logger.info(f"Detected language: {info.language} "
                        f"(prob: {info.language_probability:.2f})")

        return {
            "text": result,
            "beam": actual_beam,
            "compute_type": self.compute_type,
            "duration": round(audio_duration, 1),
            "vad_ms": vad_sensitivity,
        }

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def get_device_info(self) -> str:
        """Return a string describing the current device setup."""
        return f"{self.device.upper()} ({self.compute_type})"

