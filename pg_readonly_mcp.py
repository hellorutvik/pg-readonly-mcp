"""Read-only PostgreSQL MCP server: lets Claude answer questions from your data without being able to change it.

Layers, outermost first (the database role is the one that matters; the rest is defence in depth):
  1. Connect as a role that can only SELECT the views you grant it (see README).
  2. Every call runs in BEGIN READ ONLY with a statement_timeout, and is always rolled back.
  3. One statement per call, and it must start with SELECT / WITH / TABLE / VALUES / EXPLAIN.
  4. Results are capped at PGMCP_MAX_ROWS, and columns named in PGMCP_MASK_COLUMNS are masked.
  5. Every call, including rejected ones, is appended to an audit log (JSON lines) with who asked.
"""
import json, os, re, time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

ALLOWED_FIRST_WORDS = {"select", "with", "table", "values", "explain"}


@dataclass
class Config:
    dsn: str
    max_rows: int = 500
    timeout_ms: int = 5000
    mask_columns: frozenset = field(default_factory=frozenset)
    audit_path: str = "audit.jsonl"
    user: str = "unknown"

    @classmethod
    def from_env(cls):
        return cls(
            dsn=os.environ["DATABASE_URL"],
            max_rows=int(os.environ.get("PGMCP_MAX_ROWS", 500)),
            timeout_ms=int(os.environ.get("PGMCP_STATEMENT_TIMEOUT_MS", 5000)),
            mask_columns=frozenset(c.strip().lower() for c in os.environ.get("PGMCP_MASK_COLUMNS", "").split(",") if c.strip()),
            audit_path=os.environ.get("PGMCP_AUDIT_LOG", "audit.jsonl"),
            user=os.environ.get("PGMCP_USER", "unknown"),
        )


class Rejected(Exception):
    pass


def first_word(sql):
    """First keyword after leading whitespace, -- comments, /* */ comments and parentheses."""
    s = re.sub(r"^(\s+|--[^\n]*\n?|/\*.*?\*/|\()+", "", sql, flags=re.S)
    m = re.match(r"[A-Za-z]+", s)
    return m.group(0).lower() if m else ""


def check_sql(sql):
    if not sql or not sql.strip():
        raise Rejected("empty query")
    word = first_word(sql)
    if word not in ALLOWED_FIRST_WORDS:
        raise Rejected(f"only read queries are allowed (got {word.upper() or 'nothing'})")


def mask(value):
    if value is None:
        return None
    s = str(value)
    return "****" + s[-4:] if len(s) > 4 else "****"


def audit(cfg, **event):
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": cfg.user, **event}
    with open(cfg.audit_path, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def run_query(cfg, sql, conn=None):
    """Run one read-only statement. Returns columns, rows (masked, capped), truncated flag and timing."""
    started = time.monotonic()
    try:
        check_sql(sql)
        own = conn is None
        conn = conn or psycopg.connect(cfg.dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN READ ONLY")
                try:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(cfg.timeout_ms),))
                    # prepare=True makes the server parse it as one prepared statement, so "select 1; commit; delete ..."
                    # is refused outright. Without it, a parameterless query goes over the simple protocol,
                    # which runs several statements and could step out of the read-only transaction.
                    cur.execute(sql, prepare=True)
                    columns = [d.name for d in cur.description or []]
                    rows = cur.fetchmany(cfg.max_rows + 1)
                finally:
                    cur.execute("ROLLBACK")
        finally:
            if own:
                conn.close()
    except Exception as e:
        audit(cfg, sql=sql, ok=False, error=f"{type(e).__name__}: {e}".strip()[:500], ms=round((time.monotonic() - started) * 1000))
        raise
    truncated = len(rows) > cfg.max_rows
    rows = rows[: cfg.max_rows]
    masked = [i for i, c in enumerate(columns) if c.lower() in cfg.mask_columns]
    rows = [[mask(v) if i in masked else v for i, v in enumerate(r)] for r in rows]
    ms = round((time.monotonic() - started) * 1000)
    audit(cfg, sql=sql, ok=True, rows=len(rows), truncated=truncated, ms=ms)
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "masked_columns": [columns[i] for i in masked], "ms": ms}


LIST_SQL = """
select table_schema || '.' || table_name as name, table_type
from information_schema.tables
where table_schema not in ('pg_catalog', 'information_schema')
  and has_table_privilege(quote_ident(table_schema) || '.' || quote_ident(table_name), 'SELECT')
order by 1"""

DESCRIBE_SQL = """
select column_name, data_type, is_nullable
from information_schema.columns
where table_schema = %s and table_name = %s
order by ordinal_position"""


def list_tables(cfg, conn=None):
    return run_query(cfg, LIST_SQL, conn)["rows"]


def describe_table(cfg, name, conn=None):
    schema, _, table = name.rpartition(".")
    own = conn is None
    conn = conn or psycopg.connect(cfg.dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            if not cur.execute("select has_table_privilege(%s, 'SELECT')", (name,)).fetchone()[0]:
                raise Rejected(f"no read access to {name}")
            cols = cur.execute(DESCRIBE_SQL, (schema or "public", table)).fetchall()
    finally:
        if own:
            conn.close()
    audit(cfg, describe=name, ok=True)
    return [{"column": c, "type": t, "nullable": n == "YES", "masked": c.lower() in cfg.mask_columns} for c, t, n in cols]


def main():
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    cfg = Config.from_env()
    server = MCPServer("pg-readonly", instructions=(
        "Read-only access to a PostgreSQL database. Call list_tables and describe_table before writing SQL. "
        "Results are capped and some columns are masked. Show the user the SQL you ran."))
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @server.tool(name="list_tables", annotations=ro)
    def list_tables_tool() -> list:
        """Tables and views this connection can read, as schema.name."""
        return list_tables(cfg)

    @server.tool(name="describe_table", annotations=ro)
    def describe_table_tool(name: str) -> list:
        """Columns of one table or view (schema.name). Masked columns are flagged."""
        return describe_table(cfg, name)

    @server.tool(annotations=ro)
    def query(sql: str) -> dict:
        """Run one read-only SQL statement (SELECT / WITH / TABLE / VALUES / EXPLAIN)."""
        return run_query(cfg, sql)

    server.run("stdio")


if __name__ == "__main__":
    main()
