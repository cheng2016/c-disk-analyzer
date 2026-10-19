"""Known cache locations, and deletion that cannot leave those locations."""

from __future__ import annotations

import fnmatch
import os
import re
import stat

from app.winpath import (
    classify_entry,
    has_dotdot,
    io_path,
    norm_key,
    path_is_inside,
    scandir_any,
    stat_nofollow,
    sum_tree,
    to_display,
)
from app.winpath import _attrs

class UnsafePathError(ValueError):
    pass


_ID = re.compile(r"^[a-z0-9_]+$")
_PLACEHOLDER = re.compile(r"\{([A-Z]+)\}")
BIG_CACHE = 100 * 1024 * 1024
FLOOR_CACHE = 20 * 1024 * 1024

CACHE_RULES = [
    {
        "id": "user_temp",
        "name": "用户临时文件",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{TEMP}", "{LOCALAPPDATA}\\Temp"],
        "note": "系统和程序的临时文件。正在使用的文件会跳过。",
    },
    {
        "id": "windows_temp",
        "name": "Windows 临时文件",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{WINDIR}\\Temp"],
        "note": "只清空 Windows\\Temp 里面的内容。",
    },
    {
        "id": "chrome_cache",
        "name": "Chrome 缓存",
        "risk": "safe",
        "kind": "glob_dir",
        "paths": [
            "{LOCALAPPDATA}\\Google\\Chrome\\User Data\\*\\Cache",
            "{LOCALAPPDATA}\\Google\\Chrome\\User Data\\*\\Code Cache",
            "{LOCALAPPDATA}\\Google\\Chrome\\User Data\\*\\GPUCache",
        ],
        "note": "不含书签、历史和登录数据。",
    },
    {
        "id": "edge_cache",
        "name": "Edge 缓存",
        "risk": "safe",
        "kind": "glob_dir",
        "paths": [
            "{LOCALAPPDATA}\\Microsoft\\Edge\\User Data\\*\\Cache",
            "{LOCALAPPDATA}\\Microsoft\\Edge\\User Data\\*\\Code Cache",
            "{LOCALAPPDATA}\\Microsoft\\Edge\\User Data\\*\\GPUCache",
        ],
        "note": "不含收藏夹和登录数据。",
    },
    {
        "id": "cursor_cache",
        "name": "Cursor 缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": [
            "{APPDATA}\\Cursor\\Cache",
            "{APPDATA}\\Cursor\\Code Cache",
            "{APPDATA}\\Cursor\\GPUCache",
            "{APPDATA}\\Cursor\\CachedData",
            "{APPDATA}\\Cursor\\CachedExtensionVSIXs",
            "{APPDATA}\\Cursor\\DawnGraphiteCache",
            "{APPDATA}\\Cursor\\DawnWebGPUCache",
            "{APPDATA}\\Cursor\\logs",
            "{APPDATA}\\Cursor\\Crashpad",
        ],
        "note": "不含项目、设置和登录状态。",
    },
    {
        "id": "doubao_cache",
        "name": "豆包缓存",
        "risk": "safe",
        "kind": "glob_dir",
        "paths": [
            "{LOCALAPPDATA}\\Doubao\\User Data\\*\\Cache",
            "{LOCALAPPDATA}\\Doubao\\User Data\\*\\Code Cache",
            "{LOCALAPPDATA}\\Doubao\\User Data\\*\\GPUCache",
        ],
        "note": "不含登录和对话数据。",
    },
    {
        "id": "feishu_cache",
        "name": "飞书缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\Feishu\\User Data\\Cache"],
        "note": "只清理 Cache 目录，不含登录和聊天数据。",
    },
    {
        "id": "lark_cache",
        "name": "Lark 缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": [
            "{APPDATA}\\LarkShell\\CodeCache",
            "{APPDATA}\\LarkShell\\GrShaderCache",
            "{APPDATA}\\LarkShell\\GraphiteDawnCache",
            "{APPDATA}\\LarkShell\\ShaderCache",
            "{APPDATA}\\LarkShell\\BrowserMetrics",
            "{APPDATA}\\LarkShell\\OptimizeImage",
        ],
        "note": "不含消息和登录数据。",
    },
    {
        "id": "npm_cache",
        "name": "npm 缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\npm-cache"],
        "note": "清理后安装依赖会重新下载。",
    },
    {
        "id": "pip_cache",
        "name": "pip 缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\pip\\cache"],
        "note": "清理后安装 Python 包会重新下载。",
    },
    {
        "id": "uv_cache",
        "name": "uv 缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\uv\\cache"],
        "note": "清理后 uv 安装包会重新下载。",
    },
    {
        "id": "gradle_cache",
        "name": "Gradle 缓存",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{USERPROFILE}\\.gradle\\caches"],
        "note": "只清理 caches，不删除 JDK。",
    },
    {
        "id": "squirrel_temp",
        "name": "Squirrel 安装临时文件",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\SquirrelTemp"],
        "note": "安装器留下的临时文件。",
    },
    {
        "id": "crash_dumps",
        "name": "崩溃转储",
        "risk": "safe",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\CrashDumps"],
        "note": "程序崩溃时写出的 dump 文件。",
    },
    {
        "id": "thumb_cache",
        "name": "缩略图和图标缓存",
        "risk": "safe",
        "kind": "files",
        "paths": ["{LOCALAPPDATA}\\Microsoft\\Windows\\Explorer"],
        "names": ["thumbcache_*.db", "iconcache_*.db"],
        "note": "只删除缩略图和图标数据库，不删除同目录的其他文件。",
    },
    {
        "id": "windows_update",
        "name": "Windows 更新下载",
        "risk": "review",
        "kind": "dir",
        "paths": ["{WINDIR}\\SoftwareDistribution\\Download"],
        "note": "只统计，不自动清理。",
    },
    {
        "id": "maven_repo",
        "name": "Maven 本地仓库",
        "risk": "review",
        "kind": "dir",
        "paths": ["{USERPROFILE}\\.m2\\repository"],
        "note": "这是依赖仓库，不是缓存。只统计，不自动清理。",
    },
    {
        "id": "package_cache",
        "name": "安装包缓存",
        "risk": "review",
        "kind": "dir",
        "paths": ["{LOCALAPPDATA}\\Package Cache"],
        "note": "卸载软件时可能还会用到。只统计，不自动清理。",
    },
    {
        "id": "user_dot_cache",
        "name": "用户 .cache 目录",
        "risk": "review",
        "kind": "dir",
        "paths": ["{USERPROFILE}\\.cache"],
        "note": "内容不一定都能删。只统计，不自动清理。",
    },
    {
        "id": "wechat_data",
        "name": "微信数据",
        "risk": "review",
        "kind": "dir",
        "paths": [
            "{APPDATA}\\Tencent\\xwechat",
            "{USERPROFILE}\\Documents\\WeChat Files",
            "{USERPROFILE}\\Documents\\xwechat_files",
        ],
        "note": "含聊天记录和收到的文件。只统计，不自动清理。",
    },
    {
        "id": "tencent_files",
        "name": "腾讯应用数据",
        "risk": "review",
        "kind": "dir",
        "paths": ["{USERPROFILE}\\Documents\\Tencent Files"],
        "note": "可能含 QQ 等数据。只统计，不自动清理。",
    },
]


