# Reglas de revisión — Price Intelligence

Reglas que bloquean un commit o un PR de PI. Las aplica `pi-reviewer` (y cualquier persona que revise).

**Se leen desde la rama base, nunca del árbol de trabajo:**

```bash
git show "${PI_BASE_REF:-origin/server-improvement}:REVIEW.md"
```

Así un cambio no puede modificar sus propios checks. La base es `origin/server-improvement` (lo que corre en producción); más adelante será `origin/main`. Si este archivo todavía no existe en la base, el PR que lo crea lo revisa Mafer: es el primer paso del setup, un PR con solo los checks (ver la skill `equipo`).

## Categorías

| Categoría | Qué es | ¿Bloquea? |
|---|---|---|
| **Importante** | Bug real con un escenario concreto que falla, o cualquier punto de "Qué bloquea" | Sí |
| **Nit** | Mejora menor; nada de estilo salvo que esconda un bug | No |
| **Preexistente** | Ya estaba en la base y el cambio no lo empeora | No; va a la lista de pendientes y no se le cobra al autor |

Cada hallazgo lleva `archivo:línea`, categoría, escenario que falla y arreglo sugerido. Una revisión vale para un SHA (y su árbol): si el código cambia, se vuelve a revisar.

## Qué bloquea

1. **Secretos o identificadores en el diff:** credenciales, tokens, API keys, ID de cuenta (usar `<cuenta>`), IDs de instancia, IPs o correos, en código, docs, pruebas, fixtures o logs. También imprimir el valor de un secreto.
2. **Escrituras en Odoo sin red de seguridad:** todo código que escriba en Odoo necesita un camino `--dry-run` que no escriba, confirmar o filtrar la compañía (KSA) y no dejar la tabla vacía si el `create` falla. Correr la escritura de verdad necesita el OK de Mafer.
3. **Recursos de AWS creados fuera de `infra/`:** cualquier `create_*`/`put_*` de boto3 o comando de la CLI que cree o cambie recursos fuera de un script de `infra/` (idempotente, con `--dry-run`, perfil `wayakit-vdc`, región `eu-north-1`, etiqueta `project=price-intelligence`).
4. **Mumzworld o SACO con otro comportamiento** sin evidencia de la prueba de regresión de F6 (mismas keywords, antes y después, salidas iguales). Incluye cambios en `scraper/config.py` o `scraper/main.py` que los afecten.
5. **Fallas silenciosas:** salir con código 0 ante un error (`exit()`, `return` en vez de `sys.exit(1)`), `except` que se traga el error, pasos de `run_pipeline.sh` sin `|| exit 1`, subir a Odoo con datos viejos o vacíos.
6. **CSV con datos nuevos en el repo**, o datos de `data/` o `logs/` copiados a otro lado.
7. **Pruebas debilitadas o borradas sin justificación:** quitar asserts, ampliar tolerancias, agregar `skip`/`xfail`/`sleep`/reintentos, reducir ejemplos de Hypothesis, o agregar entradas a `tests/differential/cambios_aprobados.json` que no cumplan su esquema: `archivo`, `tipo`, `columna` (si el tipo la pide), `celdas_esperadas` (exactas), motivo, fecha y `"aprobado_por": "Mafer"`; nada de entradas que cubran un archivo entero.
   - El único `skip` permitido: E2E y diferenciales cuando falta `data/f1_antes_2026-11-15/` (y entonces el sello no puede decir `completo`).
   - La única excepción para `xfail`: **`xfail(strict=True)` solo para un bug documentado, con su contraejemplo y su destinatario** (`pi-dev`, o Mafer si hay que decidir qué es lo correcto). Cubre solo ese contraejemplo: la propiedad general sigue activa y el caso conocido se excluye con `assume(...)` o filtrando la estrategia. Un `xfail` sobre una propiedad entera, sin `strict=True` o sin contraejemplo es Importante.
8. **Cambios a los checks en el mismo PR que el código que revisan.** Los checks son: `REVIEW.md`, `.claude/` (`agents/`, `skills/`, `settings.json`), `scripts/` (`verify.sh`, `hooks/`, `install_githooks.sh` y el hook `commit-msg`), `pytest.ini`, `tests/conftest.py`, los arneses `tests/differential/_harness.py`, `tests/e2e/_helpers.py` y `tests/e2e/_pipeline_sandbox.py` (shims de `run_pipeline.sh`), `docs/checklist-confiabilidad.md` y el estado `.claude/state/` (contadores de rondas y registro de verificación). Van en un PR aparte contra la base, sin `scraper/`, `ml_model/` ni `run_pipeline.sh`. Las pruebas nuevas y las entradas de `cambios_aprobados.json` sí van con su cambio (regla 7).
   - Candado técnico: `scripts/hooks/protect_checks.sh` (PreToolUse) bloquea editar estas rutas salvo con `PI_EDITAR_CHECKS=1`, que solo pone la líder en una sesión sin equipo y para un cambio de solo checks; el gate de `TaskCompleted` falla si el diff contra la base mezcla checks con código de producción. Que el candado no salte no quita esta regla: el revisor la revisa en el diff.
9. **Docs atrasados:** un cambio de pipeline o de infraestructura sin su actualización en `docs/` en el mismo commit.
10. **Commit sin sello:** falta el trailer `Verificado: rapido|completo|ninguna (motivo)`, o el sello dice más de lo que se corrió.
    - El hook `commit-msg` (se instala con `scripts/install_githooks.sh`) valida el formato y, para `rapido` o `completo`, que `.claude/state/ultima_verificacion.json` tenga un registro OK de ese modo para el mismo árbol que se commitea.
    - `completo` solo vale si E2E y diferenciales corrieron: `verify.sh completo` sale en FALLA si se saltaron.
    - El revisor comprueba que el `tree` de ese registro sea igual a `git rev-parse <sha>^{tree}` del SHA revisado.

## Lista de confiabilidad

Además de lo anterior, revisar en cada cambio:

- **Verificación:** ¿hay evidencia de `scripts/verify.sh` (`.claude/state/ultima_verificacion.json` del mismo árbol, cuántas pruebas por marker, qué se saltó y por qué)? ¿Lo que cambió el comportamiento tiene una prueba que habría fallado antes del cambio? ¿Toda prueba nueva lleva uno de los cuatro marcadores?
- **Funciones puras:** ¿la lógica nueva de precios, unidades o limpieza está en una función sin E/S y tiene pruebas de propiedades (ida y vuelta, invariantes, idempotencia o equivalencia)?
- **Diferencias contra la base:** ¿cada diferencia en las salidas del ML está en `cambios_aprobados.json` y coincide con lo que dice el commit?
- **Pruebas sin red:** ¿ninguna prueba llama a AWS, Odoo, Apify, Bedrock, Gemini ni a las tiendas, y los E2E corren en una copia temporal, no en el repo?
- **Determinismo:** ¿se conserva `random_state=42` y no hay dependencia de la hora, del orden de un dict o de archivos que deja otra prueba? Las comparaciones diferenciales son siempre base contra nueva en la misma máquina, nunca contra salidas de producción (la corrida local no reproduce el reporte de producción; ver `docs/plan.md`).
- **Trampas conocidas de `CLAUDE.md`:** mayúsculas en subindustria, `extrasaction='raise'` del `DictWriter`, ventas fijas a 2025, `is_relevant` en `False` sin API key, key de Gemini en la URL, productos de Wayakit en los resultados.
- **Secretos:** se leen solo de `prod/wayakit-app` y no se agregó otra copia de `get_secret()`.
