#!/usr/bin/env bash
# Hook PreToolUse (Edit|Write|MultiEdit|NotebookEdit y Bash): un cambio no puede modificar sus propios checks.
#
# Qué hace:
#   - Edit, Write, MultiEdit y NotebookEdit: si la ruta (resuelta, sin "..", sin symlinks y sin
#     distinguir mayúsculas, como el disco de macOS) cae en scripts/hooks/rutas_protegidas.txt,
#     bloquea la herramienta (exit 2) y explica en stderr por qué.
#   - Bash (heurística, no es un candado completo): bloquea los comandos que claramente escriben en
#     una ruta protegida: redirección (> o >>) hacia ella, tee, sed -i / perl -i, rm, mv, truncate,
#     touch, chmod, cp/ln/install/rsync con destino protegido, git checkout/restore/rm/mv sobre ella
#     y "git config core.hooksPath". No ve lo que hace un script o un "python -c" por dentro: para eso
#     está el gate de TaskCompleted, que falla si un mismo cambio toca checks y código de producción.
#
# Excepción: PI_EDITAR_CHECKS=1 en el entorno de Claude Code deja pasar todo y lo anota en el log.
# Solo la líder lo pone (al lanzar la sesión), en un cambio dedicado a los checks, sin código de producción.
# Un compañero no puede activarlo: el hook lee el entorno del proceso de Claude Code, no el de su Bash.
#
# Si scripts/hooks/rutas_protegidas.txt no existe, usa la lista incluida aquí (borrarlo no apaga el candado).
# Si no hay python3, bloquea Edit/Write (no puede revisar la ruta) y deja pasar Bash.
# Log: .claude/state/protect_checks.log (fecha, herramienta, decisión y ruta; nunca el contenido).
#
# Prueba manual:
#   echo '{"tool_name":"Edit","tool_input":{"file_path":"REVIEW.md"}}' | scripts/hooks/protect_checks.sh; echo $?   # 2
#   echo '{"tool_name":"Edit","tool_input":{"file_path":"ml_model/x.py"}}' | scripts/hooks/protect_checks.sh; echo $? # 0

set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
INPUT="$(cat || true)"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { [ -x /usr/bin/python3 ] && PY=/usr/bin/python3; }

if [ -z "$PY" ]; then
  case "$INPUT" in
    *'"Bash"'*) exit 0 ;;
  esac
  [ "${PI_EDITAR_CHECKS:-0}" = "1" ] && exit 0
  echo "Bloqueado: no hay python3 para revisar si la ruta es un archivo de verificación protegido." >&2
  exit 2
fi

printf '%s' "$INPUT" | PI_ROOT="$ROOT" "$PY" -c '
import fnmatch, glob, json, os, shlex, sys, time

root = os.path.realpath(os.environ["PI_ROOT"])
permitir = os.environ.get("PI_EDITAR_CHECKS", "0") == "1"
lista = os.path.join(root, "scripts", "hooks", "rutas_protegidas.txt")
RESPALDO = """REVIEW.md
pytest.ini
docs/checklist-confiabilidad.md
.claude/settings.json
.claude/settings.local.json
.claude/agents/**
.claude/skills/**
.claude/state/**
scripts/**
tests/conftest.py
tests/e2e/_helpers.py
tests/e2e/_pipeline_sandbox.py
tests/differential/_harness.py"""

try:
    with open(lista, encoding="utf-8") as f:
        texto = f.read()
except OSError:
    texto = RESPALDO
patrones = [l.strip() for l in texto.splitlines() if l.strip() and not l.strip().startswith("#")]
# La lista misma también está protegida, aunque alguien la saque del archivo.
patrones.append("scripts/hooks/rutas_protegidas.txt")

