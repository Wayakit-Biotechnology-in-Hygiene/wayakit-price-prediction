---
name: verify
description: Cómo correr y probar Price Intelligence (PI) sin tocar producción. Úsala antes de cada commit y después de cualquier cambio de código (scraper/, ml_model/, run_pipeline.sh, tests/, scripts/), para elegir los chequeos de docs/checklist-confiabilidad.md, correr scripts/verify.sh, correr una etapa del pipeline offline con datos congelados y estampar el commit con el trailer "Verificado:".
allowed-tools: Bash
---

# verify — cómo verificar PI

Responder y documentar en español. Todo lo de aquí corre **en local y sin red**: nada llama a AWS, Odoo, Apify ni Bedrock.

## Lo que nunca se hace al verificar

- **Nunca correr `ml_model/odoo_api_price_suggestion.py`** (escribe y borra en Odoo). Tampoco `odoo_api_products.py`, `odoo_api_cotizations.py`, `odoo_api_competitor_products.py` ni `scraper/odoo_api_connection_products.py`: leen Odoo con credenciales de Secrets Manager.
- Nunca llamar a AWS (Secrets Manager, Bedrock, EC2, SSM, EventBridge), Odoo, Apify ni Gemini desde una prueba o desde `verify.sh`. Si el código lo necesita, se usa un cliente falso.
- **Nunca encender la instancia `wayakit-app-server`** ni correr `run_pipeline.sh` de verdad (apaga la máquina donde corre y sube a Odoo). La única excepción es su prueba E2E: corre en una copia temporal con shims en el `PATH` para `python`, `timeout`, `git`, `conda`, `aws`, `sudo` y `shutdown`, y no se corre sin el shim de `sudo`.
- Nunca correr los scripts de `ml_model/` dentro del repo real: usan rutas relativas y sobrescriben CSV y modelos. Siempre en una copia temporal (ver abajo).
- Nunca correr `scraper/main.py` (abre Chrome y scrapea sitios reales).
- No imprimir secretos ni copiar logs de la instancia (`logs/cron.log`, `logs/pipeline_execution.log`, `wayakit_app.log`): pueden traer la key de Gemini.

## Entorno

```bash
cd "$(git rev-parse --show-toplevel)"                                     # en un hook: "$CLAUDE_PROJECT_DIR"
PY="${PI_PYTHON:-$(conda info --base)/envs/wayakit-price-prediction/bin/python}"   # conda env "wayakit-price-prediction", Python 3.10
```

- Local: entorno conda `wayakit-price-prediction` (`conda env create -f environment.yml` y `$PY -m pip install -r requirements-dev.txt`). Versiones: pandas 2.3.3, scikit-learn 1.7.2, pytest 8.4.2, hypothesis 6.140.2.
- En la instancia el entorno sigue llamándose `wayakit_env` hasta F3; si hay que recrearlo allí: `conda env create -f environment.yml -n wayakit_env`.
- `verify.sh` busca el intérprete en `PI_PYTHON` o en el entorno `wayakit-price-prediction`. No uses otro Python: con otras versiones de numpy o scikit-learn el diferencial puede dar otros números.
- Una vez por clon: `scripts/install_githooks.sh` (instala el hook `commit-msg` que valida el sello; ver abajo).

## Los dos modos

```bash
scripts/verify.sh rapido     # pytest -m "unit or property" -q   (segundos; antes de cada commit)
scripts/verify.sh completo   # rapido + pytest -m "(e2e or differential) and not unit"   (minutos; si cambia ml_model/, scraper/ o run_pipeline.sh; FALLA si E2E o diferenciales se saltan)
PI_BASE_REF=origin/main scripts/verify.sh completo   # otra versión base (por defecto origin/server-improvement)
```

| Marcador | Qué prueba | Carpeta |
|---|---|---|
| `unit` | Funciones sueltas con entradas fijas | `tests/unit/` |
| `property` | Hypothesis: cientos de entradas por prueba (perfil `pi`, determinista; `HYPOTHESIS_PROFILE=exploracion` para buscar casos nuevos) contra reglas que siempre se cumplen (round-trip, invariantes, idempotencia, equivalencia) | `tests/property/` |
| `e2e` | Corre etapas reales del pipeline (`ml_model/` y `run_pipeline.sh` con shims) en una copia temporal con datos congelados y revisa el flujo completo | `tests/e2e/` |
| `differential` | Mismas entradas en la versión base (`PI_BASE_REF`) y en la nueva; compara salidas contra `tests/differential/cambios_aprobados.json` | `tests/differential/` |

