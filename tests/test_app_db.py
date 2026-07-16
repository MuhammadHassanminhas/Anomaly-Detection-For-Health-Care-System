import pytest

from cdss.app_db import MissingAppDbConfigError, load_app_db_url


def test_load_app_db_url_present() -> None:
    url = load_app_db_url({"CDSS_APP_DB_URL": "postgresql+psycopg://u:p@localhost/cdss_app"})
    assert url == "postgresql+psycopg://u:p@localhost/cdss_app"


def test_load_app_db_url_missing_raises() -> None:
    with pytest.raises(MissingAppDbConfigError, match="CDSS_APP_DB_URL"):
        load_app_db_url({})


def test_load_app_db_url_empty_raises() -> None:
    with pytest.raises(MissingAppDbConfigError, match="CDSS_APP_DB_URL"):
        load_app_db_url({"CDSS_APP_DB_URL": ""})
