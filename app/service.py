"""Scan jobs, saved results, and cache cleanup for the local page."""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from app.actions import delete_listed_files, file_block_reason, launch_uninstall
from app.caches import (
    CACHE_RULES,
    apply_watched_sizes,
    build_plan,
    clean_cache_ids,
    current_env,
    mark_recommended,
    measure_rule,
    review_placeholders,
)
from app.formatutil import format_bytes
from app.report import build_conclusion
from app.scanner import scan_drive
from app.software import list_software, measure_recent_locations
from app.winpath import norm_key

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "last_scan.json"
DRIVE = "C:\\"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def drive_info(root: str = DRIVE) -> dict:
    usage = shutil.disk_usage(root)
    total = usage.total or 0
    return {
        "letter": "C",
        "total": total,
        "used": usage.used,
        "free": usage.free,
        "percent": round(usage.used * 100 / total, 1) if total else 0,
    }


def blank_progress(stage: str = "", rough: int = 0) -> dict:
    return {
        "stage": stage,
        "files": 0,
        "dirs": 0,
        "bytes": 0,
        "current_path": "",
        "inaccessible": 0,
        "inaccessible_samples": [],
        "rough_percent": rough,
        "detail": "",
    }


def empty_summary() -> dict:
    zero = {"bytes": 0, "count": 0}
    return {
        "7": dict(zero),
        "30": dict(zero),
        "90": dict(zero),
        "groups_30": [],
        "groups_90": [],
        "truncated": False,
    }


def empty_clean() -> dict:
    return {
        "running": False,
        "ok": None,
        "message": "",
        "freed_bytes": 0,
        "deleted_files": 0,
        "skipped_files": 0,
        "errors": [],
        "finished_at": None,
    }


def empty_state() -> dict:
    return {
        "phase": "idle",
        "message": "还没有扫描",
        "complete": False,
        "stale": False,
        "started_at": None,
        "finished_at": None,
        "saved_at": None,
        "revision": 0,
        "drive": None,
        "progress": blank_progress(),
        "counted_bytes": 0,
        "top_level": [],
        "top_level_partial": True,
        "top_dirs": [],
        "top_files": [],
        "recent_files": {"7": [], "30": [], "90": []},
        "recent_summary": empty_summary(),
        "software": [],
        "caches": [],
        "conclusion": None,
        "clean": empty_clean(),
        "error": None,
    }


