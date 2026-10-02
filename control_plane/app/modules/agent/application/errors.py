from enum import StrEnum


class AgentBusinessContextReason(StrEnum):
    BUSINESS_CONTEXT_NOT_RECORDED = "BUSINESS_CONTEXT_NOT_RECORDED"
    OWNER_UNAVAILABLE = "OWNER_UNAVAILABLE"
    OWNER_DATA_INVALID = "OWNER_DATA_INVALID"
    OWNER_DATA_AMBIGUOUS = "OWNER_DATA_AMBIGUOUS"
    REQUIREMENT_NOT_FOUND = "REQUIREMENT_NOT_FOUND"
    WORKSPACE_CHANGED = "WORKSPACE_CHANGED"
    WORK_ITEM_NOT_IN_REQUIREMENT = "WORK_ITEM_NOT_IN_REQUIREMENT"
    ASSIGNMENT_MISSING = "ASSIGNMENT_MISSING"
    ASSIGNMENT_CHANGED = "ASSIGNMENT_CHANGED"


class InvalidRequirementExecutionContext(ValueError):
    """The referenced Requirement facts do not form one unambiguous context."""

    def __init__(
        self, message: str = "", *, reason: AgentBusinessContextReason | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
