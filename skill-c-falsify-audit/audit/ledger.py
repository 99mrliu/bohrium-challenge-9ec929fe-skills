#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger.py — adversarial auditor for scientific data-analysis results.

This tool NEVER produces analysis results. Its only job is to try to BREAK
a result.json produced by someone else (the "author channel"). Every check
is independent: inputs are re-hashed, every reported number is recomputed
from the raw inputs with a from-scratch implementation, figures are checked
against the data dictionary, and the output contract is re-read from
case.json rather than trusted from the author's result.

Usage:
    python3 ledger.py --input /input --work /app/work --output /app/output

Inputs it expects:
    /app/output/result.json      the author's deliverable
    /app/work/claims.jsonl       one JSON object per line, each a checkable claim:
        {"id": "m1", "kind": "mean|median|std|min|max|count|row_count|
          n_missing|count_where|sql_scalar",
         "source": "/path/file.csv", "column": "...", "value": <reported>,
         "tolerance": <optional abs/rel tol>,
         # count_where: "op": "==|!=|>|>=|<|<=", "threshold": X
         # sql_scalar:  "db": "/path/x.db", "query": "SELECT ..."}
    /app/output/fig*.meta.json   one per declared figure:
        {"png": "/app/output/fig1.png", "data_file": "/app/work/clean.csv",
         "columns": ["strength"], "title": "...", "xlabel": "...", "ylabel": "..."}

Exit code 0 = every check CONFIRMED; 1 = at least one FAIL/UNVERIFIABLE.
The ledger is written to /app/output/audit_ledger.json either way.
"""
import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

NA = {'', 'na', 'n/a', 'nan', 'null', 'none', '-', '--'}
OPS = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b,
       '>': lambda a, b: a > b, '>=': lambda a, b: a >= b,
       '<': lambda a, b: a < b, '<=': lambda a, b: a <= b}


def hash_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while True:
            b = f.read(1 << 16)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def text_of(p):
    blob = open(p, 'rb').read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            pass
    return blob.decode('latin-1', 'replace')


def blank(v):
    return v is None or str(v).strip().lower() in NA


def num(v):
    if blank(v):
        return None
    try:
        return float(str(v).strip().replace(',', ''))
    except ValueError:
        return None


def column_values(path, column):
    """Independent CSV reader: returns (numeric values, n_missing, n_rows)."""
    vals, miss = [], 0
    with open(path, newline='', encoding='utf-8-sig') as f:
        rdr = csv.DictReader(f)
        if rdr.fieldnames is None or column not in rdr.fieldnames:
            raise ValueError('column %r not in %s header %s'
                             % (column, path, rdr.fieldnames))
        n = 0
        for r in rdr:
            n += 1
            x = num(r.get(column))
            if x is None:
                miss += 1
            else:
                vals.append(x)
    return vals, miss, n


def recheck(claim):
    """Recompute one claim from raw inputs. Returns (recomputed, note)."""
    k = claim['kind']
    if k == 'sql_scalar':
        con = sqlite3.connect('file:%s?mode=ro' % claim['db'], uri=True)
        try:
            cur = con.execute(claim['query'])
            got = cur.fetchall()
        finally:
            con.close()
        if len(got) != 1 or len(got[0]) != 1:
            raise ValueError('not scalar: %d rows' % len(got))
        return got[0][0], 're-executed on raw db'
    src = claim['source']
    if k == 'row_count':
        with open(src, newline='', encoding='utf-8-sig') as f:
            r = csv.reader(f)
            header = next(r, None)
            n = sum(1 for row in r if any(str(c).strip() for c in row))
        return n, 'counted data rows (header excluded)'
    if k == 'n_missing':
        _, miss, _ = column_values(src, claim['column'])
        return miss, 'counted missing/NA tokens'
    if k == 'count_where':
        vals, miss, n = column_values(src, claim['column'])
        op = OPS[claim['op']]
        thr = claim['threshold']
        c = sum(1 for v in vals if op(v, float(thr)))
        return c, 're-filtered %d numeric values' % len(vals)
    vals, miss, n = column_values(src, claim['column'])
    if not vals:
        raise ValueError('no numeric values in column')
    m = sum(vals) / len(vals)
    if k == 'count':
        return len(vals), 'non-missing count'
    if k == 'mean':
        return m, 'mean over %d non-missing' % len(vals)
    if k == 'std':
        return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
                         if len(vals) > 1 else 0.0), 'sample std (n-1)'
    if k == 'min':
        return min(vals), ''
    if k == 'max':
        return max(vals), ''
    if k == 'median':
        s = sorted(vals)
        q = (len(s) - 1) / 2
        lo = int(math.floor(q))
        return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (q - lo), \
            'linear-interpolation median'
    raise ValueError('unknown claim kind %r' % k)


def verdict(reported, recomputed, tol):
    if isinstance(reported, (int, float)) and isinstance(recomputed, (int, float)):
        t = tol if tol is not None else (0 if isinstance(recomputed, int)
                                         and isinstance(reported, int) else 1e-6)
        return 'CONFIRMED' if abs(reported - recomputed) <= max(t, t * abs(recomputed)) \
            else 'MISMATCH'
    return 'CONFIRMED' if str(reported) == str(recomputed) else 'MISMATCH'


def deep_get(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = deep_get(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_get(v, key)
            if r is not None:
                return r
    return None


def field_names(contract):
    if isinstance(contract, dict):
        for k in ('fields', 'required', 'properties'):
            if isinstance(contract.get(k), (list, dict)):
                return field_names(contract[k])
        return list(contract.keys())
    if isinstance(contract, list):
        out = []
        for it in contract:
            out.append(it if isinstance(it, str)
                       else (it.get('name') or it.get('field') or str(it)))
        return out
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', default='/input')
    ap.add_argument('--work', default='/app/work')
    ap.add_argument('--output', default='/app/output')
    a = ap.parse_args()
    inp, work, out = Path(a.input), Path(a.work), Path(a.output)
    checks = []

    def add(name, verdict_, detail):
        checks.append({'check': name, 'verdict': verdict_, 'detail': detail})
        print('[%s] %s — %s' % (verdict_, name, detail))

    # ---- 1. input provenance: re-hash everything, compare with provenance
    actual = {}
    for p in sorted(inp.rglob('*')):
        if p.is_file():
            actual[str(p)] = hash_file(p)
    rj_path = out / 'result.json'
    if not rj_path.exists():
        add('result.json exists', 'FAIL', 'no deliverable at %s' % rj_path)
        write_ledger(out, checks, a)
        sys.exit(1)
    rj = json.loads(text_of(rj_path))
    declared = {e.get('path'): e.get('sha256')
                for e in (deep_get(rj, 'inputs') or []) if isinstance(e, dict)}
    if not declared:
        add('provenance.inputs declared', 'FAIL',
            'result.json provenance has no input hash list')
    else:
        mism = [p for p, h in actual.items()
                if p in declared and declared[p] != h]
        missing = [p for p in actual if p not in declared]
        if mism:
            add('input hashes match', 'FAIL',
                'analysis used stale/modified inputs: %s' % mism)
        elif missing:
            add('input hashes match', 'UNVERIFIABLE',
                'inputs not covered by provenance: %s' % missing)
        else:
            add('input hashes match', 'CONFIRMED',
                '%d input files re-hashed identical' % len(actual))

    # ---- 2. contract, re-read from case.json (never trust the author)
    cj = next(iter(sorted(inp.rglob('case.json'))), None)
    if cj is None:
        add('case.json found', 'UNVERIFIABLE', 'cannot locate case.json')
    else:
        case = json.loads(text_of(cj))
        cid = deep_get(case, 'case_id') or deep_get(case, 'id')
        if rj.get('case_id') != cid:
            add('case_id consistency', 'FAIL',
                'result=%r case=%r' % (rj.get('case_id'), cid))
        else:
            add('case_id consistency', 'CONFIRMED', str(cid))
        fields = field_names(deep_get(case, 'output_contract'))
        absent = [f for f in fields if f not in rj]
        nullf = [f for f in fields if f in rj and rj[f] is None]
        if absent or nullf:
            add('output_contract coverage', 'FAIL',
                'absent=%s null=%s' % (absent, nullf))
        else:
            add('output_contract coverage', 'CONFIRMED',
                '%d fields present and non-null' % len(fields))

    # ---- 3. claims: recompute every number independently
    cl_path = work / 'claims.jsonl'
    if not cl_path.exists():
        add('claims registered', 'FAIL',
            'no claims.jsonl — unverifiable deliverable (register every number)')
    else:
        claims = [json.loads(l) for l in text_of(cl_path).splitlines()
                  if l.strip()]
        n_bad = 0
        for c in claims:
            try:
                rec, note = recheck(c)
                v = verdict(c.get('value'), rec, c.get('tolerance'))
                add('claim %s (%s)' % (c.get('id'), c['kind']), v,
                    'reported=%r recomputed=%r %s' % (c.get('value'), rec, note))
            except Exception as e:
                v = 'UNVERIFIABLE'
                add('claim %s (%s)' % (c.get('id'), c.get('kind')), v, str(e))
            if v != 'CONFIRMED':
                n_bad += 1
        if not claims:
            add('claims non-empty', 'FAIL', 'claims.jsonl is empty')
        elif n_bad == 0:
            add('claims summary', 'CONFIRMED',
                '%d/%d claims independently recomputed' % (len(claims), len(claims)))

    # ---- 4. figure audit: file real + columns real + labels non-empty
    figs = rj.get('figures') or []
    for fig in figs:
        fpath = fig.get('path') if isinstance(fig, dict) else fig
        p = Path(str(fpath))
        if not p.exists():
            add('figure file %s' % fpath, 'FAIL', 'declared but missing')
            continue
        head = open(p, 'rb').read(8)
        if head[:8] != b'\x89PNG\r\n\x1a\n' and head[:3] != b'\xff\xd8\xff':
            add('figure file %s' % fpath, 'FAIL',
                'not a real PNG/JPEG (wrong magic bytes)')
            continue
        if p.stat().st_size < 2000:
            add('figure file %s' % fpath, 'FAIL',
                'suspiciously small (%d bytes) — blank figure?' % p.stat().st_size)
            continue
        meta = p.with_suffix('.meta.json')
        if not meta.exists():
            add('figure meta %s' % fpath, 'UNVERIFIABLE',
                'no .meta.json: cannot check columns/labels against data')
            continue
        m = json.loads(text_of(meta))
        problems = []
        df = m.get('data_file')
        if df and Path(df).exists():
            with open(df, newline='', encoding='utf-8-sig') as f:
                hdr = csv.reader(f).__next__()
            for c in m.get('columns') or []:
                if c not in hdr:
                    problems.append('column %r not in %s' % (c, df))
        for lbl in ('title', 'xlabel', 'ylabel'):
            if lbl in m and not str(m[lbl]).strip():
                problems.append('%s empty' % lbl)
        if problems:
            add('figure meta %s' % fpath, 'FAIL', '; '.join(problems))
        else:
            add('figure meta %s' % fpath, 'CONFIRMED',
                'columns exist in data; labels present')

    # ---- 5. provenance completeness & status honesty
    prov = rj.get('provenance') or {}
    env = prov.get('environment') or {}
    if not (env.get('python') or prov.get('python')):
        add('environment recorded', 'FAIL', 'no python version in provenance')
    else:
        add('environment recorded', 'CONFIRMED',
            'python=%s' % (env.get('python') or prov.get('python')))
    if 'seed' not in prov:
        add('seed recorded', 'UNVERIFIABLE',
            'provenance.seed absent (state "no randomness" explicitly)')
    n_fail = sum(1 for c in checks if c['verdict'] in ('FAIL', 'MISMATCH'))
    if rj.get('status') == 'success' and n_fail:
        add('status honesty', 'FAIL',
            'status=success despite %d failed checks' % n_fail)
    elif n_fail:
        add('status honesty', 'CONFIRMED',
            'status=%r consistent with %d failures' % (rj.get('status'), n_fail))
    else:
        add('status honesty', 'CONFIRMED', 'status=%r' % rj.get('status'))

    write_ledger(out, checks, a)
    bad = [c for c in checks if c['verdict'] != 'CONFIRMED']
    print('\nAUDIT %s: %d/%d checks confirmed'
          % ('FAILED' if bad else 'PASSED', len(checks) - len(bad), len(checks)))
    sys.exit(1 if bad else 0)


def write_ledger(out, checks, a):
    led = {'auditor': 'ledger.py (independent recomputation, stdlib only)',
           'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
           'paths': {'input': a.input, 'work': a.work, 'output': a.output},
           'n_confirmed': sum(1 for c in checks if c['verdict'] == 'CONFIRMED'),
           'n_total': len(checks), 'checks': checks}
    Path(a.output).mkdir(parents=True, exist_ok=True)
    with open(Path(a.output) / 'audit_ledger.json', 'w', encoding='utf-8') as f:
        json.dump(led, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
