#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""casekit.py — deterministic toolkit for fixed scientific data-analysis cases.

Design goals
------------
* stdlib-only: every subcommand runs on a bare Python 3 (>=3.8) with no
  third-party dependency. matplotlib is used opportunistically for figures
  and skipped with an explicit receipt when unavailable.
* deterministic: no randomness anywhere; identical inputs give byte-identical
  outputs. Nothing is sampled, nothing depends on wall time except log
  timestamps.
* loud, never silent: every rule application reports exactly how many rows
  or values it touched; every failure exits non-zero with a readable reason.
* auditable: every invocation appends one line to <work>/run_log.jsonl
  (argv, exit code, duration) so the whole run can be replayed by hand.

Subcommands
-----------
profile    inventory /input, hash every file, profile CSV/JSON/SQLite,
           extract case_id + output_contract from case.json, write a
           result.json skeleton EARLY so a partial deliverable always exists.
clean      apply an explicit rules.json (rename/coerce/missing/dedup/filter)
           to a CSV table; every touched row is counted in clean_report.json.
describe   count/mean/std/min/quartiles/max for chosen columns.
aggregate  group-by aggregation (count/mean/std/min/max/median/sum).
stats      Welch t-test, Pearson correlation, OLS (1-2 predictors) in pure
           Python (incomplete-beta p-values), no scipy needed.
sql        run a read-only SELECT against a SQLite database, save CSV,
           report the row count explicitly.
figure     hist/scatter/bar/line PNG via matplotlib when importable;
           otherwise writes the plotted data as CSV plus FIGURE_SKIPPED.
verify     recompute every reported number in claims.json from the RAW
           inputs by an independent path and diff with tolerance.
finalize   check result.json against the case output_contract, attach
           verification + provenance, force honest status.

All commands accept --work and --output and default to /app/work, /app/output.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import platform
import sqlite3
import sys
import time
from pathlib import Path

MISSING_TOKENS = {'', 'na', 'n/a', 'nan', 'null', 'none', '-', '--', 'unknown'}
LOG_NAME = 'run_log.jsonl'


# ---------------------------------------------------------------- utilities
def log_run(work, argv, code, dur, note=''):
    rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'argv': argv,
           'exit_code': code, 'duration_s': round(dur, 3)}
    if note:
        rec['note'] = note
    try:
        with open(Path(work) / LOG_NAME, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def die(msg, code=2):
    sys.stderr.write('casekit ERROR: %s\n' % msg)
    sys.exit(code)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def read_text_any(path):
    """Read text with explicit encoding fallback; never guess silently."""
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', errors='replace'), 'latin-1(replace)'


def is_missing(v):
    return v is None or str(v).strip().lower() in MISSING_TOKENS


def to_float(v):
    if is_missing(v):
        return None
    s = str(v).strip().replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None


def load_csv(path):
    """Return (header, rows[list[dict]], meta). Robust dialect/encoding."""
    text, enc = read_text_any(path)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',\t;|')
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == '.tsv' else csv.excel
    lines = text.splitlines()
    rdr = csv.reader(lines, dialect)
    rows_raw = [r for r in rdr if any(str(c).strip() for c in r)]
    if not rows_raw:
        die('empty table: %s' % path)
    header = [str(h).strip() for h in rows_raw[0]]
    if len(set(header)) != len(header):
        die('duplicate column names in %s: %s' % (path, header))
    recs = []
    for i, r in enumerate(rows_raw[1:]):
        if len(r) != len(header):
            recs.append({'_row_error': 'field_count=%d expected=%d'
                         % (len(r), len(header))})
        rec = {header[j]: (r[j] if j < len(r) else '') for j in range(len(header))}
        recs.append(rec)
    meta = {'encoding': enc, 'delimiter': getattr(dialect, 'delimiter', ','),
            'n_rows': len(recs), 'columns': header}
    return header, recs, meta


def percentile(sorted_vals, q):
    """Linear-interpolation percentile (numpy 'linear' method), q in [0,1]."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def col_stats(vals):
    """vals: list of float (missing already removed). Descriptive stats."""
    n = len(vals)
    if n == 0:
        return {'count': 0}
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    sv = sorted(vals)
    return {'count': n, 'mean': mean, 'std': math.sqrt(var),
            'min': sv[0], 'q1': percentile(sv, 0.25),
            'median': percentile(sv, 0.5), 'q3': percentile(sv, 0.75),
            'max': sv[-1]}


def numeric_column(recs, col):
    """Return (values, n_missing, n_nonnumeric)."""
    vals, n_miss, n_bad = [], 0, 0
    for r in recs:
        v = r.get(col)
        if is_missing(v):
            n_miss += 1
            continue
        f = to_float(v)
        if f is None:
            n_bad += 1
        else:
            vals.append(f)
    return vals, n_miss, n_bad


def find_case_json(input_dir):
    cands = sorted(Path(input_dir).rglob('case.json'))
    return cands[0] if cands else None


def find_key_recursive(obj, key):
    """Depth-first search for key in nested dict/list; first hit wins."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key_recursive(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key_recursive(v, key)
            if r is not None:
                return r
    return None


