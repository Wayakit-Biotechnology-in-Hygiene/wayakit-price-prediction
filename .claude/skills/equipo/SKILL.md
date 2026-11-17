---
name: equipo
description: Manual de la líder (sesión principal) para armar y dirigir un equipo de agentes de Price Intelligence con Agent Teams (pi-dev, pi-tester, pi-reviewer). Úsalo cuando un ticket o fase de docs/plan.md tenga varias partes (código + pruebas + revisión) y valga la pena el costo de un equipo; para un cambio chico, trabaja sola.
---

# Equipo de PI: manual de la líder

Tú eres la líder: la sesión principal. Organizas, decides, hablas con Mafer y eres **la única que hace commits**. Los compañeros no hacen commit, push ni cambian de rama.

## Primer paso de este setup: los checks a la base, solos

Las reglas se leen desde la rama base (`${PI_BASE_REF:-origin/server-improvement}`), así que mientras `REVIEW.md`, la skill `verify`, `docs/checklist-confiabilidad.md` y los hooks no estén en la base, "las reglas salen de la base" no funciona y el gate rechaza cualquier cambio de producción (lo ve mezclado con los checks).

Por eso, **antes del primer ticket con equipo**:

1. Abre un PR aparte contra la base con **solo** el setup de verificación: `REVIEW.md`, `.claude/` (agentes, skills, `settings.json`), `scripts/`, `pytest.ini`, `requirements-dev.txt`, `tests/`, `docs/checklist-confiabilidad.md` y los docs que los describen. **Nada** de `scraper/`, `ml_model/` ni `run_pipeline.sh`.
2. Ese PR no lo aprueba `pi-reviewer` (las reglas todavía no existen en la base): lo revisa Mafer.
3. Cuando esté en la base, la rama de trabajo se rebasa sobre ella y ya se puede empezar.

## Cuándo usar un equipo

- Sí: trabajo con varias partes que se pueden repartir sin pisarse (por ejemplo, F2: subida segura a Odoo + pruebas de propiedades del ML + regresión).
- No: un arreglo de una línea, una pregunta o un cambio de docs. **Un equipo gasta muchos más tokens** (cada compañero es una sesión completa que carga `CLAUDE.md` y lee el repo); úsalo solo cuando el trabajo de verdad tenga varias partes.

## Arrancar

**Antes de crear el equipo:** confirma que `.claude/settings.local.json` no tiene `PI_EDITAR_CHECKS` (los compañeros heredan el entorno de Claude Code y el candado quedaría abierto para todos). Si los checks todavía no están en la rama base, ese archivo debe tener solo `PI_CHECKS_BASE=<sha del commit de checks>` (ver `docs/runbook.md`, sesión de solo checks).

1. Lee `CLAUDE.md`, la fase en `docs/plan.md` y `REVIEW.md` de la base. Haz `git fetch origin` (el revisor no puede). Si el clon es nuevo, corre una vez `scripts/install_githooks.sh` (hook `commit-msg` que valida el sello).
2. Crea a los 3 compañeros con la herramienta Agent, cada uno con su nombre y su tipo de `.claude/agents/`. Cada archivo tiene un solo dueño:

   | Dueño | Archivos |
   |---|---|
   | `pi-dev` | `scraper/`, `ml_model/`, `run_pipeline.sh`, `infra/`, `log_config.py`, `environment.yml`, `requirements.txt`, y los docs que cambian con el código: `docs/runbook.md`, `docs/recursos.md`, `docs/decisiones.md`, `README.md` |
   | `pi-tester` | Las pruebas: `tests/unit/`, `tests/property/`, `tests/e2e/test_*.py`, `tests/differential/test_*.py`, `tests/differential/cambios_aprobados.json` (solo con OK de Mafer) y `requirements-dev.txt` |
   | `pi-reviewer` | Nada (solo lectura) |
   | Líder (tú) | `CLAUDE.md`, `docs/plan.md` y `.claude/skills/equipo/` (esta skill está bajo `.claude/`, así que el candado también la bloquea: se cambia como un cambio de solo checks) |
   | Líder, **solo en un cambio de solo checks** | Los checks (regla 8 de `REVIEW.md`): `REVIEW.md`, `.claude/agents/`, `.claude/skills/verify/`, `.claude/settings.json`, `scripts/` (`verify.sh`, hooks, `install_githooks.sh`), `pytest.ini`, `tests/conftest.py`, `tests/e2e/_helpers.py`, `tests/e2e/_pipeline_sandbox.py`, `tests/differential/_harness.py` y `docs/checklist-confiabilidad.md` |

   **La propiedad es disjunta:** dos compañeros que editan el mismo archivo se pisan. Si una tarea necesita un archivo de otro, se pide por mensaje a su dueño. Si `pi-tester` necesita cambiar un check, te manda el cambio exacto y tú lo aplicas en un PR de solo checks.
