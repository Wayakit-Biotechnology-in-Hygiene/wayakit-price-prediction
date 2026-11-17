#!/usr/bin/env bash
# Verificación de Price Intelligence (PI). Sin red: no llama a AWS, Odoo, Apify ni Bedrock.
#
# Uso (siempre DESPUÉS de "git add": el registro vale para lo que está en el índice):
#   scripts/verify.sh            # igual que "rapido"
#   scripts/verify.sh rapido     # pruebas unitarias y de propiedades (Hypothesis)
#   scripts/verify.sh completo   # rapido + E2E + diferenciales (base contra versión nueva)
#
# Variables:
#   PI_PYTHON    intérprete a usar. Si no se define, se busca el env conda "wayakit-price-prediction";
#                si no aparece, sale con 2 (nunca cae en otro python sin avisar).
#   PI_BASE_REF  versión base para las pruebas diferenciales. Por defecto origin/server-improvement
#                (lo que corre en producción).
#
# Códigos de salida: 0 OK; 1 alguna prueba falló, no se recolectó ninguna o, en "completo", se saltó
# alguna E2E o diferencial; 2 error de uso o de entorno (sin intérprete, sin versión base).
#
# "completo" exige que corran las E2E y las diferenciales: si falta data/f1_antes_2026-11-15/ o pytest
# reporta "skipped" en ese bloque, el resultado es FALLA. En ese caso el sello honesto es
#   Verificado: rapido (e2e y diferenciales no corrieron: <motivo>)
#
# Registro para el hook commit-msg (scripts/githooks/commit-msg): al terminar escribe
# .claude/state/ultima_verificacion.json con {modo, resultado, tree, fecha, head, indice_igual_arbol, resumen}
# y agrega la misma línea a .claude/state/verificaciones.jsonl (historial).
#   tree                = "git write-tree" del índice cuando corrió verify (lo que se va a commitear).
#   indice_igual_arbol  = false si había cambios sin "git add" o archivos sin seguimiento: entonces lo
#                         probado (el árbol de trabajo) no es lo que se commitea y el hook no acepta el sello.
# El hook solo acepta "Verificado: rapido|completo (...)" si hay un registro OK con el mismo tree.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$(pwd)"

MODO="${1:-rapido}"
case "$MODO" in
  rapido|completo) ;;
  *) echo "Uso: scripts/verify.sh [rapido|completo]" >&2; exit 2 ;;
esac

# Python: PI_PYTHON si se define; si no, el env conda "wayakit-price-prediction" en rutas conocidas
# (los hooks corren sin shell interactivo, así que conda puede no estar en el PATH). Nunca otro.
ENV_NOMBRE="wayakit-price-prediction"
PY="${PI_PYTHON:-}"
if [[ -n "$PY" ]]; then
  if ! command -v "$PY" >/dev/null 2>&1; then
    echo "ERROR: PI_PYTHON='$PY' no existe o no es ejecutable." >&2
    exit 2
  fi
else
  for base in "${CONDA_PREFIX:+${CONDA_PREFIX%/envs/*}}" "${CONDA_EXE:+$(dirname "$(dirname "$CONDA_EXE")")}" \
              "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" \
              "/opt/miniconda3" "/opt/anaconda3" "/opt/homebrew/Caskroom/miniconda/base"; do
    if [[ -n "$base" && -x "$base/envs/$ENV_NOMBRE/bin/python" ]]; then
      PY="$base/envs/$ENV_NOMBRE/bin/python"; break
    fi
  done
  if [[ -z "$PY" ]]; then
    {
      echo "ERROR: no encontré el env conda '$ENV_NOMBRE' (buscado en miniconda3, anaconda3, miniforge3, mambaforge y /opt)."
      echo "Créalo con 'conda env create -f environment.yml' o define PI_PYTHON=/ruta/al/python."
      echo "No se usa otro python: la comparación diferencial puede cambiar con otras versiones."
    } >&2
    exit 2
  fi
fi

export PI_BASE_REF="${PI_BASE_REF:-origin/server-improvement}"
export PYTHONDONTWRITEBYTECODE=1

# Candado: las pruebas no deben alcanzar AWS aunque alguien llame a boto3 por error.
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true

TMP_OUT="$(mktemp -d)"
trap 'rm -rf "$TMP_OUT"' EXIT

