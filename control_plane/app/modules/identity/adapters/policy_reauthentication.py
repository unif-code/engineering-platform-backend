from sqlalchemy import Connection, Engine, text

from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.policy_reauthentication import (
    consume_policy_reauthentication,
    validate_consumed_policy_reauthentication,
)
from control_plane.app.modules.identity.domain.policy_reauthentication import (
    ConsumedReauthReceipt,
    PolicyReauthBinding,
    PolicyReauthenticationDenied,
    PolicyReauthenticationUnavailable,
)


def _values(receipt: ConsumedReauthReceipt) -> dict[str, object]:
    return {
        "id": receipt.receipt_id,
        "actor_id": receipt.binding.actor_id,
        "attempt_id": receipt.binding.command_attempt_id,
        "binding": receipt.binding.canonical_json(),
        "binding_hash": receipt.binding.canonical_hash,
        "session_reference": receipt.session_reference,
        "account_version": receipt.account_version,
        "consumed_at": receipt.consumed_at,
        "expires_at": receipt.expires_at,
    }


class SqlAlchemyPolicyReauthenticationRepository:
    def __init__(self, db: Connection) -> None:
        self._db = db

    def attempt_consumed(self, actor_id: str, attempt_id: str) -> bool:
        return bool(
            self._db.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM identity.policy_reauth_consumption "
                    "WHERE actor_id=:actor_id AND attempt_id=:attempt_id)"
                ),
                {"actor_id": actor_id, "attempt_id": attempt_id},
            ).scalar_one()
        )

    def insert_consumption(self, receipt: ConsumedReauthReceipt) -> None:
        self._db.execute(
            text(
                "INSERT INTO identity.policy_reauth_consumption "
                "(id, actor_id, attempt_id, binding, binding_hash, session_reference, "
                "account_version, consumed_at, expires_at) VALUES "
                "(:id, :actor_id, :attempt_id, CAST(:binding AS jsonb), :binding_hash, "
                ":session_reference, :account_version, :consumed_at, :expires_at)"
            ),
            _values(receipt),
        )

    def matches_consumption(self, receipt: ConsumedReauthReceipt) -> bool:
        return bool(
            self._db.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM identity.policy_reauth_consumption "
                    "WHERE id=:id AND actor_id=:actor_id AND attempt_id=:attempt_id "
                    "AND binding=CAST(:binding AS jsonb) AND binding_hash=:binding_hash "
                    "AND session_reference=:session_reference AND account_version=:account_version "
                    "AND consumed_at=:consumed_at AND expires_at=:expires_at)"
                ),
                _values(receipt),
            ).scalar_one()
        )


class IdentityPolicyReauthenticationRuntime:
    """Independent Identity commit boundary; never accepts another owner's Connection."""

    def __init__(self, engine: Engine, dependencies: IdentityDependencies) -> None:
        self._engine = engine
        self._dependencies = dependencies

    def verify_and_consume_policy_reauth(
        self,
        *,
        raw_session: str,
        totp_code: str,
        binding: PolicyReauthBinding,
        attempt_id: str,
    ) -> ConsumedReauthReceipt:
        receipt: ConsumedReauthReceipt | None = None
        denial: PolicyReauthenticationDenied | None = None
        try:
            with self._engine.begin() as db:
                try:
                    receipt = consume_policy_reauthentication(
                        self._dependencies.repository_factory(db),
                        SqlAlchemyPolicyReauthenticationRepository(db),
                        raw_session=raw_session,
                        totp_code=totp_code,
                        binding=binding,
                        attempt_id=attempt_id,
                        dependencies=self._dependencies,
                    )
                except PolicyReauthenticationDenied as error:
                    denial = error
            # Only this point proves transaction exit/commit succeeded.
        except Exception:
            raise PolicyReauthenticationUnavailable("policy reauthentication unavailable") from None
        if denial is not None:
            raise denial
        if receipt is None:
            raise PolicyReauthenticationUnavailable("policy reauthentication unavailable")
        return receipt

    def validate_consumed_policy_reauth(
        self,
        *,
        raw_session: str,
        binding: PolicyReauthBinding,
        receipt: ConsumedReauthReceipt,
    ) -> None:
        try:
            with self._engine.begin() as db:
                db.execute(text("SET TRANSACTION READ ONLY"))
                validate_consumed_policy_reauthentication(
                    self._dependencies.repository_factory(db),
                    SqlAlchemyPolicyReauthenticationRepository(db),
                    raw_session=raw_session,
                    binding=binding,
                    receipt=receipt,
                    dependencies=self._dependencies,
                )
        except PolicyReauthenticationDenied:
            raise
        except Exception:
            raise PolicyReauthenticationUnavailable("policy reauthentication unavailable") from None
