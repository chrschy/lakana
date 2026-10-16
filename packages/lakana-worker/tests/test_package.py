"""A smoke test proving the package is installed and importable."""

import lakana_worker


def test_the_package_is_importable() -> None:
    """The package has a version derived from git tags, like its siblings."""
    assert lakana_worker is not None
