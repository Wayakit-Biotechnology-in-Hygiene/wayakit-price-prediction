"""Arnés de pruebas diferenciales del flujo de ML de PI.

Corre las mismas etapas (1a → 1b full → 2 → 3) con los mismos datos congelados de F1 en dos
versiones del código y compara las salidas:

- base: la ref `PI_BASE_REF` (por omisión `origin/server-improvement`, lo que corre en
  producción), extraída con `git archive`;
- nueva: el árbol de trabajo actual (HEAD + cambios sin commit).

Qué compara:

- `ml_model/wayakit_prediction_report.csv`, por `Product_ID` (con número de aparición, porque
  hay IDs repetidos en el catálogo), sin importar el orden de columnas, con tolerancia numérica
  de 1e-6;
- `competitor_*_processed.csv` y `wayakit_products_to_predict_odoo.csv`: conteo de filas y
  columnas.

Cada diferencia se busca en `tests/differential/cambios_aprobados.json`; las que no estén ahí
hacen fallar la prueba. Reglas de una aprobación (ver `validate_approvals`):

- Obligatorios: `archivo` (uno de los que se comparan), `tipo`, `motivo`, `fecha` (AAAA-MM-DD),
  `aprobado_por` (exactamente "Mafer") y `celdas_esperadas` (entero ≥ 1).
- `columna` es obligatoria para `valor_cambiado`, `columna_agregada` y `columna_eliminada`, y no
  se permite en `fila_*` ni `conteo_filas` (esas diferencias no tienen columna).
- Filtros opcionales: `clave` (`<Product_ID>#<n>`) y `subindustria` (valor exacto de la columna
  `subindustry` del reporte), solo para `valor_cambiado` y `fila_*` del reporte.
- No se aceptan otros campos (un nombre mal escrito ampliaría la aprobación sin avisar).
- `split_approved` falla (`CoberturaIncorrecta`) si una entrada cubre más o menos diferencias
  que `celdas_esperadas`: una aprobación nunca es un comodín, y una que ya no cubre nada (la
  base ya trae el cambio) se tiene que quitar.

Uso desde la terminal (raíz del repo, entorno conda `wayakit-price-prediction`):

    python tests/differential/_harness.py                 # base = PI_BASE_REF u origin/server-improvement
    python tests/differential/_harness.py --base main     # otra ref
    python tests/differential/_harness.py --keep          # conserva el directorio temporal

Sale con 0 si no hay diferencias sin aprobar, con 1 si las hay (o si una aprobación no cubre
exactamente sus `celdas_esperadas`) y con 2 si no se pudo correr (falta la ref base o los datos,
o el JSON de aprobaciones es inválido).
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
APPROVALS_FILE = Path(__file__).resolve().parent / "cambios_aprobados.json"
TOLERANCE = 1e-6
KEY_COLUMN = "Product_ID"
TOP_ROWS = 20

REPORT_FILE = h.REPORT
COUNT_FILES = [h.VOL_PROCESSED, h.UNIT_PROCESSED, h.TO_PREDICT]

SUBINDUSTRY_COLUMN = "subindustry"

TIPOS = {"valor_cambiado", "fila_agregada", "fila_eliminada", "columna_agregada",
         "columna_eliminada", "conteo_filas"}
# Qué tipos puede producir cada archivo (ver compare_frames y compare_shape).
TIPOS_POR_ARCHIVO = {
    REPORT_FILE: {"valor_cambiado", "fila_agregada", "fila_eliminada", "columna_agregada",
                  "columna_eliminada"},
    **{rel: {"conteo_filas", "columna_agregada", "columna_eliminada"} for rel in COUNT_FILES},
}
TIPOS_CON_COLUMNA = {"valor_cambiado", "columna_agregada", "columna_eliminada"}
TIPOS_CON_FILA = {"valor_cambiado", "fila_agregada", "fila_eliminada"}  # admiten clave y subindustria
APROBADOR = "Mafer"
_REQUIRED_APPROVAL_FIELDS = ("archivo", "tipo", "motivo", "fecha", "aprobado_por", "celdas_esperadas")
_OPTIONAL_APPROVAL_FIELDS = ("columna", "clave", "subindustria")


def base_ref() -> str:
    return os.environ.get("PI_BASE_REF") or DEFAULT_BASE_REF


@dataclass(frozen=True)
class Diferencia:
    archivo: str
    tipo: str
    clave: str | None = None
    columna: str | None = None
    base: object = None
    nuevo: object = None
    # Subindustria de la fila (reporte); solo sirve para filtrar aprobaciones, no para comparar.
    subindustria: str | None = field(default=None, compare=False)

    def describe(self) -> str:
        donde = " ".join(x for x in (self.clave and f"clave={self.clave}",
                                     self.columna and f"columna={self.columna}",
                                     self.subindustria and f"subindustria={self.subindustria}") if x)
        return f"{self.archivo} [{self.tipo}] {donde} base={self.base!r} nuevo={self.nuevo!r}".rstrip()


# ---------------------------------------------------------------------------------------------
# Comparación
# ---------------------------------------------------------------------------------------------
def _keyed(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Índice = `<key>#<n>`: n es la aparición del ID (hay Product_ID repetidos en el catálogo)."""
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
    """Valor nativo de Python (numpy → int/float/bool) para comparar e imprimir."""
    return v.item() if hasattr(v, "item") else v