STATE="$REPO_ROOT/.claude/state"
mkdir -p "$STATE"

# Árbol del índice al empezar (lo que se commitearía). Si cambia durante la corrida, no se registra OK.
TREE_INICIO="$(git write-tree 2>/dev/null || echo '')"
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo '')"
if git diff --quiet 2>/dev/null && [[ -z "$(git ls-files --others --exclude-standard 2>/dev/null | head -n 1)" ]]; then
  INDICE_IGUAL_ARBOL=true
else
  INDICE_IGUAL_ARBOL=false
fi

RESUMEN=()
FALLO=0
MOTIVO_SALTO=""

correr_bloque() {
  # $1 = nombre del bloque, $2 = expresión de marcadores de pytest, $3 = "sin_saltos" para exigir 0 skipped
  local nombre="$1" marcadores="$2" exigir="${3:-}" salida="$TMP_OUT/$1.txt" rc=0
  echo "== $nombre: pytest -m \"$marcadores\" =="
  # -rfEs: el resumen corto lista FAILED, ERROR y SKIPPED con su nombre (lo lee el gate).
  "$PY" -m pytest -m "$marcadores" -q -rfEs 2>&1 | tee "$salida" || rc=$?
  cp "$salida" "$STATE/ultima_verificacion_${nombre}.txt" 2>/dev/null || true
  local linea
  linea="$(grep -E '(passed|failed|error|skipped|no tests ran)' "$salida" | tail -n 1 | sed -E 's/=+//g; s/^ +| +$//g' || true)"
  if [[ "$rc" -eq 5 ]]; then
    RESUMEN+=("$nombre: FALLA (no se recolectó ninguna prueba)")
    FALLO=1
  elif [[ "$rc" -ne 0 ]]; then
    RESUMEN+=("$nombre: FALLA (${linea:-código $rc})")
    FALLO=1
  elif [[ "$exigir" == "sin_saltos" ]] && grep -Eq '[0-9]+ skipped' <<<"$linea"; then
    RESUMEN+=("$nombre: FALLA (se saltaron pruebas: $linea)")
    MOTIVO_SALTO="${MOTIVO_SALTO:-pytest reportó pruebas saltadas en $nombre}"
    FALLO=1
  else
    RESUMEN+=("$nombre: OK (${linea:-sin resumen})")
  fi
}

