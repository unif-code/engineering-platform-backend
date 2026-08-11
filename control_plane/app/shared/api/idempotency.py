import re
from typing import Annotated

from fastapi import Header, HTTPException

_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z", re.ASCII)


def require_idempotency_key(
    value: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
            description=(
                "Stable replay key: 8-128 ASCII letters, digits, dot, underscore, colon, or hyphen"
            ),
        ),
    ],
) -> str:
    """Return a bounded, log-safe command key or reject the request."""
    if _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise HTTPException(status_code=422, detail="Invalid Idempotency-Key")
    return value