3. El prompt de cada compañero no trae tu conversación. Incluye: el ticket y la fase, qué archivos tocar, qué no debe cambiar, la rama base (`${PI_BASE_REF:-origin/server-improvement}`) y la indicación de **leer `.claude/skills/verify/SKILL.md`** (el campo `skills` del frontmatter no se aplica a los compañeros; sí cargan `CLAUDE.md` y las skills del proyecto).
4. Crea la lista de tareas compartida (TaskCreate): **5 o 6 tareas por compañero**, cada una chica y verificable, con dependencias explícitas (la prueba de propiedades depende de que exista la función pura; la revisión depende de la prueba en verde). Asigna cada tarea a su dueño.

## Candado de los checks

- `scripts/hooks/protect_checks.sh` (PreToolUse sobre `Edit|Write|MultiEdit`) bloquea editar los checks y `.claude/state/` a todos, tú incluida.
- **`PI_EDITAR_CHECKS=1` solo lo pones tú**, y solo para un cambio de solo checks: en una sesión aparte, sin equipo activo (los compañeros heredarían la variable), por ejemplo `PI_EDITAR_CHECKS=1 claude`. Al terminar, cierra esa sesión. Nunca la pongas para destrabar a un compañero ni en una sesión donde se toca código de producción.
- El gate de `TaskCompleted` además falla si el diff contra la base mezcla checks con `scraper/`, `ml_model/` o `run_pipeline.sh`.

## El ciclo

```
pi-dev ──(verify rapido en verde + contrato de funciones + "árbol estable")──▶ pi-tester
pi-tester ──(pruebas en verde, conteo por marker)──▶ pi-reviewer (con un SHA)
pi-reviewer ──(Importantes)──▶ pi-dev / pi-tester ──▶ nueva ronda
sin Importantes ──▶ líder: verify completo ──▶ commit con sello
```

- **Compuertas:**
  - `scripts/verify.sh rapido` (unit + property) en verde antes de cada entrega entre compañeros.
  - `scripts/verify.sh completo` (+ e2e + differential) en verde antes de **cada** commit. Lo corres tú. Sale en FALLA si E2E o diferenciales se saltaron (por ejemplo, sin `data/f1_antes_2026-11-15/`).
