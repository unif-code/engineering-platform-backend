from control_plane.app.modules.agent_run.domain import DenialCode


class SandboxApplicationError(RuntimeError):
    """Safe application failure with a canonical platform denial."""

    def __init__(
        self,
        code: DenialCode,
        failure_dimension: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{code.value}:{failure_dimension}")
        self.code = code
        self.failure_dimension = failure_dimension
        self.retryable = retryable


class SandboxAdmissionError(SandboxApplicationError):
    pass


class PolicyDisabled(SandboxAdmissionError):
    def __init__(self) -> None:
        super().__init__(DenialCode.POLICY_DISABLED, "admission_policy")


class PolicyLimitReached(SandboxAdmissionError):
    def __init__(self) -> None:
        super().__init__(
            DenialCode.POLICY_LIMIT_REACHED,
            "active_attempt_limit",
            retryable=True,
        )


class CapacityUnavailable(SandboxAdmissionError):
    def __init__(self) -> None:
        super().__init__(
            DenialCode.CAPACITY_UNAVAILABLE,
            "capacity_units",
            retryable=True,
        )


class ActiveExecutionConflict(SandboxAdmissionError):
    def __init__(self) -> None:
        super().__init__(DenialCode.RUNTIME_BINDING_INVALID, "active_execution")


class EnvironmentConflict(SandboxAdmissionError):
    def __init__(self) -> None:
        super().__init__(DenialCode.RUNTIME_BINDING_INVALID, "sandbox_environment")


class PolicySnapshotConflict(SandboxAdmissionError):
    def __init__(self) -> None:
        super().__init__(DenialCode.RUNTIME_BINDING_INVALID, "capacity_policy")


class StaleRunnerGeneration(SandboxApplicationError):
    def __init__(self) -> None:
        super().__init__(
            DenialCode.STALE_RUNNER_GENERATION,
            "runner_generation",
        )