registrar() {
  # $1 = resultado (OK | FALLA), $2 = modo a registrar (por defecto $MODO), $3.. = líneas de resumen
  # (por defecto todo RESUMEN). Escribe el JSON de forma atómica; nunca guarda salidas de pruebas.
  local resultado="$1" modo_reg="${2:-$MODO}" tree_fin tmp
  shift; [[ $# -gt 0 ]] && shift
  local lineas
  if [[ $# -gt 0 ]]; then lineas="$(printf '%s\n' "$@")"; else lineas="$(printf '%s\n' "${RESUMEN[@]}")"; fi
  tree_fin="$(git write-tree 2>/dev/null || echo '')"
  if [[ "$resultado" == "OK" && "$tree_fin" != "$TREE_INICIO" ]]; then
    echo "AVISO: el índice cambió durante la verificación (otro 'git add'); se registra como FALLA." >&2
    resultado="FALLA"
    lineas="${lineas}"$'\n'"índice: cambió durante la corrida"
  fi
  tmp="$(mktemp "$STATE/.ultima_verificacion.XXXXXX")"
  PI_R_MODO="$modo_reg" PI_R_RES="$resultado" PI_R_TREE="$TREE_INICIO" PI_R_HEAD="$HEAD_SHA" \
  PI_R_LIMPIO="$INDICE_IGUAL_ARBOL" PI_R_RESUMEN="$lineas" \
    "$PY" -c '
import json, os, sys, time
e = os.environ
json.dump({
    "modo": e["PI_R_MODO"],
    "resultado": e["PI_R_RES"],
    "tree": e["PI_R_TREE"],
    "fecha": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "head": e["PI_R_HEAD"],
    "indice_igual_arbol": e["PI_R_LIMPIO"] == "true",
    "resumen": [l for l in e["PI_R_RESUMEN"].splitlines() if l],
}, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
' "$tmp"
  mv -f "$tmp" "$STATE/ultima_verificacion.json"
  # Historial (una línea por corrida): el gate corre "rapido" a menudo y no debe borrar el registro
  # de un "completo" del mismo índice.
  "$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=False))' \
    "$STATE/ultima_verificacion.json" >> "$STATE/verificaciones.jsonl"
  # Rotación: se conservan las últimas 500 corridas para que el historial no crezca sin límite.
  if [ "$(wc -l < "$STATE/verificaciones.jsonl")" -gt 500 ]; then
    tail -n 500 "$STATE/verificaciones.jsonl" > "$STATE/verificaciones.jsonl.tmp" \
      && mv -f "$STATE/verificaciones.jsonl.tmp" "$STATE/verificaciones.jsonl"
  fi
}

echo "PI verify — modo: $MODO"
echo "Python: $PY ($("$PY" --version 2>&1))"
echo "Repo: $REPO_ROOT ($(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') @ $(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
echo "Índice: tree ${TREE_INICIO:0:12}"
if [[ "$INDICE_IGUAL_ARBOL" != "true" ]]; then
  echo "AVISO: hay cambios sin 'git add' o archivos sin seguimiento. Las pruebas corren sobre el árbol de"
  echo "       trabajo, pero el registro es del índice: el hook commit-msg no aceptará este resultado como"
  echo "       sello rapido/completo. Haz 'git add' de todo lo que va en el commit y vuelve a correr."
fi

if [[ "$MODO" == "completo" ]]; then
  if ! BASE_SHA="$(git rev-parse --verify --quiet "${PI_BASE_REF}^{commit}")"; then
    echo "ERROR: no existe la versión base '$PI_BASE_REF'. Correr 'git fetch origin' o definir PI_BASE_REF." >&2
    exit 2
  fi
  echo "Base: $PI_BASE_REF @ ${BASE_SHA:0:7}"
  if [[ ! -d data/f1_antes_2026-11-15 ]]; then
    MOTIVO_SALTO="falta data/f1_antes_2026-11-15/"
    echo "AVISO: $MOTIVO_SALTO; las E2E y diferenciales no pueden correr y el modo completo saldrá en FALLA."
  fi
fi
echo

correr_bloque rapido "unit or property"
if [[ "$MODO" == "completo" ]]; then
  echo
  # "and not unit": las autopruebas del arnés diferencial (unit + differential) ya corrieron en rapido.
  correr_bloque e2e-diferencial "(e2e or differential) and not unit" sin_saltos
  if [[ -n "$MOTIVO_SALTO" && "$FALLO" -eq 0 ]]; then
    RESUMEN+=("e2e-diferencial: FALLA ($MOTIVO_SALTO)")
    FALLO=1
  fi
fi

echo
echo "===== Resumen ($MODO) ====="
for l in "${RESUMEN[@]}"; do echo "  $l"; done
if [[ "$FALLO" -ne 0 ]]; then
  # En "completo", si el bloque rapido pasó, ese resultado también es cierto: se registra aparte
  # como "rapido" (antes que el completo, que queda como última verificación).
  RAPIDO_OK=false
  if [[ "$MODO" == "completo" && "${RESUMEN[0]}" == "rapido: OK"* ]]; then
    registrar OK rapido "${RESUMEN[0]}"
    RAPIDO_OK=true
  fi
  registrar FALLA
  echo "Resultado: FALLA"
  if [[ -n "$MOTIVO_SALTO" ]]; then
    echo "Las E2E o diferenciales no corrieron completas ($MOTIVO_SALTO): esto NO es una verificación completa."
    if [[ "$RAPIDO_OK" == "true" && "$INDICE_IGUAL_ARBOL" == "true" ]]; then
      echo "El bloque rapido pasó y quedó registrado como \"rapido\". El sello honesto es:"
      echo "  Verificado: rapido (e2e y diferenciales no corrieron: $MOTIVO_SALTO)"
    fi
  fi
  exit 1
fi
registrar OK
echo "Resultado: OK"
UNIDO="$(printf '%s; ' "${RESUMEN[@]}")"
if [[ "$INDICE_IGUAL_ARBOL" == "true" ]]; then
  echo "Trailer sugerido: Verificado: $MODO (verify.sh $MODO — ${UNIDO%; })"
else
  echo "Sin trailer sugerido: el índice no coincide con el árbol de trabajo (ver AVISO arriba)."
fi