def current_env() -> dict[str, str]:
    return {
        "TEMP": os.environ.get("TEMP") or os.environ.get("TMP") or "",
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA") or "",
        "APPDATA": os.environ.get("APPDATA") or "",
        "USERPROFILE": os.environ.get("USERPROFILE") or "",
        "WINDIR": os.environ.get("WINDIR") or r"C:\Windows",
    }


def protected_keys(env: dict | None = None) -> set[str]:
    env = env or current_env()
    raw = [
        "C:\\",
        r"C:\Windows",
        r"C:\Windows\System32",
        r"C:\Windows\SysWOW64",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        r"C:\Users",
    ]
    for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        if env.get(key):
            raw.append(env[key])
    profile = env.get("USERPROFILE")
    if profile:
        for name in ("Documents", "Desktop", "Downloads", "Pictures", "Videos", "Music"):
            raw.append(os.path.join(profile, name))
    return {norm_key(path) for path in raw}


def _expand(template: str, env: dict) -> str:
    if has_dotdot(template):
        raise UnsafePathError("路径不能包含 ..")

    def replace(match: re.Match) -> str:
        value = env.get(match.group(1)) or ""
        if not value:
            raise KeyError(match.group(1))
        if has_dotdot(value):
            raise UnsafePathError("环境变量路径不能包含 ..")
        return value

    try:
        text = _PLACEHOLDER.sub(replace, template)
    except KeyError:
        return ""
    if "{" in text or "}" in text:
        raise UnsafePathError("路径模板无法解析")
    if has_dotdot(text):
        raise UnsafePathError("路径不能包含 ..")
    if not os.path.isabs(text):
        raise UnsafePathError("需要绝对路径")
    return os.path.normpath(text)


