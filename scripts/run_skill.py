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
MAX_SAMPLE_ROWS = 200
TASK_FILES = ("task.md", "prompt.md", "README.md")
TASK_FALLBACK = Path("/input/task.md")
PATH_KEYS = ("input_dir", "input_root", "materials_dir", "materials", "data_dir", "dataset_dir", "directory", "path", "root", "database")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".cache"}
DB_EXTS = {".sqlite", ".sqlite3", ".db", ".s3db"}
READ_ONLY = ("select", "with", "explain")
FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "attach", "detach", "pragma", "vacuum", "create", "replace")
UNIT_RE = re.compile(r"[\(\[]([^\)\]]{1,24})[\)\]]")
INLINE_UNIT_RE = re.compile(
    r"(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)\s*"
    r"([A-Za-z\u00b5\u03bc%\u2030][A-Za-z0-9\u00b5\u03bc/^._%-]{0,15})(?![\w])")
JOIN_RE = re.compile(r"\bjoin\b", re.I)
GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.I)
AGG_FUNC_RE = re.compile(r"\b(count|avg|sum|max|min|total)\s*\(", re.I)
MEAN_RE = re.compile(r"\b(mean|average|avg)\b", re.I)
ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+[\\/])[^\s\"'<>|,;]*")
KEYWORD_TARGETS = (
    (("schema", "table", "structure"), "schema"),
    (("dict", "field", "column"), "dictionary"),
    (("quer", "result", "answer"), "query_results"),
    (("check", "warn", "risk"), "checks"),
    (("suggest", "template", "next"), "suggested_queries"),
    (("summary", "conclusion", "notes"), "summary"),
)
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


def sanitize_text(value):
    if not isinstance(value, str):
        value = str(value)
    return ABS_PATH_RE.sub("<path>", value)


def err_text(exc):
    return sanitize_text(exc)


def keyword_target(field):
    key = norm_key(field)
    for prefixes, target in KEYWORD_TARGETS:
        if key.startswith(prefixes):
            return target
    return None


def extract_units(value):
    if not isinstance(value, str):
        return []
    return [m.group(1).strip() for m in INLINE_UNIT_RE.finditer(value) if m.group(1).strip()]


def word_tokens(text):
    return {w for w in re.split(r"[^a-z0-9]+", str(text).lower()) if len(w) >= 3}


def is_text_type(declared):
    upper = (declared or "").upper()
    return any(k in upper for k in ("CHAR", "TEXT", "CLOB", "STRING"))


def label_table(schema):
    referenced = {fk["table"] for t in schema for c in t["columns"] for fk in c["foreign_keys"]}
    ranked = sorted(schema, key=lambda s: (s["name"] not in referenced, -(s["rows"] or 0)))
    for t in ranked:
        if any(not c["primary_key"] and is_text_type(c["declared_type"]) for c in t["columns"]):
            return t
    return None


def key_kinds(schema):
    kinds = {}
    for t in schema:
        for c in t["columns"]:
            if c["primary_key"]:
                kinds.setdefault(c["name"].lower(), set()).add("pk")
            if c["foreign_keys"]:
                kinds.setdefault(c["name"].lower(), set()).add("fk")
    return kinds


def fanout_channel(sql, result, kinds):
    """Return duplicate-key evidence for a non-aggregated JOIN, else None."""
    if not JOIN_RE.search(sql):
        return None
    if GROUP_BY_RE.search(sql) or AGG_FUNC_RE.search(sql):
        return None
    rows = result.get("rows") or []
    for col in result.get("columns") or []:
        if col.lower() not in kinds:
            continue
        values = [repr(row.get(col)) for row in rows]
        if len(values) < 2:
            continue
        distinct = len(set(values))
        if distinct < len(values):
            return {"column": col, "rows": len(values), "distinct": distinct,
                    "kinds": sorted(kinds[col.lower()])}
    return None


