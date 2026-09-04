from control_plane.app.modules.identity.application.common import audit
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.sessions import validate_session
from control_plane.app.modules.identity.application.super_admin import verify_admin_totp
from control_plane.app.modules.identity.domain.errors import (
    SuperAdminPermissionDenied,
    TotpChallengeFailed,
)
from control_plane.app.modules.identity.domain.policy_reauthentication import (
    POLICY_REAUTH_TTL,
    ConsumedReauthReceipt,
    PolicyReauthBinding,
    PolicyReauthenticationDenied,
)
from control_plane.app.modules.identity.domain.session import SessionKind, SessionPrincipal
from control_plane.app.modules.identity.ports.policy_reauthentication import (
    PolicyReauthenticationRepository,
)
from control_plane.app.modules.identity.ports.repository import IdentityRepository


def _current_session(
    repository: IdentityRepository,
    *,
    raw_session: str,
    binding: PolicyReauthBinding,
    dependencies: IdentityDependencies,
    read_only: bool = False,
) -> SessionPrincipal:
    session = validate_session(
        repository,
        raw_token=raw_session,
        dependencies=dependencies,
        touch_activity=False,
        read_only=read_only,
    )
    if (
        session is None
        or session.session_kind is not SessionKind.FULL
        or not session.is_super_admin
        or session.account_id != binding.actor_id
        or session.session_reference is None
        or session.account_version is None
    ):
        raise PolicyReauthenticationDenied("current FULL Super Admin session required")
    return session


def consume_policy_reauthentication(
    repository: IdentityRepository,
    consumptions: PolicyReauthenticationRepository,
    *,
    raw_session: str,
    totp_code: str,
    binding: PolicyReauthBinding,
    attempt_id: str,
    dependencies: IdentityDependencies,
) -> ConsumedReauthReceipt:
    if attempt_id != binding.command_attempt_id:
        raise PolicyReauthenticationDenied("policy command attempt mismatch")
    session = _current_session(
        repository,
        raw_session=raw_session,
        binding=binding,
        dependencies=dependencies,
    )
    # Session/account locks serialize attempts before challenge rate checks and TOTP CAS.
    if consumptions.attempt_consumed(binding.actor_id, attempt_id):
        raise PolicyReauthenticationDenied("policy command attempt already consumed")
    try:
        actor = verify_admin_totp(
            repository,
            binding.actor_id,
            totp_code,
            purpose=binding.operation,
            dependencies=dependencies,
        )
    except (TotpChallengeFailed, SuperAdminPermissionDenied):
        # The runtime commits these security counters before propagating the denial.
        raise PolicyReauthenticationDenied("policy reauthentication denied") from None
    now = dependencies.clock.now()
    assert session.session_reference is not None and session.account_version is not None
    receipt = ConsumedReauthReceipt(
        receipt_id=str(dependencies.random.uuid4()),
        binding=binding,
        session_reference=session.session_reference,
        account_version=session.account_version,
        consumed_at=now,
        expires_at=now + POLICY_REAUTH_TTL,
    )
    consumptions.insert_consumption(receipt)
    audit(
        repository.db,
        dependencies=dependencies,
        actor=actor,
        action="identity.policy_reauth.consumed",
        target_type="policy_reauth_consumption",
        target_id=receipt.receipt_id,
        result="SUCCESS",
        reason=f"operation={binding.operation}; bindingHash={binding.canonical_hash}",
    )
    return receipt


def validate_consumed_policy_reauthentication(
    repository: IdentityRepository,
    consumptions: PolicyReauthenticationRepository,
    *,
    raw_session: str,
    binding: PolicyReauthBinding,
    receipt: ConsumedReauthReceipt,
    dependencies: IdentityDependencies,
) -> None:
    if not consumptions.matches_consumption(receipt):
        raise PolicyReauthenticationDenied("consumed policy authentication does not match")
    # Current session/account and freshness are judged after the immutable-fact read.
    # No cross-owner lock is implied after this explicit final authorization read.
    session = _current_session(
        repository,
        raw_session=raw_session,
        binding=binding,
        dependencies=dependencies,
        read_only=True,
    )
    assert session.session_reference is not None
    if session.account_version != receipt.account_version or not receipt.matches(
        binding,
        session_reference=session.session_reference,
        now=dependencies.clock.now(),
    ):
        raise PolicyReauthenticationDenied("consumed policy authentication does not match")
