"""Human-readable units, matching pg_size_pretty's binary steps."""


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "kB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{n} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