def contract_fields(contract):
    """Normalize output_contract (dict|list) to a list of field names."""
    if isinstance(contract, dict):
        for k in ('fields', 'required', 'properties', 'schema'):
            if isinstance(contract.get(k), (list, dict)):
                return contract_fields(contract[k])
        return list(contract.keys())
    if isinstance(contract, list):
        out = []
        for item in contract:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(item.get('name') or item.get('field')
                           or item.get('key') or json.dumps(item, ensure_ascii=False))
        return out
    return []


# ------------------------------------------------------------------ profile
def profile_table(path):
    p = Path(path)
    info = {'file': str(p), 'sha256': sha256_of(p),
            'bytes': p.stat().st_size}
    suf = p.suffix.lower()
    if suf in ('.csv', '.tsv', '.txt'):
        try:
            header, recs, meta = load_csv(p)
        except SystemExit:
            raise
        except Exception as e:  # keep profiling other files
            info['error'] = 'csv_parse_failed: %s' % e
            return info
        info.update(meta)
        info['kind'] = 'table'
        dup = len(recs) - len({tuple(sorted(r.items())) for r in recs})
        info['exact_duplicate_rows'] = dup
        cols = {}
        for c in header:
            vals, n_miss, n_bad = numeric_column(recs, c)
            entry = {'n_missing': n_miss}
            n_text = len(recs) - n_miss - n_bad - len(vals)
            if vals and n_bad == 0 and (len(vals) + n_miss) == len(recs):
                entry['type'] = 'numeric'
                entry.update(col_stats(vals))
            else:
                entry['type'] = 'text' if not vals else 'mixed'
                entry['n_nonnumeric'] = n_bad
                uniq = {}
                for r in recs:
                    v = r.get(c)
                    if not is_missing(v):
                        uniq[str(v).strip()] = uniq.get(str(v).strip(), 0) + 1
                entry['n_unique'] = len(uniq)
                if len(uniq) <= 20:
                    entry['values'] = dict(sorted(uniq.items(),
                                                  key=lambda kv: -kv[1]))
            cols[c] = entry
        info['column_profile'] = cols
    elif suf in ('.db', '.sqlite', '.sqlite3'):
        info['kind'] = 'sqlite'
        try:
            con = sqlite3.connect('file:%s?mode=ro' % p, uri=True)
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            info['tables'] = {}
            for t in tables:
                cols = con.execute('PRAGMA table_info("%s")' % t).fetchall()
                n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                tinfo = {'n_rows': n,
                         'columns': [{'name': c[1], 'decl_type': c[2],
                                      'notnull': bool(c[3])} for c in cols]}
                nulls = {}
                for c in cols:
                    nulls[c[1]] = con.execute(
                        'SELECT COUNT(*) FROM "%s" WHERE "%s" IS NULL'
                        % (t, c[1])).fetchone()[0]
                tinfo['null_counts'] = nulls
                info['tables'][t] = tinfo
            con.close()
        except sqlite3.Error as e:
            info['error'] = 'sqlite_failed: %s' % e
    elif suf == '.json':
        info['kind'] = 'json'
        try:
            obj = json.loads(read_text_any(p)[0])
            info['top_keys'] = list(obj.keys()) if isinstance(obj, dict) else None
            info['json_type'] = type(obj).__name__
        except Exception as e:
            info['error'] = 'json_parse_failed: %s' % e
    else:
        info['kind'] = 'other'
    return info


