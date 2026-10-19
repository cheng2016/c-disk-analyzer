"""Directory walks that never follow junctions or symbolic links."""

from __future__ import annotations

import os
import re

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ERROR_FILENAME_EXCED_RANGE = 206


def to_display(path: str) -> str:
    text = os.fspath(path)
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def has_dotdot(text: str) -> bool:
    return any(part == ".." for part in re.split(r"[\\/]", str(text)))


def norm_key(path) -> str:
    text = to_display(os.fspath(path))
    text = os.path.abspath(text)
    text = to_display(text)
    return os.path.normcase(os.path.normpath(text))


def path_is_inside(child, parent) -> bool:
    child_key = norm_key(child)
    parent_key = norm_key(parent)
    if child_key == parent_key:
        return True
    if len(parent_key) == 3 and parent_key[1:] == ":\\":
        return child_key.startswith(parent_key)
    return child_key.startswith(parent_key + "\\")


def _attrs(st) -> int:
    return getattr(st, "st_file_attributes", 0) or 0


def stat_nofollow(path):
    try:
        return os.stat(os.fspath(path), follow_symlinks=False)
    except OSError:
        return None


def is_reparse_stat(st) -> bool:
    return bool(st is not None and _attrs(st) & FILE_ATTRIBUTE_REPARSE_POINT)


def classify_entry(entry: os.DirEntry):
    """Return ('dir'|'file'|None, stat). None means skip."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None, None
    attrs = _attrs(st)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return None, st
    if attrs & FILE_ATTRIBUTE_DIRECTORY:
        return "dir", st
    return "file", st


def scandir_any(path: str):
    raw = os.fspath(path)
    if not raw.startswith("\\\\?\\") and len(raw) >= 240:
        raw = "\\\\?\\" + os.path.abspath(to_display(path))
    try:
        return os.scandir(raw)
    except OSError as first:
        winerror = getattr(first, "winerror", None)
        if not str(raw).startswith("\\\\?\\") and winerror == ERROR_FILENAME_EXCED_RANGE:
            try:
                return os.scandir("\\\\?\\" + os.path.abspath(to_display(path)))
            except OSError:
                return None
        return None


def io_path(path: str) -> str:
    text = to_display(path)
    if len(text) >= 240:
        return "\\\\?\\" + os.path.abspath(text)
    return text


def sum_tree(root: str, cancel=None, limit_files: int | None = None) -> dict:
    total = 0
    files = 0
    unreadable = 0
    partial = False
    stack = [os.fspath(root)]
    while stack:
        if cancel is not None and cancel.is_set():
            partial = True
            break
        current = stack.pop()
        iterator = scandir_any(current)
        if iterator is None:
            unreadable += 1
            continue
        with iterator:
            for entry in iterator:
                kind, st = classify_entry(entry)
                if st is None:
                    unreadable += 1
                    continue
                if kind is None:
                    continue
                if kind == "dir":
                    stack.append(entry.path)
                    continue
                total += st.st_size
                files += 1
                if limit_files is not None and files >= limit_files:
                    partial = True
                    stack.clear()
                    break
    return {
        "size_bytes": total,
        "file_count": files,
        "unreadable": unreadable,
        "partial": partial,
    }
