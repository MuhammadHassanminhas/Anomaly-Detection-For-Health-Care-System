"""Phase 1 step 7: export reconciliation & discrepancy log tests. Parsing
tests use small synthetic fixtures shaped like schema_for_SQL_PROJ.txt's
real (inconsistent) formatting -- no live database involved.
"""

import json
from pathlib import Path

from cdss.export_reconciliation import (
    ColumnDiscrepancy,
    ExportColumnHypothesis,
    ExportViewHypothesis,
    RelationDiscrepancy,
    ViewDiscrepancyReport,
    _parse_columns_information,
    _parse_related_tables,
    parse_export_hypotheses,
    reconcile_view,
)

# --- _parse_columns_information ---------------------------------------------


def test_parse_columns_information_empty_text() -> None:
    assert _parse_columns_information("") == []
    assert _parse_columns_information("   ") == []


def test_parse_columns_information_skips_header_row() -> None:
    text = "Column | Data Type | Column Description\nProfileID | int | Primary key"
    assert _parse_columns_information(text) == [
        ExportColumnHypothesis(name="ProfileID", data_type_hint="int")
    ]


def test_parse_columns_information_hash_prefixed_header_row() -> None:
    text = "#Columns | #Data Type | #Explain\nDiseaseID | int | key column"
    assert _parse_columns_information(text) == [
        ExportColumnHypothesis(name="DiseaseID", data_type_hint="int")
    ]


def test_parse_columns_information_row_without_description() -> None:
    text = "Column | Data Type | Column Description\nExtention | nvarchar"
    assert _parse_columns_information(text) == [
        ExportColumnHypothesis(name="Extention", data_type_hint="nvarchar")
    ]


def test_parse_columns_information_multi_line_description_still_extracts_name_and_type() -> None:
    text = (
        "Column | Data Type | Column Description\n"
        "VaccineIndication | nvarchar | reason or stage, such as a dose scheduled\n"
        "          at a specific age (e.g.,\n"
        "          3 months,\n"
        "          5 months).\n"
        "NeedleLength | int | size in millimeters"
    )
    assert _parse_columns_information(text) == [
        ExportColumnHypothesis(name="VaccineIndication", data_type_hint="nvarchar"),
        ExportColumnHypothesis(name="NeedleLength", data_type_hint="int"),
    ]


# --- _parse_related_tables ---------------------------------------------------


def test_parse_related_tables_empty_text() -> None:
    assert _parse_related_tables("") == []


def test_parse_related_tables_blank_line_separated() -> None:
    text = "dbo.immunisation: Records vaccines.\n\ndbo.diagnosis: Stores diagnoses.\n\n"
    assert _parse_related_tables(text) == ["dbo.immunisation", "dbo.diagnosis"]


def test_parse_related_tables_arrow_separated() -> None:
    text = "dbo.Appointments → AppointmentID  Connects.\ndbo.Patient => ProfileID unique id."
    assert _parse_related_tables(text) == ["dbo.Appointments", "dbo.Patient"]


def test_parse_related_tables_en_dash_separated() -> None:
    text = "dbo.patient – linked via PatientID\ndbo.provider – linked via ProviderID"
    assert _parse_related_tables(text) == ["dbo.patient", "dbo.provider"]


def test_parse_related_tables_single_sentence_no_separator() -> None:
    text = "dbo.patient Used to extract patient detail by patientid"
    assert _parse_related_tables(text) == ["dbo.patient"]


def test_parse_related_tables_ignores_short_non_schema_tokens() -> None:
    text = "dbo.patient e.g. some note, i.e. another, vs. something"
    assert _parse_related_tables(text) == ["dbo.patient"]


def test_parse_related_tables_dedupes_case_insensitively() -> None:
    text = "dbo.Patient linked via PatientID\ndbo.patient also mentioned again"
    assert _parse_related_tables(text) == ["dbo.Patient"]


def test_parse_related_tables_multiple_names_per_line() -> None:
    text = "dbo.appointmentservices / dbo.newappointmentservices – services provided"
    assert _parse_related_tables(text) == ["dbo.appointmentservices", "dbo.newappointmentservices"]


# --- parse_export_hypotheses --------------------------------------------------


def test_parse_export_hypotheses_end_to_end(tmp_path: Path) -> None:
    fixture = [
        {
            "table": "dbo.SyntheticEmpty",
            "columns": [],
            "tablerelations": "",
            "columnsinformation": "",
        },
        {
            "table": "dbo.SyntheticView",
            "columns": ["SyntheticID", "SyntheticName"],
            "tablerelations": "dbo.SyntheticOther: related via SyntheticOtherID.",
            "columnsinformation": (
                "Column | Data Type | Column Description\n"
                "SyntheticID | int | Primary key\n"
                "SyntheticName | nvarchar | A name"
            ),
        },
    ]
    path = tmp_path / "synthetic_export.txt"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    hypotheses = parse_export_hypotheses(path)

    assert hypotheses["dbo.SyntheticEmpty"] == ExportViewHypothesis(
        qualified_name="dbo.SyntheticEmpty", columns=[], related_table_names=[]
    )
    assert hypotheses["dbo.SyntheticView"] == ExportViewHypothesis(
        qualified_name="dbo.SyntheticView",
        columns=[
            ExportColumnHypothesis(name="SyntheticID", data_type_hint="int"),
            ExportColumnHypothesis(name="SyntheticName", data_type_hint="nvarchar"),
        ],
        related_table_names=["dbo.SyntheticOther"],
    )


# --- reconcile_view ------------------------------------------------------------


