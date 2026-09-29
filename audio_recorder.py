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
BLOCK_SIZE = 512   # ~32ms chunks at 16kHz → ~31 visualizer updates/s
N_BANDS = 20       # spectrum bands sent to the overlay


class SpectrumAnalyzer:
    """Turns raw mic blocks into a level + N_BANDS log-spaced band levels
    (0..1) for the overlay visualizers, so the bars follow the actual voice
    instead of a synthetic sine. One 1024-point rFFT per block (~20 µs).

    Gain-independent: each band is shown as its distance above that band's
    own noise floor (a min-tracker that follows pauses down instantly and
    creeps up slowly), so a quiet headset and a hot studio mic both fill the
    display while steady room noise and fan hum stay flat."""

    FFT_SIZE = 1024          # 64 ms window, overlapping blocks
    SNR_RANGE_DB = 30.0      # SNR that fills a (low) band completely
    FLOOR_RISE_DB = 0.04     # floor creep per block (~1.2 dB/s)
    FLOOR_MIN_DB = -95.0     # digital silence must not make hiss look huge

    def __init__(self, n_bands: int = N_BANDS, fmin: float = 90.0, fmax: float = 7000.0):
        self._buf = np.zeros(self.FFT_SIZE, dtype=np.float32)
        self._win = np.hanning(self.FFT_SIZE).astype(np.float32)
        self._norm = float(self._win.sum() / 2) ** 2
        freqs = np.fft.rfftfreq(self.FFT_SIZE, 1.0 / SAMPLE_RATE)
        edges = np.geomspace(fmin, fmax, n_bands + 1)
        self._idx = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            a = int(np.searchsorted(freqs, lo))
            b = max(a + 1, int(np.searchsorted(freqs, hi)))
            self._idx.append((a, b))
        nbins = np.array([b - a for a, b in self._idx], dtype=np.float32)
        # Noise in a band only a few bins wide flickers by ~10 dB above its
        # floor – narrow (low) bands need a bigger allowance than wide ones.
        self._offset = 4.0 + 8.0 / np.sqrt(nbins)
        # Voice energy falls off above a few hundred Hz: highs fill up with
        # less SNR so the display isn't just a bass hump.
        self._range = self.SNR_RANGE_DB - 12.0 * np.linspace(0.0, 1.0, n_bands)
        self._floor = None

    def process(self, block: np.ndarray) -> tuple[float, list[float]]:
        n = min(len(block), self.FFT_SIZE)
        self._buf[:-n] = self._buf[n:]
        self._buf[-n:] = block[-n:]

        rms = float(np.sqrt(np.mean(block ** 2)))
        # Legacy scale (typical mic RMS is 0-0.3) – the overlay's level meter.
        amplitude = min(1.0, rms * 5.0)

        power = np.abs(np.fft.rfft(self._buf * self._win)) ** 2 / self._norm
        db = np.array([10.0 * np.log10(power[a:b].mean() + 1e-12) for a, b in self._idx])

        if self._floor is None:
            self._floor = db.copy()
        self._floor = np.maximum(np.minimum(db, self._floor + self.FLOOR_RISE_DB),
                                 self.FLOOR_MIN_DB)
        snr = db - self._floor - self._offset
        v = np.clip(snr / self._range, 0.0, 1.0) ** 1.3
        return amplitude, [round(float(x), 2) for x in v]


class AudioRecorder:
    def __init__(self, on_amplitude=None, device_index=None):
        """
        Args:
            on_amplitude: Optional callback(float, list[float]) called for each
                          audio block with the RMS amplitude 0.0-1.0 and
                          N_BANDS spectrum levels 0.0-1.0 (overlay visualizer).
            device_index: Optional int for specific input device. None = system default.
        """
        self._on_amplitude = on_amplitude
        self._analyzer = SpectrumAnalyzer()
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

        # Level + spectrum for the overlay visualizer
        if self._on_amplitude and self._recording and not self._paused:
            try:
                amplitude, bands = self._analyzer.process(indata[:, 0])
                self._on_amplitude(amplitude, bands)
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
