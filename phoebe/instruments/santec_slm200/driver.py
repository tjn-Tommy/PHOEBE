"""Santec SLM-200 DLL driver (migrated from TPA_experiment slm_module/driver).

Every method here is SYNCHRONOUS and must run on the device's worker thread:
the SLM window created by ``SLM_Disp_Open`` belongs to the thread that called
it, and the vendor samples drive the DLL from one message-pumping thread.
The controller therefore creates the worker with ``pump=True`` and
``initializer=driver.load`` (so the DLL is loaded ON the worker thread), and
funnels every call through ``worker.call`` (refactor.md §12.4).
"""
from __future__ import annotations

import ctypes
import os
import tempfile
import time
from pathlib import Path

import numpy as np

from ...core.errors import (
    DeviceReportedError,
    InstrumentConnectionError,
    InstrumentTimeoutError,
)
from .csvio import write_santec_csv

# Flags (Programmer's Guide 3.6 "BMP, CSV, Data Flags")
FLAGS_COLOR_R = 0x00000001
FLAGS_COLOR_G = 0x00000002
FLAGS_COLOR_B = 0x00000004
FLAGS_COLOR_GRAY = 0x00000008
FLAGS_RATE120 = 0x20000000

# Video interface modes (Guide 3.2.2 SLM_Ctrl_WriteVI)
MODE_MEMORY = 0
MODE_DVI = 1

# SLM_STATUS codes (Guide 3.5)
SLM_OK = 0
SLM_BS = 2

_STATUS_NAMES = {
    0: "SLM_OK",
    1: "SLM_NG",
    2: "SLM_BS (busy)",
    3: "SLM_ER (parameter error)",
    -1: "SLM_INVALID_MONITOR (display number not found)",
    -2: "SLM_NOT_OPEN_MONITOR (display not opened)",
    -3: "SLM_OPEN_WINDOW_ERR (window open error)",
    -4: "SLM_DATA_FORMAT_ERR (data format error)",
    -101: "SLM_FILE_READ_ERR (file not found or value over 1023)",
    -200: "SLM_NOT_OPEN_USB (USB not opened)",
    -1000: "SLM_OTHER_ERROR",
}


def _describe_status(code: int) -> str:
    if code in _STATUS_NAMES:
        return _STATUS_NAMES[code]
    if -10032 <= code <= -10001:
        return f"FTDI USB driver error ({code})"
    return f"unknown status ({code})"


