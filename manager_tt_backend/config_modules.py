from __future__ import annotations


def ensure_dict(container: dict, key: str) -> dict:
    value = container.get(key)
    if isinstance(value, dict):
        return value
    value = {}
    container[key] = value
    return value


def truncate_text(text: str | None, limit: int = 600) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def mask_secret(secret: str | None) -> str | None:
    if not isinstance(secret, str):
        return secret
    value = secret.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"