def _valid_part(part: str, allow_drive: bool = False) -> bool:
    if part in {"", ".", "..", "*"}:
        return False
    if ":" in part:
        return bool(allow_drive and re.fullmatch(r"[A-Za-z]:", part))
    return True


def _join_parts(parts: list[str]) -> str:
    if not parts:
        return ""
    if re.fullmatch(r"[A-Za-z]:", parts[0]):
        tail = "\\".join(parts[1:])
        text = parts[0] + ("\\" + tail if tail else "\\")
    else:
        text = "\\".join(parts)
    return os.path.normpath(text)


def _split_glob(pattern: str) -> tuple[str, list[str]]:
    parts = re.split(r"[\\/]", pattern)
    if parts.count("*") != 1:
        raise UnsafePathError("缓存路径只能有一层通配符")
    index = parts.index("*")
    prefix_parts = parts[:index]
    suffix = parts[index + 1 :]
    if not prefix_parts or not all(_valid_part(part, allow_drive=(i == 0)) for i, part in enumerate(prefix_parts)):
        raise UnsafePathError("通配符前的目录无效")
    if not suffix or not all(_valid_part(part) for part in suffix):
        raise UnsafePathError("通配符后的目录无效")
    return _join_parts(prefix_parts), suffix


def iter_targets(rule: dict, env: dict):
    kind = rule["kind"]
    seen = set()
    if kind == "dir":
        generated = _dir_targets(rule["paths"], env)
    elif kind == "glob_dir":
        generated = _glob_targets(rule["paths"], env)
    elif kind == "files":
        generated = _file_targets(rule["paths"], rule.get("names") or [], env)
    else:
        raise UnsafePathError("未知的缓存类型")
    for item in generated:
        key = norm_key(item["target"])
        if key in seen:
            continue
        seen.add(key)
        yield item


def _dir_targets(paths, env):
    for raw in paths:
        if "*" in raw:
            raise UnsafePathError("目录规则不能使用通配符")
        expanded = _expand(raw, env)
        if not expanded or not os.path.isdir(expanded):
            continue
        st = stat_nofollow(expanded)
        if st is None or _attrs(st) & 0x400:
            continue
        yield {"target": expanded, "allowed": expanded, "mode": "dir"}


def _glob_targets(paths, env):
    for raw in paths:
        expanded = _expand(raw, env)
        if not expanded:
            continue
        prefix, suffix = _split_glob(expanded)
        if not os.path.isdir(prefix):
            continue
        prefix_stat = stat_nofollow(prefix)
        if prefix_stat is None or _attrs(prefix_stat) & 0x400:
            continue
        iterator = scandir_any(prefix)
        if iterator is None:
            continue
        with iterator:
            for entry in iterator:
                entry_kind, _st = classify_entry(entry)
                if entry_kind != "dir":
                    continue
                candidate = entry.path
                for part in suffix:
                    candidate = os.path.join(candidate, part)
                if not path_is_inside(candidate, prefix) or not os.path.isdir(candidate):
                    continue
                st = stat_nofollow(candidate)
                if st is None or _attrs(st) & 0x400:
                    continue
                display = to_display(candidate)
                yield {"target": display, "allowed": display, "mode": "dir"}


