from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from cryptography.exceptions import InvalidTag
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from control_plane.app.bootstrap.app import create_app
from control_plane.app.shared.db.settings import SecuritySettings
from control_plane.app.shared.security import (
    FileSecretManager,
    SecretManagerPort,
    SecretMaterial,
    SecretMaterialUnavailable,
    assert_same_origin,
    hash_password,
    seal,
    totp_provisioning_uri,
    unseal,
    validate_password_floor,
    verify_password,
    verify_totp,
)


def _write_secret_material(
    directory: Path,
    *,
    pepper: bytes = b"p" * 32,
    totp_key: bytes = b"t" * 32,
    idempotency_key: bytes = b"i" * 32,
) -> None:
    directory.mkdir(exist_ok=True)
    (directory / "pepper").write_bytes(pepper)
    (directory / "totp_key").write_bytes(totp_key)
    (directory / "idempotency_key").write_bytes(idempotency_key)


def _request_with_browser_metadata(*, origin: str | None, fetch_site: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/change",
            "raw_path": b"/api/v1/change",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("platform.example", 443),
        }
    )


def test_file_secret_manager_loads_three_independent_materials(tmp_path: Path) -> None:
    _write_secret_material(tmp_path)
    manager: SecretManagerPort = FileSecretManager(
        SecuritySettings(secret_material_path=str(tmp_path))
    )

    material = manager.load()

    assert material == SecretMaterial(
        password_pepper=b"p" * 32,
        totp_sealing_key=b"t" * 32,
        idempotency_sealing_key=b"i" * 32,
    )


@pytest.mark.parametrize(
    ("pepper", "totp_key", "idempotency_key"),
    [
        (b"", b"t" * 32, b"i" * 32),
        (b"p" * 32, b"", b"i" * 32),
        (b"p" * 32, b"t" * 32, b""),
        (b"short", b"t" * 32, b"i" * 32),
        (b"p" * 32, b"short", b"i" * 32),
        (b"p" * 32, b"t" * 32, b"short"),
        (b"k" * 32, b"k" * 32, b"i" * 32),
        (b"p" * 32, b"k" * 32, b"k" * 32),
        (b"k" * 32, b"t" * 32, b"k" * 32),
    ],
)
def test_secret_material_constructor_enforces_length_and_independence(
    pepper: bytes, totp_key: bytes, idempotency_key: bytes
) -> None:
    with pytest.raises(SecretMaterialUnavailable, match="secret material is unavailable"):
        SecretMaterial(
            password_pepper=pepper,
            totp_sealing_key=totp_key,
            idempotency_sealing_key=idempotency_key,
        )


@pytest.mark.parametrize("missing_file", ["pepper", "totp_key", "idempotency_key"])
def test_secret_material_fails_closed_when_a_file_is_missing(
    tmp_path: Path, missing_file: str
) -> None:
    _write_secret_material(tmp_path)
    (tmp_path / missing_file).unlink()

    with pytest.raises(SecretMaterialUnavailable, match="secret material is unavailable"):
        SecretMaterial.load(tmp_path)


@pytest.mark.parametrize(
    ("pepper", "totp_key", "idempotency_key"),
    [
        (b"", b"t" * 32, b"i" * 32),
        (b"short", b"t" * 32, b"i" * 32),
        (b"p" * 32, b"short", b"i" * 32),
        (b"p" * 32, b"t" * 32, b"short"),
        (b"p" * 32, b"k" * 32, b"k" * 32),
        (b"k" * 32, b"k" * 32, b"i" * 32),
        (b"k" * 32, b"t" * 32, b"k" * 32),
    ],
)
def test_secret_material_rejects_empty_malformed_or_reused_material(
    tmp_path: Path, pepper: bytes, totp_key: bytes, idempotency_key: bytes
) -> None:
    _write_secret_material(
        tmp_path,
        pepper=pepper,
        totp_key=totp_key,
        idempotency_key=idempotency_key,
    )

    with pytest.raises(SecretMaterialUnavailable, match="secret material is unavailable"):
        SecretMaterial.load(tmp_path)


def test_security_settings_reads_only_the_material_directory_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_MATERIAL_PATH", "/mounted/secrets")
    monkeypatch.setenv("PASSWORD_PEPPER", "must-not-be-a-setting")

    settings = SecuritySettings()

    assert settings.secret_material_path == "/mounted/secrets"
    assert set(type(settings).model_fields) == {"secret_material_path"}


def test_file_secret_manager_propagates_fail_closed_loading(tmp_path: Path) -> None:
    manager: SecretManagerPort = FileSecretManager(
        SecuritySettings(secret_material_path=str(tmp_path / "missing"))
    )

    with pytest.raises(SecretMaterialUnavailable, match="secret material is unavailable"):
        manager.load()


def test_password_roundtrip_depends_on_password_and_pepper() -> None:
    hashed = hash_password("Str0ng!Passw0rd#2026", pepper=b"p1")

    assert hashed.startswith("$argon2id$")
    assert verify_password("Str0ng!Passw0rd#2026", hashed, pepper=b"p1")
    assert not verify_password("wrong-password", hashed, pepper=b"p1")
    assert not verify_password("Str0ng!Passw0rd#2026", hashed, pepper=b"p2")


def test_password_verification_rejects_malformed_hash_without_exposing_an_error() -> None:
    assert not verify_password("Str0ng!Passw0rd#2026", "not-an-argon2-hash", pepper=b"p1")


