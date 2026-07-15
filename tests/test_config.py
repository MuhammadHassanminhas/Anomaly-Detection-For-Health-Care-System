import logging

import pytest

from cdss.config import MissingSourceConfigError, load_source_config

WINDOWS_AUTH_VARS = {
    "CDSS_SOURCE_HOST": "192.168.0.9",
    "CDSS_SOURCE_PORT": "1433",
    "CDSS_SOURCE_DB": "INDICI_BI_Full",
    "CDSS_SOURCE_AUTH": "windows",
    "CDSS_SOURCE_ENCRYPT": "true",
    "CDSS_SOURCE_TRUST_SERVER_CERTIFICATE": "true",
}

SQL_AUTH_VARS = {
    **WINDOWS_AUTH_VARS,
    "CDSS_SOURCE_AUTH": "sql",
    "CDSS_SOURCE_USER": "cdss_reader",
    "CDSS_SOURCE_PASSWORD": "s3cr3t-value-9f2a",
}


def test_load_windows_auth_config_present() -> None:
    cfg = load_source_config(WINDOWS_AUTH_VARS)
    assert cfg.host == "192.168.0.9"
    assert cfg.port == 1433
    assert cfg.database == "INDICI_BI_Full"
    assert cfg.auth == "windows"
    assert cfg.user is None
    assert cfg.password is None
    assert cfg.encrypt is True
    assert cfg.trust_server_certificate is True


def test_load_sql_auth_config_present() -> None:
    cfg = load_source_config(SQL_AUTH_VARS)
    assert cfg.auth == "sql"
    assert cfg.user == "cdss_reader"
    assert cfg.password == "s3cr3t-value-9f2a"


def test_windows_auth_does_not_require_user_or_password() -> None:
    # Present in WINDOWS_AUTH_VARS already without USER/PASSWORD; must not raise.
    load_source_config(WINDOWS_AUTH_VARS)


def test_sql_auth_missing_user_and_password_raises() -> None:
    bad = dict(WINDOWS_AUTH_VARS)
    bad["CDSS_SOURCE_AUTH"] = "sql"
    with pytest.raises(MissingSourceConfigError) as exc_info:
        load_source_config(bad)
    message = str(exc_info.value)
    assert "CDSS_SOURCE_USER" in message
    assert "CDSS_SOURCE_PASSWORD" in message


def test_load_source_config_missing_always_required() -> None:
    with pytest.raises(MissingSourceConfigError) as exc_info:
        load_source_config({})
    message = str(exc_info.value)
    for name in (
        "CDSS_SOURCE_HOST",
        "CDSS_SOURCE_PORT",
        "CDSS_SOURCE_DB",
        "CDSS_SOURCE_AUTH",
        "CDSS_SOURCE_ENCRYPT",
        "CDSS_SOURCE_TRUST_SERVER_CERTIFICATE",
    ):
        assert name in message


def test_load_source_config_partial() -> None:
    partial = dict(WINDOWS_AUTH_VARS)
    del partial["CDSS_SOURCE_PORT"]
    with pytest.raises(MissingSourceConfigError) as exc_info:
        load_source_config(partial)
    message = str(exc_info.value)
    assert "CDSS_SOURCE_PORT" in message
    assert "CDSS_SOURCE_HOST" not in message


def test_load_source_config_invalid_port() -> None:
    bad = dict(WINDOWS_AUTH_VARS)
    bad["CDSS_SOURCE_PORT"] = "not-a-number"
    with pytest.raises(ValueError, match="CDSS_SOURCE_PORT"):
        load_source_config(bad)


def test_load_source_config_invalid_encrypt() -> None:
    bad = dict(WINDOWS_AUTH_VARS)
    bad["CDSS_SOURCE_ENCRYPT"] = "maybe"
    with pytest.raises(ValueError, match="CDSS_SOURCE_ENCRYPT"):
        load_source_config(bad)


def test_load_source_config_invalid_trust_server_certificate() -> None:
    bad = dict(WINDOWS_AUTH_VARS)
    bad["CDSS_SOURCE_TRUST_SERVER_CERTIFICATE"] = "maybe"
    with pytest.raises(ValueError, match="CDSS_SOURCE_TRUST_SERVER_CERTIFICATE"):
        load_source_config(bad)


def test_load_source_config_invalid_auth() -> None:
    bad = dict(WINDOWS_AUTH_VARS)
    bad["CDSS_SOURCE_AUTH"] = "kerberos"
    with pytest.raises(ValueError, match="CDSS_SOURCE_AUTH"):
        load_source_config(bad)


def test_source_config_repr_never_leaks_password() -> None:
    cfg = load_source_config(SQL_AUTH_VARS)
    assert "s3cr3t-value-9f2a" not in repr(cfg)
    assert "s3cr3t-value-9f2a" not in str(cfg)


def test_log_scrubbing_no_secret_value_in_any_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = load_source_config(SQL_AUTH_VARS)
    logger = logging.getLogger("cdss.config.test")
    with caplog.at_level(logging.DEBUG):
        logger.info("loaded config: %s", cfg)
        logger.debug("config repr: %r", cfg)
    assert caplog.records
    for record in caplog.records:
        assert "s3cr3t-value-9f2a" not in record.getMessage()
