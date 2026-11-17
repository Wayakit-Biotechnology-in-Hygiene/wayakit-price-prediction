#!/usr/bin/env bash
# Instala los hooks de git versionados de PI: deja "git config core.hooksPath scripts/githooks" en
# el .git/config de ESTE clon (no es global ni toca otros repos).
#
# Qué queda configurado:
#   - scripts/githooks/commit-msg: todo commit lleva el sello "Verificado: <modo> (<detalle>)" y, si dice
#     rapido o completo, tiene que coincidir con una corrida OK de scripts/verify.sh para el mismo índice.
#   - Con core.hooksPath, git deja de leer .git/hooks/: si ahí había hooks propios (no *.sample), se avisa.
#
# Uso (desde cualquier carpeta del repo; se puede correr más de una vez):
#   scripts/install_githooks.sh             # instala
#   scripts/install_githooks.sh --dry-run   # muestra qué haría, sin cambiar nada
#   scripts/install_githooks.sh --estado    # muestra la configuración actual
# Para desinstalar: git config --unset core.hooksPath (lo hace la líder, a mano).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
DESEADO="scripts/githooks"
ACCION="${1:-instalar}"
ACTUAL="$(git config --local --get core.hooksPath || true)"

case "$ACCION" in
  --estado)
    echo "core.hooksPath (local): ${ACTUAL:-<sin definir: git usa .git/hooks>}"
    [ "$ACTUAL" = "$DESEADO" ] && echo "Instalado." || echo "No instalado: correr scripts/install_githooks.sh"
    exit 0 ;;
  --dry-run|instalar) ;;
  *) echo "Uso: scripts/install_githooks.sh [--dry-run|--estado]" >&2; exit 2 ;;
esac

[ -f "$DESEADO/commit-msg" ] || { echo "ERROR: falta $DESEADO/commit-msg." >&2; exit 1; }

PROPIOS="$(find "$(git rev-parse --git-dir)/hooks" -maxdepth 1 -type f ! -name '*.sample' 2>/dev/null || true)"
if [ -n "$PROPIOS" ]; then
  echo "AVISO: con core.hooksPath git deja de usar estos hooks de .git/hooks/:"
  printf '  %s\n' $PROPIOS
fi
if [ -n "$ACTUAL" ] && [ "$ACTUAL" != "$DESEADO" ]; then
  echo "AVISO: core.hooksPath ya valía '$ACTUAL'; se cambia a '$DESEADO'."
fi

if [ "$ACCION" = "--dry-run" ]; then
  echo "[dry-run] chmod +x $DESEADO/*"
  echo "[dry-run] git config --local core.hooksPath $DESEADO"
  exit 0
fi

chmod +x "$DESEADO"/*
git config --local core.hooksPath "$DESEADO"
echo "Listo: core.hooksPath = $(git config --local --get core.hooksPath)"
echo "Desde ahora cada commit necesita su trailer \"Verificado:\" (ver scripts/githooks/commit-msg)."
