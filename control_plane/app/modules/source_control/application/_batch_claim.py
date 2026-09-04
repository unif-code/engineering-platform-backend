class InboxClaimLost(Exception):
    """Another worker owns the exact Inbox lease selected from a read-only scan."""


class InboxProcessingFailed(Exception):
    """Failure after an acquired claim; retry must use that claim's attempt fence."""

    def __init__(self, cause: Exception, *, expected_attempts: int) -> None:
        super().__init__("Inbox processing failed")
        self.cause = cause
        self.expected_attempts = expected_attempts


__all__: list[str] = []