Para una sola prueba: `$PY -m pytest tests/unit/test_x.py::test_y -q`. Toda prueba lleva uno de los cuatro marcadores: una prueba sin marcador no corre en ningún modo de `verify.sh`.

`verify.sh` quita las credenciales de AWS del entorno (`AWS_CONFIG_FILE=/dev/null`, sin perfil, sin IMDS) como candado. Si una prueba falla por "no credentials", la prueba está intentando salir a AWS: eso es el error, no el candado.

### Cómo leer el resultado

- Al final imprime `===== Resumen =====`, una línea por bloque (`OK` o `FALLA` con el conteo de pytest) y `Resultado: OK|FALLA`. Con OK imprime además el **trailer sugerido** para el commit.
- Código de salida: 0 todo bien; 1 alguna prueba falló **o no se recolectó ninguna** (una verificación que no corre nada no cuenta como verde); 2 uso incorrecto o `PI_BASE_REF` no existe (`git fetch origin`).
- `-rfEs` lista las pruebas que fallaron, los errores y las saltadas con su motivo. Si falta `data/f1_antes_2026-11-15/` (ignorada por git), las E2E y diferenciales se saltan y **`completo` sale en FALLA**: sin los datos, el sello máximo es `rapido`, diciendo que E2E y diferenciales no corrieron.
- Cada corrida deja `.claude/state/ultima_verificacion.json` con el modo, el resultado y el **árbol** verificado (`git write-tree` del índice, es decir, lo que ya tiene `git add`). Por eso el orden es `git add` de todo lo que va en el commit, luego `verify.sh` y luego `git commit`; si quedan cambios sin `git add` o archivos sin seguimiento, `verify.sh` avisa y no sugiere trailer. El hook `commit-msg` y `pi-reviewer` leen ese registro: si cambias o agregas un archivo después de verificar, el árbol ya no coincide y hay que volver a correr.

## Correr una etapa offline (copia temporal + datos congelados)

Los datos de la corrida del 13–15 nov 2026 están en `data/f1_antes_2026-11-15/` (`scraper/` y `ml_model/`). Las etapas de ML (`1a`, `1b`, `2`, `3`) no usan la red; tardan unos 30 s en total.

```bash
PY="${PI_PYTHON:-$(conda info --base)/envs/wayakit-price-prediction/bin/python}"
REPO="$(git rev-parse --show-toplevel)"
T="$(mktemp -d)"; mkdir "$T/repo"                      # log_config.py escribe en ../logs → queda en $T/logs
rsync -a --exclude .git --exclude data --exclude logs "$REPO/" "$T/repo/"   # código del árbol de trabajo
# Para la versión base en su lugar: git -C "$REPO" archive "${PI_BASE_REF:-origin/server-improvement}" | tar -x -C "$T/repo"
cp -R "$REPO/data/f1_antes_2026-11-15/ml_model/." "$T/repo/ml_model/"
cp -R "$REPO/data/f1_antes_2026-11-15/scraper/."  "$T/repo/scraper/"
cd "$T/repo"
$PY ml_model/1a_preprocess_data.py                 # competitors_complete + competitor_products_from_odoo → competitor_*_processed.csv
$PY ml_model/1b_preprocess_data.py --run_mode full # → wayakit_products_to_predict_odoo.csv
$PY ml_model/2_train_models.py                     # → ml_model/trained_models/*.joblib
$PY ml_model/3_predicted_prices.py                 # → ml_model/wayakit_prediction_report.csv
```