@pytest.mark.parametrize(
    ("plain", "violation"),
    [
        ("Sh0rt!", "length"),
        ("A!" + "a" * 63, "length"),
        ("alllowercase!123", "uppercase"),
        ("ALLUPPERCASE!123", "lowercase"),
        ("NoSpecialPassword2026", "special"),
        ("Password!2026aaaa", "weak"),
        ("Xx!AcmePortal2026", "context"),
    ],
)
def test_password_floor_reports_each_policy_violation(plain: str, violation: str) -> None:
    context = ["acme"] if violation == "context" else []

    assert violation in validate_password_floor(plain, context=context)


@pytest.mark.parametrize("plain", ["Aa!bcdefghijklm", "A!" + "a" * 62])
def test_password_floor_accepts_valid_inclusive_length_boundaries(plain: str) -> None:
    assert validate_password_floor(plain, context=[]) == []


def test_sealed_data_roundtrips_and_uses_a_fresh_nonce() -> None:
    key = b"k" * 32

    first = seal(b"credential", key)
    second = seal(b"credential", key)

    assert first != second
    assert unseal(first, key) == b"credential"
    assert unseal(second, key) == b"credential"


@pytest.mark.parametrize("corruption", ["tamper", "wrong-key"])
def test_unseal_authenticates_token_and_key(corruption: str) -> None:
    key = b"k" * 32
    token = seal(b"credential", key)
    if corruption == "tamper":
        changed = bytearray(token)
        changed[-1] ^= 1
        token = bytes(changed)
        unsealing_key = key
    else:
        unsealing_key = b"x" * 32

    with pytest.raises(InvalidTag):
        unseal(token, unsealing_key)


def test_sealing_rejects_non_256_bit_keys_and_truncated_tokens() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        seal(b"credential", b"short")
    with pytest.raises(ValueError, match="32 bytes"):
        unseal(b"token", b"short")
    with pytest.raises(ValueError, match="too short"):
        unseal(b"token", b"k" * 32)


def test_totp_provisioning_uri_identifies_account_and_issuer() -> None:
    secret = "JBSWY3DPEHPK3PXP"

    uri = urlparse(totp_provisioning_uri(secret, "alice@example.com"))

    assert (uri.scheme, uri.netloc) == ("otpauth", "totp")
    assert uri.path == "/Engineering%20Platform:alice%40example.com"
    assert parse_qs(uri.query) == {
        "secret": [secret],
        "issuer": ["Engineering Platform"],
    }


def test_totp_provisioning_uri_rejects_an_empty_account() -> None:
    with pytest.raises(ValueError, match="account is required"):
        totp_provisioning_uri("JBSWY3DPEHPK3PXP", "  ")


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_totp_returns_the_step_actually_matched_in_the_allowed_window(
    monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    fixed_time = 1_800_000_000
    monkeypatch.setattr("control_plane.app.shared.security.totp.time.time", lambda: fixed_time)
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).at(fixed_time + offset * 30)

    matched_step = verify_totp(secret, code, last_used_step=None)

    assert matched_step == fixed_time // 30 + offset


def test_totp_rejects_replay_across_the_full_allowed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_time = 1_800_000_000
    monkeypatch.setattr("control_plane.app.shared.security.totp.time.time", lambda: fixed_time)
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    current_step = fixed_time // 30

    assert verify_totp(secret, totp.at(fixed_time - 30), last_used_step=current_step) is None
    assert verify_totp(secret, totp.at(fixed_time), last_used_step=current_step) is None
    assert (
        verify_totp(secret, totp.at(fixed_time + 30), last_used_step=current_step)
        == current_step + 1
    )


@pytest.mark.parametrize(
    ("secret", "code"),
    [("not-base32!", "123456"), (pyotp.random_base32(), "not-a-code")],
)
def test_totp_rejects_malformed_secret_or_code(secret: str, code: str) -> None:
    assert verify_totp(secret, code, last_used_step=None) is None


@pytest.mark.parametrize(
    ("origin", "fetch_site"),
    [
        ("https://platform.example", "same-origin"),
        ("https://platform.example:443", None),
        (None, "same-origin"),
        (None, None),
    ],
)
def test_same_origin_accepts_consistent_browser_or_metadata_free_server_requests(
    origin: str | None, fetch_site: str | None
) -> None:
    request = _request_with_browser_metadata(origin=origin, fetch_site=fetch_site)

    assert_same_origin(request)


@pytest.mark.parametrize(
    ("origin", "fetch_site"),
    [
        ("https://evil.example", "cross-site"),
        ("https://platform.example", "cross-site"),
        ("https://evil.example", "same-origin"),
        (None, "same-site"),
        ("null", None),
        ("not an origin", None),
    ],
)
def test_same_origin_rejects_cross_site_conflicting_or_malformed_metadata(
    origin: str | None, fetch_site: str | None
) -> None:
    request = _request_with_browser_metadata(origin=origin, fetch_site=fetch_site)

    with pytest.raises(HTTPException) as raised:
        assert_same_origin(request)

    assert raised.value.status_code == 403
    assert raised.value.detail == "Cross-origin request forbidden"


def test_same_origin_rejection_is_rendered_as_a_403_problem() -> None:
    app = create_app()

    @app.post("/test-only/same-origin")
    async def same_origin_probe(request: Request) -> dict[str, str]:
        assert_same_origin(request)
        return {"status": "ok"}

    response = TestClient(app).post(
        "/test-only/same-origin",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == "Cross-origin request forbidden"
