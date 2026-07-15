import cdss


def test_package_importable() -> None:
    assert cdss.__version__ == "0.1.0"
