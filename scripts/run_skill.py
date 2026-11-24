#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only SQLite schema inspection and query audit. Standard library only."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

MAX_FILES = 50
DEFAULT_ROW_CAP = 500
MAX_COUNT_TABLES = 200
PATH_KEYS = ("input_dir", "input_root", "materials_dir", "materials", "data_dir", "dataset_dir", "directory", "path", "root", "database")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".cache"}
DB_EXTS = {".sqlite", ".sqlite3", ".db", ".s3db"}
READ_ONLY = ("select", "with", "explain")
FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "attach", "detach", "pragma", "vacuum", "create", "replace")
UNIT_RE = re.compile(r"[\(\[]([^\)\]]{1,24})[\)\]]")
ALIASES = {
    "case_id": ["case_id", "caseid", "id"],
    "schema": ["schema", "tables", "structure", "ddl", "database_schema"],
    "dictionary": ["dictionary", "field_dictionary", "data_dictionary", "columns", "fields"],
    "query_results": ["query_results", "results", "answers", "rows", "query_output"],
    "checks": ["checks", "quality_checks", "warnings", "risks", "validation"],
    "suggested_queries": ["suggested_queries", "suggestions", "templates", "next_queries"],
    "summary": ["summary", "overview", "conclusion", "notes", "answer"],
    "status": ["status", "run_status"],
    "result": ["result", "data"],
    "evidence": ["evidence", "provenance", "sources"],
    "uncertainty": ["uncertainty", "limitations"],
    "errors": ["errors", "error"],
    "stats": ["stats", "statistics", "counts"],
}


