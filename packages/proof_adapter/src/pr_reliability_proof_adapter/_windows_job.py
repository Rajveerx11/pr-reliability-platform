"""Small Windows Job Object boundary for kill-on-close child lifecycle."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_create_job = _kernel32.CreateJobObjectW
_create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
_create_job.restype = wintypes.HANDLE
_set_job_information = _kernel32.SetInformationJobObject
_set_job_information.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
_set_job_information.restype = wintypes.BOOL
_open_process = _kernel32.OpenProcess
_open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_open_process.restype = wintypes.HANDLE
_assign_process = _kernel32.AssignProcessToJobObject
_assign_process.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_assign_process.restype = wintypes.BOOL
_terminate_job = _kernel32.TerminateJobObject
_terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
_terminate_job.restype = wintypes.BOOL
_close_handle = _kernel32.CloseHandle
_close_handle.argtypes = (wintypes.HANDLE,)
_close_handle.restype = wintypes.BOOL


def _windows_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


@dataclass
class WindowsJob:
    """Own one kill-on-close job containing the supervised process tree."""

    handle: int
    _closed: bool = False

    @classmethod
    def attach(cls, process_id: int) -> WindowsJob:
        handle = _create_job(None, None)
        if not handle:
            raise _windows_error()
        job = cls(handle)
        process_handle = None
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _set_job_information(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise _windows_error()
            process_handle = _open_process(
                _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
                False,
                process_id,
            )
            if not process_handle:
                raise _windows_error()
            if not _assign_process(handle, process_handle):
                raise _windows_error()
            return job
        except Exception:
            job.close()
            raise
        finally:
            if process_handle and not _close_handle(process_handle):
                job.close()
                raise _windows_error()

    def terminate(self) -> None:
        """Terminate every associated process, then close the job handle."""
        if self._closed:
            raise RuntimeError("Windows process job is already closed")
        if not _terminate_job(self.handle, 1):
            error = _windows_error()
            self.close()
            raise error
        self.close()

    def close(self) -> None:
        """Close the job, activating kill-on-close when still populated."""
        if self._closed:
            return
        self._closed = True
        if not _close_handle(self.handle):
            raise _windows_error()
