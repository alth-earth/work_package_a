"""UTC-only time helpers used at every public boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from arctic_route_data.errors import MetadataValidationError


def ensure_utc(value: datetime, *, field: str = "time") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MetadataValidationError(f"{field} 必须包含时区；请使用 UTC ISO-8601 时间。")
    return value.astimezone(UTC)


def parse_utc(value: str | datetime, *, field: str = "time") -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, field=field)
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MetadataValidationError(f"{field} 不是合法 ISO-8601 时间: {value!r}") from exc
    return ensure_utc(parsed, field=field)


def isoformat_utc(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")