def compare_frames(base: pd.DataFrame, new: pd.DataFrame, archivo: str,
                   key: str = KEY_COLUMN, tol: float = TOLERANCE) -> list[Diferencia]:
    """Compara dos tablas por `key`, sin importar el orden de filas ni de columnas."""
    diffs: list[Diferencia] = []
    base_cols, new_cols = set(base.columns), set(new.columns)
    for c in sorted(new_cols - base_cols):
        diffs.append(Diferencia(archivo, "columna_agregada", columna=c))
    for c in sorted(base_cols - new_cols):
        diffs.append(Diferencia(archivo, "columna_eliminada", columna=c))
    if key not in base_cols or key not in new_cols:
        diffs.append(Diferencia(archivo, "columna_eliminada", columna=key,
                                base=key in base_cols, nuevo=key in new_cols))
        return diffs

    b, n = _keyed(base, key), _keyed(new, key)

    def _sub(df: pd.DataFrame, k: str) -> str | None:
        if SUBINDUSTRY_COLUMN not in df.columns:
            return None
        v = _py(df.at[k, SUBINDUSTRY_COLUMN])
        return None if _is_missing(v) else str(v)

    for k in [k for k in n.index if k not in b.index]:
        diffs.append(Diferencia(archivo, "fila_agregada", clave=k, subindustria=_sub(n, k)))
    for k in [k for k in b.index if k not in n.index]:
        diffs.append(Diferencia(archivo, "fila_eliminada", clave=k, subindustria=_sub(b, k)))

    common_cols = sorted((base_cols & new_cols) - {key})
    common_rows = [k for k in b.index if k in n.index]
    b_common = b.loc[common_rows, common_cols]
    n_common = n.loc[common_rows, common_cols]
    # Fila por fila (en el orden de la base): el resumen muestra juntos los cambios de un producto.
    b_vals = {c: b_common[c].tolist() for c in common_cols}
    n_vals = {c: n_common[c].tolist() for c in common_cols}
    for i, k in enumerate(common_rows):
        sub = _sub(b, k) if SUBINDUSTRY_COLUMN in base_cols else _sub(n, k)
        for col in common_cols:
            vb, vn = _py(b_vals[col][i]), _py(n_vals[col][i])
            if not _same(vb, vn, tol):
                diffs.append(Diferencia(archivo, "valor_cambiado", clave=k, columna=col, base=vb, nuevo=vn,
                                        subindustria=sub))
    return diffs


def compare_shape(base: pd.DataFrame, new: pd.DataFrame, archivo: str) -> list[Diferencia]:
    """Solo conteo de filas y conjunto de columnas."""
    diffs = []
    if len(base) != len(new):
        diffs.append(Diferencia(archivo, "conteo_filas", base=len(base), nuevo=len(new)))
    for c in sorted(set(new.columns) - set(base.columns)):
        diffs.append(Diferencia(archivo, "columna_agregada", columna=c))
    for c in sorted(set(base.columns) - set(new.columns)):
        diffs.append(Diferencia(archivo, "columna_eliminada", columna=c))
    return diffs


def compare_output_dirs(base_repo: Path, new_repo: Path, tol: float = TOLERANCE) -> list[Diferencia]:
    """Compara las salidas del flujo de ML de dos copias del repo ya corridas."""
    diffs = compare_frames(pd.read_csv(base_repo / REPORT_FILE), pd.read_csv(new_repo / REPORT_FILE),
                           REPORT_FILE, tol=tol)
    for rel in COUNT_FILES:
        diffs += compare_shape(pd.read_csv(base_repo / rel), pd.read_csv(new_repo / rel), rel)
    return diffs


# ---------------------------------------------------------------------------------------------
# Cambios aprobados
# ---------------------------------------------------------------------------------------------
class AprobacionesInvalidas(ValueError):
    pass