def _file_targets(paths, names, env):
    if not names:
        raise UnsafePathError("缺少文件名规则")
    for raw in paths:
        if "*" in raw:
            raise UnsafePathError("文件规则的目录不能使用通配符")
        expanded = _expand(raw, env)
        if not expanded or not os.path.isdir(expanded):
            continue
        root_stat = stat_nofollow(expanded)
        if root_stat is None or _attrs(root_stat) & 0x400:
            continue
        iterator = scandir_any(expanded)
        if iterator is None:
            continue
        with iterator:
            for entry in iterator:
                entry_kind, _st = classify_entry(entry)
                if entry_kind != "file":
                    continue
                if any(fnmatch.fnmatch(entry.name, pattern) for pattern in names):
                    yield {
                        "target": to_display(entry.path),
                        "allowed": expanded,
                        "mode": "file",
                    }


def assert_deletable(target, allowed_roots, mode="dir", protected=None) -> str:
    raw = os.fspath(target)
    if has_dotdot(raw):
        raise UnsafePathError("路径不能包含 ..")
    key = norm_key(raw)
    if has_dotdot(key) or not os.path.isabs(key):
        raise UnsafePathError("路径不能包含 .." if has_dotdot(key) else "需要绝对路径")
    allowed_keys = []
    for root in allowed_roots:
        root_raw = os.fspath(root)
        if has_dotdot(root_raw):
            raise UnsafePathError("路径不能包含 ..")
        allowed_keys.append(norm_key(root_raw))
    if not allowed_keys or not any(path_is_inside(key, item) for item in allowed_keys):
        raise UnsafePathError("路径超出允许的缓存目录")
    protected_set = protected if protected is not None else protected_keys()
    if key in protected_set or any(item in protected_set for item in allowed_keys):
        raise UnsafePathError("路径受保护")
    pieces = [part for part in key.split("\\") if part]
    if len(pieces) < 3:
        raise UnsafePathError("路径层级太浅")
    if mode == "file":
        parent = os.path.dirname(key)
        if not any(parent == item or path_is_inside(parent, item) for item in allowed_keys):
            raise UnsafePathError("文件不在允许目录内")
    elif mode != "dir":
        raise UnsafePathError("未知清理方式")
    return key


def _empty_stats():
    return {"freed_bytes": 0, "deleted_files": 0, "skipped_files": 0, "errors": []}


def _unlink(path: str, root_key: str, errors: list) -> tuple[int, bool]:
    key = norm_key(path)
    if not path_is_inside(key, root_key):
        raise UnsafePathError("删除路径越界")
    st = stat_nofollow(path)
    if st is None:
        return 0, False
    if _attrs(st) & 0x400:
        return 0, True
    try:
        os.chmod(io_path(path), stat.S_IWRITE)
    except OSError:
        pass
    try:
        os.unlink(io_path(path))
    except OSError as exc:
        if len(errors) < 30:
            errors.append({"path": to_display(path), "message": str(exc)[:180]})
        return 0, True
    return st.st_size, False


def delete_inside(target, allowed_roots, mode="dir", progress=None) -> dict:
    assert_deletable(target, allowed_roots, mode=mode)
    target_path = to_display(os.fspath(target))
    st = stat_nofollow(target_path)
    stats = _empty_stats()
    if st is None:
        return stats
    if _attrs(st) & 0x400:
        raise UnsafePathError("拒绝清理联接点或符号链接")
    root_key = norm_key(target_path)
    if mode == "file":
        if _attrs(st) & 0x10:
            raise UnsafePathError("目标不是文件")
        freed, skipped = _unlink(target_path, os.path.dirname(root_key), stats["errors"])
        if skipped:
            stats["skipped_files"] += 1
        else:
            stats["freed_bytes"] += freed
            stats["deleted_files"] += 1
        return stats
    if not _attrs(st) & 0x10:
        raise UnsafePathError("目标不是目录")

    files = []
    dirs = []
    stack = [target_path]
    while stack:
        current = stack.pop()
        dirs.append(current)
        iterator = scandir_any(current)
        if iterator is None:
            stats["skipped_files"] += 1
            continue
        with iterator:
            for entry in iterator:
                kind, entry_stat = classify_entry(entry)
                if entry_stat is None or kind is None:
                    stats["skipped_files"] += 1
                    continue
                display = to_display(entry.path)
                if not path_is_inside(display, root_key):
                    raise UnsafePathError("删除路径越界")
                if kind == "dir":
                    stack.append(display)
                else:
                    files.append(display)
    for path in files:
        freed, skipped = _unlink(path, root_key, stats["errors"])
        if skipped:
            stats["skipped_files"] += 1
        else:
            stats["freed_bytes"] += freed
            stats["deleted_files"] += 1
            if progress and stats["deleted_files"] % 100 == 0:
                progress(stats["freed_bytes"], stats["deleted_files"], path)
    for path in sorted(dirs, key=lambda item: item.count("\\"), reverse=True):
        if norm_key(path) == root_key or not path_is_inside(path, root_key):
            continue
        try:
            os.rmdir(io_path(path))
        except OSError:
            pass
    return stats