def log(herr, decision, ruta):
    try:
        estado = os.path.join(root, ".claude", "state")
        os.makedirs(estado, exist_ok=True)
        with open(os.path.join(estado, "protect_checks.log"), "a", encoding="utf-8") as f:
            f.write("%s herramienta=%s decision=%s ruta=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), herr, decision, ruta))
    except OSError:
        pass

def relativa(ruta, cwd):
    """Ruta relativa a la raíz del repo, resuelta; None si queda fuera del repo."""
    if not ruta:
        return None
    ruta = os.path.expanduser(ruta)
    if not os.path.isabs(ruta):
        ruta = os.path.join(cwd, ruta)
    real = os.path.realpath(ruta)
    rel = os.path.relpath(real, root)
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return rel.replace(os.sep, "/")

def protegida(rel):
    if rel is None:
        return False
    r = rel.casefold()
    for p in patrones:
        q = p.casefold()
        if q.endswith("/**"):
            base = q[:-3]
            if r == base or r.startswith(base + "/"):
                return True
        elif r == q:
            return True
    return False

def bloquear(herr, rel, detalle=""):
    log(herr, "bloqueado", rel)
    sys.stderr.write(
        "Bloqueado: \"%s\" es un archivo de verificación protegido (ver scripts/hooks/rutas_protegidas.txt).%s\n"
        "Un cambio no puede modificar las pruebas, reglas o hooks que lo juzgan.\n"
        "Si de verdad hace falta otro check, no lo rodees: avísale a la líder. Ella lo hace en un cambio aparte,\n"
        "sin código de producción (scraper/, ml_model/, run_pipeline.sh), con PI_EDITAR_CHECKS=1.\n"
        % (rel, (" " + detalle) if detalle else ""))
    sys.exit(2)

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
herr = d.get("tool_name") or ""
ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
cwd = d.get("cwd") if isinstance(d.get("cwd"), str) and os.path.isdir(d.get("cwd")) else root

# ---------- Edit / Write / MultiEdit / NotebookEdit ----------
if herr != "Bash":
    rutas = [ti.get(k) for k in ("file_path", "notebook_path", "path") if isinstance(ti.get(k), str)]
    for ruta in rutas:
        rel = relativa(ruta, cwd)
        if protegida(rel):
            if permitir:
                log(herr, "permitido_PI_EDITAR_CHECKS", rel)
                continue
            bloquear(herr, rel)
    sys.exit(0)

# ---------- Bash (heurística) ----------
cmd = ti.get("command")
if not isinstance(cmd, str) or not cmd.strip() or permitir:
    sys.exit(0)

try:
    lx = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    tokens = list(lx)
except ValueError:
    sys.exit(0)  # comillas sin cerrar: no se adivina

SEP = set(";&|()\n")
segmentos, actual = [], []
for t in tokens:
    if t and set(t) <= SEP:
        if actual:
            segmentos.append(actual)
        actual = []
    else:
        actual.append(t)
if actual:
    segmentos.append(actual)

def rels_de(tok):
    """Rutas protegidas a las que apunta un token (expande comodines)."""
    if any(c in tok for c in "*?["):
        base = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
        hallados = glob.glob(base, recursive=True) or [base.split("*")[0].split("?")[0].split("[")[0]]
        out = [relativa(h, cwd) for h in hallados]
    else:
        out = [relativa(tok, cwd)]
    return [r for r in out if protegida(r)]

def revisar(herr_det, toks):
    for t in toks:
        for r in rels_de(t):
            bloquear("Bash", r, "(" + herr_det + ")")

for seg in segmentos:
    # Redirecciones: el token que sigue a > o >> es el destino.
    for i, t in enumerate(seg):
        if t in (">", ">>", ">|", "&>", "&>>", "<>") and i + 1 < len(seg):
            revisar("redirección " + t, [seg[i + 1]])
    # Comando efectivo: se saltan asignaciones VAR=x y envoltorios comunes.
    i = 0
    while i < len(seg):
        t = seg[i]
        if "=" in t and not t.startswith("-") and t.split("=", 1)[0].replace("_", "").isalnum():
            i += 1
        elif t in ("sudo", "command", "env", "nohup", "time", "exec"):
            i += 1
        else:
            break
    if i >= len(seg):
        continue
    prog = os.path.basename(seg[i])
    args = [a for a in seg[i + 1:] if a not in (">", ">>", "<", ">|", "&>", "&>>", "<>")]
    no_opc = [a for a in args if not a.startswith("-")]
    if prog in ("rm", "unlink", "truncate", "touch", "chmod", "chown", "mv", "tee", "shred"):
        revisar(prog, no_opc)
    elif prog in ("cp", "ln", "install", "rsync", "ditto") and no_opc:
        revisar(prog + " (destino)", no_opc[-1:])
    elif prog in ("sed", "gsed", "perl") and any(
            a == "--in-place" or a.startswith("--in-place=") or (a.startswith("-") and not a.startswith("--") and "i" in a)
            for a in args):
        revisar(prog + " en sitio", no_opc)
    elif prog == "git":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in ("-C", "-c") else 1
        sub = args[j] if j < len(args) else ""
        resto = args[j + 1:]
        claves = [k for k, a in enumerate(resto) if a.casefold() == "core.hookspath"]
        escribe = bool(claves) and (claves[0] + 1 < len(resto)
                                    or any(a.startswith("--unset") or a in ("--add", "--replace-all") for a in resto))
        if sub == "config" and escribe and "--get" not in resto:
            log("Bash", "bloqueado", "git config core.hooksPath")
            sys.stderr.write("Bloqueado: core.hooksPath lo configura solo scripts/install_githooks.sh (lo corre la líder).\n")
            sys.exit(2)
        if sub in ("checkout", "restore", "rm", "mv"):
            revisar("git " + sub, [a for a in resto if not a.startswith("-")])
sys.exit(0)
'
