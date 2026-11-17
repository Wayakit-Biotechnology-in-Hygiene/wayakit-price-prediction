#!/usr/bin/env bash
# Hook TaskCompleted (Agent Teams): una tarea solo se cierra si pasa la verificación rápida.
#
# Qué hace:
#   1. Lee el JSON de stdin sin suponer su forma (el esquema de TaskCompleted no está
#      documentado). La primera vez guarda solo los NOMBRES de las llaves, nunca los
#      valores, en .claude/state/hook_inputs.log para conocer el esquema.
#   1b. Separación checks / código: compara el árbol de trabajo (incluidos archivos sin
#      seguimiento) contra merge-base(PI_CHECKS_BASE, HEAD). Si en ese mismo diff cambian
#      archivos de verificación (scripts/hooks/rutas_protegidas.txt) Y código de producción
#      (scraper/, ml_model/, run_pipeline.sh), falla con exit 2: un cambio no puede modificar
#      los checks que lo juzgan. Van en cambios separados. Si la versión base no existe, falla.
#   2. Corre `scripts/verify.sh rapido`. Si el script no existe, falla (no deja pasar).
#   3. Si falla, suma 1 al contador .claude/state/fallas_<tarea>.count (la tarea sale del id
#      o del asunto si vienen en el JSON; si no, 'global') y sale con 2: la tarea sigue
#      abierta y stderr llega al agente como retroalimentación.
#      A partir de la tercera ronda fallida le pide detenerse y escalar a Mafer, y lo anota
#      en .claude/state/escalaciones.log.
#   4. Si pasa, borra el contador y sale con 0.
#   El mensaje de falla incluye las pruebas que fallaron (líneas FAILED/ERROR de pytest), el resumen
#   de verify.sh y `git status --short`: el árbol es compartido y la falla puede ser de otro compañero.
#
# Variables:
#   PI_CHECKS_BASE         versión contra la que se mide "el cambio" en el paso 1b. Por defecto
#                          PI_BASE_REF o origin/server-improvement. Mientras el setup de checks no
#                          esté en la base, la líder puede apuntarla a un commit que ya lo tenga.
#   PI_GATE_FORCE_FAIL=1   simula una falla sin correr las pruebas (para probar el hook).
#   CLAUDE_PROJECT_DIR     raíz del repo (la pone Claude Code); si falta, se usa git.
#
# Prueba manual:
#   echo '{"task_id":"7","task_subject":"x"}' | PI_GATE_FORCE_FAIL=1 scripts/hooks/gate_task_completed.sh; echo $?

set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
STATE="$ROOT/.claude/state"
mkdir -p "$STATE"
LIMITE=3

INPUT="$(cat || true)"

# Lee el JSON con jq si existe; si no, con python3. Nunca falla: devuelve vacío.
json_eval() {
  local filtro_jq="$1" codigo_py="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$INPUT" | jq -r "$filtro_jq" 2>/dev/null || true
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$INPUT" | python3 -c "$codigo_py" 2>/dev/null || true
  fi
}

# 1. Esquema: solo nombres de llaves, una vez.
if ! grep -q 'TaskCompleted' "$STATE/hook_inputs.log" 2>/dev/null; then
  LLAVES="$(json_eval \
    '[paths(scalars) | map(tostring) | join(".")] | unique | join(", ")' \
    'import json,sys
d=json.load(sys.stdin)
def p(o,pre=""):
    if isinstance(o,dict):
        for k,v in o.items(): yield from p(v,pre+str(k)+".")
    elif isinstance(o,list):
        for i,v in enumerate(o): yield from p(v,pre+str(i)+".")
    else: yield pre[:-1]
print(", ".join(sorted(set(p(d)))))')"
  printf '%s TaskCompleted llaves: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${LLAVES:-<no se pudo leer el JSON>}" \
    >> "$STATE/hook_inputs.log"
fi

