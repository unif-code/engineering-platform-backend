"""Platform-owned bounds for facts that must remain publicly representable."""

from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field

PlatformReference = Annotated[str, Field(min_length=1, max_length=2048)]
PlatformName = Annotated[str, Field(min_length=1, max_length=200)]
PlatformSummary = Annotated[str, Field(max_length=10_000)]
PlatformUUID = Annotated[str, AfterValidator(lambda value: str(UUID(value)))]
ContentDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