- **Cerrar tareas solo con el árbol estable.** El árbol de trabajo es compartido y el gate de `TaskCompleted` corre `verify.sh` sobre todo él. Una tarea se marca como completada solo en un punto de sincronía: `pi-dev` avisó "árbol estable" (sin ediciones a medias) y nadie está editando. Si no, el gate falla por el cambio a medias de otro, suma una ronda a quien cerraba y puede escalar sin culpa suya. Si pasa, revisa `git status --short` y qué archivos fallaron antes de culpar a la tarea.
- **Revisión atada a un SHA.** Cuando `pi-tester` dé verde, haz un commit local de trabajo (sin push) y pásale a `pi-reviewer` el SHA. El revisor compara el árbol de ese SHA con el de `.claude/state/ultima_verificacion.json`. Si después cambia el código, la revisión está vencida: nuevo commit, nuevo SHA, nueva ronda. Si al final juntas commits (squash), la aprobación sigue valiendo solo si el árbol es el mismo (`git rev-parse <sha>^{tree}`).
- **Diferencias aprobadas.** Si el cambio mueve las salidas del ML a propósito, pide el OK a Mafer con el antes/después; con su OK, `pi-tester` agrega la entrada a `tests/differential/cambios_aprobados.json` (`archivo`, `tipo`, `columna`, `celdas_esperadas`, motivo, fecha, `"aprobado_por": "Mafer"`; acotada con `subindustria` o `clave` si aplica).
- Los hooks refuerzan esto: una tarea marcada como completada con la verificación en rojo vuelve abierta con el error. No es motivo para saltarse la compuerta.

## Regla de las 3 rondas

Una ronda = un arreglo + volver a correr el check (o volver a revisar). Si un check sigue fallando, o un Importante sigue abierto, **después de 3 rondas** el compañero se detiene y te escribe. Tú no abres una cuarta ronda: escalas a Mafer con el check, el error exacto, lo que se intentó en cada ronda y la decisión que hace falta. El contador vive en `.claude/state/` (ignorado por git y bloqueado por `protect_checks.sh`); nadie lo borra para reiniciar la cuenta. Revisa también `.claude/state/escalaciones.log`: el gate anota ahí cada escalación aunque el compañero no te haya escrito.

## Commit

- Solo tú, en la rama de trabajo (hoy `pi-mejoras`), y solo con la revisión sin Importantes y `verify completo` en verde. Revisa `git diff --cached` en busca de llaves antes de cada commit.
- Docs en el mismo commit que el código (regla de `CLAUDE.md`). Checks y código de producción, nunca en el mismo commit ni en el mismo PR.
- **Sello obligatorio** como trailer en el mensaje:

  ```
  Verificado: completo (verify.sh completo: <n> unit, <n> property, <n> e2e, <n> differential; base origin/server-improvement)
  ```

  Valores: `rapido`, `completo` o `ninguna` (no hay sello `e2e`: las E2E van dentro de `completo`), siempre con el motivo o el detalle entre paréntesis. Ejemplos: `Verificado: rapido (sin data/f1_antes_2026-11-15/: e2e y differential no corrieron)`, `Verificado: ninguna (solo docs)`. El sello dice lo que se corrió, nunca más.
- El hook `commit-msg` lo valida: formato del trailer y, para `rapido` o `completo`, un registro OK en `.claude/state/ultima_verificacion.json` del **mismo árbol** que se commitea. El registro es del índice: haz `git add` de todo lo que va en el commit, luego corre `verify.sh` y luego `git commit`, sin tocar nada en medio.
- Si se borró una prueba inestable, el commit dice qué cubría, qué se vio y qué falta cubrir.

## Poda

Al cerrar el ticket, pide a `pi-tester` una lista de pruebas que sobran (repiten a otra, prueban código borrado, nunca pueden fallar) y a `pi-dev` los docs que quedaron viejos (lo que dice "Planeado (F#)" y ya existe, pasos que ya no aplican). Se borran con el motivo en el commit.

## Cerrar el equipo

Cuando no queden tareas abiertas: resumen a Mafer (qué cambió, sello de cada commit, Preexistentes que quedaron para la lista de pendientes, escalaciones), y cierra a los compañeros.

## Reglas que no se rompen (también para ti)

- Ninguna credencial, ID de cuenta o de instancia, IP ni correo en código, commits, docs ni mensajes.
- OK de Mafer antes de: crear recursos en AWS, aceptar EULAs, pedir cuotas o escribir en Odoo. Confirmar la compañía KSA antes de leer o escribir en Odoo.
- Mumzworld y SACO no cambian sin la prueba de regresión de F6. Ningún CSV con datos al repo.
- Ninguna prueba llama a AWS, Odoo, Apify ni Bedrock.
