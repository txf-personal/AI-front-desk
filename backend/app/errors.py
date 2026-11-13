"""Shop rules refuse a change before any row is written."""


class PolicyError(Exception):
    """A shop rule refused the change. No row was written."""


def require_text(value: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(message)
    return value.strip()