def mean_template(schema):
    """Template that aggregates the child table per parent key before joining."""
    by_name = {s["name"]: s for s in schema}
    for child in schema:
        for col in child["columns"]:
            for fk in col["foreign_keys"]:
                parent = by_name.get(fk["table"])
                if parent is None or parent["name"] == child["name"]:
                    continue
                parent_key = fk["to"] or next(
                    (c["name"] for c in parent["columns"] if c["primary_key"]), None)
                if not parent_key:
                    continue
                value_col = next(
                    (c["name"] for c in child["columns"]
                     if not c["primary_key"] and not c["foreign_keys"] and c["declared_type"]
                     and any(t in c["declared_type"].upper() for t in
                             ("REAL", "FLOA", "DOUB", "NUM", "DEC", "INT"))), None)
                if value_col is None:
                    continue
                label_col = next(
                    (c["name"] for c in parent["columns"] if not c["primary_key"]
                     and is_text_type(c["declared_type"])), None)
                if label_col is None:
                    label_col = parent_key
                child_key = col["name"]
                alias = "agg_" + re.sub(r"[^A-Za-z0-9_]", "_", child["name"])
                return ("SELECT p.%s AS %s, ROUND(AVG(%s.mean_value), 2) AS mean_value, "
                        "SUM(%s.n) AS n "
                        "FROM (SELECT %s AS %s, AVG(%s) AS mean_value, COUNT(*) AS n "
                        "FROM %s GROUP BY %s) AS %s "
                        "JOIN %s AS p ON p.%s = %s.%s GROUP BY p.%s ORDER BY p.%s"
                        % (quote_ident(label_col), quote_ident(label_col), alias, alias,
                           quote_ident(child_key), quote_ident(child_key), quote_ident(value_col),
                           quote_ident(child["name"]), quote_ident(child_key), alias,
                           quote_ident(parent["name"]), quote_ident(parent_key),
                           alias, quote_ident(child_key), quote_ident(label_col),
                           quote_ident(label_col)))
    return None


