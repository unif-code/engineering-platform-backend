from control_plane.app import __version__


def test_release_version_is_0_4_0() -> None:
    assert __version__ == "0.4.0"
