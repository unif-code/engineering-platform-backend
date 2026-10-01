from control_plane.app import __version__


def test_contract_version_is_0_10_0() -> None:
    assert __version__ == "0.12.0"
