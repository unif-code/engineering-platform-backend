import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from control_plane.app.modules.source_control import SourceControlDependencyUnavailable
from control_plane.app.modules.source_control.adapters import (
    DevSecretReferenceResolver,
    SourceControlDevPolicy,
    SourceControlDevSettings,
)

NOW = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)


def _settings(secret_root: Path, **overrides: object) -> SourceControlDevSettings:
    values: dict[str, object] = {
        "gitlab_api_url": "https://gitlab.dev.example/api/v4",
        "connection_id": "gitlab-dev",
        "request_timeout_seconds": 5.0,
        "policy_version": 1,
        "reconcile_base_delay_seconds": 15,
        "reconcile_max_delay_seconds": 120,
        "webhook_replay_window_seconds": 300,
        "secret_reference_root": secret_root,
    }
    values.update(overrides)
    return SourceControlDevSettings.model_validate(values)


def test_dev_settings_are_typed_bounded_and_contain_no_secret_values(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    assert str(settings.gitlab_api_url) == "https://gitlab.dev.example/api/v4"
    assert settings.connection_id == "gitlab-dev"
    assert settings.request_timeout_seconds == 5.0
    assert settings.policy_version == 1
    assert settings.reconcile_base_delay_seconds == 15
    assert settings.reconcile_max_delay_seconds == 120
    assert settings.webhook_replay_window_seconds == 300
    assert settings.secret_reference_root == tmp_path
    serialized = str(settings.model_dump(mode="json")).lower()
    assert all(name not in serialized for name in ("token", "password", "pat", "secret_value"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"gitlab_api_url": "ftp://gitlab.dev.example/api/v4"},
        {"connection_id": " "},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": 31},
        {"policy_version": 0},
        {"reconcile_base_delay_seconds": 121},
        {"webhook_replay_window_seconds": 29},
        {"webhook_replay_window_seconds": 901},
    ],
)
def test_dev_settings_reject_missing_or_invalid_configuration(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, **overrides)


def test_dev_settings_require_complete_non_secret_configuration() -> None:
    with pytest.raises(ValidationError):
        SourceControlDevSettings.model_validate({})


def test_dev_secret_resolver_reads_one_bounded_non_empty_file(tmp_path: Path) -> None:
    root = tmp_path / "source-control-secrets"
    root.mkdir()
    (root / "gitlab-pat").write_text("test-only-token\n", encoding="utf-8")
    resolver = DevSecretReferenceResolver(root=root, max_bytes=64)

    assert resolver.resolve("secret-ref:gitlab-pat") == "test-only-token"


@pytest.mark.parametrize(
    "reference",
    [
        "openbao:gitlab-pat",
        "secret-ref:/absolute",
        "secret-ref:C:/absolute",
        "secret-ref:../outside",
        "secret-ref:nested/../../outside",
        "secret-ref:nested\\outside",
        "secret-ref:",
    ],
)
def test_dev_secret_resolver_rejects_non_dev_or_escaping_references(
    tmp_path: Path,
    reference: str,
) -> None:
    resolver = DevSecretReferenceResolver(root=tmp_path, max_bytes=64)

    with pytest.raises(SourceControlDependencyUnavailable):
        resolver.resolve(reference)


def test_dev_secret_resolver_prevents_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside-token"
    outside.write_text("must-not-escape", encoding="utf-8")
    link = root / "linked-token"
    try:
        link.symlink_to(outside)
    except OSError:
        outside_directory = tmp_path / "outside-directory"
        outside_directory.mkdir()
        (outside_directory / "token").write_text("must-not-escape", encoding="utf-8")
        link = root / "linked-directory"
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside_directory)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip("This host permits neither symlinks nor directory junctions")
        reference = "secret-ref:linked-directory/token"
    else:
        reference = "secret-ref:linked-token"

    with pytest.raises(SourceControlDependencyUnavailable):
        DevSecretReferenceResolver(root=root, max_bytes=64).resolve(reference)


@pytest.mark.parametrize("content", ["", "sensitive-content-that-is-too-large"])
def test_dev_secret_resolver_errors_never_echo_file_content(
    tmp_path: Path,
    content: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "invalid").write_text(content, encoding="utf-8")
    resolver = DevSecretReferenceResolver(root=root, max_bytes=8)

    with pytest.raises(SourceControlDependencyUnavailable) as captured:
        resolver.resolve("secret-ref:invalid")

    if content:
        assert content not in str(captured.value)
    assert str(captured.value) == "Source Control secret is unavailable"


def test_dev_policy_produces_deterministic_bounded_backoff_and_replay_window(
    tmp_path: Path,
) -> None:
    policy = SourceControlDevPolicy(_settings(tmp_path))

    assert policy.version == 1
    assert policy.next_reconcile_at(now=NOW, attempts=0) == NOW + timedelta(seconds=15)
    assert policy.next_reconcile_at(now=NOW, attempts=1) == NOW + timedelta(seconds=15)
    assert policy.next_reconcile_at(now=NOW, attempts=2) == NOW + timedelta(seconds=30)
    assert policy.next_reconcile_at(now=NOW, attempts=3) == NOW + timedelta(seconds=60)
    assert policy.next_reconcile_at(now=NOW, attempts=99) == NOW + timedelta(seconds=120)
    assert policy.webhook_replay_window() == timedelta(seconds=300)
