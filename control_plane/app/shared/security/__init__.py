"""Shared cryptographic primitives and secret material boundaries."""

from control_plane.app.shared.security.csrf import assert_same_origin
from control_plane.app.shared.security.password import (
    hash_password,
    validate_password_floor,
    verify_password,
)
from control_plane.app.shared.security.sealed import seal, unseal
from control_plane.app.shared.security.secrets import (
    FileSecretManager,
    SecretManagerPort,
    SecretMaterial,
    SecretMaterialUnavailable,
)
from control_plane.app.shared.security.totp import totp_provisioning_uri, verify_totp

__all__ = [
    "FileSecretManager",
    "SecretManagerPort",
    "SecretMaterial",
    "SecretMaterialUnavailable",
    "assert_same_origin",
    "hash_password",
    "seal",
    "totp_provisioning_uri",
    "unseal",
    "validate_password_floor",
    "verify_password",
    "verify_totp",
]
