#!/usr/bin/env bash
# Hook PreToolUse (Bash): aviso temprano y amable sobre el sello "Verificado:" de cada `git commit`.
#
# La regla la hace cumplir el hook de git scripts/githooks/commit-msg (instalado con
# scripts/install_githooks.sh), que lee el mensaje FINAL y lo compara con el registro de verify.sh.
# Este hook solo adelanta el aviso para que el agente lo corrija antes de intentar el commit.
#
# Detección estricta: el comando se separa con shlex en segmentos (; && || | y saltos de línea) y solo
# cuenta un segmento cuyo programa es git (tras VAR=x, env, command...) y cuyo subcomando, saltando las
# opciones globales (-C dir, -c k=v, --git-dir=..., --no-pager), es "commit". `echo "git commit"`,
# grep, comentarios o textos con esas palabras no cuentan.
#
# Qué revisa en cada git commit:
#   - Salto del hook de git: --no-verify, -n (también dentro de -anm...) o -c core.hooksPath=...
#     SIEMPRE se bloquea (exit 2), en cualquier modo salvo off: anularía la verificación real.
#   - El mensaje (de -m/--message, incluido el heredoc $(cat <<'EOF' ...), de -F/--file o, con
#     --amend sin mensaje nuevo, el del último commit) debe tener exactamente un trailer que cumpla
#       ^Verificado: (rapido|completo|ninguna) \(.+\)$
#     leído con "git interpret-trailers --parse".
#   - rapido/completo: si el comando no cambia el índice (sin git add previo, -a ni rutas), busca en
#     .claude/state/verificaciones.jsonl una corrida OK de verify.sh para el tree del índice actual.
#   - Avisa si core.hooksPath no apunta a scripts/githooks (el hook de git no está instalado).
#
# Modo (variable PI_COMMIT_GATE):
#   off      no revisa nada.
#   observe  (predeterminado) no bloquea el sello: avisa (systemMessage + stderr) y anota en el log.
#   enforce  bloquea (exit 2) si el sello falta, no cumple el formato o no tiene registro. Sin válvula.
# Log: .claude/state/commit_gate.log (fecha, sesión, modo y resultado; nunca el texto del comando ni del mensaje).
#
# Prueba manual:
#   echo '{"session_id":"s1","tool_input":{"command":"git commit -m x"}}' \
#     | PI_COMMIT_GATE=enforce scripts/hooks/commit_stamp.sh; echo $?

set -u

MODO="${PI_COMMIT_GATE:-observe}"
[ "$MODO" = "off" ] && exit 0

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
STATE="$ROOT/.claude/state"
mkdir -p "$STATE"
INPUT="$(cat || true)"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { [ -x /usr/bin/python3 ] && PY=/usr/bin/python3; }
if [ -z "$PY" ]; then
  printf '%s modo=%s sin_python3 (no se revisa; lo revisa commit-msg)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODO" >> "$STATE/commit_gate.log"
  exit 0
fi

printf '%s' "$INPUT" | PI_ROOT="$ROOT" PI_MODO="$MODO" "$PY" -c '
import json, os, re, shlex, subprocess, sys, time

root = os.environ["PI_ROOT"]
modo = os.environ["PI_MODO"]
state = os.path.join(root, ".claude", "state")
REGEX = re.compile(r"^Verificado: (rapido|completo|ninguna) \(.+\)$")

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
cmd = ti.get("command")
if not isinstance(cmd, str) or "commit" not in cmd:
    sys.exit(0)
sesion = re.sub(r"[^A-Za-z0-9_-]", "_", str(d.get("session_id") or "sin_sesion"))[:60]
cwd = d.get("cwd") if isinstance(d.get("cwd"), str) and os.path.isdir(d.get("cwd")) else root