class Analyzer:
    def __init__(self):
        self._lock = threading.Lock()
        self._snap = empty_state()
        self._scan_thread = None
        self._clean_thread = None
        self._cancel = threading.Event()
        self._last_save = 0.0
        self._load()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._snap)

    def _publish(self, **kwargs) -> None:
        with self._lock:
            self._snap.update(kwargs)
            self._snap["conclusion"] = build_conclusion(self._snap)
            self._snap["revision"] = int(self._snap.get("revision") or 0) + 1

    def _set_progress(self, progress: dict, message: str | None = None) -> None:
        with self._lock:
            self._snap["progress"] = progress
            if message:
                self._snap["message"] = message

    def _touch_progress(self, message: str, rough: int, stage: str) -> None:
        with self._lock:
            progress = dict(self._snap.get("progress") or blank_progress())
            progress["rough_percent"] = rough
            progress["stage"] = stage
            self._snap["progress"] = progress
            self._snap["message"] = message

    def save(self) -> None:
        with self._lock:
            self._snap["saved_at"] = now_iso()
            data = copy.deepcopy(self._snap)
        self._write(data)
        self._last_save = time.monotonic()

    def _maybe_save(self) -> None:
        if time.monotonic() - self._last_save < 20:
            return
        try:
            self.save()
        except OSError as exc:
            print(f"[c-disk] 保存结果失败: {exc}", flush=True)

    def _write(self, data: dict) -> None:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = DATA_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(DATA_PATH)

    def _load(self) -> None:
        if not DATA_PATH.exists():
            return
        try:
            loaded = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(loaded, dict):
            return
        base = empty_state()
        for key in base:
            if key in loaded:
                base[key] = loaded[key]
        if base.get("phase") in {"quick", "deep"}:
            base["phase"] = "idle"
            base["message"] = "上次扫描未完成，下面是已保存的部分结果。"
        base["clean"] = empty_clean()
        base["stale"] = False
        self._snap = base

    def start_scan(self) -> dict:
        with self._lock:
            if self._scan_thread and self._scan_thread.is_alive():
                return {"ok": False, "message": "扫描已在进行"}
            if self._clean_thread and self._clean_thread.is_alive():
                return {"ok": False, "message": "正在清理缓存，请稍后再扫描"}
            self._cancel.clear()
            self._scan_thread = threading.Thread(target=self._run_scan, name="c-disk-scan", daemon=True)
            self._scan_thread.start()
        return {"ok": True, "message": "已开始分析"}

    def stop_scan(self) -> dict:
        self._cancel.set()
        return {"ok": True, "message": "正在停止"}

    def on_progress(self, progress: dict) -> None:
        path = progress.get("current_path") or ""
        self._set_progress(progress, f"正在扫描 {path}" if path else "正在扫描 C 盘")

    def on_lists(self, payload: dict) -> None:
        payload = dict(payload)
        watched = payload.pop("watched", [])
        with self._lock:
            caches = apply_watched_sizes(list(self._snap.get("caches") or []), watched)
        path = (payload.get("progress") or {}).get("current_path") or ""
        self._publish(
            caches=mark_recommended(caches),
            message=f"正在扫描 {path}" if path else "正在扫描 C 盘",
            **payload,
        )
        self._maybe_save()

    def _finish_cancel(self) -> None:
        self._publish(
            phase="cancelled",
            complete=False,
            stale=False,
            message="扫描已停止，下面是已完成的部分",
            finished_at=now_iso(),
        )
        self.save()

    def _finish_done(self) -> None:
        with self._lock:
            progress = dict(self._snap.get("progress") or blank_progress())
            progress["rough_percent"] = 100
            progress["stage"] = "完成"
            self._snap["progress"] = progress
            self._snap["phase"] = "done"
            self._snap["complete"] = True
            self._snap["stale"] = False
            self._snap["top_level_partial"] = False
            self._snap["message"] = "扫描完成"
            self._snap["finished_at"] = now_iso()
            self._snap["error"] = None
            self._snap["conclusion"] = build_conclusion(self._snap)
            self._snap["revision"] = int(self._snap.get("revision") or 0) + 1
        self.save()

    def _run_scan(self) -> None:
        cancel = self._cancel
        try:
            print("[c-disk] 开始分析 C 盘", flush=True)
            self._publish(
                phase="quick",
                complete=False,
                stale=True,
                started_at=now_iso(),
                finished_at=None,
                error=None,
                message="正在读取 C 盘容量",
                top_level=[],
                top_dirs=[],
                top_files=[],
                recent_files={"7": [], "30": [], "90": []},
                recent_summary=empty_summary(),
                counted_bytes=0,
                top_level_partial=True,
                progress=blank_progress("磁盘", 5),
            )
            drive = drive_info()
            self._publish(drive=drive, message="正在读取已安装软件")
            self._touch_progress("正在读取已安装软件", 12, "软件")
            apps = list_software()
            if cancel.is_set():
                self._publish(software=apps)
                self._finish_cancel()
                return
            self._publish(software=apps, message="正在测量应用缓存")
            self.save()

            entries = []
            claimed: set[str] = set()
            env = current_env()
            safe_rules = [rule for rule in CACHE_RULES if rule.get("risk") == "safe"]
            for index, rule in enumerate(safe_rules):
                if cancel.is_set():
                    self._publish(caches=mark_recommended(entries))
                    self._finish_cancel()
                    return
                rough = 18 + int(50 * index / max(len(safe_rules), 1))
                self._touch_progress(f"正在测量缓存：{rule['name']}", rough, "缓存")
                try:
                    entry = measure_rule(rule, env, claimed, cancel=cancel)
                except Exception as exc:
                    print(f"[c-disk] 跳过缓存 {rule['id']}: {exc}", flush=True)
                    entry = None
                if entry:
                    entries.append(entry)
                    self._publish(caches=mark_recommended(list(entries)), message=f"已测量缓存：{rule['name']}")
            review_entries = review_placeholders(env)
            caches = mark_recommended(entries + review_entries)
            self._publish(caches=caches, message="正在测量近期安装的软件目录")
            self._touch_progress("正在测量近期安装的软件目录", 72, "软件")
            if cancel.is_set():
                self._finish_cancel()
                return

            def on_app(name: str) -> None:
                self._touch_progress(f"正在测量软件目录：{name}", 76, "软件")

            measure_recent_locations(apps, cancel, on_app)
            if cancel.is_set():
                self._publish(software=apps)
                self._finish_cancel()
                return
            self._publish(software=apps, phase="deep", stale=False, message="开始遍历 C 盘")
            self._touch_progress("开始遍历 C 盘", 8, "全盘")
            self.save()
            print("[c-disk] 开始遍历 C:\\", flush=True)
            with self._lock:
                watch_paths = [
                    path
                    for entry in self._snap.get("caches") or []
                    if entry.get("risk") == "review"
                    for path in entry.get("paths") or []
                ]
            status = scan_drive(
                DRIVE,
                cancel,
                self.on_progress,
                self.on_lists,
                drive.get("used") or 0,
                watch_paths,
            )
            if status == "cancelled":
                self._finish_cancel()
                return
            self._finish_done()
            print("[c-disk] 扫描完成", flush=True)
        except Exception as exc:
            traceback.print_exc()
            self._publish(phase="error", error=str(exc), message="扫描失败", finished_at=now_iso(), complete=False)
            try:
                self.save()
            except OSError:
                pass

    def preview_clean(self, ids: list[str]) -> dict:
        plan, rejected = build_plan(list(ids), CACHE_RULES, current_env())
        if rejected:
            return {
                "ok": False,
                "message": "包含不能清理的项目，已取消本次清理",
                "reason": "包含不能清理的项目，已取消本次清理",
                "rejected": rejected,
            }
        snap = self.snapshot()
        known = {item["id"]: item for item in snap.get("caches") or []}
        items = []
        total = 0
        for rule, targets in plan:
            paths = []
            for target in targets:
                shown = target["target"] if target["mode"] == "dir" else target["allowed"]
                if shown not in paths:
                    paths.append(shown)
            size = (known.get(rule["id"]) or {}).get("size_bytes") or 0
            total += size
            items.append(
                {
                    "id": rule["id"],
                    "name": rule["name"],
                    "paths": paths,
                    "size_bytes": size,
                    "file_count": (known.get(rule["id"]) or {}).get("file_count") or 0,
                }
            )
        return {"ok": True, "items": items, "total_bytes": total, "message": ""}

    def start_clean(self, ids: list[str]) -> dict:
        plan, rejected = build_plan(list(ids), CACHE_RULES, current_env())
        if rejected:
            return {
                "ok": False,
                "message": "包含不能清理的项目，已取消本次清理",
                "rejected": rejected,
            }
        with self._lock:
            if self._snap.get("phase") == "quick":
                return {"ok": False, "message": "正在测量缓存，请稍后再清理"}
            if self._clean_thread and self._clean_thread.is_alive():
                return {"ok": False, "message": "清理已在进行"}
            self._snap["clean"] = empty_clean() | {
                "running": True,
                "message": "正在清理缓存",
            }
            self._clean_thread = threading.Thread(
                target=self._run_clean,
                args=(list(ids),),
                name="c-disk-clean",
                daemon=True,
            )
            self._clean_thread.start()
        return {"ok": True, "message": "已开始清理"}

    def _run_clean(self, ids: list[str]) -> None:
        try:
            def progress(name, freed, deleted, _path):
                with self._lock:
                    self._snap["clean"].update(
                        {
                            "running": True,
                            "message": f"正在清理 {name}，已释放 {format_bytes(freed)}",
                            "freed_bytes": freed,
                            "deleted_files": deleted,
                        }
                    )

            result = clean_cache_ids(ids, progress=progress)
            caches = self._remeasure(ids)
            drive = drive_info()
            if result.get("ok"):
                message = f"已释放 {format_bytes(result['freed_bytes'])}，删除 {result['deleted_files']} 个文件"
                if result["skipped_files"]:
                    message += f"，跳过 {result['skipped_files']} 个正在使用或无法删除的文件"
                if result["deleted_files"] == 0 and result["freed_bytes"] == 0:
                    message = "这些缓存目录里没有可删除的文件"
                    if result["skipped_files"]:
                        message += f"，跳过 {result['skipped_files']} 个文件"
            else:
                message = result.get("reason") or "清理没有执行"
            with self._lock:
                self._snap["drive"] = drive
                self._snap["caches"] = caches
                self._snap["clean"] = {
                    "running": False,
                    "ok": bool(result.get("ok")),
                    "message": message,
                    "freed_bytes": result.get("freed_bytes") or 0,
                    "deleted_files": result.get("deleted_files") or 0,
                    "skipped_files": result.get("skipped_files") or 0,
                    "errors": result.get("errors") or [],
                    "finished_at": now_iso(),
                }
                self._snap["conclusion"] = build_conclusion(self._snap)
                self._snap["revision"] = int(self._snap.get("revision") or 0) + 1
            self.save()
        except Exception as exc:
            traceback.print_exc()
            with self._lock:
                self._snap["clean"] = {
                    "running": False,
                    "ok": False,
                    "message": f"清理失败：{exc}",
                    "freed_bytes": 0,
                    "deleted_files": 0,
                    "skipped_files": 0,
                    "errors": [],
                    "finished_at": now_iso(),
                }
                self._snap["revision"] = int(self._snap.get("revision") or 0) + 1

    def _remeasure(self, ids: list[str]) -> list[dict]:
        wanted = set(ids)
        fresh = {}
        claimed: set[str] = set()
        env = current_env()
        for rule in CACHE_RULES:
            if rule["id"] not in wanted:
                continue
            try:
                entry = measure_rule(rule, env, claimed)
            except Exception as exc:
                print(f"[c-disk] 重新测量失败 {rule['id']}: {exc}", flush=True)
                entry = None
            if entry:
                fresh[entry["id"]] = entry
        with self._lock:
            old = list(self._snap.get("caches") or [])
        updated = []
        seen = set()
        for entry in old:
            seen.add(entry["id"])
            if entry["id"] not in wanted:
                updated.append(entry)
            elif entry["id"] in fresh:
                updated.append(fresh[entry["id"]])
            else:
                cleared = dict(entry)
                cleared["size_bytes"] = 0
                cleared["file_count"] = 0
                cleared["recommended"] = False
                cleared["selectable"] = False
                updated.append(cleared)
        for cache_id, entry in fresh.items():
            if cache_id not in seen:
                updated.append(entry)
        return mark_recommended(updated)

    def _file_rows(self) -> list[dict]:
        with self._lock:
            snap = self._snap
            rows = list(snap.get("top_files") or [])
            recent = snap.get("recent_files") or {}
            for bucket in recent.values():
                if isinstance(bucket, list):
                    rows.extend(bucket)
        return rows

    def _allowed_file_keys(self) -> set[str]:
        keys = set()
        for row in self._file_rows():
            path = row.get("path") or ""
            if path:
                keys.add(norm_key(path))
        return keys

    def preview_files(self, paths: list[str]) -> dict:
        if not isinstance(paths, list) or len(paths) > 100:
            return {"ok": False, "message": "一次最多删除 100 个文件", "items": [], "rejected": [], "total_bytes": 0}
        allowed = self._allowed_file_keys()
        known = {norm_key(row.get("path") or ""): row for row in self._file_rows() if row.get("path")}
        items = []
        rejected = []
        seen = set()
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            key = norm_key(path)
            if key in seen:
                continue
            seen.add(key)
            row = known.get(key)
            if key not in allowed or row is None:
                rejected.append({"path": path, "reason": "不在本次扫描结果里"})
                continue
            reason = file_block_reason(path)
            if reason:
                rejected.append({"path": row.get("path") or path, "reason": reason})
                continue
            items.append(
                {
                    "name": os.path.basename(row["path"]),
                    "path": row["path"],
                    "paths": [row["path"]],
                    "size_bytes": row.get("size_bytes") or 0,
                }
            )
        total = sum(item["size_bytes"] for item in items)
        if not items:
            return {"ok": False, "message": "没有可以删除的文件", "items": [], "rejected": rejected, "total_bytes": 0}
        return {"ok": True, "message": "", "items": items, "rejected": rejected, "total_bytes": total}

    def delete_files(self, paths: list[str]) -> dict:
        preview = self.preview_files(paths)
        if not preview.get("ok"):
            return preview
        allowed = {norm_key(item["path"]) for item in preview["items"]}
        result = delete_listed_files([item["path"] for item in preview["items"]], allowed)
        removed = {norm_key(item["path"]) for item in result.get("deleted") or []}
        if removed:
            with self._lock:
                self._snap["top_files"] = [
                    row for row in self._snap.get("top_files") or [] if norm_key(row.get("path") or "") not in removed
                ]
                recent = self._snap.get("recent_files") or {}
                for bucket, rows in list(recent.items()):
                    if isinstance(rows, list):
                        recent[bucket] = [row for row in rows if norm_key(row.get("path") or "") not in removed]
                self._snap["recent_files"] = recent
                self._snap["drive"] = drive_info()
                freed = result.get("freed_bytes") or 0
                skipped = len(result.get("rejected") or []) + len(preview.get("rejected") or [])
                message = f"已删除 {len(result.get('deleted') or [])} 个文件，释放 {format_bytes(freed)}"
                if skipped:
                    message += f"，{skipped} 个未删除"
                self._snap["clean"] = empty_clean() | {
                    "ok": True,
                    "message": message,
                    "freed_bytes": freed,
                    "deleted_files": len(result.get("deleted") or []),
                    "finished_at": now_iso(),
                }
                self._snap["conclusion"] = build_conclusion(self._snap)
                self._snap["revision"] = int(self._snap.get("revision") or 0) + 1
            self.save()
        result["rejected"] = (preview.get("rejected") or []) + (result.get("rejected") or [])
        result["message"] = (self.snapshot().get("clean") or {}).get("message") or result.get("message") or ""
        result["ok"] = bool(result.get("deleted"))
        if not result["ok"] and not result["message"]:
            result["message"] = "没有删除任何文件"
        return result

    def _shown_software(self, items: list[dict]) -> tuple[list[dict], list[dict]]:
        with self._lock:
            shown = list(self._snap.get("software") or [])
        by_id = {app.get("id"): app for app in shown if app.get("id")}
        fresh = None
        matched = []
        rejected = []
        for item in items:
            if not isinstance(item, dict):
                rejected.append({"name": "", "reason": "请求无效"})
                continue
            name = item.get("name") or ""
            location = item.get("install_location") or ""
            app = by_id.get(item.get("id") or "")
            if app is None:
                wanted = name.casefold()
                loc = os.path.normcase(location)
                for candidate in shown:
                    if (candidate.get("name") or "").casefold() != wanted:
                        continue
                    cand_loc = os.path.normcase(candidate.get("install_location") or "")
                    if loc and cand_loc and loc != cand_loc:
                        continue
                    app = candidate
                    break
            if app is None:
                rejected.append({"name": name, "reason": "不在当前软件列表里"})
                continue
            command = app.get("uninstall_string") or ""
            if not command:
                if fresh is None:
                    fresh = list_software()
                command = _command_for(fresh, app)
            if not command:
                rejected.append({"name": app.get("name") or name, "reason": "没有卸载程序"})
                continue
            matched.append({"name": app.get("name") or name, "install_location": app.get("install_location") or "", "command": command})
        return matched, rejected

    def preview_uninstall(self, items: list[dict]) -> dict:
        if len(items) > 20:
            return {"ok": False, "message": "一次最多卸载 20 个软件", "items": [], "rejected": []}
        matched, rejected = self._shown_software(items)
        if not matched:
            return {"ok": False, "message": "没有可以卸载的软件", "items": [], "rejected": rejected}
        return {
            "ok": True,
            "message": "",
            "items": [
                {"name": item["name"], "paths": [item["install_location"]] if item["install_location"] else [], "size_bytes": None}
                for item in matched
            ],
            "rejected": rejected,
        }

    def start_uninstall(self, items: list[dict]) -> dict:
        preview = self.preview_uninstall(items)
        if not preview.get("ok"):
            return preview
        matched, rejected = self._shown_software(items)
        started = []
        for item in matched:
            try:
                launch_uninstall(item["command"])
            except (OSError, ValueError) as exc:
                rejected.append({"name": item["name"], "reason": str(exc)[:180]})
                continue
            started.append(item["name"])
        if not started:
            return {"ok": False, "message": "没有打开卸载程序", "rejected": rejected, "started": []}
        message = f"已打开 {len(started)} 个卸载程序，请在弹出的窗口里完成卸载"
        if rejected:
            message += f"，{len(rejected)} 个没有打开"
        with self._lock:
            self._snap["clean"] = empty_clean() | {"ok": True, "message": message, "finished_at": now_iso()}
            self._snap["revision"] = int(self._snap.get("revision") or 0) + 1
        return {"ok": True, "message": message, "started": started, "rejected": rejected}


def _command_for(apps: list[dict], wanted: dict) -> str:
    name = (wanted.get("name") or "").casefold()
    location = os.path.normcase(wanted.get("install_location") or "")
    for app in apps:
        if (app.get("name") or "").casefold() != name:
            continue
        cand = os.path.normcase(app.get("install_location") or "")
        if location and cand and location != cand:
            continue
        if app.get("uninstall_string"):
            return app["uninstall_string"]
    return ""
