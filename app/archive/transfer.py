"""Copy the IBO archive between environments (for example production to dev).

The archive is parsed from source PDFs that only live on one machine, so the
other environments cannot rebuild it. This module dumps every ``ibo_*`` table to
a gzip'd stream of JSON lines and loads such a dump back, keeping row ids so the
links between tables (and the figure images) stay intact.

Only archive tables are touched. Questions authors created from the archive keep
their own ``archive_question_id``, which is not part of a dump.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import io
import json
import zlib
from typing import IO, Iterator

from sqlalchemy import Table, delete, func, select, text, update
from sqlalchemy.engine import Connection

from app.db import Base

FORMAT = "nbo-archive-v1"
BATCH_ROWS = 200
BATCH_BYTES = 8 * 1024 * 1024


def archive_tables() -> list[Table]:
    """Archive tables, parents before children."""
    import app.models  # noqa: F401  (register every model)
    from app.models import archive  # noqa: F401

    return [t for t in Base.metadata.sorted_tables if t.name.startswith("ibo_")]


def _late_columns(tables: list[Table]) -> dict[str, list[str]]:
    """Nullable foreign keys that point at the same or a later table.

    They are filled in after all rows exist, which is how circular links and
    parent/child links inside one table are loaded.
    """
    order = {t.name: i for i, t in enumerate(tables)}
    late: dict[str, list[str]] = {}
    for table in tables:
        for fk in table.foreign_keys:
            ref = fk.column.table.name
            if ref in order and order[ref] >= order[table.name]:
                if not fk.parent.nullable:
                    raise RuntimeError(f"{table.name}.{fk.parent.name} cannot be loaded late")
                late.setdefault(table.name, []).append(fk.parent.name)
    return late


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def _encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dt.datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, dt.date):
        return {"$d": value.isoformat()}
    return value


def _decode(value):
    if isinstance(value, dict) and len(value) == 1:
        if "$b" in value:
            return base64.b64decode(value["$b"])
        if "$dt" in value:
            return dt.datetime.fromisoformat(value["$dt"])
        if "$d" in value:
            return dt.date.fromisoformat(value["$d"])
    return value


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def iter_lines(conn: Connection) -> Iterator[bytes]:
    tables = archive_tables()
    yield (json.dumps({"format": FORMAT, "tables": [t.name for t in tables]}) + "\n").encode()
    for table in tables:
        pk = list(table.primary_key.columns)
        query = select(table).order_by(*pk).execution_options(yield_per=BATCH_ROWS)
        for row in conn.execute(query).mappings():
            record = {k: _encode(v) for k, v in row.items()}
            yield (json.dumps({"t": table.name, "r": record}, ensure_ascii=False) + "\n").encode()


def iter_gzip(conn: Connection) -> Iterator[bytes]:
    """The dump as a gzip stream, produced piece by piece."""
    packer = zlib.compressobj(6, zlib.DEFLATED, 31)
    for line in iter_lines(conn):
        chunk = packer.compress(line)
        if chunk:
            yield chunk
    yield packer.flush()


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def _sync_sequences(conn: Connection, tables: list[Table]) -> None:
    if conn.dialect.name != "postgresql":
        return
    for table in tables:
        conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                f"(SELECT MAX(id) FROM {table.name}) IS NOT NULL)"
            ),
            {"t": table.name},
        )


def load(conn: Connection, stream: IO[bytes]) -> dict[str, int]:
    """Replace the archive tables with the contents of a dump.

    Runs on the caller's connection, so a failure leaves the database as it was
    once the surrounding transaction is rolled back.
    """
    tables = archive_tables()
    by_name = {t.name: t for t in tables}
    late = _late_columns(tables)

    reader = io.BufferedReader(gzip.GzipFile(fileobj=stream, mode="rb"))
    header = json.loads(reader.readline() or b"{}")
    if header.get("format") != FORMAT:
        raise ValueError("not an NBO archive dump")
    unknown = set(header.get("tables", [])) - set(by_name)
    if unknown:
        raise ValueError(f"dump has tables this version does not know: {sorted(unknown)}")

    # Empty the archive, children first.
    for table in reversed(tables):
        conn.execute(delete(table))

    pending: dict[str, list[tuple[object, dict]]] = {}
    buffer: dict[str, list[dict]] = {}
    sizes: dict[str, int] = {}
    counts: dict[str, int] = {t.name: 0 for t in tables}

    def flush(name: str) -> None:
        rows = buffer.pop(name, [])
        sizes.pop(name, None)
        if rows:
            conn.execute(by_name[name].insert(), rows)

    for raw in reader:
        item = json.loads(raw)
        name = item["t"]
        table = by_name[name]
        row = {k: _decode(v) for k, v in item["r"].items()}
        for col in late.get(name, []):
            if row.get(col) is not None:
                pending.setdefault(name, []).append((row["id"], {col: row[col]}))
                row[col] = None
        buffer.setdefault(name, []).append(row)
        sizes[name] = sizes.get(name, 0) + len(raw)
        counts[name] += 1
        if len(buffer[name]) >= BATCH_ROWS or sizes[name] >= BATCH_BYTES:
            flush(name)
        # Rows arrive table by table in dependency order; write a table's rows out
        # before the next table starts so foreign keys always find their parent.
        for other in [n for n in list(buffer) if n != name]:
            flush(other)

    for name in list(buffer):
        flush(name)

    # Second pass: the links that point forward or into the same table.
    for name, items in pending.items():
        table = by_name[name]
        for row_id, values in items:
            conn.execute(update(table).where(table.c.id == row_id).values(**values))

    _sync_sequences(conn, tables)
    return counts


def counts(conn: Connection) -> dict[str, int]:
    return {t.name: int(conn.scalar(select(func.count()).select_from(t)) or 0) for t in archive_tables()}
