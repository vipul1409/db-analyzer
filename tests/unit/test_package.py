import db_analyzer


def test_package_exposes_version() -> None:
    assert db_analyzer.__version__ == "0.1.0"