def anotar(res):
    try:
        with open(os.path.join(state, "commit_gate.log"), "a", encoding="utf-8") as f:
            f.write("%s sesion=%s modo=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), sesion, modo, res))
    except OSError:
        pass

def git(*args, entrada=None, dir_=None):
    try:
        p = subprocess.run(["git", "-C", dir_ or cwd] + list(args), input=entrada, capture_output=True, text=True, timeout=10)
        return p.returncode, p.stdout
    except Exception:
        return 1, ""

# ---------- Separar en segmentos ----------
try:
    lx = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    tokens = list(lx)
except ValueError:
    anotar("comando_no_analizable (lo revisa commit-msg)")
    sys.exit(0)
SEP = set(";&|()\n")
segmentos, actual = [], []
for t in tokens:
    if t and set(t) <= SEP:
        if actual: segmentos.append(actual)
        actual = []
    else:
        actual.append(t)
if actual: segmentos.append(actual)

def analizar_git(seg):
    """(dir, opciones_globales, subcomando, args) si el segmento es una llamada a git; si no, None."""
    i = 0
    while i < len(seg):
        t = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t in ("env", "command", "nohup", "time", "exec", "sudo"):
            i += 1
        else:
            break
    if i >= len(seg) or os.path.basename(seg[i]) != "git":
        return None
    i += 1
    dir_, globales = cwd, []
    while i < len(seg) and seg[i].startswith("-"):
        t = seg[i]
        if t in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(seg):
            if t == "-C":
                dir_ = seg[i + 1] if os.path.isabs(seg[i + 1]) else os.path.join(dir_, seg[i + 1])
            globales += [t, seg[i + 1]]
            i += 2
        else:
            globales.append(t)
            i += 1
    if i >= len(seg):
        return None
    return dir_, globales, seg[i], seg[i + 1:]

hubo_add = False
problemas, avisos, bloqueos = [], [], []
encontrado = False
CON_ARG = set("mFcCt")  # opciones cortas de git commit que llevan argumento

def cuerpo_heredoc(v):
    m = re.match(r"^\$\(\s*cat\s+<<-?\s*[\x27\"]?(\w+)[\x27\"]?\s*\n(.*)\n\s*\1\s*\n?\s*\)\s*$", v, re.S)
    return m.group(2) if m else v

for seg in segmentos:
    if seg[0] == "cd" and len(seg) > 1:
        destino = os.path.expanduser(seg[1])
        destino = destino if os.path.isabs(destino) else os.path.join(cwd, destino)
        if os.path.isdir(destino):
            cwd = destino
        continue
    g = analizar_git(seg)
    if g is None:
        continue
    dir_, globales, sub, args = g
    if sub in ("add", "rm", "mv", "stash", "reset", "restore", "checkout"):
        hubo_add = True
        continue
    if sub != "commit":
        continue
    encontrado = True
    for k in range(0, len(globales) - 1):
        if globales[k] == "-c" and globales[k + 1].lower().startswith("core.hookspath"):
            bloqueos.append("git -c core.hooksPath=... anula el hook commit-msg")
    msgs, archivo, amend, cambia_indice = [], None, False, hubo_add
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            cambia_indice = cambia_indice or k + 1 < len(args)
            break
        if a == "--no-verify":
            bloqueos.append("--no-verify salta el hook commit-msg")
        elif a in ("-m", "--message") and k + 1 < len(args):
            msgs.append(args[k + 1]); k += 1
        elif a.startswith("--message="):
            msgs.append(a.split("=", 1)[1])
        elif a in ("-F", "--file") and k + 1 < len(args):
            archivo = args[k + 1]; k += 1
        elif a.startswith("--file="):
            archivo = a.split("=", 1)[1]
        elif a == "--amend":
            amend = True
        elif a in ("--all", "--include", "--only", "--interactive", "--patch"):
            cambia_indice = True
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            # grupo de opciones cortas: -anm "x", -mx, -Farchivo
            for j, c in enumerate(a[1:], start=1):
                if c == "n":
                    bloqueos.append("-n (--no-verify) salta el hook commit-msg")
                if c in ("a", "i", "o", "p"):
                    cambia_indice = True
                if c in ("u", "S"):
                    break  # -uno, -S<llave>: el resto es su valor, no más opciones
                if c in CON_ARG:
                    resto = a[j + 1:]
                    valor = resto if resto else (args[k + 1] if k + 1 < len(args) else "")
                    if not resto: k += 1
                    if c == "m": msgs.append(valor)
                    elif c == "F": archivo = valor
                    break
        elif not a.startswith("-"):
            cambia_indice = True  # rutas: git commit <archivo>
        k += 1

    # Mensaje a revisar
    mensaje = None
    if msgs:
        mensaje = "\n\n".join(cuerpo_heredoc(m) for m in msgs)
    elif archivo and archivo != "-":
        ruta = archivo if os.path.isabs(archivo) else os.path.join(dir_, archivo)
        try:
            mensaje = open(ruta, encoding="utf-8").read()
        except OSError:
            avisos.append("no pude leer el archivo de -F; lo revisará commit-msg")
    elif amend:
        rc, out = git("log", "-1", "--format=%B", dir_=dir_)
        mensaje = out if rc == 0 else None
    else:
        avisos.append("el mensaje se escribe en el editor; lo revisará commit-msg")

    if mensaje is not None:
        rc, limpio = git("stripspace", "--strip-comments", entrada=mensaje, dir_=dir_)
        rc, trailers = git("interpret-trailers", "--parse", entrada=limpio if rc == 0 else mensaje, dir_=dir_)
        sellos = [l for l in trailers.splitlines() if l.lower().startswith("verificado:")]
        if not sellos:
            if "verificado:" in mensaje.lower():
                problemas.append("el \"Verificado:\" no es un trailer: va en su propia línea, en el último párrafo, tras una línea en blanco")
            else:
                problemas.append("falta la línea \"Verificado: <rapido|completo|ninguna> (<detalle>)\"")
        elif len(sellos) > 1:
            problemas.append("hay %d trailers \"Verificado:\"; debe haber uno" % len(sellos))
        elif not REGEX.match(sellos[0]):
            problemas.append("el sello no cumple ^Verificado: (rapido|completo|ninguna) \\(.+\\)$")
        else:
            m = sellos[0].split()[1]
            if m in ("rapido", "completo"):
                if cambia_indice:
                    avisos.append("el comando cambia el índice (git add, -a o rutas): el registro de verify.sh no coincidirá; haz git add, corre verify y luego commitea")
                else:
                    rc, tree = git("write-tree", dir_=dir_)
                    tree = tree.strip()
                    ok = False
                    try:
                        with open(os.path.join(state, "verificaciones.jsonl"), encoding="utf-8") as f:
                            regs = [json.loads(l) for l in f if l.strip()]
                    except (OSError, ValueError):
                        regs = []
                    for modo_r in (["completo"] if m == "completo" else ["rapido", "completo"]):
                        c = [r for r in regs if r.get("tree") == tree and r.get("modo") == modo_r]
                        if c and c[-1].get("resultado") == "OK" and c[-1].get("indice_igual_arbol") is True:
                            ok = True
                    if not ok:
                        problemas.append("el sello dice \"%s\" pero no hay una corrida OK de scripts/verify.sh %s para el índice actual (corre verify después de git add)" % (m, m))

if not encontrado:
    sys.exit(0)

rc, hp = git("config", "--get", "core.hooksPath")
if hp.strip() != "scripts/githooks":
    avisos.append("el hook de git commit-msg no está instalado (core.hooksPath); la líder debe correr scripts/install_githooks.sh")

if bloqueos:
    anotar("bloqueado_salto_hook")
    sys.stderr.write("Commit bloqueado: " + "; ".join(sorted(set(bloqueos))) + ".\n"
                     "En este repo cada commit pasa por scripts/githooks/commit-msg. No se salta: si el sello no se puede\n"
                     "cumplir, usa \"Verificado: ninguna (motivo)\".\n")
    sys.exit(2)

if problemas and modo == "enforce":
    anotar("bloqueado_sello: problemas=%d" % len(problemas))
    sys.stderr.write("Commit bloqueado (PI_COMMIT_GATE=enforce):\n" + "".join("  - %s\n" % p for p in problemas)
                     + "Formato: Verificado: <rapido|completo|ninguna> (<detalle o motivo>), REVIEW.md regla 10.\n"
                     "Si no se verificó, escribe \"Verificado: ninguna (motivo)\"; no inventes una verificación.\n")
    sys.exit(2)

if problemas or avisos:
    anotar("aviso: problemas=%d avisos=%d" % (len(problemas), len(avisos)))
    texto = "Aviso del sello Verificado (el hook commit-msg puede rechazar este commit):\n" + \
            "".join("  - %s\n" % p for p in problemas + avisos)
    sys.stderr.write(texto)
    print(json.dumps({"systemMessage": texto.strip()}, ensure_ascii=False))
else:
    anotar("con_sello_ok")
sys.exit(0)
'
