"""Differential test harness for the PI ML flow.

Runs the same stages (1a → 1b full → 2 → 3) with the same frozen F1 data on two
versions of the code and compares the outputs:

- base: the ref `PI_BASE_REF` (default `origin/server-improvement`, what runs in
  production), extracted with `git archive`;
- new: the current working tree (HEAD + uncommitted changes).

What it compares:

- `ml_model/wayakit_prediction_report.csv`, by `Product_ID` (with an occurrence number, because
  the catalog has repeated IDs), regardless of column order, with a numeric tolerance
  of 1e-6;
- `competitor_*_processed.csv` and `wayakit_products_to_predict_odoo.csv`: row count and
  columns.

Each difference is looked up in `tests/differential/approved_changes.json`; those not listed
there make the test fail. Rules for an approval (see `validate_approvals`):

- Required: `file` (one of the compared files), `type`, `reason`, `date` (YYYY-MM-DD),
  `approved_by` (exactly "Mafer") and `expected_cells` (integer ≥ 1).
- `column` is required for `value_changed`, `column_added` and `column_removed`, and is not
  allowed in `row_*` or `row_count` (those differences have no column).
- Optional filters: `key` (`<Product_ID>#<n>`) and `subindustry` (exact value of the report's
  `subindustry` column), only for `value_changed` and `row_*` of the report.
- No other fields are accepted (a misspelled name would silently widen the approval).
- `split_approved` fails (`WrongCoverage`) if an entry covers more or fewer differences
  than `expected_cells`: an approval is never a wildcard, and one that no longer covers anything
  (the base already has the change) must be removed.

Usage from the terminal (repo root, conda environment `wayakit-price-prediction`):

    python tests/differential/_harness.py                 # base = PI_BASE_REF or origin/server-improvement
    python tests/differential/_harness.py --base main     # another ref
    python tests/differential/_harness.py --keep          # keeps the temporary directory

Exits with 0 if there are no unapproved differences, with 1 if there are (or if an approval does
not cover exactly its `expected_cells`) and with 2 if it could not run (the base ref or the data
is missing, or the approvals JSON is invalid).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.e2e import _helpers as h  # noqa: E402

DEFAULT_BASE_REF = "origin/server-improvement"
APPROVALS_FILE = Path(__file__).resolve().parent / "approved_changes.json"
TOLERANCE = 1e-6
KEY_COLUMN = "Product_ID"
TOP_ROWS = 20

REPORT_FILE = h.REPORT
COUNT_FILES = [h.VOL_PROCESSED, h.UNIT_PROCESSED, h.TO_PREDICT]

SUBINDUSTRY_COLUMN = "subindustry"

TYPES = {"value_changed", "row_added", "row_removed", "column_added",
         "column_removed", "row_count"}
# Which types each file can produce (see compare_frames and compare_shape).
TYPES_BY_FILE = {
    REPORT_FILE: {"value_changed", "row_added", "row_removed", "column_added",
                  "column_removed"},
    **{rel: {"row_count", "column_added", "column_removed"} for rel in COUNT_FILES},
}
TYPES_WITH_COLUMN = {"value_changed", "column_added", "column_removed"}
TYPES_WITH_ROW = {"value_changed", "row_added", "row_removed"}  # accept key and subindustry
APPROVER = "Mafer"
_REQUIRED_APPROVAL_FIELDS = ("file", "type", "reason", "date", "approved_by", "expected_cells")
_OPTIONAL_APPROVAL_FIELDS = ("column", "key", "subindustry")


def base_ref() -> str:
    return os.environ.get("PI_BASE_REF") or DEFAULT_BASE_REF


@dataclass(frozen=True)
class Difference:
    file: str
    type: str
    key: str | None = None
    column: str | None = None
    base: object = None
    new: object = None
    # Subindustry of the row (report); only used to filter approvals, not to compare.
    subindustry: str | None = field(default=None, compare=False)

    def describe(self) -> str:
        where = " ".join(x for x in (self.key and f"key={self.key}",
                                     self.column and f"column={self.column}",
                                     self.subindustry and f"subindustry={self.subindustry}") if x)
        return f"{self.file} [{self.type}] {where} base={self.base!r} new={self.new!r}".rstrip()


# ---------------------------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------------------------
def _keyed(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Index = `<key>#<n>`: n is the occurrence of the ID (the catalog has repeated Product_IDs)."""
    ids = df[key].astype(str)
    out = df.copy()
    out.index = ids + "#" + ids.groupby(ids).cumcount().astype(str)
    return out


