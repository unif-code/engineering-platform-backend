from control_plane.app import __version__


def test_contract_version_is_4_0_0() -> None:
    assert __version__ == "4.0.0"
