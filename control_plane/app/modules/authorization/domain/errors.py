class AuthorizationError(RuntimeError):
    """Base class for expected authorization-domain errors."""


class InvalidGrant(AuthorizationError):
    pass


class GrantNotFound(AuthorizationError):
    pass


class StaleGrantVersion(AuthorizationError):
    pass


class AuthorizationUnavailable(AuthorizationError):
    pass


class AuthorizationDenied(AuthorizationError):
    pass
