"""
NoType – mute other audio while dictating (optional)
Mutes the default playback device while a dictation records and unmutes it
afterwards, but only if NoType was the one that muted it: speakers the user
had muted before stay muted.

All Core Audio calls run on one worker thread (one COM apartment). A marker
file remembers which device we muted, so a crash mid-dictation can't leave
the PC silent: the next backend start unmutes it.
"""

import logging
import os
import queue
import sys
import threading
import time
from ctypes import POINTER, c_float, c_uint, c_void_p
from ctypes.wintypes import BOOL, DWORD, LPCWSTR, LPWSTR

logger = logging.getLogger("NoType.OutputMute")

E_RENDER, E_MULTIMEDIA = 0, 1
CLSCTX_ALL = 0x17

_ifaces = None


def _interfaces():
    """Minimal Core Audio COM declarations (vtable order matters)."""
    global _ifaces
    if _ifaces is not None:
        return _ifaces
    from ctypes import HRESULT  # Windows only
    from comtypes import COMMETHOD, GUID, IUnknown

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = (
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint), "pnChannelCount")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel",
                      (["in"], c_float, "fLevelDB"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar",
                      (["in"], c_float, "fLevel"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel",
                      (["in"], c_uint, "nChannel"), (["in"], c_float, "fLevelDB"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar",
                      (["in"], c_uint, "nChannel"), (["in"], c_float, "fLevel"),
                      (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel",
                      (["in"], c_uint, "nChannel"), (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar",
                      (["in"], c_uint, "nChannel"), (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetMute",
                      (["in"], BOOL, "bMute"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
        )

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = (
            COMMETHOD([], HRESULT, "Activate",
                      (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"),
                      (["in"], c_void_p, "pActivationParams"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore",
                      (["in"], DWORD, "stgmAccess"), (["out"], POINTER(c_void_p), "ppProperties")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(LPWSTR), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "pdwState")),
        )

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = (
            COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                      (["in"], DWORD, "dataFlow"), (["in"], DWORD, "dwStateMask"),
                      (["out"], POINTER(c_void_p), "ppDevices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                      (["in"], DWORD, "dataFlow"), (["in"], DWORD, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
            COMMETHOD([], HRESULT, "GetDevice",
                      (["in"], LPCWSTR, "pwstrId"), (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "pClient")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "pClient")),
        )

    _ifaces = {
        "clsid": GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        "IAudioEndpointVolume": IAudioEndpointVolume,
        "IMMDeviceEnumerator": IMMDeviceEnumerator,
    }
    return _ifaces


class OutputMuter:
    """`mute(delay)` when a dictation starts, `restore()` when it ends.
    A restore that arrives before the delayed mute fired cancels it."""

    def __init__(self, marker_path: str):
        self._marker = marker_path
        self._q = queue.Queue()
        self._enabled = sys.platform.startswith("win")
        if self._enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    def mute(self, delay: float = 0.0):
        if self._enabled:
            self._q.put(("mute", time.monotonic() + delay))

    def restore(self):
        if self._enabled:
            self._q.put(("restore", 0.0))

    def shutdown(self, timeout: float = 1.0):
        """Restore and stop the worker (backend exit)."""
        if not self._enabled:
            return
        done = threading.Event()
        self._q.put(("quit", done))
        done.wait(timeout)

    # ── worker thread ───────────────────────────────────────────────
    def _loop(self):
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # this thread already got an apartment from the comtypes import
        self._enum = None
        self._muted = None  # endpoint volume we muted, None = nothing to restore
        self._recover()
        due = None
        while True:
            timeout = None if due is None else max(0.0, due - time.monotonic())
            try:
                op, arg = self._q.get(timeout=timeout)
            except queue.Empty:
                due = None
                self._do_mute()
                continue
            if op == "mute":
                due = arg
            elif op == "restore":
                due = None
                self._do_restore()
            elif op == "quit":
                self._do_restore()
                arg.set()
                return

    def _enumerator(self):
        if self._enum is None:
            import comtypes
            i = _interfaces()
            self._enum = comtypes.CoCreateInstance(
                i["clsid"], i["IMMDeviceEnumerator"], comtypes.CLSCTX_INPROC_SERVER)
        return self._enum

    def _volume(self, device):
        i = _interfaces()
        unk = device.Activate(i["IAudioEndpointVolume"]._iid_, CLSCTX_ALL, None)
        return unk.QueryInterface(i["IAudioEndpointVolume"])

    def _do_mute(self):
        if self._muted is not None:
            return
        try:
            device = self._enumerator().GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA)
            vol = self._volume(device)
            if vol.GetMute():
                return  # already muted by the user – not ours to undo
            try:
                with open(self._marker, "w", encoding="utf-8") as f:
                    f.write(device.GetId() or "")
            except OSError:
                pass
            vol.SetMute(True, None)
            self._muted = vol
            logger.info("Playback muted for dictation")
        except Exception as e:
            logger.warning(f"Muting playback failed: {e}")

    def _do_restore(self):
        vol, self._muted = self._muted, None
        if vol is None:
            return
        try:
            if vol.GetMute():
                vol.SetMute(False, None)
            logger.info("Playback restored")
        except Exception as e:
            logger.warning(f"Restoring playback failed: {e}")
        self._drop_marker()

    def _recover(self):
        """Unmute the device a crashed run left muted."""
        if not os.path.exists(self._marker):
            return
        try:
            with open(self._marker, "r", encoding="utf-8") as f:
                device_id = f.read().strip()
            en = self._enumerator()
            device = (en.GetDevice(device_id) if device_id
                      else en.GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA))
            vol = self._volume(device)
            if vol.GetMute():
                vol.SetMute(False, None)
                logger.info("Playback unmuted after an interrupted dictation")
        except Exception as e:
            logger.warning(f"Recovering muted playback failed: {e}")
        self._drop_marker()

    def _drop_marker(self):
        try:
            os.remove(self._marker)
        except OSError:
            pass
