"""Phase 3 step 5: bind_named_params bridges cdss.compiler's T-SQL named
params (`@name`) to the ODBC positional (`?`) contract the source access
layer requires -- pure, no database needed.
"""

from __future__ import annotations

import pytest

from cdss.executor import bind_named_params


def test_single_param_is_replaced_and_bound() -> None:
    sql, params = bind_named_params("SELECT * FROM t WHERE id = @foo", {"foo": 42})
    assert sql == "SELECT * FROM t WHERE id = ?"
    assert params == [42]


def test_multiple_distinct_params_bind_in_occurrence_order() -> None:
    sql, params = bind_named_params(
        "SELECT * FROM t WHERE a > @lo AND a <= @hi", {"lo": 1, "hi": 2}
    )
    assert sql == "SELECT * FROM t WHERE a > ? AND a <= ?"
    assert params == [1, 2]


def test_same_param_repeated_binds_value_each_occurrence() -> None:
    sql, params = bind_named_params("SELECT @x, @x FROM t", {"x": 7})
    assert sql == "SELECT ?, ? FROM t"
    assert params == [7, 7]


def test_no_params_in_sql_leaves_text_unchanged() -> None:
    sql, params = bind_named_params("SELECT * FROM t", {"unused": 1})
    assert sql == "SELECT * FROM t"
    assert params == []


def test_missing_bound_value_raises_key_error_naming_it() -> None:
    with pytest.raises(KeyError, match="missing_param"):
        bind_named_params("SELECT * FROM t WHERE id = @missing_param", {})


def test_similarly_prefixed_names_are_not_confused() -> None:
    sql, params = bind_named_params(
        "SELECT * FROM t WHERE a = @code AND b IN (@code_0, @code_1)",
        {"code": "x", "code_0": "y", "code_1": "z"},
    )
    assert sql == "SELECT * FROM t WHERE a = ? AND b IN (?, ?)"
    assert params == ["x", "y", "z"]
