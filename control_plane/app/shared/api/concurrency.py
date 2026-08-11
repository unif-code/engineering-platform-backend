import re
from typing import Annotated

from fastapi import Header, HTTPException

_ENTITY_TAG = re.compile(r'"v([1-9][0-9]*)"\Z', re.ASCII)


def entity_tag(version: int) -> str:
    if version < 1:
        raise ValueError("entity version must be positive")
    return f'"v{version}"'


def require_if_match(
    value: Annotated[
        str,
        Header(
            alias="If-Match",
            description='Strong entity tag in the form "v<positive-version>"',
            examples=['"v1"'],
        ),
    ],
) -> int:
    match = _ENTITY_TAG.fullmatch(value)
    if match is None:
        raise HTTPException(status_code=422, detail="Invalid If-Match")
    return int(match.group(1))
