"""Independent protected signal credentials; no broker password or execution key."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess

from .common import ENDPOINT, UUID, Refusal, exact, text, unique_json

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, repr=False)
class Config:
    terminal_path: str
    terminal_data_path: str
    expected_login: str
    expected_server: str
    connection_id: str
    token: str = field(repr=False)


def normalized(value):
    return ntpath.normcase(ntpath.normpath(value))


def plain_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise Refusal("unsafe_path", True)
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Refusal("unsafe_path", True)
    return path.resolve(strict=True)


def external_path(value):
    path = plain_path(value)
    if path == ROOT or ROOT in path.parents or any(re.match(r"onedrive(?:\s|$)", p, re.I) for p in path.parts):
        raise Refusal("unsafe_path", True)
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(key):
            synced = Path(os.environ[key]).resolve()
            if path == synced or synced in path.parents:
                raise Refusal("unsafe_path", True)
    if any((p / ".git").exists() for p in path.parents):
        raise Refusal("unsafe_path", True)
    return path


def powershell_environment():
    """Pin native verifier modules to Windows PowerShell, not an inherited pwsh.

    Codex/pwsh can export its PowerShell 7 module directory into PSModulePath.
    Windows PowerShell 5 then autoloads incompatible Security type data. This
    verifier needs only the installed Windows modules, and should not load a
    writable user module directory while checking a credential file's ACL.
    """
    result = dict(os.environ)
    result["PSModulePath"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/Modules")
    return result


def verify_acl(path):
    command = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", "-File",
               str(Path(__file__).with_name("verify-acl.ps1")), "-LiteralPath", str(path)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, check=False,
                                env=powershell_environment(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        raise Refusal("permissions_unverified", True) from None
    if result.returncode != 0:
        raise Refusal("permissions_unverified", True)


def load_config(filename):
    if os.name != "nt":
        raise Refusal("windows_required", True)
    try:
        location = external_path(filename)
        if location.name != "mt5-signals.json" or not location.is_file() or location.stat().st_size > 16384:
            raise Refusal("invalid_config", True)
        verify_acl(location.parent)
        verify_acl(location)
        data = json.loads(location.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_json)
        exact(data, ["terminalPath", "terminalDataPath", "expectedLogin", "expectedServer", "connectionId", "token", "endpoint"])
        if not isinstance(data["expectedLogin"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", data["expectedLogin"]):
            raise Refusal("invalid_config", True)
        if not text(data["expectedServer"]) or not isinstance(data["connectionId"], str) or not UUID.fullmatch(data["connectionId"]):
            raise Refusal("invalid_config", True)
        if not isinstance(data["token"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["token"]) or data["endpoint"] != ENDPOINT:
            raise Refusal("invalid_config", True)
        terminal = plain_path(data["terminalPath"])
        terminal_data = plain_path(data["terminalDataPath"])
        if not terminal.is_file() or terminal.name.lower() != "terminal64.exe" or not terminal_data.is_dir():
            raise Refusal("invalid_config", True)
        return Config(str(terminal), str(terminal_data), data["expectedLogin"], data["expectedServer"], data["connectionId"], data["token"])
    except Refusal:
        raise
    except Exception:
        raise Refusal("config_unavailable", True) from None


def terminal_running(executable):
    """Check the exact running executable before attaching; never launch MT5."""
    if os.name != "nt":
        return False

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        getattr(kernel, name).restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.szExeFile.lower() == ntpath.basename(executable).lower():
                process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        size = wintypes.DWORD(len(buffer))
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)) and normalized(buffer.value) == normalized(executable):
                            return True
                    finally:
                        kernel.CloseHandle(process)
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return False
