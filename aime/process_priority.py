"""Windows Task Scheduler starts tasks at priority 7: below-normal CPU, with low disk and memory priority, whatever the
task does. On a busy PC such a process starves: a scheduled tick took 17 minutes to reach its first job, and headless
Edge (which inherits the priority) overran its render timeout. Our scheduled scripts ask for normal priority before
they load the app. Only this process and what it starts are affected; no Windows setting changes."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

NORMAL_PRIORITY_CLASS = 0x20
PROCESS_MEMORY_PRIORITY = 0  # PROCESS_INFORMATION_CLASS.ProcessMemoryPriority
MEMORY_PRIORITY_NORMAL = 5
PROCESS_IO_PRIORITY = 33  # PROCESSINFOCLASS.ProcessIoPriority
IO_PRIORITY_NORMAL = 2
CURRENT_PROCESS = wintypes.HANDLE(-1)


def _windows():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtSetInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.c_void_p]
    return kernel32, ntdll


def use_normal_priority() -> bool:
    """True when this process now runs at normal priority. A failure never stops a scheduled run: it just stays slow."""
    if os.name != "nt":
        return False
    try:
        kernel32, ntdll = _windows()
        memory, io = wintypes.ULONG(MEMORY_PRIORITY_NORMAL), wintypes.ULONG(IO_PRIORITY_NORMAL)
        kernel32.SetProcessInformation(CURRENT_PROCESS, PROCESS_MEMORY_PRIORITY, ctypes.byref(memory), ctypes.sizeof(memory))
        ntdll.NtSetInformationProcess(CURRENT_PROCESS, PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io))
        return bool(kernel32.SetPriorityClass(CURRENT_PROCESS, NORMAL_PRIORITY_CLASS))
    except (OSError, AttributeError, ctypes.ArgumentError):
        return False


def current() -> dict:
    """What this process runs at, for the heartbeat: the evidence that the scheduled runs are not starved."""
    if os.name != "nt":
        return {}
    try:
        kernel32, ntdll = _windows()
        memory, io = wintypes.ULONG(), wintypes.ULONG()
        kernel32.GetProcessInformation(CURRENT_PROCESS, PROCESS_MEMORY_PRIORITY, ctypes.byref(memory), ctypes.sizeof(memory))
        ntdll.NtQueryInformationProcess(CURRENT_PROCESS, PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io), None)
        return {"class": hex(kernel32.GetPriorityClass(CURRENT_PROCESS)), "memory": memory.value, "io": io.value}
    except (OSError, AttributeError, ctypes.ArgumentError):
        return {}