def _is_missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT


def _same(a, b, tol: float) -> bool:
    if _is_missing(a) and _is_missing(b):
        return True
    if _is_missing(a) or _is_missing(b):
        return False
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num) and not isinstance(a, bool) and not isinstance(b, bool):
        if math.isinf(a) or math.isinf(b):
            return a == b
        return abs(float(a) - float(b)) <= tol
    return a == b


def _py(v):
    """Native Python value (numpy → int/float/bool) for comparing and printing."""
    return v.item() if hasattr(v, "item") else v


def compare_frames(base: pd.DataFrame, new: pd.DataFrame, file: str,
                   key: str = KEY_COLUMN, tol: float = TOLERANCE) -> list[Difference]:
    """Compares two tables by `key`, regardless of row or column order."""
    diffs: list[Difference] = []
    base_cols, new_cols = set(base.columns), set(new.columns)
    for c in sorted(new_cols - base_cols):
        diffs.append(Difference(file, "column_added", column=c))
    for c in sorted(base_cols - new_cols):
        diffs.append(Difference(file, "column_removed", column=c))
    if key not in base_cols or key not in new_cols:
        diffs.append(Difference(file, "column_removed", column=key,
                                base=key in base_cols, new=key in new_cols))
        return diffs

    b, n = _keyed(base, key), _keyed(new, key)

    def _sub(df: pd.DataFrame, k: str) -> str | None:
        if SUBINDUSTRY_COLUMN not in df.columns:
            return None
        v = _py(df.at[k, SUBINDUSTRY_COLUMN])
        return None if _is_missing(v) else str(v)

    for k in [k for k in n.index if k not in b.index]:
        diffs.append(Difference(file, "row_added", key=k, subindustry=_sub(n, k)))
    for k in [k for k in b.index if k not in n.index]:
        diffs.append(Difference(file, "row_removed", key=k, subindustry=_sub(b, k)))

    common_cols = sorted((base_cols & new_cols) - {key})
    common_rows = [k for k in b.index if k in n.index]
    b_common = b.loc[common_rows, common_cols]
    n_common = n.loc[common_rows, common_cols]
    # Row by row (in base order): the summary shows a product's changes together.
    b_vals = {c: b_common[c].tolist() for c in common_cols}
    n_vals = {c: n_common[c].tolist() for c in common_cols}
    for i, k in enumerate(common_rows):
        sub = _sub(b, k) if SUBINDUSTRY_COLUMN in base_cols else _sub(n, k)
        for col in common_cols:
            vb, vn = _py(b_vals[col][i]), _py(n_vals[col][i])
            if not _same(vb, vn, tol):
                diffs.append(Difference(file, "value_changed", key=k, column=col, base=vb, new=vn,
                                        subindustry=sub))
    return diffs


def compare_shape(base: pd.DataFrame, new: pd.DataFrame, file: str) -> list[Difference]:
    """Only row count and set of columns."""
    diffs = []
    if len(base) != len(new):
        diffs.append(Difference(file, "row_count", base=len(base), new=len(new)))
    for c in sorted(set(new.columns) - set(base.columns)):
        diffs.append(Difference(file, "column_added", column=c))
    for c in sorted(set(base.columns) - set(new.columns)):
        diffs.append(Difference(file, "column_removed", column=c))
    return diffs


def compare_output_dirs(base_repo: Path, new_repo: Path, tol: float = TOLERANCE) -> list[Difference]:
    """Compares the ML flow outputs of two repo copies that have already run."""
    diffs = compare_frames(pd.read_csv(base_repo / REPORT_FILE), pd.read_csv(new_repo / REPORT_FILE),
                           REPORT_FILE, tol=tol)
    for rel in COUNT_FILES:
        diffs += compare_shape(pd.read_csv(base_repo / rel), pd.read_csv(new_repo / rel), rel)
    return diffs


# ---------------------------------------------------------------------------------------------
# Approved changes
# ---------------------------------------------------------------------------------------------
class InvalidApprovals(ValueError):
    pass


