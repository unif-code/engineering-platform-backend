class AgentDomainError(ValueError):
    """A deterministic Agent control-plane domain denial."""


class IllegalAttemptTransition(AgentDomainError):
    pass


class CheckpointRequired(AgentDomainError):
    """Raised when an Attempt would be waiting without a checkpoint."""


class AttemptNotResumable(AgentDomainError):
    pass


class CheckpointReplacementForbidden(AgentDomainError):
    pass


class InvalidFencingToken(AgentDomainError):
    pass


class ResumeGenerationRequired(AgentDomainError):
    pass


class RepositoryWriteForbidden(Exception):
    """Raised before a write-capable runtime permission enters a binding."""

    pass


class EventReplayConflict(AgentDomainError):
    """Raised when an event replay changes immutable canonical evidence."""

    pass
