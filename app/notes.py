"""Short labels for paths this tool will not delete."""

from __future__ import annotations

import os


def annotate(path: str) -> str:
    if not path:
        return ""
    low = path.replace("/", "\\").lower()
    base = os.path.basename(low)
    if base in {"pagefile.sys", "hiberfil.sys", "swapfile.sys"}:
        return "系统文件，本工具不删除"
    if "\\softwaredistribution\\download" in low:
        return "Windows 更新下载，本工具不自动清理"
    if "\\.m2\\repository" in low or low.endswith("\\.m2\\repository"):
        return "Maven 仓库，本工具不自动清理"
    if any(part in low for part in ("\\xwechat", "\\wechat files", "\\xwechat_files", "\\tencent files")):
        return "聊天或用户数据，本工具不自动清理"
    if "\\package cache" in low:
        return "安装包缓存，本工具不自动清理"
    if "\\winsxs" in low:
        return "Windows 组件存储，本工具不删除"
    if "\\.gradle\\jdks" in low or low.endswith("\\.gradle\\jdks"):
        return "Gradle JDK，不是缓存，本工具不删除"
    if "androidstudio" in low:
        return "Android Studio 数据，本工具不自动清理"
    if base == "$recycle.bin":
        return "回收站，可在系统中清空"
    if low.endswith("\\downloads") or "\\downloads\\" in low:
        return "下载目录，请自行确认后再处理"
    return ""
