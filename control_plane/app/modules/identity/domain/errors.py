class IdentityError(RuntimeError):
    pass


class AccountConflict(IdentityError):
    pass


class AccountNotFound(IdentityError):
    pass


class InvalidAccountTransition(IdentityError):
    pass


class StaleAccountVersion(IdentityError):
    pass


class LastEffectiveSuperAdmin(IdentityError):
    pass


class SuperAdminBootstrapConflict(IdentityError):
    pass


class SuperAdminConflict(IdentityError):
    pass


class SuperAdminPermissionDenied(IdentityError):
    pass


class SuperAdminRecoveryDenied(IdentityError):
    pass


class PasswordFloorViolation(IdentityError):
    def __init__(self, violations: list[str]) -> None:
        super().__init__("password does not meet the security floor")
        self.violations = tuple(violations)


class AuthenticationFailed(IdentityError):
    pass


class LoginBackoffActive(IdentityError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("login backoff is active")
        self.retry_after_seconds = retry_after_seconds


class TotpChallengeFailed(IdentityError):
    pass
