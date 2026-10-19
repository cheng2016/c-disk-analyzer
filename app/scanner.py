"""Full C: walk. Junctions and symbolic links are not followed."""

from __future__ import annotations

import heapq
import os
import time
from datetime import datetime

from app.notes import annotate
from app.winpath import classify_entry, norm_key, scandir_any, to_display

MIN_RECENT_BYTES = 50 * 1024 * 1024
DAY = 86400


class TopList:
    def __init__(self, limit: int):
        self.limit = limit
        self.heap = []
        self.seq = 0

    def accepts(self, size: int, floor: int = 0) -> bool:
        if size < floor:
            return False
        if len(self.heap) < self.limit:
            return True
        return size > self.heap[0][0]

    def add(self, size: int, item: dict) -> None:
        self.seq += 1
        entry = (size, self.seq, item)
        if len(self.heap) < self.limit:
            heapq.heappush(self.heap, entry)
        elif size > self.heap[0][0]:
            heapq.heapreplace(self.heap, entry)

    def items(self) -> list[dict]:
        ranked = sorted(self.heap, key=lambda row: (-row[0], -row[1]))
        return [dict(item) for _size, _seq, item in ranked]


def _created(timestamp: float) -> str:
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _record(path: str, size: int, created_ts: float) -> dict:
    display = to_display(path)
    created = int(created_ts) if created_ts else 0
    return {
        "path": display,
        "size_bytes": size,
        "created_ts": created,
        "created": _created(created),
        "note": annotate(display),
    }


def _group_key(path: str) -> str:
    parent = os.path.dirname(to_display(path))
    parts = parent.split("\\")
    if len(parts) > 4:
        return "\\".join(parts[:4])
    return parent or path


def _top_groups(counter: dict, limit: int = 8) -> list[dict]:
    ranked = sorted(counter.items(), key=lambda item: -item[1])[:limit]
    return [{"path": path, "size_bytes": size} for path, size in ranked]


def _dir_row(path: str, size: int, depth: int) -> dict:
    display = to_display(path)
    return {"path": display, "size_bytes": size, "depth": depth, "note": annotate(display)}