class SlmDllDriver:
    """Thin ctypes wrapper over SLMFunc.dll; worker-thread-only by contract."""

    def __init__(self, dll_path: str, *, display_no: int = 1,
                 rate120: bool = False) -> None:
        self._dll_path = Path(dll_path)
        self.display_no = int(display_no)
        self.flags = FLAGS_RATE120 if rate120 else 0
        self.dll: ctypes.CDLL | None = None
        self.is_open = False

    # ---- lifecycle (worker thread) --------------------------------------------
    def load(self) -> None:
        """Load the DLL — runs as the worker-thread initializer so every DLL
        handle is created on (and stays on) that thread."""
        if not self._dll_path.exists():
            raise InstrumentConnectionError(
                f"SLMFunc.dll not found: {self._dll_path}")
        if hasattr(os, "add_dll_directory"):
            # so the dependent FTD3XX.dll next to SLMFunc.dll is found
            os.add_dll_directory(str(self._dll_path.parent))
        self.dll = ctypes.CDLL(str(self._dll_path))
        self._bind_functions()

    def _require(self) -> ctypes.CDLL:
        if self.dll is None:
            raise InstrumentConnectionError("SLM DLL is not loaded")
        return self.dll

    def _bind_functions(self) -> None:
        dll = self.dll
        assert dll is not None
        dll.SLM_Disp_Info2.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint16), ctypes.c_char_p,
        ]
        dll.SLM_Disp_Info2.restype = ctypes.c_int32
        dll.SLM_Disp_Open.argtypes = [ctypes.c_uint32]
        dll.SLM_Disp_Open.restype = ctypes.c_int32
        dll.SLM_Disp_Close.argtypes = [ctypes.c_uint32]
        dll.SLM_Disp_Close.restype = ctypes.c_int32
        dll.SLM_Disp_GrayScale.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint16,
        ]
        dll.SLM_Disp_GrayScale.restype = ctypes.c_int32
        dll.SLM_Disp_ReadCSV.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p,
        ]
        dll.SLM_Disp_ReadCSV.restype = ctypes.c_int32
        # USB control functions for Memory/DVI switching (Guide 1.3.1)
        dll.SLM_Ctrl_Open.argtypes = [ctypes.c_uint32]
        dll.SLM_Ctrl_Open.restype = ctypes.c_int32
        dll.SLM_Ctrl_Close.argtypes = [ctypes.c_uint32]
        dll.SLM_Ctrl_Close.restype = ctypes.c_int32
        dll.SLM_Ctrl_WriteVI.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        dll.SLM_Ctrl_WriteVI.restype = ctypes.c_int32
        dll.SLM_Ctrl_ReadVI.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.SLM_Ctrl_ReadVI.restype = ctypes.c_int32
        dll.SLM_Ctrl_ReadSU.argtypes = [ctypes.c_uint32]
        dll.SLM_Ctrl_ReadSU.restype = ctypes.c_int32

    def _check(self, result: int, func_name: str) -> None:
        if result != SLM_OK:
            raise DeviceReportedError(
                f"{func_name}: {_describe_status(result)}")

    # ---- display path -------------------------------------------------------
    def display_info(self, display_no: int | None = None) -> tuple[int, int, str]:
        """(width, height, name); the SLM reports a name starting "LCOS-SLM"."""
        display_no = self.display_no if display_no is None else int(display_no)
        height = ctypes.c_uint16()
        width = ctypes.c_uint16()
        name = ctypes.create_string_buffer(128)
        ret = self._require().SLM_Disp_Info2(
            display_no, ctypes.byref(width), ctypes.byref(height), name)
        self._check(ret, "SLM_Disp_Info2")
        return width.value, height.value, name.value.decode("mbcs", errors="replace")

    def search_displays(self, max_display: int = 8) -> list[tuple[int, int, int, str]]:
        found = []
        for display_no in range(1, max_display + 1):
            try:
                width, height, name = self.display_info(display_no)
            except DeviceReportedError:
                continue
            found.append((display_no, width, height, name))
        return found

    def open_display(self) -> None:
        ret = self._require().SLM_Disp_Open(self.display_no)
        self._check(ret, "SLM_Disp_Open")
        self.is_open = True

    def close_display(self) -> None:
        ret = self._require().SLM_Disp_Close(self.display_no)
        self.is_open = False
        self._check(ret, "SLM_Disp_Close")

    def load_csv(self, csv_path: str, flags: int | None = None) -> None:
        flags = self.flags if flags is None else int(flags)
        ret = self._require().SLM_Disp_ReadCSV(
            self.display_no, flags, str(Path(csv_path).resolve()))
        self._check(ret, "SLM_Disp_ReadCSV")

    def load_grayscale(self, level: int, flags: int | None = None) -> None:
        flags = self.flags if flags is None else int(flags)
        ret = self._require().SLM_Disp_GrayScale(self.display_no, flags, int(level))
        self._check(ret, "SLM_Disp_GrayScale")

    def display_frame(self, frame: np.ndarray) -> Path:
        """Write ``frame`` to a temp CSV and load it — one worker-thread job so
        the (blocking) CSV write never touches the event loop."""
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", prefix="phoebe_slm_", delete=False)
        handle.close()
        path = write_santec_csv(frame, handle.name)
        self.load_csv(str(path))
        return path

    # ---- USB control path ------------------------------------------------------
    def set_video_mode(self, mode: int, slm_number: int = 1,
                       timeout: float = 60.0) -> None:
        """Guide 1.3.1: Ctrl_Open → wait ready (ReadSU) → WriteVI → Ctrl_Close.
        Runs as ONE worker job so it never interleaves with display calls."""
        if mode not in (MODE_MEMORY, MODE_DVI):
            raise ValueError("mode must be 0 (Memory) or 1 (DVI)")
        dll = self._require()
        ret = dll.SLM_Ctrl_Open(slm_number)
        self._check(ret, "SLM_Ctrl_Open")
        try:
            deadline = time.monotonic() + timeout
            while True:
                ret = dll.SLM_Ctrl_ReadSU(slm_number)
                if ret == SLM_OK:
                    break
                if ret != SLM_BS or time.monotonic() >= deadline:
                    self._check(ret, "SLM_Ctrl_ReadSU")
                    raise InstrumentTimeoutError(
                        "SLM_Ctrl_ReadSU: timed out waiting for ready")
                time.sleep(0.5)
            ret = dll.SLM_Ctrl_WriteVI(slm_number, mode)
            self._check(ret, "SLM_Ctrl_WriteVI")
        finally:
            dll.SLM_Ctrl_Close(slm_number)

    def get_video_mode(self, slm_number: int = 1) -> int:
        dll = self._require()
        ret = dll.SLM_Ctrl_Open(slm_number)
        self._check(ret, "SLM_Ctrl_Open")
        try:
            mode = ctypes.c_uint32()
            ret = dll.SLM_Ctrl_ReadVI(slm_number, ctypes.byref(mode))
            self._check(ret, "SLM_Ctrl_ReadVI")
            return int(mode.value)
        finally:
            dll.SLM_Ctrl_Close(slm_number)

    def ping(self, slm_number: int = 1) -> None:
        """USB heartbeat (SLM_Ctrl_ReadSU); SLM_BS (busy) still counts as alive."""
        dll = self._require()
        ret = dll.SLM_Ctrl_Open(slm_number)
        self._check(ret, "SLM_Ctrl_Open")
        try:
            ret = dll.SLM_Ctrl_ReadSU(slm_number)
            if ret not in (SLM_OK, SLM_BS):
                self._check(ret, "SLM_Ctrl_ReadSU")
        finally:
            dll.SLM_Ctrl_Close(slm_number)