def test_reconcile_view_empty_hypothesis_produces_zero_discrepancies() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticEmpty", columns=[], related_table_names=[]
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticempty": [("SyntheticID", "int")]},
        in_scope_views=frozenset({"dbo.syntheticempty"}),
    )
    assert report == ViewDiscrepancyReport(
        qualified_name="dbo.SyntheticEmpty", column_discrepancies=[], relation_discrepancies=[]
    )


def test_reconcile_view_documented_column_missing_live() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView",
        columns=[ExportColumnHypothesis(name="GhostColumn", data_type_hint="int")],
        related_table_names=[],
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticview": []},
        in_scope_views=frozenset({"dbo.syntheticview"}),
    )
    assert report.column_discrepancies == [
        ColumnDiscrepancy(
            discrepancy_type="documented_missing_live",
            column_name="GhostColumn",
            documented_type="int",
            live_type=None,
        )
    ]


def test_reconcile_view_undocumented_live_column() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView",
        columns=[ExportColumnHypothesis(name="DocumentedColumn", data_type_hint="int")],
        related_table_names=[],
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={
            "dbo.syntheticview": [("DocumentedColumn", "int"), ("SurpriseColumn", "nvarchar")]
        },
        in_scope_views=frozenset({"dbo.syntheticview"}),
    )
    assert report.column_discrepancies == [
        ColumnDiscrepancy(
            discrepancy_type="undocumented_live_column",
            column_name="SurpriseColumn",
            documented_type=None,
            live_type="nvarchar",
        )
    ]


def test_reconcile_view_type_mismatch_different_family() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView",
        columns=[ExportColumnHypothesis(name="SomeDate", data_type_hint="date")],
        related_table_names=[],
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticview": [("SomeDate", "int")]},
        in_scope_views=frozenset({"dbo.syntheticview"}),
    )
    assert report.column_discrepancies == [
        ColumnDiscrepancy(
            discrepancy_type="type_mismatch",
            column_name="SomeDate",
            documented_type="date",
            live_type="int",
        )
    ]


def test_reconcile_view_same_type_family_no_discrepancy() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView",
        columns=[ExportColumnHypothesis(name="SomeDate", data_type_hint="date")],
        related_table_names=[],
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticview": [("SomeDate", "datetime")]},
        in_scope_views=frozenset({"dbo.syntheticview"}),
    )
    assert report.column_discrepancies == []


def test_reconcile_view_unrecognized_documented_type_not_compared() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView",
        columns=[ExportColumnHypothesis(name="Weird", data_type_hint="not_a_real_type")],
        related_table_names=[],
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticview": [("Weird", "int")]},
        in_scope_views=frozenset({"dbo.syntheticview"}),
    )
    assert report.column_discrepancies == []


def test_reconcile_view_relation_out_of_scope() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView", columns=[], related_table_names=["dbo.NotInScope"]
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticview": []},
        in_scope_views=frozenset({"dbo.syntheticview"}),
    )
    assert report.relation_discrepancies == [
        RelationDiscrepancy(related_table_name="dbo.NotInScope", status="out_of_scope")
    ]


def test_reconcile_view_relation_in_scope_corroborated_by_shared_id_column() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView", columns=[], related_table_names=["dbo.SyntheticOther"]
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={
            "dbo.syntheticview": [("SyntheticOtherID", "int")],
            "dbo.syntheticother": [("SyntheticOtherID", "int")],
        },
        in_scope_views=frozenset({"dbo.syntheticview", "dbo.syntheticother"}),
    )
    assert report.relation_discrepancies == [
        RelationDiscrepancy(related_table_name="dbo.SyntheticOther", status="in_scope_corroborated")
    ]


def test_reconcile_view_relation_in_scope_uncorroborated_no_shared_id() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView", columns=[], related_table_names=["dbo.SyntheticOther"]
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={
            "dbo.syntheticview": [("Unrelated", "int")],
            "dbo.syntheticother": [("AlsoUnrelated", "int")],
        },
        in_scope_views=frozenset({"dbo.syntheticview", "dbo.syntheticother"}),
    )
    assert report.relation_discrepancies == [
        RelationDiscrepancy(
            related_table_name="dbo.SyntheticOther", status="in_scope_uncorroborated"
        )
    ]


def test_reconcile_view_relation_in_scope_but_not_yet_profiled() -> None:
    hypothesis = ExportViewHypothesis(
        qualified_name="dbo.SyntheticView", columns=[], related_table_names=["dbo.SyntheticOther"]
    )
    report = reconcile_view(
        hypothesis,
        live_columns_by_view={"dbo.syntheticview": []},
        in_scope_views=frozenset({"dbo.syntheticview", "dbo.syntheticother"}),
    )
    assert report.relation_discrepancies == [
        RelationDiscrepancy(
            related_table_name="dbo.SyntheticOther", status="in_scope_uncorroborated"
        )
    ]


def test_dataclasses_are_frozen() -> None:
    col = ExportColumnHypothesis(name="X", data_type_hint="int")
    view = ExportViewHypothesis(qualified_name="dbo.X", columns=[col], related_table_names=[])
    disc = ColumnDiscrepancy(
        discrepancy_type="type_mismatch", column_name="X", documented_type="int", live_type="date"
    )
    rel = RelationDiscrepancy(related_table_name="dbo.Y", status="out_of_scope")
    report = ViewDiscrepancyReport(
        qualified_name="dbo.X", column_discrepancies=[disc], relation_discrepancies=[rel]
    )
    assert view.qualified_name == "dbo.X"
    assert report.qualified_name == "dbo.X"
