from control_plane.app import __version__


def test_version_is_single_sourced() -> None:
    assert __version__ == "0.2.0"
