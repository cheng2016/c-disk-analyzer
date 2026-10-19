"""Plain-language answers for the three questions on the page."""

from __future__ import annotations

from app.formatutil import format_bytes


def _names(apps: list[dict], limit: int = 3) -> str:
    ranked = sorted(apps, key=lambda app: app.get("size_bytes") or 0, reverse=True)
    parts = []
    for app in ranked[:limit]:
        if not app.get("size_bytes"):
            continue
        parts.append(f"{app['name']}（{format_bytes(app['size_bytes'])}）")
    return "、".join(parts)


def build_conclusion(snap: dict) -> dict:
    drive = snap.get("drive") or {}
    used = drive.get("used") or 0
    software = snap.get("software") or []
    caches = snap.get("caches") or []
    recent = [app for app in software if app.get("recent")]
    known = [app for app in recent if app.get("size_bytes")]
    total = sum(app["size_bytes"] for app in known)
    unknown = len(recent) - len(known)
    percent = (total / used * 100) if used else 0
    major = bool(known) and (percent >= 15 or total >= 10 * 1024**3)
    names = _names(known)
    if not recent:
        software_text = "卸载注册表里没有近 90 天的安装记录。最近安装的软件解释不了这次 C 盘占用。"
        software_tone = "ok"
    elif not known:
        software_text = (
            f"近 90 天安装了 {len(recent)} 个软件，但注册表里没有可用的体积，"
            "还要看下面的目录和文件才能判断它们占了多少。"
        )
        software_tone = "info"
    else:
        extra = f"另有 {unknown} 个没有体积数据。" if unknown else ""
        if major:
            software_text = (
                f"近 90 天安装了 {len(recent)} 个软件，能统计到的体积合计 {format_bytes(total)}，"
                f"约占已用空间 {percent:.1f}%。最近安装的软件占得比较多。"
            )
            software_tone = "warn"
        else:
            software_text = (
                f"近 90 天安装了 {len(recent)} 个软件，能统计到的体积合计 {format_bytes(total)}，"
                f"约占已用空间 {percent:.1f}%。最近安装的软件不是 C 盘空间的主要来源。"
            )
            software_tone = "ok"
        if names:
            software_text += f"较大的是 {names}。"
        if extra:
            software_text += extra

    summary = snap.get("recent_summary") or {}
    last_30 = summary.get("30") or {}
    last_90 = summary.get("90") or {}
    phase = snap.get("phase")
    partial = bool(snap.get("top_level_partial"))
    if phase == "quick" and not (last_90.get("count") or 0):
        recent_text = "全盘扫描还没开始。近 30 天和近 90 天的大文件会在扫描过程中出现。"
        recent_tone = "info"
    else:
        groups = summary.get("groups_30") or []
        grouped = "、".join(
            f"{item['path']}（{format_bytes(item['size_bytes'])}）" for item in groups[:3]
        )
        recent_text = (
            f"近 30 天新增且大于 50 MB 的文件合计 {format_bytes(last_30.get('bytes') or 0)}"
            f"（{last_30.get('count') or 0} 个），近 90 天合计 {format_bytes(last_90.get('bytes') or 0)}"
            f"（{last_90.get('count') or 0} 个）。"
        )
        if grouped:
            recent_text += f"近 30 天主要集中在 {grouped}。"
        elif not (last_30.get("count") or 0):
            recent_text += "近 30 天没有看到大于 50 MB 的新文件。"
        if partial and phase != "done":
            recent_text += "扫描还没走完，这些数字还会增加。"
        recent_bytes = last_30.get("bytes") or 0
        recent_tone = "warn" if recent_bytes >= 5 * 1024**3 else "info"

    levels = snap.get("top_level") or []
    dirs = snap.get("top_dirs") or []
    if not levels and phase != "done":
        culprit_text = "正在统计各目录体积。每个一级目录扫描完成后会出现在总览里。"
        culprit_tone = "info"
    else:
        level_text = "、".join(
            f"{row['path']}（{format_bytes(row['size_bytes'])}）" for row in levels[:3]
        )
        sub_text = "、".join(
            f"{row['path']}（{format_bytes(row['size_bytes'])}）" for row in dirs[:3]
        )
        culprit_text = f"一级目录里最大的是 {level_text or '暂无'}。"
        if sub_text:
            culprit_text += f"更具体的大目录是 {sub_text}。"
        notes = []
        for row in list(dirs[:5]) + list(levels[:4]):
            if row.get("note"):
                notes.append(row["note"])
        unique = list(dict.fromkeys(notes))
        if unique:
            culprit_text += "其中：" + "；".join(unique[:4]) + "。"
        counted = snap.get("counted_bytes") or 0
        if phase == "done" and counted and used:
            culprit_text += (
                f"全盘文件体积合计 {format_bytes(counted)}，系统报告已用 {format_bytes(used)}。"
                "两边不会完全一样，差在无权限目录、压缩和磁盘簇。"
            )
        inaccessible = (snap.get("progress") or {}).get("inaccessible") or 0
        if inaccessible:
            culprit_text += f"有 {inaccessible} 处没有权限读取。"
        if partial and phase != "done":
            culprit_text += "还有目录没扫完。"
        culprit_tone = "warn"

    safe = [item for item in caches if item.get("risk") == "safe"]
    safe_bytes = sum(item.get("size_bytes") or 0 for item in safe)
    recommended = [item for item in safe if item.get("recommended")]
    recommended_bytes = sum(item.get("size_bytes") or 0 for item in recommended)
    recommended_names = "、".join(item["name"] for item in recommended[:6])
    cache_text = f"可安全清理的应用缓存合计 {format_bytes(safe_bytes)}。"
    if recommended:
        cache_text += (
            f"已默认选中体积靠前的 {len(recommended)} 项，约 {format_bytes(recommended_bytes)}："
            f"{recommended_names}。"
        )
    else:
        cache_text += "目前没有体积明显、且可以安全清理的缓存。"
    review = [
        item
        for item in caches
        if item.get("risk") == "review" and (item.get("size_bytes") or 0) >= 500 * 1024 * 1024
    ]
    if review:
        review_text = "、".join(
            f"{item['name']}（{format_bytes(item['size_bytes'])}）" for item in review[:4]
        )
        cache_text += f"另外这些大项不会自动删：{review_text}。"
    return {
        "blocks": [
            {"title": "最近安装的软件", "text": software_text, "tone": software_tone},
            {"title": "最近新增的文件", "text": recent_text, "tone": recent_tone},
            {"title": "占用大户", "text": culprit_text, "tone": culprit_tone},
            {"title": "应用缓存", "text": cache_text, "tone": "info"},
        ],
        "recent_software_bytes": total,
        "recent_software_percent": round(percent, 1),
        "recent_software_is_major": major,
        "safe_cache_bytes": safe_bytes,
    }