# Clave de la tarea: id o asunto si vienen; si no, 'global'. Solo [A-Za-z0-9_-], máx. 60.
TAREA="$(json_eval \
  '[.task_id, .taskId, (.task|objects|.id), .task_subject, (.task|objects|.subject), .subject]
   | map(select(. != null and . != "")) | (.[0] // "") | tostring' \
  'import json,sys
d=json.load(sys.stdin); t=d.get("task") if isinstance(d.get("task"),dict) else {}
for v in (d.get("task_id"),d.get("taskId"),t.get("id"),d.get("task_subject"),t.get("subject"),d.get("subject")):
    if v not in (None,""): print(v); break')"
TAREA="$(printf '%s' "$TAREA" | tr -c 'A-Za-z0-9_-' '_' | cut -c1-60)"
[ -n "$TAREA" ] || TAREA="global"
CONTADOR="$STATE/fallas_${TAREA}.count"

# Cuenta una ronda fallida; en la tercera pide escalar. No sale.
contar_ronda() {
  N=$(( $(cat "$CONTADOR" 2>/dev/null || echo 0) + 1 ))
  echo "$N" > "$CONTADOR"
}
escalar_si_toca() {
  if [ "$N" -ge "$LIMITE" ]; then
    echo "Tercera ronda fallida: detente y escala a Mafer con lo que intentaste." >&2
    printf '%s tarea=%s rondas=%s motivo=%s\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$TAREA" "$N" "$1" >> "$STATE/escalaciones.log"
  fi
}
estado_arbol() {
  echo "Estado del árbol compartido (git status --short):"
  git -C "$ROOT" status --short 2>/dev/null | head -n 40
  echo "Ojo: el árbol es compartido. Si la falla está en archivos que no son de tu tarea, puede ser"
  echo "trabajo a medias de otro compañero: no lo toques; avisa y cierra la tarea en un punto de"
  echo "sincronía (cuando pi-dev avise \"árbol estable\")."
}

# 1b. Checks y código de producción no van en el mismo cambio.
CHECKS_BASE="${PI_CHECKS_BASE:-${PI_BASE_REF:-origin/server-improvement}}"
if ! BASE_SHA="$(git -C "$ROOT" rev-parse --verify --quiet "${CHECKS_BASE}^{commit}")"; then
  contar_ronda
  {
    echo "No existe la versión base '$CHECKS_BASE' para revisar la separación entre checks y código."
    echo "Correr 'git fetch origin' o pedir a la líder que defina PI_CHECKS_BASE. La tarea sigue abierta (ronda $N)."
  } >&2
  escalar_si_toca "sin_base"
  exit 2
fi
MB="$(git -C "$ROOT" merge-base "$BASE_SHA" HEAD 2>/dev/null || echo "$BASE_SHA")"
CAMBIADOS="$( { git -C "$ROOT" diff --name-only "$MB" -- 2>/dev/null; git -C "$ROOT" ls-files --others --exclude-standard 2>/dev/null; } | sort -u)"
LISTA_PROT="$ROOT/scripts/hooks/rutas_protegidas.txt"
PROT_PATRONES="$( { grep -Ev '^[[:space:]]*(#|$)' "$LISTA_PROT" 2>/dev/null; echo 'scripts/hooks/rutas_protegidas.txt'; } | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
[ -f "$LISTA_PROT" ] || PROT_PATRONES="$(printf '%s\n' REVIEW.md pytest.ini docs/checklist-confiabilidad.md \
  .claude/settings.json '.claude/agents/**' '.claude/skills/**' 'scripts/**' \
  tests/conftest.py tests/e2e/_helpers.py tests/e2e/_pipeline_sandbox.py tests/differential/_harness.py)"
CHECKS_TOCADOS=""
PROD_TOCADO=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in scraper/*|ml_model/*|run_pipeline.sh) PROD_TOCADO="${PROD_TOCADO}  $f\n"; continue ;; esac
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    case "$p" in
      */\*\*) [ "${f#"${p%/\*\*}"/}" != "$f" ] && { CHECKS_TOCADOS="${CHECKS_TOCADOS}  $f\n"; break; } ;;
      *) [ "$f" = "$p" ] && { CHECKS_TOCADOS="${CHECKS_TOCADOS}  $f\n"; break; } ;;
    esac
  done <<< "$PROT_PATRONES"
done <<< "$CAMBIADOS"
if [ -n "$CHECKS_TOCADOS" ] && [ -n "$PROD_TOCADO" ]; then
  contar_ronda
  {
    echo "Gate: este cambio toca a la vez archivos de verificación y código de producción (base: $CHECKS_BASE @ ${MB:0:7})."
    echo "Un cambio no puede modificar los checks que lo juzgan. Sepáralos: primero los checks, en un"
    echo "cambio aparte que hace la líder con PI_EDITAR_CHECKS=1; luego el código. La tarea sigue abierta (ronda $N)."
    echo "Checks modificados:"; printf '%b' "$CHECKS_TOCADOS" | head -n 20
    echo "Código de producción modificado:"; printf '%b' "$PROD_TOCADO" | head -n 20
    estado_arbol
  } >&2
  escalar_si_toca "checks_y_codigo"
  exit 2
fi

# 2. Verificación rápida.
SALIDA="$STATE/ultima_verificacion_${TAREA}.txt"
if [ "${PI_GATE_FORCE_FAIL:-0}" = "1" ]; then
  echo "Falla simulada (PI_GATE_FORCE_FAIL=1)." > "$SALIDA"
  RC=1
elif [ ! -f "$ROOT/scripts/verify.sh" ]; then
  # Antes dejaba pasar; ahora borrar verify.sh no puede abrir el gate.
  echo "No existe scripts/verify.sh: no se puede verificar y la tarea no se cierra." > "$SALIDA"
  RC=2
else
  (cd "$ROOT" && bash scripts/verify.sh rapido) > "$SALIDA" 2>&1
  RC=$?
fi

# 4. Éxito: se reinicia el contador.
if [ "$RC" -eq 0 ]; then
  rm -f "$CONTADOR"
  exit 0
fi

# 3. Falla: se cuenta la ronda.
contar_ronda

{
  echo "La verificación rápida (scripts/verify.sh rapido) falló: ronda $N (límite: $LIMITE) para la tarea '$TAREA'."
  echo "La tarea sigue abierta."
  FALLIDAS="$(grep -E '^(FAILED|ERROR) ' "$SALIDA" 2>/dev/null | head -n 20)"
  if [ -n "$FALLIDAS" ]; then
    echo "Pruebas que fallaron:"
    printf '%s\n' "$FALLIDAS" | sed 's/^/  /'
  fi
  echo "Resumen de verify.sh:"
  sed -n '/===== Resumen/,$p' "$SALIDA" | head -n 15
  [ "$RC" -eq 2 ] && { echo "Error de entorno o de uso (exit 2):"; tail -n 5 "$SALIDA"; }
  estado_arbol
  echo "Salida completa: .claude/state/$(basename "$SALIDA")"
  echo "No se arregla con sleeps, reintentos ni skips: busca la causa raíz."
} >&2

escalar_si_toca "verify_rapido salida=.claude/state/$(basename "$SALIDA")"
exit 2