class DriveScan:
    def __init__(self, root: str, cancel, drive_used: int, watch_paths=None):
        self.root = root
        self.cancel = cancel
        self.drive_used = drive_used or 0
        self.watch = {norm_key(path) for path in (watch_paths or [])}
        self.watched = []
        self.now = time.time()
        self.cuts = {7: self.now - 7 * DAY, 30: self.now - 30 * DAY, 90: self.now - 90 * DAY}
        self.recent = {7: TopList(800), 30: TopList(800), 90: TopList(800)}
        self.counts = {7: {"bytes": 0, "count": 0}, 30: {"bytes": 0, "count": 0}, 90: {"bytes": 0, "count": 0}}
        self.groups = {30: {}, 90: {}}
        self.top_files = TopList(100)
        self.top_dirs = TopList(80)
        self.top_level = []
        self.files = 0
        self.dirs = 0
        self.counted = 0
        self.inaccessible = 0
        self.samples = []
        self.stack = []
        self.cancelled = False

    def _note_unreadable(self, path: str) -> None:
        self.inaccessible += 1
        if len(self.samples) < 12:
            self.samples.append(to_display(path))

    def _branch(self) -> str:
        for frame in self.stack:
            if frame["depth"] == 1:
                return to_display(frame["path"])
        if self.stack:
            return to_display(self.stack[-1]["path"])
        return self.root

    def progress(self) -> dict:
        rough = 0
        if self.drive_used:
            rough = min(99, int(self.counted * 100 / self.drive_used))
        return {
            "stage": "全盘",
            "files": self.files,
            "dirs": self.dirs,
            "bytes": self.counted,
            "current_path": self._branch(),
            "inaccessible": self.inaccessible,
            "inaccessible_samples": list(self.samples),
            "rough_percent": rough,
            "detail": "",
        }

    def payload(self, partial: bool) -> dict:
        return {
            "top_level": sorted(self.top_level, key=lambda row: -row["size_bytes"]),
            "top_level_partial": partial,
            "top_dirs": self.top_dirs.items(),
            "top_files": self.top_files.items(),
            "recent_files": {str(days): self.recent[days].items() for days in (7, 30, 90)},
            "recent_summary": {
                "7": dict(self.counts[7]),
                "30": dict(self.counts[30]),
                "90": dict(self.counts[90]),
                "groups_30": _top_groups(self.groups[30]),
                "groups_90": _top_groups(self.groups[90]),
                "truncated": False,
            },
            "counted_bytes": self.counted,
            "progress": self.progress(),
            "watched": list(self.watched),
        }

    def _remember_recent(self, record: dict, created: float) -> None:
        if created > self.now + DAY:
            return
        size = record["size_bytes"]
        group = _group_key(record["path"])
        for days in (90, 30, 7):
            if created < self.cuts[days]:
                continue
            self.counts[days]["bytes"] += size
            self.counts[days]["count"] += 1
            self.recent[days].add(size, record)
            if days in self.groups:
                self.groups[days][group] = self.groups[days].get(group, 0) + size

    def _finish_dir(self, frame: dict) -> None:
        self.dirs += 1
        size = frame["size"]
        if self.stack:
            self.stack[-1]["size"] += size
        display = to_display(frame["path"])
        if norm_key(display) in self.watch:
            self.watched.append({"path": display, "size_bytes": size})
        if frame["depth"] == 1:
            self.top_level.append(_dir_row(frame["path"], size, 1))
        elif frame["depth"] >= 2 and size >= MIN_RECENT_BYTES:
            row = _dir_row(frame["path"], size, frame["depth"])
            self.top_dirs.add(size, row)

    def _close_all(self) -> None:
        for frame in self.stack:
            try:
                frame["it"].close()
            except OSError:
                pass

    def run(self, on_progress, on_lists) -> str:
        iterator = scandir_any(self.root)
        if iterator is None:
            self._note_unreadable(self.root)
            on_lists(self.payload(partial=True))
            return "done"
        self.stack.append({"path": self.root, "depth": 0, "size": 0, "it": iterator})
        last_progress = 0.0
        last_lists = 0.0
        try:
            while self.stack:
                if self.cancel is not None and self.cancel.is_set():
                    self.cancelled = True
                    break
                frame = self.stack[-1]
                try:
                    entry = next(frame["it"])
                except StopIteration:
                    frame["it"].close()
                    self._finish_dir(self.stack.pop())
                    continue
                except OSError:
                    self._note_unreadable(frame["path"])
                    try:
                        frame["it"].close()
                    except OSError:
                        pass
                    self._finish_dir(self.stack.pop())
                    continue

                kind, st = classify_entry(entry)
                if st is None:
                    self._note_unreadable(entry.path)
                elif kind == "dir":
                    child = scandir_any(entry.path)
                    if child is None:
                        self._note_unreadable(entry.path)
                    else:
                        self.stack.append(
                            {"path": entry.path, "depth": frame["depth"] + 1, "size": 0, "it": child}
                        )
                elif kind == "file":
                    size = st.st_size
                    frame["size"] += size
                    self.files += 1
                    self.counted += size
                    if self.top_files.accepts(size):
                        self.top_files.add(size, _record(entry.path, size, st.st_ctime))
                    created = st.st_ctime or 0
                    if size >= MIN_RECENT_BYTES and created >= self.cuts[90]:
                        self._remember_recent(_record(entry.path, size, created), created)

                now = time.monotonic()
                if on_progress and now - last_progress >= 0.6:
                    last_progress = now
                    on_progress(self.progress())
                if on_lists and now - last_lists >= 3:
                    last_lists = now
                    on_lists(self.payload(partial=True))
        finally:
            self._close_all()
            if on_lists:
                on_lists(self.payload(partial=self.cancelled))
        return "cancelled" if self.cancelled else "done"


def scan_drive(root: str, cancel, on_progress, on_lists, drive_used: int, watch_paths=None) -> str:
    return DriveScan(root, cancel, drive_used, watch_paths).run(on_progress, on_lists)