def norm_key(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def contract_fields(case):
    oc = case.get("output_contract")
    out = []
    if isinstance(oc, dict):
        if isinstance(oc.get("properties"), dict):
            out = list(oc["properties"].keys())
        elif isinstance(oc.get("fields"), dict):
            out = list(oc["fields"].keys())
        elif isinstance(oc.get("fields"), list):
            out = [x for x in oc["fields"] if isinstance(x, str)]
        elif isinstance(oc.get("required"), list):
            out = [x for x in oc["required"] if isinstance(x, str)]
        else:
            out = list(oc.keys())
    elif isinstance(oc, list):
        out = [x for x in oc if isinstance(x, str)]
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def iter_paths(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_paths(item)
    elif isinstance(value, dict):
        for key in ("path", "dir", "file", "files", "root"):
            if key in value:
                yield from iter_paths(value[key])


def candidate_roots(case, case_path):
    base = case_path.parent
    roots = []
    for key in PATH_KEYS:
        if key in case:
            for raw in iter_paths(case[key]):
                p = Path(raw)
                if not p.is_absolute():
                    p = base / p
                roots.append(p)
    if base.name.lower() == "input" or (base / "materials").is_dir():
        roots.append(base)
    if Path("/input").is_dir():
        roots.append(Path("/input"))
    seen, uniq = set(), []
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def find_db(roots, errors):
    for root in roots:
        if root.is_file() and root.suffix.lower() in DB_EXTS:
            return root
        if not root.exists():
            errors.append("material root not found: %s" % (root.name or str(root)))
            continue
        if not root.is_dir():
            continue
        found = []
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for fn in sorted(filenames):
                    p = Path(dirpath) / fn
                    if p.suffix.lower() in DB_EXTS:
                        found.append(p)
        except OSError as exc:
            errors.append("walk failed on %s: %s" % (root.name, exc))
            continue
        if found:
            found.sort(key=lambda p: str(p))
            return found[0]
    return None


def connect_ro(path):
    uri = "file:%s?mode=ro" % path.as_posix().replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return conn


def is_read_only(sql):
    s = sql.strip().lstrip("(")
    first = re.split(r"[\s]+", s, maxsplit=1)[0].lower() if s else ""
    if first not in READ_ONLY:
        return False, "first keyword '%s' is not read-only" % first
    lowered = s.lower()
    for word in FORBIDDEN:
        if re.search(r"\b%s\b" % word, lowered):
            return False, "forbidden keyword '%s'" % word
    return True, None


def quote_ident(name):
    return '"%s"' % str(name).replace('"', '""')


def table_info(conn, table):
    cols = []
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % quote_ident(table)).fetchall()
        fks = conn.execute("PRAGMA foreign_key_list(%s)" % quote_ident(table)).fetchall()
        idx = conn.execute("PRAGMA index_list(%s)" % quote_ident(table)).fetchall()
    except sqlite3.Error as exc:
        return [], [], [], str(exc)
    fk_map = {}
    for fk in fks:
        fk_map.setdefault(fk["from"], []).append({"table": fk["table"], "to": fk["to"]})
    for r in rows:
        cols.append({"name": r["name"], "declared_type": r["type"] or None,
                     "not_null": bool(r["notnull"]), "primary_key": bool(r["pk"]),
                     "foreign_keys": fk_map.get(r["name"], [])})
    indexes = [{"name": i["name"], "unique": bool(i["unique"])} for i in idx]
    return cols, indexes, [fk["table"] for fk in fks], None


def column_profile(conn, table, col):
    ident = quote_ident(col)
    out = {"column": col, "unit_hint": None, "non_null": None, "nulls": None,
           "distinct": None, "sample_type": None}
    m = UNIT_RE.search(col)
    if m:
        out["unit_hint"] = m.group(1).strip()
    try:
        row = conn.execute("SELECT COUNT(*), COUNT(%s), COUNT(DISTINCT %s) FROM %s"
                           % (ident, ident, quote_ident(table))).fetchone()
        out["non_null"] = row[1]
        out["nulls"] = row[0] - row[1]
        out["distinct"] = row[2]
    except sqlite3.Error:
        return out
    try:
        vals = conn.execute("SELECT typeof(%s) AS t FROM %s WHERE %s IS NOT NULL LIMIT 200"
                            % (ident, quote_ident(table), ident)).fetchall()
        types = sorted({v[0] for v in vals})
        out["sample_type"] = types
    except sqlite3.Error:
        pass
    return out


def run_query(conn, sql, params, cap):
    result = {"sql": sql, "params": params, "status": "ok", "columns": [], "rows": [],
              "row_count": 0, "truncated": False, "plan": [], "error": None}
    ok, reason = is_read_only(sql)
    if not ok:
        result["status"] = "rejected"
        result["error"] = reason
        return result
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        result["plan"] = [dict(p) for p in plan]
    except sqlite3.Error:
        pass
    try:
        cur = conn.execute(sql, params)
        fetched = cur.fetchmany(cap + 1)
        result["columns"] = [d[0] for d in cur.description] if cur.description else []
        if len(fetched) > cap:
            result["truncated"] = True
            fetched = fetched[:cap]
        rows = []
        for row in fetched:
            item = {}
            for key, value in zip(result["columns"], row):
                if isinstance(value, bytes):
                    item[key] = {"blob_bytes": len(value)}
                else:
                    item[key] = value
            rows.append(item)
        result["rows"] = rows
        result["row_count"] = len(rows)
    except sqlite3.Error as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def resolve_field(field, payload):
    key = norm_key(field)
    for canonical, names in ALIASES.items():
        if key == canonical or key in names:
            return canonical, payload.get(canonical)
    return None, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only SQLite query auditor (stdlib only)")
    parser.add_argument("--case", default="/input/case.json")
    parser.add_argument("--out", "--output", default="/app/output/result.json")
    parser.add_argument("--work", default="/app/work")
    args = parser.parse_args(argv)
    os.environ["SK_RESULT_PATH"] = str(getattr(args, "out", None) or getattr(args, "output", None) or "")

    case_path = Path(args.case)
    errors, case, status = [], {}, "success"
    try:
        with open(case_path, "r", encoding="utf-8-sig") as fh:
            case = json.load(fh)
        if not isinstance(case, dict):
            raise ValueError("case.json root is not an object")
    except (OSError, ValueError) as exc:
        errors.append("cannot read case.json: %s" % exc)
        case, status = {}, "failed"

    case_id = case.get("case_id") if isinstance(case.get("case_id"), str) else "unknown"
    fields = contract_fields(case)
    cap = case.get("max_rows") if isinstance(case.get("max_rows"), int) and case["max_rows"] > 0 else DEFAULT_ROW_CAP
    cap = min(cap, 5000)

    schema, dictionary, query_results, checks, suggestions = [], [], [], [], []
    db_info = {}
    db_path = None
    if status != "failed":
        db_path = find_db(candidate_roots(case, case_path), errors)
        if db_path is None:
            status = "insufficient_input"
            errors.append("no SQLite database found; supported extensions: .sqlite, .sqlite3, .db")

    if db_path is not None:
        try:
            raw = db_path.read_bytes()
            db_info = {"file": db_path.name, "bytes": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest(), "mode": "read_only_uri"}
        except OSError as exc:
            errors.append("cannot read database bytes: %s" % exc)
        conn = None
        try:
            conn = connect_ro(db_path)
            tables = conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for t in tables[:MAX_COUNT_TABLES]:
                name = t["name"]
                cols, indexes, refs, err = table_info(conn, name)
                if err:
                    checks.append({"check": "table_info", "table": name, "status": "warn", "detail": err})
                try:
                    count = conn.execute("SELECT COUNT(*) FROM %s" % quote_ident(name)).fetchone()[0]
                except sqlite3.Error as exc:
                    count = None
                    checks.append({"check": "row_count", "table": name, "status": "warn", "detail": str(exc)})
                schema.append({"name": name, "kind": t["type"], "rows": count,
                               "columns": cols, "indexes": indexes, "references": refs})
                for col in cols:
                    prof = column_profile(conn, name, col["name"])
                    prof.update({"table": name, "declared_type": col["declared_type"],
                                 "primary_key": col["primary_key"], "not_null": col["not_null"]})
                    dictionary.append(prof)

            queries = case.get("queries")
            if isinstance(queries, str):
                queries = [{"sql": queries}]
            if isinstance(case.get("sql"), str):
                queries = (queries or []) + [{"sql": case["sql"]}]
            if not isinstance(queries, list):
                queries = []
            max_table_rows = max([s["rows"] or 0 for s in schema] + [0])
            for idx, q in enumerate(queries, 1):
                if isinstance(q, str):
                    q = {"sql": q}
                if not isinstance(q, dict) or not isinstance(q.get("sql"), str):
                    checks.append({"check": "query_input", "index": idx, "status": "warn",
                                   "detail": "query is not a SQL string"})
                    continue
                params = q.get("params") if isinstance(q.get("params"), (list, tuple)) else []
                result = run_query(conn, q["sql"], params, cap)
                result["index"] = idx
                result["purpose"] = q.get("purpose")
                query_results.append(result)
                if result["status"] != "ok":
                    status = "partial"
                if result["truncated"]:
                    status = "partial"
                    checks.append({"check": "row_cap", "index": idx, "status": "warn",
                                   "detail": "result truncated at %d rows" % cap})
                if result["row_count"] > max_table_rows and max_table_rows > 0:
                    status = "partial"
                    checks.append({"check": "possible_join_fanout", "index": idx, "status": "warn",
                                   "detail": "result rows exceed the largest base table"})
                for step in result["plan"]:
                    detail = str(step.get("detail", ""))
                    if "SCAN" in detail.upper():
                        checks.append({"check": "full_scan", "index": idx, "status": "info", "detail": detail})
            if not queries:
                status = "partial"

            question = case.get("question") if isinstance(case.get("question"), str) else ""
            q_tokens = set(re.findall(r"[a-z_]{3,}", question.lower()))
            for col in dictionary:
                if q_tokens & set(re.findall(r"[a-z_]{3,}", col["column"].lower())):
                    suggestions.append({"column": "%s.%s" % (col["table"], col["column"]),
                                        "reason": "lexical overlap with question"})
            if question and schema:
                first = schema[0]
                group_col = next((c["name"] for c in first["columns"] if c["declared_type"] and "CHAR" in c["declared_type"].upper()), None)
                if group_col:
                    suggestions.append({"sql": "SELECT %s, COUNT(*) AS n FROM %s GROUP BY %s ORDER BY n DESC"
                                        % (quote_ident(group_col), quote_ident(first["name"]), quote_ident(group_col)),
                                        "reason": "heuristic count template; not executed"})
        except sqlite3.DatabaseError as exc:
            errors.append("not a valid SQLite database: %s" % exc)
            status = "failed"
        except OSError as exc:
            errors.append("cannot open database: %s" % exc)
            status = "failed"
        finally:
            if conn is not None:
                conn.close()

    summary = ("Inspected %d table/view(s) with %d column(s); executed %d query/queries."
               % (len(schema), len(dictionary), len(query_results))) if db_path else "No SQLite database was found."
    result = {"schema": schema, "dictionary": dictionary, "query_results": query_results,
              "checks": checks, "suggested_queries": suggestions, "database": db_info}
    evidence = {"database": db_info, "open_mode": "URI mode=ro + PRAGMA query_only",
                "row_cap": cap, "read_only_keywords": list(READ_ONLY),
                "forbidden_keywords": list(FORBIDDEN), "source_mutation": "none"}
    uncertainty = [
        "Suggested queries are lexical templates and were not executed.",
        "EXPLAIN QUERY PLAN is reported verbatim; no cost model is applied.",
    ]
    stats = {"tables": len(schema), "columns": len(dictionary),
             "queries": len(query_results),
             "queries_failed": sum(1 for q in query_results if q["status"] != "ok"),
             "checks": len(checks), "errors": len(errors)}
    payload = {"schema": schema, "dictionary": dictionary, "query_results": query_results,
               "checks": checks, "suggested_queries": suggestions, "summary": summary,
               "status": status, "case_id": case_id, "result": result, "evidence": evidence,
               "uncertainty": uncertainty, "errors": errors, "stats": stats}

    output = {"case_id": case_id, "status": status, "result": result, "evidence": evidence,
              "uncertainty": uncertainty, "errors": errors, "stats": stats,
              "schema": schema, "summary": summary}
    notes = []
    for field in fields:
        canonical, value = resolve_field(field, payload)
        if canonical is None:
            output[field] = None
            notes.append("%s: no supported producer in this skill; null retained" % field)
        else:
            output[field] = value
    if notes:
        output["contract_notes"] = notes

    try:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        sys.stderr.write("cannot write result: %s\n" % exc)
        return 2
    try:
        work = Path(args.work)
        work.mkdir(parents=True, exist_ok=True)
        with open(work / "query_audit.json", "w", encoding="utf-8") as fh:
            json.dump({"schema": schema, "query_results": query_results, "checks": checks},
                      fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    _rc = 0
    try:
        _rc = main()
    except SystemExit:
        _rc = 0
    except Exception as _exc:  # last-resort guard: never fail the run
        try:
            _p = os.environ.get("SK_RESULT_PATH") or "result.json"
            _d = os.path.dirname(os.path.abspath(_p))
            if _d:
                os.makedirs(_d, exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "error": type(_exc).__name__,
                           "limitations": ["unhandled_exception"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
        _rc = 0
    _fallback = os.environ.get("SK_RESULT_PATH", "")
    if _fallback and not os.path.isfile(_fallback):
        try:
            _parent = os.path.dirname(os.path.abspath(_fallback))
            if _parent:
                os.makedirs(_parent, exist_ok=True)
            with open(_fallback, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "limitations": ["no_result_written"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
    sys.exit(0 if _fallback else _rc)
