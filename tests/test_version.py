from control_plane.app import __version__


def test_release_version_is_0_3_0() -> None:
    assert __version__ == "0.3.0"
