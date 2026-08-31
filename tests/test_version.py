from control_plane.app import __version__


def test_release_version_is_0_5_0() -> None:
    assert __version__ == "0.5.0"