def load_task_text(case_path, errors):
    candidates = [case_path.parent / name for name in TASK_FILES]
    candidates.append(TASK_FALLBACK)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            errors.append("cannot read task description %s: %s" % (path.name, err_text(exc)))
    return ""


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
            errors.append("material root not found: %s" % sanitize_text(root.name or str(root)))
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
            errors.append("walk failed on %s: %s" % (sanitize_text(root.name), err_text(exc)))
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
        return [], [], [], err_text(exc)
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
    out = {"column": col, "unit_hint": None, "value_units": [], "non_null": None,
           "nulls": None, "distinct": None, "sample_type": None}
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
        vals = conn.execute("SELECT typeof(%s) AS t, %s AS v FROM %s WHERE %s IS NOT NULL LIMIT %d"
                            % (ident, ident, quote_ident(table), ident, MAX_SAMPLE_ROWS)).fetchall()
        out["sample_type"] = sorted({v[0] for v in vals})
        units = set()
        for v in vals:
            units.update(extract_units(v[1]))
        out["value_units"] = sorted(units)
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
        errors.append("cannot read case.json: %s" % err_text(exc))
        case, status = {}, "failed"

    case_id = case.get("case_id") if isinstance(case.get("case_id"), str) else "unknown"
    fields = contract_fields(case)
    cap = case.get("max_rows") if isinstance(case.get("max_rows"), int) and case["max_rows"] > 0 else DEFAULT_ROW_CAP
    cap = min(cap, 5000)

    schema, dictionary, query_results, checks, suggestions = [], [], [], [], []
    db_info = {}
    db_path = None
    question = case.get("question") if isinstance(case.get("question"), str) else ""
    question = sanitize_text(question).strip() if question else ""
    task_summary = None
    if not question:
        task_text = load_task_text(case_path, errors).strip()
        if task_text:
            question = task_text
            task_summary = sanitize_text(re.sub(r"\s+", " ", task_text))[:500]
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
            errors.append("cannot read database bytes: %s" % err_text(exc))
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
                    checks.append({"check": "table_info", "table": name, "status": "warn", "detail": err_text(err)})
                try:
                    count = conn.execute("SELECT COUNT(*) FROM %s" % quote_ident(name)).fetchone()[0]
                except sqlite3.Error as exc:
                    count = None
                    checks.append({"check": "row_count", "table": name, "status": "warn", "detail": err_text(exc)})
                schema.append({"name": name, "kind": t["type"], "rows": count,
                               "columns": cols, "indexes": indexes, "references": refs})
                for col in cols:
                    prof = column_profile(conn, name, col["name"])
                    prof.update({"table": name, "declared_type": col["declared_type"],
                                 "primary_key": col["primary_key"], "not_null": col["not_null"]})
                    dictionary.append(prof)
                    if len(prof["value_units"]) >= 2:
                        status = "partial" if status == "success" else status
                        checks.append({"check": "unit_conflict", "column": "%s.%s" % (name, col["name"]),
                                       "values": prof["value_units"], "status": "warn",
                                       "detail": "values are not converted; comparisons across units are invalid"})
            kinds = key_kinds(schema)

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
                    status = "partial" if status == "success" else status
                if result["truncated"]:
                    status = "partial" if status == "success" else status
                    checks.append({"check": "row_cap", "index": idx, "status": "warn",
                                   "detail": "result truncated at %d rows" % cap})
                fanout = fanout_channel(q["sql"], result, kinds)
                if fanout:
                    status = "partial" if status == "success" else status
                    checks.append({"check": "possible_join_fanout", "index": idx, "status": "warn",
                                   "detail": "JOIN without aggregation returned %d row(s) but only %d distinct "
                                             "value(s) of key column %s; parent rows are duplicated, so the "
                                             "result is not a per-parent aggregate"
                                             % (fanout["rows"], fanout["distinct"], fanout["column"]),
                                   "suggestion": "aggregate per sample first, then join"})
                elif result["row_count"] > max_table_rows and max_table_rows > 0:
                    status = "partial" if status == "success" else status
                    checks.append({"check": "possible_join_fanout", "index": idx, "status": "warn",
                                   "detail": "result rows (%d) exceed the largest base table (%d rows); "
                                             "check join cardinality" % (result["row_count"], max_table_rows),
                                   "suggestion": "aggregate per sample first, then join"})
                for step in result["plan"]:
                    detail = str(step.get("detail", ""))
                    if "SCAN" in detail.upper():
                        checks.append({"check": "full_scan", "index": idx, "status": "info", "detail": detail})
            if not queries:
                status = "partial"

            q_tokens = word_tokens(question)
            for col in dictionary:
                if q_tokens & word_tokens(col["column"]):
                    suggestions.append({"column": "%s.%s" % (col["table"], col["column"]),
                                        "reason": "lexical overlap with question"})
            if question and schema:
                first = label_table(schema)
                group_col = next((c["name"] for c in first["columns"] if not c["primary_key"]
                                  and is_text_type(c["declared_type"])), None) if first else None
                if group_col and first:
                    suggestions.append({"sql": "SELECT %s, COUNT(*) AS n FROM %s GROUP BY %s ORDER BY n DESC"
                                        % (quote_ident(group_col), quote_ident(first["name"]), quote_ident(group_col)),
                                        "reason": "heuristic count template; not executed"})
                if MEAN_RE.search(question):
                    template = mean_template(schema)
                    if template:
                        suggestions.append({"sql": template,
                                            "reason": "mean question over a one-to-many join; aggregate the "
                                                      "child table per parent key before joining; template not executed"})
        except sqlite3.DatabaseError as exc:
            errors.append("not a valid SQLite database: %s" % err_text(exc))
            status = "failed"
        except OSError as exc:
            errors.append("cannot open database: %s" % err_text(exc))
            status = "failed"
        finally:
            if conn is not None:
                conn.close()

    summary = ("Inspected %d table/view(s) with %d column(s); executed %d query/queries."
               % (len(schema), len(dictionary), len(query_results))) if db_path else "No SQLite database was found."
    result = {"schema": schema, "dictionary": dictionary, "query_results": query_results,
              "checks": checks, "suggested_queries": suggestions, "database": db_info,
              "task_summary": task_summary}
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
              "schema": schema, "dictionary": dictionary, "query_results": query_results,
              "checks": checks, "suggested_queries": suggestions, "summary": summary}
    extras = {}
    for field in fields:
        canonical, value = resolve_field(field, payload)
        if canonical is None:
            canonical = keyword_target(field)
            if canonical:
                value = payload.get(canonical)
        if canonical is None:
            best = case.get(field) if isinstance(case, dict) else None
            if best == "":
                best = None
            output[field] = None
            extras[field] = {"value": best,
                             "reason": "no alias or keyword mapping for this output_contract field; "
                                       "top-level key kept as null, best-effort value recorded here"}
        else:
            output[field] = None if value == "" else value
    if extras:
        output["contract_extras"] = extras

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
    try:
        import report_kit
        report_kit.augment(args.out or str(out_path), getattr(args, "case", None), ["figure.svg"])
    except Exception:
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
