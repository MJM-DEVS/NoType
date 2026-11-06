"""
NoType – Rescue copy of the running recording.

A dictation is written to a WAV file while it is being spoken, so it survives
whatever fails afterwards: the speech engine, the GPU driver, the app or the
PC. The file is deleted as soon as the dictation came out as text (or was
cancelled). What stays in the folder is what still needs transcribing; the
user can run it again, and old files are removed after MAX_AGE_DAYS.

Format: 16 kHz, 16 bit, mono. Small (~1.9 MB per minute) and every player can
open it. A file whose recording never ended has no sizes in its header; that
is repaired when the folder is read, so it also plays by hand.
"""

import logging
import os
import queue
import re
import struct
import threading
import time

import numpy as np

from config import CONFIG_DIR

logger = logging.getLogger("NoType.Spool")

SAMPLE_RATE = 16000
RESCUE_DIR = os.path.join(CONFIG_DIR, "rescue")
MAX_AGE_DAYS = 7
MAX_ITEMS = 10
MIN_SECONDS = 1.0        # shorter than this is nothing worth offering
_HEADER_SIZE = 44
_SYNC_SECONDS = 2.0      # how much a power cut may cost at most
_ID = re.compile(r"^(\d{8}-\d{6})-\d{3}$")


def _wav_header(data_bytes: int) -> bytes:
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE",
                       b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
                       b"data", data_bytes)


class SpoolWriter:
    """Writes one recording. `write` is called from the audio callback, so it
    only hands the block to a thread: a slow disk can never stall the mic, and
    a failing one costs the rescue copy, not the recording."""

    def __init__(self):
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        self.id = f"{stamp}-{int(now * 1000) % 1000:03d}"
        self.path = _path(self.id)
        os.makedirs(RESCUE_DIR, exist_ok=True)
        # Unbuffered: what was written survives a crash of this process.
        self._file = open(self.path, "wb", buffering=0)
        self._file.write(_wav_header(0))
        self._bytes = 0
        self._queue = queue.SimpleQueue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def write(self, block: np.ndarray) -> None:
        pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2")
        self._queue.put(pcm.tobytes())

    def _run(self) -> None:
        last_sync = time.monotonic()
        broken = False
        while True:
            data = self._queue.get()
            if data is None:
                break
            if broken:
                continue
            try:
                self._file.write(data)
                self._bytes += len(data)
                if time.monotonic() - last_sync >= _SYNC_SECONDS:
                    os.fsync(self._file.fileno())
                    last_sync = time.monotonic()
            except OSError as e:
                broken = True
                logger.warning(f"Rescue copy stopped: {e}")
        try:
            self._file.seek(0)
            self._file.write(_wav_header(self._bytes))
            os.fsync(self._file.fileno())
        except OSError as e:
            logger.warning(f"Finishing the rescue copy failed: {e}")
        finally:
            self._file.close()

    def close(self) -> None:
        """Recording over: everything on disk, sizes in the header."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=5)

    def discard(self) -> None:
        self.close()
        _remove(self.path)

    @property
    def seconds(self) -> float:
        return self._bytes / (SAMPLE_RATE * 2)


def start():
    """A writer for a new recording, None if the folder can't be written
    (the dictation still works then, without a rescue copy)."""
    try:
        return SpoolWriter()
    except OSError as e:
        logger.warning(f"No rescue copy for this recording: {e}")
        return None


def _path(item_id: str) -> str:
    return os.path.join(RESCUE_DIR, item_id + ".wav")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError as e:
        if not isinstance(e, FileNotFoundError):
            logger.warning(f"Deleting a rescue copy failed: {e}")


def _repair_header(path: str, data_bytes: int) -> None:
    """A recording that never reached close(): put the real sizes in."""
    try:
        with open(path, "r+b") as f:
            if f.read(_HEADER_SIZE) != _wav_header(data_bytes):
                f.seek(0)
                f.write(_wav_header(data_bytes))
    except OSError as e:
        logger.warning(f"Repairing a rescue copy failed: {e}")


def list_items(active=()) -> list:
    """Recordings waiting to be transcribed, oldest first:
    [{id, started (epoch seconds), seconds}]. `active`: ids of the recordings
    being worked on right now, they are no leftovers."""
    items = []
    try:
        names = os.listdir(RESCUE_DIR)
    except OSError:
        return items
    for name in names:
        item_id, ext = os.path.splitext(name)
        match = _ID.match(item_id)
        if ext != ".wav" or not match or item_id in active:
            continue
        path = os.path.join(RESCUE_DIR, name)
        try:
            data_bytes = os.path.getsize(path) - _HEADER_SIZE
            started = time.mktime(time.strptime(match.group(1), "%Y%m%d-%H%M%S"))
        except (OSError, ValueError, OverflowError):
            continue
        data_bytes -= data_bytes % 2  # a crash may have cut a sample in half
        seconds = data_bytes / (SAMPLE_RATE * 2)
        if seconds < MIN_SECONDS:
            _remove(path)
            continue
        _repair_header(path, data_bytes)
        items.append({"id": item_id, "started": started, "seconds": round(seconds, 1)})
    items.sort(key=lambda i: i["id"])
    return items


def load(item_id: str) -> np.ndarray:
    """The recording as float32 samples, the way the engine wants them."""
    if not _ID.match(item_id or ""):
        raise ValueError("Unknown recording")
    with open(_path(item_id), "rb") as f:
        f.seek(_HEADER_SIZE)
        data = f.read()
    data = data[:len(data) - len(data) % 2]
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def delete(item_id: str) -> None:
    if _ID.match(item_id or ""):
        _remove(_path(item_id))


def clear(active=()) -> None:
    """Everything waiting goes (the running recordings have their own end)."""
    for item in list_items(active):
        delete(item["id"])


def purge() -> None:
    """Old files out, and never more than MAX_ITEMS (the newest stay)."""
    items = list_items()
    cutoff = time.time() - MAX_AGE_DAYS * 24 * 3600
    for i, item in enumerate(items):
        if item["started"] < cutoff or i < len(items) - MAX_ITEMS:
            logger.info(f"Removing an old rescue copy ({item['seconds']:.0f}s)")
            delete(item["id"])
