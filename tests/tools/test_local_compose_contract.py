from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "docker-compose.yml"
README_FILE = REPOSITORY_ROOT / "README.md"


def mapping_block(source: str, key: str, *, indent: int) -> str:
    marker = f"{' ' * indent}{key}:\n"
    lines = source.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line == marker]
    assert len(matches) == 1
    block: list[str] = []

    for line in lines[matches[0] + 1 :]:
        line_indent = len(line) - len(line.lstrip(" "))
        if line.strip() and line_indent <= indent:
            break
        block.append(line)

    return "".join(block)


def assert_local_compose_names(compose: str) -> None:
    assert compose.startswith("name: engineering-platform-local\n")
    services = mapping_block(compose, "services", indent=0)
    postgres = mapping_block(services, "postgres", indent=2)
    postgres_volumes = mapping_block(postgres, "volumes", indent=4)
    postgres_networks = mapping_block(postgres, "networks", indent=4)
    volumes = mapping_block(compose, "volumes", indent=0)
    pgdata = mapping_block(volumes, "pgdata", indent=2)
    networks = mapping_block(compose, "networks", indent=0)
    default_network = mapping_block(networks, "default", indent=2)

    assert "    container_name: engineering-platform-local-postgres\n" in postgres
    assert "      - pgdata:/var/lib/postgresql\n" in postgres_volumes
    assert "      - default\n" in postgres_networks
    assert "    name: engineering-platform-local-postgres-data\n" in pgdata
    assert "    name: engineering-platform-local-network\n" in default_network


def test_local_compose_resources_have_stable_names() -> None:
    assert_local_compose_names(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_local_compose_contract_rejects_container_name_on_another_service() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    misplaced_name = compose.replace(
        "services:\n",
        "services:\n"
        "  decoy:\n"
        "    image: postgres:18\n"
        "    container_name: engineering-platform-local-postgres\n",
    ).replace(
        "  postgres:\n    container_name: engineering-platform-local-postgres\n",
        "  postgres:\n",
    )

    with pytest.raises(AssertionError):
        assert_local_compose_names(misplaced_name)


@pytest.mark.parametrize(
    ("expected_section", "misplaced_value", "incorrect_section"),
    [
        (
            "    volumes:\n      - pgdata:/var/lib/postgresql\n",
            "      - pgdata:/var/lib/postgresql\n",
            "    volumes:\n      - wrongdata:/var/lib/postgresql\n",
        ),
        (
            "    networks:\n      - default\n",
            "      - default\n",
            "    networks:\n      - wrongnet\n",
        ),
    ],
)
def test_local_compose_contract_rejects_wiring_outside_its_section(
    expected_section: str,
    misplaced_value: str,
    incorrect_section: str,
) -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    misplaced_wiring = compose.replace(
        "    image: postgres:18\n",
        f"    image: postgres:18\n    command:\n{misplaced_value}",
    ).replace(
        expected_section,
        incorrect_section,
    )

    with pytest.raises(AssertionError):
        assert_local_compose_names(misplaced_wiring)


@pytest.mark.parametrize(
    ("resource", "name"),
    [
        ("Compose 项目", "engineering-platform-local"),
        ("PostgreSQL 容器", "engineering-platform-local-postgres"),
        ("PostgreSQL 数据卷", "engineering-platform-local-postgres-data"),
        ("默认网络", "engineering-platform-local-network"),
    ],
)
def test_readme_documents_stable_local_compose_names(resource: str, name: str) -> None:
    readme = README_FILE.read_text(encoding="utf-8")

    assert f"| {resource} | `{name}` |" in readme


def test_readme_distinguishes_safe_shutdown_from_data_deletion() -> None:
    readme = README_FILE.read_text(encoding="utf-8")

    assert "`docker compose down` 只停止并移除容器与网络，保留数据库卷" in readme
    assert "仅在明确要清空本地数据时\n才使用 `docker compose down --volumes`" in readme
