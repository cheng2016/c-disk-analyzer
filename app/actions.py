"""Delete listed files and start official uninstallers. Paths come from the scan, not from free text."""

from __future__ import annotations

import os
import shlex
import subprocess

from app.winpath import has_dotdot, norm_key, path_is_inside, stat_nofollow, to_display

SYSTEM_FILES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys"}
WINDOWS_ROOT = r"C:\Windows"
LAUNCH_BY_NAME = {"msiexec.exe", "msiexec", "rundll32.exe", "rundll32"}


def file_block_reason(path: str) -> str:
    raw = os.fspath(path)
    if not raw or has_dotdot(raw):
        return "路径无效"
    key = norm_key(raw)
    name = os.path.basename(key)
    if name in SYSTEM_FILES:
        return "系统文件，不能删除"
    if path_is_inside(key, WINDOWS_ROOT):
        return "Windows 目录里的文件不能在这里删除"
    pieces = [part for part in key.split("\\") if part]
    if len(pieces) < 2:
        return "路径层级太浅"
    st = stat_nofollow(key)
    if st is None:
        return "文件已经不存在"
    attrs = getattr(st, "st_file_attributes", 0) or 0
    if attrs & 0x400:
        return "这是链接，不能删除"
    if attrs & 0x10:
        return "这是文件夹，请改用卸载或清理缓存"
    return ""


def split_uninstall(command: str) -> list[str]:
    text = (command or "").strip()
    if not text or has_dotdot(text):
        raise ValueError("没有可用的卸载命令")
    if text.lower().startswith("msiexec"):
        parts = shlex.split(text, posix=False)
    elif text.startswith('"'):
        end = text.find('"', 1)
        if end < 0:
            raise ValueError("卸载命令的引号不完整")
        exe = text[1:end]
        rest = text[end + 1 :].strip()
        parts = [exe, *shlex.split(rest, posix=False)] if rest else [exe]
    else:
        parts = shlex.split(text, posix=False)
    if not parts or not parts[0]:
        raise ValueError("没有可用的卸载命令")
    return parts


def command_is_launchable(args: list[str]) -> str:
    exe = args[0]
    base = os.path.basename(exe).lower()
    if base in LAUNCH_BY_NAME:
        return ""
    if os.path.isfile(exe):
        return ""
    return "卸载程序不存在"


def delete_listed_files(paths: list[str], allowed: set[str]) -> dict:
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        return {"ok": False, "message": "删除请求无效", "deleted": [], "rejected": [], "freed_bytes": 0}
    unique = []
    seen = set()
    for path in paths:
        key = norm_key(path) if path and not has_dotdot(path) else ""
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if not unique:
        return {"ok": False, "message": "没有选择文件", "deleted": [], "rejected": [], "freed_bytes": 0}
    if len(unique) > 100:
        return {"ok": False, "message": "一次最多删除 100 个文件", "deleted": [], "rejected": [], "freed_bytes": 0}

    deleted = []
    rejected = []
    freed = 0
    for path in unique:
        key = norm_key(path)
        if key not in allowed:
            rejected.append({"path": path, "reason": "不在本次扫描结果里"})
            continue
        reason = file_block_reason(path)
        if reason:
            rejected.append({"path": to_display(path), "reason": reason})
            continue
        st = stat_nofollow(path)
        size = st.st_size if st else 0
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError as exc:
            rejected.append({"path": to_display(path), "reason": str(exc)[:180]})
            continue
        freed += size
        deleted.append({"path": to_display(path), "size_bytes": size})
    return {
        "ok": True,
        "message": "",
        "deleted": deleted,
        "rejected": rejected,
        "freed_bytes": freed,
    }


def launch_uninstall(command: str) -> None:
    args = split_uninstall(command)
    reason = command_is_launchable(args)
    if reason:
        raise ValueError(reason)
    subprocess.Popen(args, shell=False, close_fds=True)