def build_plan(ids: list[str], rules: list[dict], env: dict):
    by_id = {rule["id"]: rule for rule in rules}
    rejected = []
    plan = []
    for cache_id in ids:
        if not _ID.fullmatch(cache_id):
            rejected.append({"id": cache_id, "reason": "无效的缓存编号"})
            continue
        rule = by_id.get(cache_id)
        if rule is None:
            rejected.append({"id": cache_id, "reason": "不在清理清单内"})
            continue
        if rule.get("risk") != "safe":
            rejected.append({"id": cache_id, "reason": "该项目不会自动清理"})
            continue
        try:
            targets = list(iter_targets(rule, env))
        except UnsafePathError as exc:
            rejected.append({"id": cache_id, "reason": str(exc)})
            continue
        plan.append((rule, targets))
    if rejected:
        return [], rejected
    return plan, []


def _blank(ok: bool, reason: str = "", rejected=None):
    return {
        "ok": ok,
        "reason": reason,
        "rejected": rejected or [],
        "freed_bytes": 0,
        "deleted_files": 0,
        "skipped_files": 0,
        "errors": [],
        "cleaned": [],
    }


def clean_cache_ids(ids, rules=None, env=None, progress=None) -> dict:
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        return _blank(False, "清理请求无效")
    unique = []
    seen = set()
    for item in ids:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    if not unique:
        return _blank(False, "没有选择缓存")
    if len(unique) > 40:
        return _blank(False, "一次选择太多")
    rules = CACHE_RULES if rules is None else rules
    env = env or current_env()
    plan, rejected = build_plan(unique, rules, env)
    if rejected:
        return _blank(False, "包含不能清理的项目，已取消本次清理", rejected)

    validated = []
    for rule, targets in plan:
        for target in targets:
            try:
                assert_deletable(target["target"], [target["allowed"]], mode=target["mode"])
            except UnsafePathError as exc:
                return _blank(False, str(exc), [{"id": rule["id"], "reason": str(exc)}])
            validated.append((rule, target))

    freed = deleted = skipped = 0
    errors = []
    cleaned: dict[str, dict] = {}
    for rule, target in validated:
        def report(done_freed, done_deleted, path, name=rule["name"]):
            if progress:
                progress(name, done_freed, done_deleted, path)

        stats = delete_inside(
            target["target"],
            [target["allowed"]],
            mode=target["mode"],
            progress=report,
        )
        row = cleaned.setdefault(
            rule["id"],
            {"id": rule["id"], "name": rule["name"], "freed_bytes": 0, "deleted_files": 0, "skipped_files": 0},
        )
        row["freed_bytes"] += stats["freed_bytes"]
        row["deleted_files"] += stats["deleted_files"]
        row["skipped_files"] += stats["skipped_files"]
        freed += stats["freed_bytes"]
        deleted += stats["deleted_files"]
        skipped += stats["skipped_files"]
        for error in stats["errors"]:
            if len(errors) < 30:
                errors.append(error)
    return {
        "ok": True,
        "reason": "",
        "rejected": [],
        "freed_bytes": freed,
        "deleted_files": deleted,
        "skipped_files": skipped,
        "errors": errors,
        "cleaned": list(cleaned.values()),
    }


