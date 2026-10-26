# pg-readonly-mcp

An MCP server that lets Claude answer questions from a PostgreSQL database without being able to change it. Your ops or finance team asks in plain English. Claude writes the SQL, runs it read-only, and shows the query it ran.

Three tools: `list_tables`, `describe_table` and `query`. Each is marked `readOnlyHint` so MCP clients can treat it as safe.

## Security model

The database decides what Claude can see. The server adds four more layers on top, so one mistake doesn't expose data.

| Layer | What it does | Where |
|---|---|---|
| 1. Database role | Claude connects as a role that can only `SELECT` the views you grant. Tables you don't grant don't exist for it. | Postgres |
| 2. Read-only transaction | Every call runs in `BEGIN READ ONLY` with a `statement_timeout` and is rolled back afterwards. A write fails even if the role could write. | Postgres |
| 3. One statement, read verbs only | The server prepares each query as a single statement, so `select 1; commit; delete ...` is refused. The first keyword must be `SELECT`, `WITH`, `TABLE`, `VALUES` or `EXPLAIN`. | server |
| 4. Row cap and masking | Results stop at `PGMCP_MAX_ROWS`. Columns you list in `PGMCP_MASK_COLUMNS` come back as `****` plus the last four characters. | server |
| 5. Audit log | Every call, including rejected ones, is appended as a JSON line with time, user, SQL, outcome, row count and duration. | server |

The tests check each layer against a real Postgres. Some of them connect as a superuser and try to write, to prove that layers 2 and 3 hold on their own.

### What this does not do

- Masking works on result column names. A query can still alias a column (`select iban as x`), so the database view is the right place to leave out or hash fields Claude should never see. Use masking as a second net, not the first.
- The audit user comes from `PGMCP_USER`, set once per deployment. For per-person audit across a team, run one server per user or put it behind your SSO.
- Data Claude reads goes to the model provider you configure. For EU data, use an EU-hosted model endpoint, for example Claude on AWS Bedrock in Frankfurt.

## Setup

### 1. Create the read-only role (run as an admin)

```sql
create role claude_reader login password 'change-me';
create schema reporting;

-- Expose only what Claude should see, through views.
create view reporting.customer_orders as
  select c.name, o.total, o.ordered_at
  from orders o join customers c on c.id = o.customer_id;

grant usage on schema reporting to claude_reader;
grant select on reporting.customer_orders to claude_reader;

-- Belt and braces: the role's own sessions default to read-only.
alter role claude_reader set default_transaction_read_only = on;
```

Point it at a read replica if you have one.

### 2. Install

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

### 3. Add it to Claude Desktop or Claude Code

```json
{
  "mcpServers": {
    "company-data": {
      "command": "/path/to/pg-readonly-mcp/.venv/bin/pg-readonly-mcp",
      "env": {
        "DATABASE_URL": "postgresql://claude_reader:change-me@db.internal:5432/shop",
        "PGMCP_USER": "jane@yourcompany.com",
        "PGMCP_MASK_COLUMNS": "iban,email,phone",
        "PGMCP_MAX_ROWS": "500",
        "PGMCP_STATEMENT_TIMEOUT_MS": "5000",
        "PGMCP_AUDIT_LOG": "/var/log/pg-readonly-mcp/audit.jsonl"
      }
    }
  }
}
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | required | Connection string for the read-only role |
| `PGMCP_USER` | `unknown` | Who is asking, written to every audit line |
| `PGMCP_MASK_COLUMNS` | none | Comma-separated column names to mask |
| `PGMCP_MAX_ROWS` | `500` | Row cap per query |
| `PGMCP_STATEMENT_TIMEOUT_MS` | `5000` | Postgres `statement_timeout` per query |
| `PGMCP_AUDIT_LOG` | `audit.jsonl` | Path of the JSON-lines audit log |

An audit line looks like this:

```json
{"ts": "2026-10-26T14:02:11+00:00", "user": "jane@yourcompany.com", "sql": "select name, sum(total) ...", "ok": true, "rows": 5, "truncated": false, "ms": 41}
```

## Tests

```bash
.venv/bin/pip install pytest
PYTHONPATH=. .venv/bin/python -m pytest -q
```

The integration tests start a throwaway Postgres with `initdb` and `pg_ctl`, and skip when those aren't installed. They cover:
- the role can't read tables it wasn't granted;
- a write inside a CTE fails even as a superuser;
- `select 1; commit; delete ...` is refused;
- queries time out;
- rows are capped and masked;
- every call lands in the audit log with the user.

## License

MIT
