from control_plane.app import __version__


def test_release_version_is_0_2_2() -> None:
    assert __version__ == "0.2.2"
