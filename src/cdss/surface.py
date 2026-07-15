"""Phase 0 step 5: enumerate the visible view/table surface (D-001 input).

Combines INFORMATION_SCHEMA.TABLES (object + type) with an effective-
permissions check (sys.fn_my_permissions) so `can_select` reflects an actual
grantable SELECT, not just metadata visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cdss.source import AuditedSourceConnection

ObjectType = Literal["view", "table", "other"]

_TABLE_TYPE_MAP: dict[str, ObjectType] = {
    "VIEW": "view",
    "BASE TABLE": "table",
}

TABLES_QUERY = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
)

SELECTABLE_QUERY = (
    "SELECT s.name, o.name "
    "FROM sys.objects o "
    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
    "CROSS APPLY sys.fn_my_permissions(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT') p "
    "WHERE o.type IN ('U', 'V') AND p.permission_name = 'SELECT' "
    "ORDER BY s.name, o.name"
)


@dataclass(frozen=True)
class SurfaceObject:
    schema: str
    name: str
    object_type: ObjectType
    can_select: bool

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


def enumerate_surface(audited: AuditedSourceConnection) -> list[SurfaceObject]:
    tables = audited.execute_query(TABLES_QUERY)
    selectable = {(str(row[0]), str(row[1])) for row in audited.execute_query(SELECTABLE_QUERY)}

    objects = []
    for schema_raw, name_raw, table_type_raw in tables:
        schema, name, table_type = str(schema_raw), str(name_raw), str(table_type_raw)
        objects.append(
            SurfaceObject(
                schema=schema,
                name=name,
                object_type=_TABLE_TYPE_MAP.get(table_type, "other"),
                can_select=(schema, name) in selectable,
            )
        )
    return objects