Ojo:
- **Los scripts salen con 0 aunque fallen** (`exit()`, `except` que se tragan el error). El código de salida no basta: revisar que el archivo de salida exista, sea nuevo y cumpla las reglas del mapa funcional.
- Se puede correr solo una etapa: los insumos de cada una ya están en los datos congelados.
- Mismo código + mismos datos en la misma máquina = mismos árboles (RandomForest con `random_state=42`; verificado el 2026-11-17 entrenando dos veces). La predicción con `n_jobs=-1` puede variar en ~1e-15 entre llamadas (suma de árboles en hilos), por eso el diferencial compara con tolerancia 1e-6.
- Re-correr el código actual sobre los datos congelados **no** reproduce el `wayakit_prediction_report.csv` de producción: cambian 520 de 738 precios, hasta 51.86 SAR, aunque los procesados salen iguales. La causa está en investigación (CPU/BLAS, versiones de scikit-learn o numpy en la instancia, o que esa corrida no fuera e25e2c9). Por eso el diferencial compara **base contra nueva en la misma máquina y sobre los mismos insumos**, nunca contra salidas de producción.

## Mapa funcional (lo que ve el usuario)

| Etapa | Entrada | Salida | Resultado esperado |
|---|---|---|---|
| Productos de Odoo | `product.master` (KSA) | `ml_model/wayakit_products.csv` | Productos activos. **No se corre al verificar** (Odoo) |
| Lista de búsqueda | `wayakit_products.csv` + `scraper/modifiers_mapping.csv` | `scraper/analysis-odoo.csv` | Una fila por producto × sitio con su keyword (`Search Modifiers`) |
| Scraping | `analysis-odoo.csv`, `TARGET_MAP` | `scraper/competitors_complete.csv` (13 columnas, `scraper/config.py:3-7`) | Filas con `price_sar` > 0 y `total_quantity` > 0, de varias fuentes. Pocas filas o una fuente en 0 = corrida mala (13–15 nov: 53 filas). **No se corre al verificar** (red) |
| Preproceso `1a` | `competitors_complete.csv`, `competitor_products_from_odoo.csv` | `competitor_volumetric_processed.csv`, `competitor_unit_processed.csv` | Precio por litro o por pieza finito y > 0; recorte p10–p90 |
| Preproceso `1b` | los dos anteriores + `wayakit_products.csv` | `wayakit_products_to_predict_odoo.csv` | Solo productos predecibles (tipo con competencia) |
| Entrenamiento `2` | cotizaciones + procesados | `trained_models/*.joblib` | Dos modelos (volumétrico y unidades) y estadísticas de mercado |
| Predicción `3` | modelos + `wayakit_products_to_predict_odoo.csv` | `wayakit_prediction_report.csv` | **Una fila por fila de `wayakit_products_to_predict_odoo.csv`** (738 con los datos congelados; `Product_ID` puede repetirse: hay SKU duplicados en Odoo); `predicted_price` sin NaN; `predicted_price ≥ round(cost_per_unit × 1.30, 2)` (el precio se redondea a 2 decimales); con ≥ 3 competidores, recortado al rango de mercado salvo que gane el piso |
| Subida | reporte | `product.price.suggestion` en Odoo | **Nunca al verificar.** Solo con OK de Mafer y compañía KSA confirmada |

## Qué chequeos correr: `docs/checklist-confiabilidad.md`

1. **Leer el checklist desde la rama base**, no desde la rama del cambio (un cambio no puede relajar sus propias reglas):
   ```bash
   git show "${PI_BASE_REF:-origin/server-improvement}":docs/checklist-confiabilidad.md
   ```
   Si no existe en la base todavía, usar la copia local y decirlo en el trailer.
2. Leer el diff: `git diff "${PI_BASE_REF:-origin/server-improvement}"...HEAD --stat` y `git diff --cached`.
3. Marcar las filas cuyo **Disparador** coincide con los archivos o patrones del diff. Lo normal son **3 a 10**. Si no coincide ninguna, decirlo.
4. Correr solo esas (más `verify.sh rapido` siempre, y `completo` si el diff toca `ml_model/`, `scraper/` o `run_pipeline.sh`).
5. Anotar los IDs (`C01`…) en el trailer.

## Si algo falla: máximo 3 rondas

- Una **ronda** = cambiar algo y volver a correr el chequeo que falló.
- Si el mismo chequeo sigue fallando después de la **3.ª ronda**, **parar**: no más cambios. Escalar a Mafer con: chequeo que falla, salida resumida (sin secretos), las 3 cosas que se intentaron y la hipótesis actual. El contador de rondas lo llevan los hooks en `.claude/state/` (ignorado por git); no editarlo a mano para "reiniciar" el conteo.
- No se "arregla" una prueba para que pase si lo que está mal es el código. Si la prueba está mal, decirlo y explicarlo.