def load_approvals(path: Path = APPROVALS_FILE) -> list[dict]:
    """Lee y valida `cambios_aprobados.json`. Un archivo inexistente equivale a lista vacía."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("cambios") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise AprobacionesInvalidas(f"{path.name}: se espera un objeto con la lista 'cambios'.")
    validate_approvals(entries)
    return entries


def _blank(v) -> bool:
    return v is None or not str(v).strip()


def validate_approvals(entries: list[dict]) -> None:
    """Valida las reglas de una aprobación (ver docstring del módulo); junta todos los problemas."""
    problems = []
    allowed = set(_REQUIRED_APPROVAL_FIELDS) | set(_OPTIONAL_APPROVAL_FIELDS)
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"cambios[{i}] no es un objeto")
            continue
        p = f"cambios[{i}]"
        extra = sorted(set(e) - allowed)
        if extra:
            problems.append(f"{p} trae campos no permitidos: {', '.join(extra)} "
                            f"(permitidos: {', '.join(sorted(allowed))})")
        for f in _REQUIRED_APPROVAL_FIELDS:
            if _blank(e.get(f)):
                problems.append(f"{p} sin '{f}'")
        archivo, tipo = e.get("archivo"), e.get("tipo")
        if not _blank(archivo) and archivo not in TIPOS_POR_ARCHIVO:
            problems.append(f"{p} archivo '{archivo}' no se compara "
                            f"(válidos: {', '.join(sorted(TIPOS_POR_ARCHIVO))})")
        if not _blank(tipo):
            if tipo not in TIPOS:
                problems.append(f"{p} tipo '{tipo}' no válido (válidos: {', '.join(sorted(TIPOS))})")
            elif archivo in TIPOS_POR_ARCHIVO and tipo not in TIPOS_POR_ARCHIVO[archivo]:
                problems.append(f"{p} tipo '{tipo}' no aplica a {archivo} "
                                f"(aplican: {', '.join(sorted(TIPOS_POR_ARCHIVO[archivo]))})")
            if tipo in TIPOS_CON_COLUMNA and _blank(e.get("columna")):
                problems.append(f"{p} tipo '{tipo}' exige 'columna'")
            if tipo in TIPOS and tipo not in TIPOS_CON_COLUMNA and "columna" in e:
                problems.append(f"{p} tipo '{tipo}' no lleva 'columna' (nunca coincidiría)")
            for f in ("clave", "subindustria"):
                if f in e and (tipo not in TIPOS_CON_FILA or _blank(e[f])):
                    problems.append(f"{p} '{f}' solo se permite, no vacío, con tipo "
                                    f"{', '.join(sorted(TIPOS_CON_FILA))}")
        if "aprobado_por" in e and not _blank(e.get("aprobado_por")) and e["aprobado_por"] != APROBADOR:
            problems.append(f"{p} aprobado_por '{e['aprobado_por']}': solo aprueba \"{APROBADOR}\"")
        n = e.get("celdas_esperadas")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            problems.append(f"{p} celdas_esperadas debe ser un entero ≥ 1 (es {n!r})")
        fecha = str(e.get("fecha") or "")
        if fecha and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
            problems.append(f"{p} fecha '{fecha}' no es AAAA-MM-DD")
        elif fecha:
            try:
                date.fromisoformat(fecha)
            except ValueError:
                problems.append(f"{p} fecha '{fecha}' no existe")
    if problems:
        raise AprobacionesInvalidas("cambios_aprobados.json inválido:\n  " + "\n  ".join(problems))


class CoberturaIncorrecta(AprobacionesInvalidas):
    """Una aprobación cubre más o menos diferencias que su `celdas_esperadas`."""


def _matches(entry: dict, d: Diferencia) -> bool:
    if entry["archivo"] != d.archivo or entry["tipo"] != d.tipo:
        return False
    return all(f not in entry or entry[f] == getattr(d, f) for f in _OPTIONAL_APPROVAL_FIELDS)


def split_approved(diffs: list[Diferencia], approvals: list[dict]):
    """Devuelve (sin aprobar, aprobadas).

    Lanza `AprobacionesInvalidas` si una entrada no cumple las reglas y `CoberturaIncorrecta` si
    alguna cubre un número de diferencias distinto de su `celdas_esperadas` (incluido 0).
    """
    validate_approvals(approvals)
    covered: list[list[Diferencia]] = [[] for _ in approvals]
    pending, approved = [], []
    for d in diffs:
        hit = False
        for i, e in enumerate(approvals):
            if _matches(e, d):
                covered[i].append(d)
                hit = True
        (approved if hit else pending).append(d)
    wrong = [(e, c) for e, c in zip(approvals, covered) if len(c) != e["celdas_esperadas"]]
    if wrong:
        lines = ["Aprobaciones que no cubren exactamente sus celdas_esperadas:"]
        for e, c in wrong:
            lines.append(f"  - esperadas {e['celdas_esperadas']}, cubre {len(c)}: {json.dumps(e, ensure_ascii=False)}")
            lines += [f"      · {d.describe()}" for d in c[:5]]
            if len(c) > 5:
                lines.append(f"      · ... y {len(c) - 5} más")
        lines.append(f"  Diferencias sin aprobar: {len(pending)}. Acotar la entrada (columna, clave, "
                     "subindustria), corregir celdas_esperadas con el OK de Mafer o quitarla si ya no aplica.")
        raise CoberturaIncorrecta("\n".join(lines))
    return pending, approved


# ---------------------------------------------------------------------------------------------
# Resumen legible
# ---------------------------------------------------------------------------------------------
def summarize(pending: list[Diferencia], approved: list[Diferencia],
              base_label: str = "base", new_label: str = "nueva") -> str:
    lines = [f"Comparación diferencial: {base_label} vs {new_label}"]
    lines.append(f"  Diferencias sin aprobar: {len(pending)} | aprobadas: {len(approved)}")
    if pending:
        by_tipo: dict[str, int] = {}
        for d in pending:
            by_tipo[f"{d.archivo} [{d.tipo}]"] = by_tipo.get(f"{d.archivo} [{d.tipo}]", 0) + 1
        lines.append("  Por archivo y tipo:")
        lines += [f"    {k}: {v}" for k, v in sorted(by_tipo.items())]
        changed = [d for d in pending if d.tipo == "valor_cambiado"]
        if changed:
            per_col: dict[str, int] = {}
            for d in changed:
                per_col[d.columna] = per_col.get(d.columna, 0) + 1
            rows = {d.clave for d in changed}
            lines.append(f"  Valores cambiados: {len(changed)} celdas en {len(rows)} filas. Columnas:")
            lines += [f"    {c}: {k}" for c, k in sorted(per_col.items(), key=lambda x: (-x[1], x[0]))]
        lines.append(f"  Primeras {min(TOP_ROWS, len(pending))} diferencias:")
        for d in pending[:TOP_ROWS]:
            delta = ""
            if isinstance(d.base, (int, float)) and isinstance(d.nuevo, (int, float)) \
                    and not isinstance(d.base, bool) and not isinstance(d.nuevo, bool):
                delta = f" (Δ={d.nuevo - d.base:+.6g})"
            lines.append(f"    - {d.describe()}{delta}")
        lines.append("  Si el cambio es esperado, agrégalo a tests/differential/cambios_aprobados.json "
                     "con tipo, columna, celdas_esperadas, motivo, fecha y aprobado_por \"Mafer\" "
                     "(solo con su OK).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Corrida completa
# ---------------------------------------------------------------------------------------------
@dataclass
class ResultadoDiferencial:
    ref: str
    sha: str
    base_run: h.PipelineRun
    new_run: h.PipelineRun
    diffs: list[Diferencia]
    pending: list[Diferencia]
    approved: list[Diferencia]

    def summary(self) -> str:
        return summarize(self.pending, self.approved,
                         base_label=f"{self.ref} ({self.sha[:10]}, {self.base_run.seconds:.0f} s)",
                         new_label=f"árbol de trabajo ({self.new_run.seconds:.0f} s)")


def run_differential(workdir: Path, ref: str | None = None,
                     approvals_path: Path = APPROVALS_FILE) -> ResultadoDiferencial:
    """Arma `<workdir>/base/repo` y `<workdir>/new/repo`, corre ambos y compara."""
    ref = ref or base_ref()
    sha = h.resolve_ref(ref)
    if sha is None:
        raise RuntimeError(f"La ref base '{ref}' no existe localmente; corre `git fetch origin` "
                           "o define PI_BASE_REF.")
    approvals = load_approvals(approvals_path)  # validar antes de gastar tiempo en correr
    base_run = h.prepare_and_run(workdir / "base", ref=ref, label=f"base {ref}")
    new_run = h.prepare_and_run(workdir / "new", label="nueva (árbol de trabajo)")
    diffs = compare_output_dirs(base_run.repo_dir, new_run.repo_dir)
    pending, approved = split_approved(diffs, approvals)
    return ResultadoDiferencial(ref, sha, base_run, new_run, diffs, pending, approved)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compara las salidas del flujo de ML: ref base vs árbol de trabajo.")
    ap.add_argument("--base", default=None, help=f"ref base (por omisión PI_BASE_REF o {DEFAULT_BASE_REF})")
    ap.add_argument("--keep", action="store_true", help="no borrar el directorio temporal")
    args = ap.parse_args(argv)

    reason = h.data_missing_reason()
    if reason:
        print(f"No se puede correr: {reason}", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="pi-diferencial-"))
    try:
        result = run_differential(tmp, ref=args.base)
    except CoberturaIncorrecta as e:
        print(str(e), file=sys.stderr)
        return 1
    except (RuntimeError, AprobacionesInvalidas, h.PipelineError) as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        if args.keep:
            print(f"Directorio conservado: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(result.summary())
    return 1 if result.pending else 0


if __name__ == "__main__":
    sys.exit(main())
