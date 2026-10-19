"""Byte sizes for the written conclusion."""


def format_bytes(value) -> str:
    if value is None:
        return "未知"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    if number < 0:
        return "未知"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    if index == 0:
        return f"{int(number)} B"
    text = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{text} {units[index]}"
