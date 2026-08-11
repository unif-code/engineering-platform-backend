import binascii
import hmac
import time

import pyotp

_ISSUER = "Engineering Platform"
_WINDOW_OFFSETS = (-1, 0, 1)


def totp_provisioning_uri(secret: str, account: str) -> str:
    """Build an authenticator-compatible provisioning URI."""
    normalized_account = account.strip()
    if not normalized_account:
        raise ValueError("account is required")
    return pyotp.TOTP(secret).provisioning_uri(
        name=normalized_account,
        issuer_name=_ISSUER,
    )


def verify_totp(secret: str, code: str, *, last_used_step: int | None) -> int | None:
    """Return a matched step in the ±1 window unless it has already been consumed."""
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None

    try:
        totp = pyotp.TOTP(secret)
        current_step = int(time.time()) // totp.interval
        matched_steps: list[int] = []
        for offset in _WINDOW_OFFSETS:
            step = current_step + offset
            if hmac.compare_digest(totp.at(step * totp.interval), code):
                matched_steps.append(step)
    except (binascii.Error, TypeError, ValueError):
        return None

    fresh_steps = [
        step for step in matched_steps if last_used_step is None or step > last_used_step
    ]
    return max(fresh_steps, default=None)
