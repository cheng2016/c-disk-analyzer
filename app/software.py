"""Installed software from the Windows uninstall registry."""

from __future__ import annotations

import hashlib
import os
import re
import winreg
from datetime import datetime, timedelta

from app.winpath import has_dotdot, is_reparse_stat, stat_nofollow, sum_tree

UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)

SKIP_NAME = re.compile(
    r"^(security update|update for|hotfix|definition update|service pack)\b",
    re.IGNORECASE,
)
SKIP_RELEASE = {"update", "security update", "hotfix", "service pack"}
RECENT_DAYS = 90
MEASURE_DAYS = 180
BULKY_BYTES = 500 * 1024 * 1024
TOP_RECENT_BYTES = 100 * 1024 * 1024
FILE_LIMIT = 100_000

BROAD_PATHS = (
    "C:\\",
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Users",
    r"C:\ProgramData",
)


def _access(hive):
    access = winreg.KEY_READ
    if hive == winreg.HKEY_LOCAL_MACHINE:
        access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
    return access


def _query(key, name):
    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value).strip()


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None


def _parse_date(value):
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        text = _text(value)
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None


def _broad_keys() -> set[str]:
    paths = list(BROAD_PATHS)
    profile = os.environ.get("USERPROFILE")
    if profile:
        paths.append(profile)
    keys = set()
    for path in paths:
        keys.add(os.path.normcase(os.path.normpath(path)))
    return keys


def specific_directory(location: str) -> str | None:
    if not location or has_dotdot(location):
        return None
    path = os.path.normpath(location.strip().strip('"'))
    if not path or not os.path.isabs(path):
        return None
    if os.path.isfile(path):
        path = os.path.dirname(path)
    key = os.path.normcase(path)
    if re.fullmatch(r"[a-z]:\\", key) or key in _broad_keys():
        return None
    if not os.path.isdir(path):
        return None
    st = stat_nofollow(path)
    if st is None or is_reparse_stat(st):
        return None
    return path


def _read_key(hive, path) -> list[dict]:
    access = _access(hive)
    try:
        root = winreg.OpenKey(hive, path, 0, access)
    except OSError:
        return []
    found = []
    try:
        count = winreg.QueryInfoKey(root)[0]
        for index in range(count):
            try:
                sub_name = winreg.EnumKey(root, index)
            except OSError:
                continue
            try:
                sub = winreg.OpenKey(root, sub_name, 0, access)
            except OSError:
                continue
            try:
                found.append(_read_app(sub))
            finally:
                sub.Close()
    finally:
        root.Close()
    return [item for item in found if item]


def _read_app(sub) -> dict | None:
    if _number(_query(sub, "SystemComponent")) == 1:
        return None
    release = _text(_query(sub, "ReleaseType")).lower()
    if release in SKIP_RELEASE:
        return None
    name = _text(_query(sub, "DisplayName"))
    if len(name) < 2 or SKIP_NAME.match(name):
        return None
    installed = _parse_date(_query(sub, "InstallDate"))
    cutoff = datetime.now() - timedelta(days=RECENT_DAYS)
    estimated = _number(_query(sub, "EstimatedSize"))
    if estimated and estimated > 0:
        size = estimated * 1024
        source = "注册表估算"
    else:
        size = None
        source = "未知"
    return {
        "name": name,
        "publisher": _text(_query(sub, "Publisher")),
        "version": _text(_query(sub, "DisplayVersion")),
        "install_date": installed.strftime("%Y-%m-%d") if installed else "",
        "install_ts": int(installed.timestamp()) if installed else 0,
        "recent": bool(installed and installed >= cutoff),
        "bulky": False,
        "partial": False,
        "size_bytes": size,
        "size_source": source,
        "install_location": _text(_query(sub, "InstallLocation")),
        "uninstall_string": _text(_query(sub, "UninstallString")),
    }


def _dedupe(items: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for app in items:
        location = os.path.normcase(app["install_location"]) if app["install_location"] else ""
        key = (app["name"].casefold(), location)
        previous = merged.get(key)
        if previous is None:
            merged[key] = app
            order.append(key)
            continue
        new_size = app.get("size_bytes")
        old_size = previous.get("size_bytes")
        if new_size is not None and (old_size is None or new_size > old_size):
            previous["size_bytes"] = new_size
            previous["size_source"] = app["size_source"]
        for field in ("publisher", "version", "install_date", "install_ts", "uninstall_string"):
            if not previous.get(field) and app.get(field):
                previous[field] = app[field]
        if app["recent"]:
            previous["recent"] = True
    rows = [merged[key] for key in order]
    located = {row["name"].casefold() for row in rows if row["install_location"]}
    return [row for row in rows if row["install_location"] or row["name"].casefold() not in located]


def mark_bulky(apps: list[dict]) -> list[dict]:
    recent = sorted(
        (app for app in apps if app.get("recent") and app.get("size_bytes")),
        key=lambda app: app["size_bytes"],
        reverse=True,
    )
    top = {id(app) for app in recent[:5] if app["size_bytes"] >= TOP_RECENT_BYTES}
    for app in apps:
        size = app.get("size_bytes") or 0
        app["bulky"] = bool(app.get("recent") and size and (size >= BULKY_BYTES or id(app) in top))
    return apps


def software_id(name: str, location: str) -> str:
    raw = f"{name.casefold()}|{os.path.normcase(location or '')}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def assign_identity(apps: list[dict]) -> list[dict]:
    for app in apps:
        app["id"] = software_id(app.get("name") or "", app.get("install_location") or "")
        app["can_uninstall"] = bool(app.get("uninstall_string"))
    return apps


def list_software() -> list[dict]:
    found: list[dict] = []
    for hive, path in UNINSTALL_KEYS:
        found.extend(_read_key(hive, path))
    apps = assign_identity(_dedupe(found))
    mark_bulky(apps)
    apps.sort(key=lambda app: (app.get("size_bytes") or -1, app.get("install_ts") or 0), reverse=True)
    return apps


def measure_recent_locations(apps: list[dict], cancel=None, progress=None) -> None:
    cutoff = (datetime.now() - timedelta(days=MEASURE_DAYS)).timestamp()
    for app in apps:
        if cancel is not None and cancel.is_set():
            break
        if not app.get("install_ts") or app["install_ts"] < cutoff:
            continue
        folder = specific_directory(app.get("install_location") or "")
        if not folder:
            continue
        if progress:
            progress(app["name"])
        stats = sum_tree(folder, cancel=cancel, limit_files=FILE_LIMIT)
        measured = stats["size_bytes"]
        estimated = app.get("size_bytes")
        partial = bool(stats["partial"])
        if partial and estimated and estimated > measured:
            app["size_source"] = "注册表估算（目录未测完）"
            app["partial"] = True
        else:
            app["size_bytes"] = measured
            app["partial"] = partial
            app["size_source"] = "目录实测（未完）" if partial else "目录实测"
    mark_bulky(apps)
    apps.sort(key=lambda app: (app.get("size_bytes") or -1, app.get("install_ts") or 0), reverse=True)