def review_placeholders(env: dict | None = None) -> list[dict]:
    """List user-data folders without walking them. Sizes arrive from the disk scan."""
    env = env or current_env()
    entries = []
    for rule in CACHE_RULES:
        if rule.get("risk") == "safe":
            continue
        try:
            targets = list(iter_targets(rule, env))
        except UnsafePathError:
            continue
        paths = []
        for target in targets:
            shown = target["target"] if target["mode"] == "dir" else target["allowed"]
            if shown not in paths:
                paths.append(shown)
        if not paths:
            continue
        entries.append(
            {
                "id": rule["id"],
                "name": rule["name"],
                "risk": "review",
                "risk_label": "不自动清理",
                "paths": paths,
                "size_bytes": None,
                "file_count": None,
                "unreadable": 0,
                "partial": True,
                "note": ((rule.get("note") or "") + " 体积会在全盘扫描走到该目录后更新。").strip(),
                "recommended": False,
                "selectable": False,
            }
        )
    return entries


def apply_watched_sizes(caches: list[dict], watched: list[dict]) -> list[dict]:
    found = {norm_key(item["path"]): item["size_bytes"] for item in watched}
    if not found:
        return caches
    updated = []
    for entry in caches:
        if entry.get("risk") != "review":
            updated.append(entry)
            continue
        paths = entry.get("paths") or []
        matched = [found[norm_key(path)] for path in paths if norm_key(path) in found]
        if not matched:
            updated.append(entry)
            continue
        row = dict(entry)
        row["size_bytes"] = sum(matched)
        row["partial"] = len(matched) < len(paths)
        note = row.get("note") or ""
        if not row["partial"]:
            row["note"] = note.replace(" 体积会在全盘扫描走到该目录后更新。", "").strip()
        updated.append(row)
    return updated


def mark_recommended(entries: list[dict]) -> list[dict]:
    safe = [
        entry
        for entry in entries
        if entry.get("risk") == "safe" and (entry.get("size_bytes") or 0) >= FLOOR_CACHE
    ]
    safe.sort(key=lambda entry: entry.get("size_bytes") or 0, reverse=True)
    big = [entry for entry in safe if (entry.get("size_bytes") or 0) >= BIG_CACHE]
    chosen = {entry["id"] for entry in (big if big else safe[:3])}
    for entry in entries:
        entry["recommended"] = entry["id"] in chosen
        entry["selectable"] = entry.get("risk") == "safe" and (entry.get("size_bytes") or 0) > 0
    return entries


def measure_rule(rule: dict, env: dict, claimed: set[str], cancel=None) -> dict | None:
    targets = []
    for target in iter_targets(rule, env):
        key = norm_key(target["target"])
        if key in claimed:
            continue
        claimed.add(key)
        targets.append(target)
    if not targets:
        return None
    size = 0
    files = 0
    unreadable = 0
    partial = False
    paths = []
    for target in targets:
        if cancel is not None and cancel.is_set():
            partial = True
            break
        shown = target["target"] if target["mode"] == "dir" else target["allowed"]
        if shown not in paths:
            paths.append(shown)
        if target["mode"] == "file":
            st = stat_nofollow(target["target"])
            if st is None:
                unreadable += 1
                continue
            size += st.st_size
            files += 1
            continue
        stats = sum_tree(target["target"], cancel=cancel)
        size += stats["size_bytes"]
        files += stats["file_count"]
        unreadable += stats["unreadable"]
        partial = partial or stats["partial"]
    if size == 0 and files == 0 and unreadable == 0:
        return None
    note = rule.get("note") or ""
    if unreadable:
        note = (note + " 有部分内容无法读取。").strip()
    if partial:
        note = (note + " 统计未完成。").strip()
    return {
        "id": rule["id"],
        "name": rule["name"],
        "risk": rule["risk"],
        "risk_label": "可安全清理" if rule["risk"] == "safe" else "不自动清理",
        "paths": paths,
        "size_bytes": size,
        "file_count": files,
        "unreadable": unreadable,
        "partial": partial,
        "note": note,
        "recommended": False,
        "selectable": rule["risk"] == "safe" and size > 0,
    }
