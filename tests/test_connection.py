from cdss.config import SourceConfig
from cdss.connection import build_connection_string

WINDOWS_CFG = SourceConfig(
    host="192.168.0.9",
    port=1433,
    database="INDICI_BI_Full",
    auth="windows",
    user=None,
    password=None,
    encrypt=True,
    trust_server_certificate=True,
)

SQL_CFG = SourceConfig(
    host="sql01.internal",
    port=1434,
    database="INDICI_BI_Full",
    auth="sql",
    user="cdss_reader",
    password="s3cr3t-value-9f2a",
    encrypt=False,
    trust_server_certificate=False,
)


def test_windows_auth_connection_string_uses_trusted_connection() -> None:
    conn_str = build_connection_string(WINDOWS_CFG)
    assert "SERVER=192.168.0.9,1433" in conn_str
    assert "DATABASE=INDICI_BI_Full" in conn_str
    assert "Trusted_Connection=yes" in conn_str
    assert "Encrypt=yes" in conn_str
    assert "TrustServerCertificate=yes" in conn_str
    assert "UID=" not in conn_str
    assert "PWD=" not in conn_str


def test_sql_auth_connection_string_uses_credentials() -> None:
    conn_str = build_connection_string(SQL_CFG)
    assert "SERVER=sql01.internal,1434" in conn_str
    assert "UID=cdss_reader" in conn_str
    assert "PWD=s3cr3t-value-9f2a" in conn_str
    assert "Trusted_Connection" not in conn_str
    assert "Encrypt=no" in conn_str
    assert "TrustServerCertificate=no" in conn_str


def test_connection_string_never_appears_truncated_driver() -> None:
    conn_str = build_connection_string(WINDOWS_CFG)
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in conn_str
