"""Human-readable units: sizes in pg_size_pretty's binary steps, and durations."""

BLOCK_BYTES = 8192  # a PostgreSQL block, as the statistics count them


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "kB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{n} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.0f} days"
