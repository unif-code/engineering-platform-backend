class InboxClaimLost(Exception):
    """Another worker owns the exact Inbox lease selected from a read-only scan."""


__all__: list[str] = []
