"""Unit tests for the SQL guard and masking, plus integration tests against a throwaway Postgres
cluster (initdb + pg_ctl). The integration tests skip when Postgres binaries are not installed."""
import json, os, shutil, socket, subprocess, tempfile

import psycopg
import pytest

from pg_readonly_mcp import Config, Rejected, check_sql, describe_table, first_word, list_tables, mask, run_query


# ---------- unit ----------

@pytest.mark.parametrize("sql", ["select 1", "  SELECT * FROM t", "with x as (select 1) select * from x",
                                 "-- note\nselect 1", "/* c */ (select 1)", "explain select 1", "table orders", "values (1)"])
def test_read_queries_pass(sql):
    check_sql(sql)


@pytest.mark.parametrize("sql", ["insert into t values (1)", "update t set a = 1", "delete from t", "drop table t",
                                 "set transaction read write", "copy t to '/tmp/x'", "-- select\ndelete from t", "", "   "])
def test_write_and_other_statements_rejected(sql):
    with pytest.raises(Rejected):
        check_sql(sql)


def test_first_word_skips_comments():
    assert first_word("/* hi */ -- there\n  WITH a as (select 1) select 1") == "with"


def test_mask_keeps_last_four_only():
    assert mask("GB29NWBK60161331926819") == "****6819"
    assert mask("abc") == "****"
    assert mask(None) is None


# ---------- integration ----------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pg():
    initdb = shutil.which("initdb") or "/opt/homebrew/opt/postgresql@16/bin/initdb"
    pg_ctl = shutil.which("pg_ctl") or "/opt/homebrew/opt/postgresql@16/bin/pg_ctl"
    if not os.path.exists(initdb):
        pytest.skip("Postgres binaries not installed")
    root = tempfile.mkdtemp(prefix="pgmcp-")
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}  # postmaster refuses to start without a valid locale on macOS
    data, sock, port = os.path.join(root, "data"), root, _free_port()
    subprocess.run([initdb, "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8", "--locale=C"], check=True, capture_output=True, env=env)
    subprocess.run([pg_ctl, "-D", data, "-w", "-l", os.path.join(root, "log"), "start", "-o",
                    f"-p {port} -k {sock} -c listen_addresses=''"], check=True, capture_output=True, env=env)
    admin = f"host={sock} port={port} user=postgres dbname=postgres"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("create database shop")
    with psycopg.connect(f"host={sock} port={port} user=postgres dbname=shop", autocommit=True) as c:
        c.execute("""
          create table customers (id int primary key, name text, iban text, email text);
          create table orders (id serial primary key, customer_id int, total numeric, ordered_at date);
          create table salaries (person text, amount numeric);
          insert into customers values (1, 'Harbor Cafes', 'GB29NWBK60161331926819', 'ops@harbor.example'),
                                       (2, 'Linden Hotels', 'DE89370400440532013000', 'ap@linden.example');
          insert into orders (customer_id, total, ordered_at) select 1 + (g % 2), g, date '2026-04-01' + g from generate_series(1, 120) g;
          insert into salaries values ('ceo', 1);
          create role reader login;
          create schema reporting;
          create view reporting.customer_orders as
            select c.name, c.iban, c.email, o.total, o.ordered_at from orders o join customers c on c.id = o.customer_id;
          grant usage on schema reporting to reader;
          grant select on reporting.customer_orders to reader;
        """)
    yield f"host={sock} port={port} user=reader dbname=shop", f"host={sock} port={port} user=postgres dbname=shop"
    subprocess.run([pg_ctl, "-D", data, "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def cfg(pg, tmp_path):
    return Config(dsn=pg[0], max_rows=50, timeout_ms=500, mask_columns=frozenset({"iban", "email"}),
                  audit_path=str(tmp_path / "audit.jsonl"), user="test.user@example")


def _audit(cfg):
    with open(cfg.audit_path) as f:
        return [json.loads(l) for l in f]


def test_reads_masks_and_caps(cfg):
    r = run_query(cfg, "select name, iban, email, total from reporting.customer_orders order by total")
    assert r["columns"] == ["name", "iban", "email", "total"]
    assert r["row_count"] == 50 and r["truncated"] is True
    assert {row[1] for row in r["rows"]} <= {"****6819", "****3000"}
    assert all(row[2] == "****mple" for row in r["rows"])
    assert set(r["masked_columns"]) == {"iban", "email"}


def test_list_and_describe_show_only_granted_objects(cfg):
    names = [row[0] for row in list_tables(cfg)]
    assert names == ["reporting.customer_orders"]
    cols = {c["column"]: c for c in describe_table(cfg, "reporting.customer_orders")}
    assert cols["iban"]["masked"] and not cols["name"]["masked"]
    with pytest.raises(Rejected):
        describe_table(cfg, "public.salaries")


def test_role_cannot_read_ungranted_tables(cfg):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run_query(cfg, "select * from public.salaries")


def test_writes_fail_even_for_a_superuser(cfg, pg):
    # Bypass the keyword guard on purpose: the read-only transaction still stops the write.
    admin_cfg = Config(dsn=pg[1], audit_path=cfg.audit_path)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        run_query(admin_cfg, "with x as (delete from orders returning 1) select count(*) from x")
    with psycopg.connect(pg[1]) as c:
        assert c.execute("select count(*) from orders").fetchone()[0] == 120


@pytest.mark.parametrize("sql", ["select 1; select 2", "select 1; commit; delete from orders"])
def test_multiple_statements_rejected(pg, cfg, sql):
    # Run as superuser so only this layer stands between the query and the write.
    admin_cfg = Config(dsn=pg[1], audit_path=cfg.audit_path)
    with pytest.raises(psycopg.errors.SyntaxError):
        run_query(admin_cfg, sql)
    with psycopg.connect(pg[1]) as c:
        assert c.execute("select count(*) from orders").fetchone()[0] == 120


def test_statement_timeout(cfg):
    with pytest.raises(psycopg.errors.QueryCanceled):
        run_query(cfg, "select pg_sleep(2)")


def test_every_call_is_audited_with_user(cfg):
    run_query(cfg, "select 1")
    with pytest.raises(Rejected):
        run_query(cfg, "delete from orders")
    log = _audit(cfg)
    assert [e["ok"] for e in log] == [True, False]
    assert all(e["user"] == "test.user@example" for e in log)
    assert "Rejected" in log[1]["error"]