## Pruebas inestables (flaky)

- Prohibido: `sleep`, reintentos (`pytest-rerunfailures`, bucles), `@pytest.mark.skip`/`xfail` para esconder la falla, subir tolerancias sin motivo.
- **Única excepción para `xfail`:** `xfail(strict=True)` solo para un bug documentado, con el contraejemplo mínimo en `reason` y el destinatario (`pi-dev`, o Mafer si hay que decidir qué es lo correcto). El `xfail` cubre solo ese contraejemplo: la propiedad general sigue activa (el caso conocido se excluye con `assume(...)` o filtrando la estrategia) y el bug queda en una prueba mínima con `@example`. Cuando se arregla el bug, el XPASS hace fallar la verificación y se quita el `xfail`.
- Se busca la causa raíz: semilla fija (Hypothesis con `derandomize` o `@seed`, `random_state`), orden de filas (ordenar antes de comparar), fechas (inyectar la fecha), archivos compartidos (cada prueba en su `tmp_path`), red (no hay red).
- Si no se encuentra la causa, **se borra la prueba** y se documenta en el commit. Si revela un riesgo real, se le pide a `pi-dev` (dueño de `docs/runbook.md`) que lo agregue en Problemas conocidos; `pi-tester` no edita el runbook.

## Los checks no cambian junto con el código

- Los checks son las rutas de la regla 8 de `REVIEW.md` (`scripts/`, `pytest.ini`, `tests/conftest.py`, los arneses `tests/e2e/_helpers.py`, `tests/e2e/_pipeline_sandbox.py` y `tests/differential/_harness.py`, `.claude/`, `REVIEW.md`, `docs/checklist-confiabilidad.md` y `.claude/state/`).
- `scripts/hooks/protect_checks.sh` (PreToolUse sobre `Edit|Write|MultiEdit`) bloquea editarlos salvo con `PI_EDITAR_CHECKS=1`, que solo pone la líder, en una sesión sin equipo y para un cambio de solo checks.
- El gate de `TaskCompleted` falla si el diff contra la base mezcla checks con código de producción (`scraper/`, `ml_model/`, `run_pipeline.sh`): van en PR separados.
- Las pruebas nuevas (`tests/unit/`, `tests/property/`, `test_*.py`) y las entradas de `cambios_aprobados.json` sí van con el código que prueban.

## Sello del commit: trailer `Verificado:`

Todo commit lleva una línea final `Verificado: <rapido|completo|ninguna> (<detalle o motivo>)` que dice cómo se verificó (mismo formato que `REVIEW.md`, regla 10). `scripts/verify.sh` imprime el trailer sugerido al terminar en verde. Va en el último párrafo del mensaje: si el commit lleva `Co-Authored-By`, pon el `Verificado:` justo arriba, en el mismo párrafo (sin línea en blanco entre los dos), o el hook `commit-msg` no lo reconoce como trailer. Formatos:

```
Verificado: completo (verify.sh completo — rapido: OK (<n> passed); e2e-diferencial: OK (<n> passed); checklist C01, C03, C17 leído de origin/server-improvement)
Verificado: rapido (verify.sh rapido — rapido: OK (<n> passed); e2e y diferenciales no corrieron: falta data/f1_antes_2026-11-15; checklist C08)
Verificado: ninguna (solo docs)
```

- `ninguna` es válido y obligatorio cuando no se corrió nada; siempre con el motivo entre paréntesis.
- **El hook `commit-msg`** (lo instala `scripts/install_githooks.sh`) lee el mensaje final con `git interpret-trailers --parse` y rechaza el commit si el trailer no cumple `Verificado: <rapido|completo|ninguna> (<detalle>)`. Si dice `rapido` o `completo`, exige además un registro OK de ese modo en `.claude/state/verificaciones.jsonl` para el **mismo árbol** que se va a commitear. El registro es el árbol del **índice** (lo que ya tiene `git add`), así que el orden es: revisar `git status` para que no entre nada ajeno, `git add` de todo lo que va en el commit, `verify.sh` y `git commit`, sin tocar nada en medio.
- Nunca poner `OK` de algo que no se corrió en esta versión del código, ni copiar el trailer de otro commit.
- Antes del commit: `git diff --cached` sin llaves ni IDs (chequeo C19) y sin CSV nuevos (C20).
