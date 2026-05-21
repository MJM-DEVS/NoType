"""
NoType – Audio Recorder
Records audio from the microphone using sounddevice.
Provides amplitude data for the EQ visualizer via callback.
Supports pause/resume and device selection.
"""

import logging
import threading
import numpy as np
import sounddevice as sd

logger = logging.getLogger("NoType.AudioRecorder")

SAMPLE_RATE = 16000  # Whisper expects 16kHz
CHANNELS = 1
DTYPE = "float32"
BLOCK_SIZE = 1024  # ~64ms chunks at 16kHz


class AudioRecorder:
    def __init__(self, on_amplitude=None, device_index=None):
        """
        Args:
            on_amplitude: Optional callback(float) called with RMS amplitude 0.0-1.0
                          for each audio block (used by EQ visualizer).
            device_index: Optional int for specific input device. None = system default.
        """
        self._on_amplitude = on_amplitude
        self._device_index = device_index
        self._frames = []
        self._recording = False
        self._paused = False
        self._stream = None
        self._lock = threading.Lock()

    def _audio_callback(self, indata, frames, time_info, status):
        """Called for each audio block during recording."""
        # Surface PortAudio status flags so e.g. an unplugged microphone shows
        # up in the log instead of silently emitting silence.
        if status:
            try:
                logger.warning(f"PortAudio status: {status}")
            except Exception:
                pass

        with self._lock:
            if self._recording and not self._paused:
                self._frames.append(indata.copy())

        # Send amplitude to EQ visualizer
        if self._on_amplitude and self._recording and not self._paused:
            rms = float(np.sqrt(np.mean(indata ** 2)))
            # Normalize roughly to 0-1 range (typical mic RMS is 0-0.3)
            amplitude = min(1.0, rms * 5.0)
            try:
                self._on_amplitude(amplitude)
            except Exception:
                pass

    def start(self):
        """Start recording from the selected microphone."""
        with self._lock:
            self._frames = []
            self._recording = True
            self._paused = False

        kwargs = dict(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=self._audio_callback,
        )
        if self._device_index is not None:
            kwargs["device"] = self._device_index

        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

    def get_recent_audio(self, max_seconds: float = 8.0) -> np.ndarray:
        """Snapshot of the most recent `max_seconds` of buffered audio.

        Used by the live-preview loop to feed Whisper a rolling window of audio
        while recording is still in progress. Thread-safe: takes a copy under
        the lock so the audio callback can keep appending in the background.
        """
        max_samples = int(max_seconds * SAMPLE_RATE)
        with self._lock:
            if not self._frames:
                return np.array([], dtype=np.float32)
            # Walk frames from the end until we have `max_samples` worth.
            accumulated = 0
            start_idx = 0
            for i in range(len(self._frames) - 1, -1, -1):
                accumulated += len(self._frames[i])
                if accumulated >= max_samples:
                    start_idx = i
                    break
            recent = self._frames[start_idx:]
            if not recent:
                return np.array([], dtype=np.float32)
            return np.concatenate(recent, axis=0).flatten()

    def stop(self) -> np.ndarray:
        """Stop recording and return the audio data as a numpy array.

        Returns:
            numpy array of shape (samples,) with float32 values, 16kHz mono.
        """
        with self._lock:
            self._recording = False
            self._paused = False

        # Stream may already be closed if the device was unplugged mid-record.
        if self._stream:
            try:
                self._stream.stop()
            except Exception as e:
                logger.warning(f"Stream stop raised (ignored): {e}")
            try:
                self._stream.close()
            except Exception as e:
                logger.warning(f"Stream close raised (ignored): {e}")
            self._stream = None

        with self._lock:
            if not self._frames:
                return np.array([], dtype=np.float32)
            audio = np.concatenate(self._frames, axis=0).flatten()
            self._frames = []

        return audio

    def pause(self):
        """Pause recording (audio data is not captured while paused)."""
        with self._lock:
            self._paused = True

    def resume(self):
        """Resume recording after a pause."""
        with self._lock:
            self._paused = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    @staticmethod
    def list_devices():
        """List available audio input devices."""
        return sd.query_devices()