def load_approvals(path: Path = APPROVALS_FILE) -> list[dict]:
    """Reads and validates `approved_changes.json`. A missing file equals an empty list."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("changes") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise InvalidApprovals(f"{path.name}: expected an object with the 'changes' list.")
    validate_approvals(entries)
    return entries


def _blank(v) -> bool:
    return v is None or not str(v).strip()


def validate_approvals(entries: list[dict]) -> None:
    """Validates the rules for an approval (see the module docstring); collects every problem."""
    problems = []
    allowed = set(_REQUIRED_APPROVAL_FIELDS) | set(_OPTIONAL_APPROVAL_FIELDS)
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"changes[{i}] is not an object")
            continue
        p = f"changes[{i}]"
        extra = sorted(set(e) - allowed)
        if extra:
            problems.append(f"{p} has fields that are not allowed: {', '.join(extra)} "
                            f"(allowed: {', '.join(sorted(allowed))})")
        for f in _REQUIRED_APPROVAL_FIELDS:
            if _blank(e.get(f)):
                problems.append(f"{p} missing '{f}'")
        file, type_ = e.get("file"), e.get("type")
        if not _blank(file) and file not in TYPES_BY_FILE:
            problems.append(f"{p} file '{file}' is not compared "
                            f"(valid: {', '.join(sorted(TYPES_BY_FILE))})")
        if not _blank(type_):
            if type_ not in TYPES:
                problems.append(f"{p} type '{type_}' is not valid (valid: {', '.join(sorted(TYPES))})")
            elif file in TYPES_BY_FILE and type_ not in TYPES_BY_FILE[file]:
                problems.append(f"{p} type '{type_}' does not apply to {file} "
                                f"(applicable: {', '.join(sorted(TYPES_BY_FILE[file]))})")
            if type_ in TYPES_WITH_COLUMN and _blank(e.get("column")):
                problems.append(f"{p} type '{type_}' requires 'column'")
            if type_ in TYPES and type_ not in TYPES_WITH_COLUMN and "column" in e:
                problems.append(f"{p} type '{type_}' takes no 'column' (it would never match)")
            for f in ("key", "subindustry"):
                if f in e and (type_ not in TYPES_WITH_ROW or _blank(e[f])):
                    problems.append(f"{p} '{f}' is only allowed, non-empty, with type "
                                    f"{', '.join(sorted(TYPES_WITH_ROW))}")
        if "approved_by" in e and not _blank(e.get("approved_by")) and e["approved_by"] != APPROVER:
            problems.append(f"{p} approved_by '{e['approved_by']}': only \"{APPROVER}\" can approve")
        n = e.get("expected_cells")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            problems.append(f"{p} expected_cells must be an integer ≥ 1 (it is {n!r})")
        date_ = str(e.get("date") or "")
        if date_ and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_):
            problems.append(f"{p} date '{date_}' is not YYYY-MM-DD")
        elif date_:
            try:
                date.fromisoformat(date_)
            except ValueError:
                problems.append(f"{p} date '{date_}' does not exist")
    if problems:
        raise InvalidApprovals("approved_changes.json is invalid:\n  " + "\n  ".join(problems))


class WrongCoverage(InvalidApprovals):
    """An approval covers more or fewer differences than its `expected_cells`."""


def _matches(entry: dict, d: Difference) -> bool:
    if entry["file"] != d.file or entry["type"] != d.type:
        return False
    return all(f not in entry or entry[f] == getattr(d, f) for f in _OPTIONAL_APPROVAL_FIELDS)


def split_approved(diffs: list[Difference], approvals: list[dict]):
    """Returns (unapproved, approved).

    Raises `InvalidApprovals` if an entry breaks the rules and `WrongCoverage` if
    any covers a number of differences other than its `expected_cells` (including 0).
    """
    validate_approvals(approvals)
    covered: list[list[Difference]] = [[] for _ in approvals]
    pending, approved = [], []
    for d in diffs:
        hit = False
        for i, e in enumerate(approvals):
            if _matches(e, d):
                covered[i].append(d)
                hit = True
        (approved if hit else pending).append(d)
    wrong = [(e, c) for e, c in zip(approvals, covered) if len(c) != e["expected_cells"]]
    if wrong:
        lines = ["Approvals that do not cover exactly their expected_cells:"]
        for e, c in wrong:
            lines.append(f"  - expected {e['expected_cells']}, covers {len(c)}: {json.dumps(e, ensure_ascii=False)}")
            lines += [f"      · {d.describe()}" for d in c[:5]]
            if len(c) > 5:
                lines.append(f"      · ... and {len(c) - 5} more")
        lines.append(f"  Unapproved differences: {len(pending)}. Narrow the entry (column, key, "
                     "subindustry), fix expected_cells with Mafer's OK or remove it if it no longer applies.")
        raise WrongCoverage("\n".join(lines))
    return pending, approved


# ---------------------------------------------------------------------------------------------
# Readable summary
# ---------------------------------------------------------------------------------------------
def summarize(pending: list[Difference], approved: list[Difference],
              base_label: str = "base", new_label: str = "new") -> str:
    lines = [f"Differential comparison: {base_label} vs {new_label}"]
    lines.append(f"  Unapproved differences: {len(pending)} | approved: {len(approved)}")
    if pending:
        by_type: dict[str, int] = {}
        for d in pending:
            by_type[f"{d.file} [{d.type}]"] = by_type.get(f"{d.file} [{d.type}]", 0) + 1
        lines.append("  By file and type:")
        lines += [f"    {k}: {v}" for k, v in sorted(by_type.items())]
        changed = [d for d in pending if d.type == "value_changed"]
        if changed:
            per_col: dict[str, int] = {}
            for d in changed:
                per_col[d.column] = per_col.get(d.column, 0) + 1
            rows = {d.key for d in changed}
            lines.append(f"  Changed values: {len(changed)} cells in {len(rows)} rows. Columns:")
            lines += [f"    {c}: {k}" for c, k in sorted(per_col.items(), key=lambda x: (-x[1], x[0]))]
        lines.append(f"  First {min(TOP_ROWS, len(pending))} differences:")
        for d in pending[:TOP_ROWS]:
            delta = ""
            if isinstance(d.base, (int, float)) and isinstance(d.new, (int, float)) \
                    and not isinstance(d.base, bool) and not isinstance(d.new, bool):
                delta = f" (Δ={d.new - d.base:+.6g})"
            lines.append(f"    - {d.describe()}{delta}")
        lines.append("  If the change is expected, add it to tests/differential/approved_changes.json "
                     "with type, column, expected_cells, reason, date and approved_by \"Mafer\" "
                     "(only with her OK).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------------------------
@dataclass
class DifferentialResult:
    ref: str
    sha: str
    base_run: h.PipelineRun
    new_run: h.PipelineRun
    diffs: list[Difference]
    pending: list[Difference]
    approved: list[Difference]

    def summary(self) -> str:
        return summarize(self.pending, self.approved,
                         base_label=f"{self.ref} ({self.sha[:10]}, {self.base_run.seconds:.0f} s)",
                         new_label=f"working tree ({self.new_run.seconds:.0f} s)")


def run_differential(workdir: Path, ref: str | None = None,
                     approvals_path: Path = APPROVALS_FILE) -> DifferentialResult:
    """Builds `<workdir>/base/repo` and `<workdir>/new/repo`, runs both and compares."""
    ref = ref or base_ref()
    sha = h.resolve_ref(ref)
    if sha is None:
        raise RuntimeError(f"The base ref '{ref}' does not exist locally; run `git fetch origin` "
                           "or set PI_BASE_REF.")
    approvals = load_approvals(approvals_path)  # validate before spending time on the runs
    base_run = h.prepare_and_run(workdir / "base", ref=ref, label=f"base {ref}")
    new_run = h.prepare_and_run(workdir / "new", label="new (working tree)")
    diffs = compare_output_dirs(base_run.repo_dir, new_run.repo_dir)
    pending, approved = split_approved(diffs, approvals)
    return DifferentialResult(ref, sha, base_run, new_run, diffs, pending, approved)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compares the ML flow outputs: base ref vs working tree.")
    ap.add_argument("--base", default=None, help=f"base ref (default PI_BASE_REF or {DEFAULT_BASE_REF})")
    ap.add_argument("--keep", action="store_true", help="do not delete the temporary directory")
    args = ap.parse_args(argv)

    reason = h.data_missing_reason()
    if reason:
        print(f"Cannot run: {reason}", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="pi-differential-"))
    try:
        result = run_differential(tmp, ref=args.base)
    except WrongCoverage as e:
        print(str(e), file=sys.stderr)
        return 1
    except (RuntimeError, InvalidApprovals, h.PipelineError) as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        if args.keep:
            print(f"Directory kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(result.summary())
    return 1 if result.pending else 0


if __name__ == "__main__":
    sys.exit(main())