def cmd_profile(a):
    input_dir, work, out = Path(a.input), Path(a.work), Path(a.output)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    files = [p for p in sorted(input_dir.rglob('*')) if p.is_file()]
    inv = {'input_dir': str(input_dir), 'n_files': len(files), 'files': []}
    for p in files:
        inv['files'].append({'path': str(p), 'bytes': p.stat().st_size,
                             'sha256': sha256_of(p)})
    profiles, case_meta = [], {}
    for p in files:
        if p.name in ('case.json',):
            obj = json.loads(read_text_any(p)[0])
            case_meta['case_json'] = str(p)
            case_meta['case_id'] = (find_key_recursive(obj, 'case_id')
                                    or find_key_recursive(obj, 'id'))
            contract = find_key_recursive(obj, 'output_contract')
            case_meta['output_contract_fields'] = contract_fields(contract)
            case_meta['top_keys'] = list(obj.keys()) if isinstance(obj, dict) else None
            dd = find_key_recursive(obj, 'data_dictionary')
            if dd is not None:
                with open(work / 'data_dictionary.json', 'w', encoding='utf-8') as f:
                    json.dump(dd, f, ensure_ascii=False, indent=2)
                case_meta['data_dictionary_saved'] = 'data_dictionary.json'
            continue
        profiles.append(profile_table(p))
    env = {'python': sys.version.split()[0], 'platform': platform.platform(),
           'executable': sys.executable}
    try:
        import matplotlib
        env['matplotlib'] = matplotlib.__version__
    except Exception:
        env['matplotlib'] = None
    for mod in ('pandas', 'numpy', 'scipy'):
        try:
            m = __import__(mod)
            env[mod] = getattr(m, '__version__', '?')
        except Exception:
            env[mod] = None
    bundle = {'environment': env, 'case': case_meta,
              'inventory': inv, 'profiles': profiles}
    with open(work / 'profile.json', 'w', encoding='utf-8') as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)

    # human summary
    lines = ['# Data profile', '',
             'case_id: %s' % case_meta.get('case_id'),
             'output_contract fields: %s'
             % ', '.join(case_meta.get('output_contract_fields') or ['<none found>']),
             '', '## Files']
    for pr in profiles:
        lines.append('- %s (%s, %d bytes, sha256 %s…)'
                     % (pr['file'], pr.get('kind'), pr['bytes'], pr['sha256'][:12]))
        if pr.get('kind') == 'table':
            lines.append('  rows=%d cols=%s' % (pr['n_rows'], pr['columns']))
            lines.append('  exact duplicate rows: %d' % pr['exact_duplicate_rows'])
            for c, cp in pr['column_profile'].items():
                if cp['type'] == 'numeric':
                    lines.append('  - %s: numeric, missing=%d, mean=%.6g, min=%.6g, max=%.6g'
                                 % (c, cp['n_missing'], cp.get('mean', float('nan')),
                                    cp.get('min', float('nan')), cp.get('max', float('nan'))))
                else:
                    lines.append('  - %s: %s, missing=%d, unique=%s'
                                 % (c, cp['type'], cp['n_missing'], cp.get('n_unique')))
        if pr.get('kind') == 'sqlite':
            for t, ti in (pr.get('tables') or {}).items():
                lines.append('  table %s: %d rows, cols=%s, nulls=%s'
                             % (t, ti['n_rows'],
                                [c['name'] for c in ti['columns']], ti['null_counts']))
        if pr.get('error'):
            lines.append('  ERROR: %s' % pr['error'])
    with open(work / 'profile.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    # EARLY result.json skeleton: a partial deliverable always exists
    skeleton = {
        'case_id': case_meta.get('case_id'),
        'status': 'in_progress',
        'results': {},
        'figures': [],
        'queries': [],
        'anomalies': [],
        'provenance': {
            'inputs': inv['files'],
            'environment': env,
            'seed': None,
            'run_log': LOG_NAME,
            'toolkit': 'casekit.py (stdlib-only, deterministic)'},
        'verification': None,
    }
    for fld in case_meta.get('output_contract_fields') or []:
        if fld not in skeleton:
            skeleton[fld] = None  # placeholder, must be replaced with real values
    rj = out / 'result.json'
    if not rj.exists():
        with open(rj, 'w', encoding='utf-8') as f:
            json.dump(skeleton, f, ensure_ascii=False, indent=2)
    print('profiled %d files -> %s ; result skeleton -> %s'
          % (len(profiles), work / 'profile.json', rj))


# -------------------------------------------------------------------- clean
def cmd_clean(a):
    rules = json.loads(read_text_any(a.rules)[0])
    header, recs, meta = load_csv(Path(a.table))
    report = {'table': a.table, 'rows_in': len(recs), 'rules_applied': [],
              'encoding': meta['encoding']}

    def require(cols, stage):
        cols = [c for c in (cols or []) if c]
        bad = [c for c in cols if c not in header]
        if bad:
            die('rule "%s" references missing column(s) %s; available: %s. '
                'Fix rules.json — refusing to guess.'
                % (stage, bad, header))

    rename = rules.get('rename') or {}
    if rename:
        # headers were whitespace-stripped at load; match stripped names too
        norm = {str(k).strip(): v for k, v in rename.items()}
        applied = {}
        for r in recs:
            for old in list(r.keys()):
                new = rename.get(old) or norm.get(str(old).strip())
                if new and new != old:
                    r[new] = r.pop(old)
                    applied[old] = new
        header = [applied.get(h, h) for h in header]
        unmatched = [k for k in rename
                     if k not in applied and str(k).strip() not in
                     {str(a).strip() for a in applied}]
        if unmatched:
            die('rename keys matched no column: %s (have %s)'
                % (unmatched, header))
        report['rules_applied'].append({'rule': 'rename', 'map': applied})

    sel = rules.get('select_columns')
    if sel:
        missing_cols = [c for c in sel if c not in header]
        if missing_cols:
            die('select_columns not present: %s (have %s)' % (missing_cols, header))
        recs = [{c: r.get(c, '') for c in sel} for r in recs]
        header = sel
        report['rules_applied'].append({'rule': 'select_columns', 'columns': sel})

    coerce = rules.get('coerce') or {}
    require(list(coerce), 'coerce')
    for col, typ in coerce.items():
        n_fail = 0
        for r in recs:
            v = r.get(col)
            if is_missing(v):
                continue
            f = to_float(v)
            if f is None:
                n_fail += 1
                r[col] = ''
            elif typ == 'int':
                r[col] = str(int(f))
            elif typ == 'float':
                r[col] = repr(f)
            else:
                r[col] = str(v).strip()
        report['rules_applied'].append(
            {'rule': 'coerce', 'column': col, 'type': typ,
             'unparseable_set_to_missing': n_fail})

    miss = rules.get('missing') or {}
    policy = miss.get('policy', 'report_only')
    miss_cols = miss.get('columns') or header
    require(miss_cols, 'missing.columns')
    require(list(miss.get('impute') or {}), 'missing.impute')
    n_miss_before = sum(1 for r in recs for c in miss_cols if is_missing(r.get(c)))
    if policy == 'drop_rows':
        before = len(recs)
        dropped_idx = [i for i, r in enumerate(recs)
                       if any(is_missing(r.get(c)) for c in miss_cols)]
        recs = [r for i, r in enumerate(recs) if i not in set(dropped_idx)]
        report['rules_applied'].append(
            {'rule': 'missing:drop_rows', 'columns': miss_cols,
             'rows_dropped': before - len(recs),
             'dropped_row_indices_0based_cap200': dropped_idx[:200]})
    elif policy == 'impute':
        for col, how in (miss.get('impute') or {}).items():
            vals, _, _ = numeric_column(recs, col)
            if how == 'mean':
                fill = sum(vals) / len(vals) if vals else None
            elif how == 'median':
                fill = percentile(sorted(vals), 0.5) if vals else None
            elif how == 'mode':
                freq = {}
                for r in recs:
                    v = r.get(col)
                    if not is_missing(v):
                        freq[v] = freq.get(v, 0) + 1
                fill = max(freq, key=freq.get) if freq else None
            else:
                fill = how  # literal fill value
            if fill is None:
                die('cannot impute %s: no observed values' % col)
            n_fill = 0
            for r in recs:
                if is_missing(r.get(col)):
                    r[col] = repr(fill) if isinstance(fill, float) else str(fill)
                    n_fill += 1
            report['rules_applied'].append(
                {'rule': 'missing:impute', 'column': col, 'method': str(how),
                 'fill_value': fill, 'n_imputed': n_fill})
    else:
        report['rules_applied'].append(
            {'rule': 'missing:report_only', 'missing_cells_in_scope': n_miss_before})

    dedup = rules.get('dedup') or {}
    dpolicy = dedup.get('policy', 'keep')
    if dpolicy == 'drop_exact':
        seen, kept, dropped = set(), [], 0
        for r in recs:
            key = tuple((c, r.get(c)) for c in header)
            if key in seen:
                dropped += 1
            else:
                seen.add(key)
                kept.append(r)
        recs = kept
        report['rules_applied'].append({'rule': 'dedup:drop_exact',
                                        'rows_dropped': dropped})
    elif dpolicy == 'drop_by':
        keys = dedup.get('keys') or []
        require(keys, 'dedup.keys')
        seen, kept, dropped = set(), [], 0
        for r in recs:
            key = tuple(r.get(k) for k in keys)
            if key in seen:
                dropped += 1
            else:
                seen.add(key)
                kept.append(r)
        recs = kept
        report['rules_applied'].append({'rule': 'dedup:drop_by', 'keys': keys,
                                        'rows_dropped': dropped})
    else:
        report['rules_applied'].append({'rule': 'dedup:keep'})

    for flt in rules.get('filter') or []:
        col, op, val = flt['column'], flt['op'], flt.get('value')
        if col not in header:
            die('filter column not present: %s' % col)
        before = len(recs)

        def keep(r):
            v = r.get(col)
            if op == 'notna':
                return not is_missing(v)
            if op == 'na':
                return is_missing(v)
            fv, fval = to_float(v), to_float(val)
            if fv is not None and fval is not None:
                lhs, rhs = fv, fval
            else:
                lhs, rhs = str(v).strip(), str(val).strip()
            return {'==': lhs == rhs, '!=': lhs != rhs, '>': lhs > rhs,
                    '>=': lhs >= rhs, '<': lhs < rhs, '<=': lhs <= rhs}[op]
        recs = [r for r in recs if keep(r)]
        report['rules_applied'].append(
            {'rule': 'filter', 'column': col, 'op': op, 'value': val,
             'rows_dropped': before - len(recs)})

    report['rows_out'] = len(recs)
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in recs:
            w.writerow({c: r.get(c, '') for c in header})
    with open(a.report, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print('clean: %d -> %d rows; report -> %s'
          % (report['rows_in'], report['rows_out'], a.report))


# ------------------------------------------------------- describe/aggregate
def cmd_describe(a):
    header, recs, _ = load_csv(Path(a.table))
    cols = a.columns.split(',') if a.columns else header
    out = {}
    for c in cols:
        if c not in header:
            die('column not present: %s (have %s)' % (c, header))
        vals, n_miss, n_bad = numeric_column(recs, c)
        st = col_stats(vals)
        st['n_missing'] = n_miss
        st['n_nonnumeric'] = n_bad
        out[c] = st
    dest = a.out or str(Path(a.work) / 'describe.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_aggregate(a):
    header, recs, _ = load_csv(Path(a.table))
    gb = a.group_by.split(',') if a.group_by else []
    for c in gb:
        if c not in header:
            die('group-by column not present: %s' % c)
    metrics = []
    for m in a.metrics.split(','):
        col, fn = m.split(':')
        if col not in header:
            die('metric column not present: %s' % col)
        metrics.append((col.strip(), fn.strip()))
    groups = {}
    for r in recs:
        key = tuple(r.get(c, '') for c in gb)
        groups.setdefault(key, []).append(r)
    result = []
    for key in sorted(groups):
        row = {'__group__': dict(zip(gb, key)), 'n_rows': len(groups[key])}
        for col, fn in metrics:
            vals, n_miss, _ = numeric_column(groups[key], col)
            st = col_stats(vals)
            row['%s_%s' % (col, fn)] = st.get(fn) if fn != 'sum' else sum(vals)
            row['%s_n_used' % col] = st['count']
            row['%s_n_missing' % col] = n_miss
        result.append(row)
    dest = a.out or str(Path(a.work) / 'aggregate.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


# -------------------------------------------------------------------- stats
def _betacf(x, a, b):
    """Continued fraction for incomplete beta (Numerical Recipes)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return h


def ibeta(x, a, b):
    """Regularized incomplete beta I_x(a,b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(x, a, b) / a
    return 1.0 - bt * _betacf(1.0 - x, b, a) / b


def t_cdf_two_sided(t, df):
    """Two-sided p-value for Student t with given df."""
    if df <= 0:
        return None
    x = df / (df + t * t)
    return min(1.0, ibeta(x, df / 2.0, 0.5))


def welch_ttest(x, y):
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2:
        die('Welch t-test needs >=2 observations per group (got %d, %d)'
            % (nx, ny))
    mx, my = sum(x) / nx, sum(y) / ny
    vx = sum((v - mx) ** 2 for v in x) / (nx - 1)
    vy = sum((v - my) ** 2 for v in y) / (ny - 1)
    se2 = vx / nx + vy / ny
    if se2 == 0:
        die('Welch t-test degenerate: zero variance in both groups')
    t = (mx - my) / math.sqrt(se2)
    df = se2 ** 2 / ((vx / nx) ** 2 / (nx - 1) + (vy / ny) ** 2 / (ny - 1))
    return {'test': 'welch_ttest', 'n1': nx, 'n2': ny, 'mean1': mx,
            'mean2': my, 'var1': vx, 'var2': vy, 't': t, 'df': df,
            'p_two_sided': t_cdf_two_sided(t, df),
            'assumptions_checked': 'Welch variant: no equal-variance assumption; '
                                   'normality NOT verified by this tool — state this'}


def pearson(x, y):
    n = len(x)
    if n < 3:
        die('Pearson correlation needs >=3 paired observations')
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    if sxx == 0 or syy == 0:
        die('Pearson degenerate: zero variance')
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    r = sxy / math.sqrt(sxx * syy)
    t = r * math.sqrt((n - 2) / max(1e-300, 1 - r * r))
    return {'test': 'pearson', 'n': n, 'r': r, 't': t, 'df': n - 2,
            'p_two_sided': t_cdf_two_sided(t, n - 2)}


def ols(X, y, names):
    """OLS via normal equations, <=2 predictors + intercept."""
    n = len(y)
    cols = [[1.0] * n] + X
    p = len(cols)
    xtx = [[sum(cols[i][k] * cols[j][k] for k in range(n)) for j in range(p)]
           for i in range(p)]
    xty = [sum(cols[i][k] * y[k] for k in range(n)) for i in range(p)]
    # Gaussian elimination with partial pivoting
    m = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            die('OLS singular design matrix (collinear predictors?)')
        m[c], m[piv] = m[piv], m[c]
        for r in range(p):
            if r != c:
                fac = m[r][c] / m[c][c]
                m[r] = [m[r][j] - fac * m[c][j] for j in range(p + 1)]
    beta = [m[i][p] / m[i][i] for i in range(p)]
    resid = [y[k] - sum(beta[i] * cols[i][k] for i in range(p)) for k in range(n)]
    sse = sum(e * e for e in resid)
    ymean = sum(y) / n
    sst = sum((v - ymean) ** 2 for v in y)
    r2 = 1 - sse / sst if sst > 0 else None
    return {'test': 'ols', 'n': n,
            'coefficients': dict(zip(['intercept'] + names, beta)),
            'r_squared': r2, 'sse': sse,
            'residual_df': n - p,
            'note': 'normal-equation OLS; check residual assumptions separately'}


def paired_columns(recs, cx, cy):
    x, y, skipped = [], [], 0
    for r in recs:
        fx, fy = to_float(r.get(cx)), to_float(r.get(cy))
        if fx is None or fy is None:
            skipped += 1
            continue
        x.append(fx)
        y.append(fy)
    return x, y, skipped


def cmd_stats(a):
    header, recs, _ = load_csv(Path(a.table))
    if a.kind == 'ttest':
        gvals = {}
        for r in recs:
            g = r.get(a.group)
            v = to_float(r.get(a.column))
            if is_missing(g) or v is None:
                continue
            gvals.setdefault(str(g).strip(), []).append(v)
        names = sorted(gvals)
        if len(names) != 2:
            die('ttest needs exactly 2 groups, got %s' % names)
        res = welch_ttest(gvals[names[0]], gvals[names[1]])
        res['groups'] = names
    elif a.kind == 'corr':
        x, y, skipped = paired_columns(recs, a.x, a.y)
        res = pearson(x, y)
        res['x'], res['y'], res['pairs_skipped_missing'] = a.x, a.y, skipped
    elif a.kind == 'ols':
        names = [c.strip() for c in a.x.split(',')]
        if not 1 <= len(names) <= 2:
            die('ols supports 1-2 predictors')
        cols, yv, used = [], [], 0
        for r in recs:
            fy = to_float(r.get(a.y))
            fxs = [to_float(r.get(c)) for c in names]
            if fy is None or any(f is None for f in fxs):
                continue
            yv.append(fy)
            used += 1
            for j, f in enumerate(fxs):
                if len(cols) <= j:
                    cols.append([])
                cols[j].append(f)
        res = ols(cols, yv, names)
        res['y'], res['x'] = a.y, names
        res['rows_used'], res['rows_skipped'] = used, len(recs) - used
    else:
        die('unknown stats kind: %s' % a.kind)
    dest = a.out or str(Path(a.work) / ('stats_%s.json' % a.kind))
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------- sql
def cmd_sql(a):
    q = a.query.strip().rstrip(';')
    first = q.split(None, 1)[0].lower() if q.split() else ''
    if first not in ('select', 'with', 'pragma', 'explain'):
        die('only read-only queries allowed (got %s)' % first)
    if not Path(a.db).is_file():
        die('database not found: %s' % a.db)
    con = sqlite3.connect('file:%s?mode=ro' % a.db, uri=True)
    try:
        cur = con.execute(q)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except sqlite3.Error as e:
        con.close()
        die('query failed: %s | query was: %s' % (e, q))
    con.close()
    rec = {'db': a.db, 'query': q, 'n_rows': len(rows), 'columns': cols}
    if a.out:
        with open(a.out, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        rec['csv'] = a.out
    if len(rows) == 0:
        rec['warning'] = ('EMPTY RESULT: do not silently accept; verify join keys, '
                          'WHERE terms and table names before reporting')
    dest = a.receipt or str(Path(a.work) / 'sql_receipt.json')
    receipts = []
    if Path(dest).exists():
        try:
            receipts = json.load(open(dest))
        except Exception:
            receipts = []
    receipts.append(rec)
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(receipts, f, ensure_ascii=False, indent=2)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    preview = [dict(zip(cols, r)) for r in rows[:10]]
    print(json.dumps({'preview_first_10': preview}, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------- figure
def cmd_figure(a):
    header, recs, _ = load_csv(Path(a.table))
    for c in [a.x] + ([a.y] if a.y else []):
        if c and c not in header:
            die('figure column not present: %s (have %s)' % (c, header))
    receipt = {'kind': a.kind, 'table': a.table, 'x': a.x, 'y': a.y,
               'title': a.title, 'xlabel': a.xlabel or a.x,
               'ylabel': a.ylabel or a.y or 'count', 'out': a.out}
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception as e:
        # honest degradation: save the plotted data, never fake a figure
        data_csv = str(Path(a.out).with_suffix('.data.csv'))
        with open(data_csv, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            if a.y:
                w.writerow([a.x, a.y])
                for r in recs:
                    w.writerow([r.get(a.x, ''), r.get(a.y, '')])
            else:
                w.writerow([a.x])
                for r in recs:
                    w.writerow([r.get(a.x, '')])
        receipt['status'] = 'SKIPPED_no_matplotlib'
        receipt['figure_data_csv'] = data_csv
        receipt['reason'] = str(e)
        with open(str(Path(a.out).with_suffix('.receipt.json')), 'w',
                  encoding='utf-8') as f:
            json.dump(receipt, f, ensure_ascii=False, indent=2)
        print('matplotlib unavailable (%s); figure data saved to %s; '
              'report the figure as not rendered' % (e, data_csv))
        return
    if a.kind == 'hist':
        vals, n_miss, _ = numeric_column(recs, a.x)
        if not vals:
            die('hist: no numeric values in %s' % a.x)
        plt.figure(figsize=(6, 4))
        plt.hist(vals, bins=a.bins, edgecolor='black')
        receipt['n_points'], receipt['n_missing_excluded'] = len(vals), n_miss
    elif a.kind == 'scatter':
        x, y, skipped = paired_columns(recs, a.x, a.y)
        if not x:
            die('scatter: no paired numeric values')
        plt.figure(figsize=(6, 4))
        plt.scatter(x, y, s=12, alpha=0.7)
        receipt['n_points'], receipt['pairs_skipped_missing'] = len(x), skipped
    elif a.kind in ('bar', 'line'):
        xs = [r.get(a.x, '') for r in recs]
        ys, bad = [], 0
        for r in recs:
            f = to_float(r.get(a.y))
            if f is None:
                bad += 1
                ys.append(0.0)
            else:
                ys.append(f)
        if bad:
            die('bar/line: %d non-numeric y values; clean or filter first' % bad)
        plt.figure(figsize=(max(6, len(xs) * 0.4), 4))
        (plt.bar if a.kind == 'bar' else plt.plot)(xs, ys)
        plt.xticks(rotation=90, fontsize=7)
        receipt['n_points'] = len(xs)
    else:
        die('unknown figure kind: %s' % a.kind)
    plt.title(a.title or '')
    plt.xlabel(receipt['xlabel'])
    plt.ylabel(receipt['ylabel'])
    plt.tight_layout()
    plt.savefig(a.out, dpi=150)
    plt.close('all')
    receipt['status'] = 'rendered'
    receipt['bytes'] = Path(a.out).stat().st_size
    with open(str(Path(a.out).with_suffix('.receipt.json')), 'w',
              encoding='utf-8') as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------- verify
def compute_claim(claim):
    """Recompute one claim from RAW inputs. Returns (value, detail)."""
    kind = claim['kind']
    if kind == 'sql_scalar':
        con = sqlite3.connect('file:%s?mode=ro' % claim['db'], uri=True)
        try:
            rows = con.execute(claim['query']).fetchall()
        finally:
            con.close()
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ValueError('sql_scalar claim returned %d rows' % len(rows))
        return rows[0][0], 're-executed query'
    src = claim['source']
    header, recs, _ = load_csv(Path(src))
    if kind == 'row_count':
        return len(recs), 're-counted raw rows'
    if kind == 'n_missing':
        return sum(1 for r in recs if is_missing(r.get(claim['column']))), \
            're-counted missing tokens'
    if kind == 'count_where':
        col, op, val = claim['column'], claim['op'], claim['value']
        n = 0
        for r in recs:
            fv, fval = to_float(r.get(col)), to_float(val)
            lhs, rhs = (fv, fval) if (fv is not None and fval is not None) \
                else (str(r.get(col)).strip(), str(val).strip())
            if {'==': lhs == rhs, '!=': lhs != rhs, '>': lhs > rhs,
                '>=': lhs >= rhs, '<': lhs < rhs, '<=': lhs <= rhs}[op]:
                n += 1
        return n, 're-filtered raw rows'
    vals, _, _ = numeric_column(recs, claim['column'])
    st = col_stats(vals)
    if kind == 'mean':
        return st.get('mean'), 'recomputed over %d non-missing values' % st['count']
    if kind == 'median':
        return st.get('median'), 'recomputed (linear interpolation)'
    if kind == 'std':
        return st.get('std'), 'recomputed sample std (n-1)'
    if kind in ('min', 'max', 'count'):
        return st.get(kind), 'recomputed'
    raise ValueError('unsupported claim kind: %s' % kind)


def cmd_verify(a):
    claims = json.loads(read_text_any(a.claims)[0])
    if isinstance(claims, dict):
        claims = claims.get('claims', [])
    results, n_fail = [], 0
    for c in claims:
        rec = {'id': c.get('id'), 'kind': c['kind'], 'reported': c.get('value')}
        try:
            actual, detail = compute_claim(c)
            rec['recomputed'] = actual
            rec['detail'] = detail
            tol = c.get('tolerance')
            if tol is None:
                tol = 0 if isinstance(actual, int) else 1e-6
            rep = c.get('value')
            if isinstance(actual, (int, float)) and isinstance(rep, (int, float)):
                ok = abs(actual - rep) <= max(tol, tol * abs(actual))
            else:
                ok = str(actual) == str(rep)
            rec['verdict'] = 'CONFIRMED' if ok else 'MISMATCH'
        except Exception as e:
            rec['verdict'] = 'UNVERIFIABLE'
            rec['detail'] = str(e)
        if rec['verdict'] != 'CONFIRMED':
            n_fail += 1
        results.append(rec)
    out = {'n_claims': len(results), 'n_confirmed': len(results) - n_fail,
           'n_failed': n_fail, 'all_confirmed': n_fail == 0,
           'claims': results}
    dest = a.out or str(Path(a.work) / 'verification.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if n_fail:
        sys.exit(1)


# ----------------------------------------------------------------- finalize
def cmd_finalize(a):
    out = Path(a.output)
    rj_path = out / 'result.json'
    if not rj_path.exists():
        die('result.json missing; run profile first')
    rj = json.loads(read_text_any(rj_path)[0])
    problems = []
    ver_path = Path(a.work) / 'verification.json'
    if ver_path.exists():
        ver = json.load(open(ver_path))
        rj['verification'] = ver
        if not ver.get('all_confirmed'):
            problems.append('verification: %d claim(s) not confirmed'
                            % ver.get('n_failed', -1))
    else:
        problems.append('no verification.json: run `verify` before finalize')
    case_file = find_case_json(a.input)
    if case_file:
        obj = json.loads(read_text_any(case_file)[0])
        case_id = find_key_recursive(obj, 'case_id') or find_key_recursive(obj, 'id')
        if rj.get('case_id') != case_id:
            problems.append('case_id mismatch: result=%r case=%r'
                            % (rj.get('case_id'), case_id))
        fields = contract_fields(find_key_recursive(obj, 'output_contract'))
        for fld in fields:
            if fld not in rj:
                problems.append('output_contract field missing in result.json: %s' % fld)
            elif rj[fld] is None:
                problems.append('output_contract field still null: %s '
                                '(fill a real value or document why)' % fld)
    figs = rj.get('figures') or []
    for fig in figs:
        p = fig.get('path') if isinstance(fig, dict) else fig
        if p and not Path(p).exists() and not (out / str(p)).exists():
            problems.append('declared figure not found on disk: %s' % p)
    if problems:
        if rj.get('status') == 'success':
            rj['status'] = 'partial'
            rj.setdefault('anomalies', []).append(
                {'kind': 'finalize_downgrade',
                 'detail': 'status downgraded success->partial by finalize gate',
                 'problems': problems})
        rj['finalize_gate'] = {'passed': False, 'problems': problems}
        with open(rj_path, 'w', encoding='utf-8') as f:
            json.dump(rj, f, ensure_ascii=False, indent=2)
        print('FINALIZE GATE FAILED:')
        for p in problems:
            print(' -', p)
        sys.exit(1)
    prev_gate = rj.get('finalize_gate') or {}
    if rj.get('status') in (None, 'in_progress') or prev_gate.get('passed') is False:
        # fresh completion, or an earlier gate failure now resolved
        rj['status'] = 'success'
    rj['finalize_gate'] = {'passed': True, 'problems': []}
    with open(rj_path, 'w', encoding='utf-8') as f:
        json.dump(rj, f, ensure_ascii=False, indent=2)
    print('FINALIZE GATE PASSED: result.json complete, status=%s' % rj['status'])


# ---------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(prog='casekit', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--work', default='/app/work')
    ap.add_argument('--output', default='/app/output')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('profile', help='inventory + profile all inputs, write skeleton')
    p.add_argument('--input', default='/input')
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser('clean', help='apply explicit rules.json to a CSV')
    p.add_argument('--table', required=True)
    p.add_argument('--rules', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--report', required=True)
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser('describe', help='descriptive stats per column')
    p.add_argument('--table', required=True)
    p.add_argument('--columns', default='')
    p.add_argument('--out', default='')
    p.set_defaults(fn=cmd_describe)

    p = sub.add_parser('aggregate', help='group-by aggregation')
    p.add_argument('--table', required=True)
    p.add_argument('--group-by', default='')
    p.add_argument('--metrics', required=True,
                   help='e.g. "strength:mean,strength:std,strength:count"')
    p.add_argument('--out', default='')
    p.set_defaults(fn=cmd_aggregate)

    p = sub.add_parser('stats', help='ttest | corr | ols (pure python)')
    p.add_argument('kind', choices=['ttest', 'corr', 'ols'])
    p.add_argument('--table', required=True)
    p.add_argument('--column', default='')
    p.add_argument('--group', default='')
    p.add_argument('--x', default='')
    p.add_argument('--y', default='')
    p.add_argument('--out', default='')
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser('sql', help='read-only SELECT against SQLite')
    p.add_argument('--db', required=True)
    p.add_argument('--query', required=True)
    p.add_argument('--out', default='')
    p.add_argument('--receipt', default='')
    p.set_defaults(fn=cmd_sql)

    p = sub.add_parser('figure', help='hist|scatter|bar|line PNG')
    p.add_argument('--table', required=True)
    p.add_argument('kind', choices=['hist', 'scatter', 'bar', 'line'])
    p.add_argument('--x', required=True)
    p.add_argument('--y', default='')
    p.add_argument('--title', default='')
    p.add_argument('--xlabel', default='')
    p.add_argument('--ylabel', default='')
    p.add_argument('--bins', type=int, default=20)
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_figure)

    p = sub.add_parser('verify', help='recompute claims.json from raw inputs')
    p.add_argument('--claims', required=True)
    p.add_argument('--out', default='')
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser('finalize', help='contract gate on result.json')
    p.add_argument('--input', default='/input')
    p.set_defaults(fn=cmd_finalize)

    args = ap.parse_args()
    t0 = time.time()
    code = 0
    try:
        args.fn(args)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        raise
    except Exception as e:
        code = 1
        sys.stderr.write('casekit ERROR: %s\n' % e)
        sys.exit(1)
    finally:
        log_run(args.work, sys.argv, code, time.time() - t0)


if __name__ == '__main__':
    main()
