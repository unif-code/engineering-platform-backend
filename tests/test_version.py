from control_plane.app import __version__


def test_release_version_is_0_9_0() -> None:
    assert __version__ == "0.9.0"
