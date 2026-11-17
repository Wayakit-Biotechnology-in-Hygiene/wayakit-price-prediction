# CLAUDE.md — Price Intelligence

Contexto para Claude Code en este repo. Responder siempre en español.

## Qué es

Proyecto de Wayakit que sugiere precios de venta para los productos de Arabia Saudita (KSA) a partir de los precios de la competencia. Hoy: scrapers con Selenium (Amazon, Mumzworld, SACO y tiendas B2B) y Gemini para decidir relevancia → `scraper/competitors_complete.csv` → `ml_model/` (precio por litro o por pieza, recorte p10–p90, RandomForest) → `product.price.suggestion` en Odoo. Corre en la EC2 `wayakit-app-server` (eu-north-1, casi siempre apagada): dos schedules de EventBridge del grupo `default` la encienden jueves y viernes a las 21:50 (CDMX), el crontab de `ec2-user` lanza `run_pipeline.sh` el viernes 04:00 UTC (etapa 1) y `run_pipeline.sh --stage 2` el sábado 04:00 UTC (jueves y viernes 22:00 CDMX), y el `trap EXIT` la apaga al terminar. La instancia corre la rama **`server-improvement`** (e25e2c9), no `main`. Acceso por SSM (sin SSH).

Documentación: [docs/runbook.md](docs/runbook.md) (cómo operar y problemas conocidos), [docs/recursos.md](docs/recursos.md) (qué existe), [docs/decisiones.md](docs/decisiones.md) (por qué).

## Estado

No se mantiene aquí. Fases, hallazgos y preguntas abiertas: [docs/plan.md](docs/plan.md). Lo que falta decidir: [docs/decisiones.md#pendiente](docs/decisiones.md#pendiente). En AWS todavía no se ha creado nada nuevo para PI; lo marcado "Planeado (F#)" no existe.

## Reglas que no se rompen

- **Ninguna credencial** en código, repo, logs ni chat; tampoco ID de cuenta (usar `<cuenta>`), correos, IPs ni IDs de instancia. Nunca imprimir el valor de un secreto, ni para verificar. Revisar `git diff --cached` en busca de llaves antes de cada commit.
- **Secretos solo en Secrets Manager `prod/wayakit-app`** (el código lee las llaves `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_TOKEN` y `GEMINI_API_KEY`). Leerlos desde un solo módulo: Planeado (F2) `pi_secrets.py`. Hoy hay 6 copias de `get_secret()`, no agregar otra: `ml_model/odoo_api_products.py:14`, `ml_model/odoo_api_cotizations.py:15`, `ml_model/odoo_api_competitor_products.py:17`, `ml_model/odoo_api_price_suggestion.py:21`, `scraper/odoo_api_connection_products.py:17`, `scraper/services/ai_service.py:12`.
- **Perfil SSO `wayakit-vdc`**, nunca `default`. **Región `eu-north-1`** para todo.
- **Etiqueta `project=price-intelligence`** en todo recurso nuevo.
- **Pedir confirmación a Mafer antes de:** crear recursos en AWS, aceptar acuerdos o EULAs de modelos, pedir cuotas, o escribir precios en Odoo (correr `odoo_api_price_suggestion.py`).
- **Odoo: confirmar la compañía (KSA o MX) antes de leer o escribir.** Hoy ninguna consulta filtra por `company_id`; se asume KSA porque todo está en SAR, pero el código no lo fuerza.
- **Mumzworld y SACO no se cambian** (ticket A). Cualquier cambio compartido (`config.py`, `main.py`) necesita la prueba de regresión de F6.
- **No agregar CSV con datos al repo.** `.gitignore` excluye `*.csv` y `scraper/.gitignore` vuelve a incluir `modifiers_mapping.csv`, `analysis-odoo.csv` y `analysis-odoo_partial.csv`. Hoy están versionados `scraper/modifiers_mapping.csv`, `scraper/analysis-odoo.csv` y `ml_model/wayakit_products_competition.csv` (este último trae precios históricos de la competencia); no sumar otros.
- **Los docs se actualizan en el mismo commit** que el cambio de código o de infraestructura.
- **`origin` (local / importado; columna Planeada, F6–F7) sale solo de los datos** (vendedor, envío "global" de Noon, fl oz, país de origen cuando viene). Sin listas de marcas mantenidas a mano (decisión de Mafer).

## Mapa del código

- **`run_pipeline.sh`.** Etapa 1 (`:65-109`): siempre completa desde e25e2c9 (`IS_FULL_RUN="true"`, `:68`; el modo parcial quedó deshabilitado y su rama, `:95-100`, ya no se alcanza), lee productos de Odoo (`:86`) y genera `scraper/analysis-odoo.csv` para revisión manual (`:90-93`). Etapa 2 (`--stage 2`, `:116-215`): `git pull` (`:133`), scraping con `timeout 30h` (`:154`), lecturas de Odoo (`:155-156`), ML y subida (`:163-167`; la rama parcial, `:172-202`, sigue en el código pero no se alcanza).
- **Contrato de un scraper:** `__init__(driver_path, relevance_agent)` y `scrape(keyword, search_mode) -> list[dict]`, con `search_mode` en `{'volume', 'units'}` (excepción: AeroSense no recibe `relevance_agent`). Cada dict trae `Product`, `Price_SAR` (empaque completo), `Company`, `URL`, `Unit of measurement` (`ml`, `L`, `fl oz`, `units`) y `Total quantity` (total del pack, > 0). Registro en `scraper/main.py:282-290` y en `TARGET_MAP` (`scraper/config.py:9-24`).
- **`competitors_complete.csv`** (`scraper/config.py:3-7`): `date, industry, subindustry, type_of_product, generic_product_type, product, price_sar, company, source, url, unit_of_measurement, total_quantity, channel`. Se arma en `scraper/main.py:368-383`.
- **Precio por litro o por pieza:** `ml_model/1a_preprocess_data.py:47-70`, no en el scraper.
- **Modelo** (`ml_model/2_train_models.py:160-169`): dos RandomForest (volumétrico y unidades; 300 árboles, `min_samples_leaf=1`, métricas de holdout 80/20 en el log, `:62-80`) con `volume_liters` o `total_quantity` más `type_of_product`, `subindustry`, `channel`, `generic_product_type`, `industry` y `company` (la marca en Amazon, Mumzworld y SACO). Ya no usa `approved_quote_price` como feature (sigue siendo el target de las filas de Wayakit). No usa la fuente. El rango de mercado (`*_market_stats.joblib`) sale solo de competidores (`:177-178`). En la predicción, con 3 competidores o más, el precio se recorta a `market_min`–`market_max` (`ml_model/3_predicted_prices.py:126-132`) y después se aplica el piso de margen `costo × 1.30` (`:135-136`), que siempre gana.
- **Odoo** (XML-RPC). Lee `product.master`, `sale.order.line` y `competitor.product`. Escribe `product.price.suggestion`: en modo completo copia a `product.price.suggestion.history`, hace `unlink` de todo y luego `create` (`ml_model/odoo_api_price_suggestion.py:110-182`). Si el `create` falla, la tabla queda vacía y el script sale con 0.

## Trampas conocidas

- **Fallas silenciosas:** `exit()` sale con 0 (`odoo_api_price_suggestion.py:52,56,65,78,109`, también en `odoo_api_products.py` y `odoo_api_cotizations.py`); `1a_preprocess_data.py:87-90` traga excepciones; los pasos de la etapa 2 no llevan `|| exit 1`. El pipeline puede reportar "Éxito" y subir datos viejos.
- **`git pull` en la etapa 2** (`run_pipeline.sh:133`) no revisa el resultado y choca con `analysis-odoo.csv`, que la etapa 1 reescribe.
- **Subindustria sensible a mayúsculas:** `TARGET_MAP.get` (`main.py:333`) usa `'Land Transportation'` y los datos traen `'Land transportation'`; esas filas nunca se scrapean y no hay aviso.
- **Columna nueva en el CSV = `config.py` + `main.py` a la vez:** `csv.DictWriter` (`main.py:255`) usa el `extrasaction='raise'` por defecto.
- **Ventas fijas a 2025** (`ml_model/odoo_api_cotizations.py:75-76`): las ventas de 2026 no entran al entrenamiento.
- **Sin API key, `is_relevant` devuelve `False`** para todo (`scraper/services/ai_service.py:138-139`): la corrida termina sin productos (salvo AeroSense, que no usa la IA) y sin error.
- **La key de Gemini va en la URL** (`ai_service.py:54-55`) y **está en texto plano** en `logs/cron.log` y `logs/pipeline_execution.log` de la instancia (confirmado en F1). No copiar esos logs a ningún lado ni imprimirlos sin redactar `key=`.
- **Un paso que falla no detiene el pipeline:** en la corrida del 13–15 nov 2026 el scraping llegó al timeout de 30 h con 53 filas y aun así se entrenó y se subió a Odoo en modo full.
- **Tamaños inconsistentes en Apify:** en `junglee`, `Item Volume`, `Unit Count` y `Size` muchas veces no coinciden; conservar la regla de 2 de 3 fuentes.
- **Los productos de Wayakit salen en los resultados** (Amazon y Noon). Hoy nada los excluye; los adaptadores de Apify deben descartar marca y vendedor Wayakit.

## Convenciones de `infra/` — Planeado (F3)

- Un script por recurso, con boto3. Cada uno se puede correr más de una vez (si existe, lo actualiza; nunca lo duplica), acepta `--dry-run` y tiene un docstring en español que dice qué deja configurado y cómo se usa.
- Configuración compartida en `infra/config.py` (perfil, región, etiqueta, nombres, cron). No duplicar constantes.
- No guardar el ID de cuenta ni correos en el repo: el ID se obtiene con STS y el correo se pasa por argumento.
- No nombrar scripts como módulos de la biblioteca estándar (`secrets.py` rompía boto3 en VdC).
- Al agregar un recurso: script nuevo + fila en `docs/recursos.md` + paso en `docs/runbook.md` + decisión en `docs/decisiones.md` si aplica.

## IA de relevancia

- **Hoy:** Gemini por HTTP (`gemini-2.5-flash-lite` para relevancia, `gemini-2.5-pro` para contar unidades de wipes), un título por llamada.
- **Planeado (F2):** Bedrock con la API Converse en eu-north-1: **DeepSeek V3.2** (`deepseek.v3.2`) como principal y **Claude Haiku 4.5** como respaldo si falla o devuelve JSON inválido. `temperature=0`, JSON forzado con `outputConfig` (JSON schema; DeepSeek no admite `toolConfig` en Converse), lotes de 25 títulos, timeout de 120 s (la primera llamada con un schema nuevo compila la gramática). `RelevanceAgent` conserva `is_relevant()` y `extract_wipes_units()`; si los fallos pasan del 5 %, salir con código 1. Al terminar, revocar la key de Gemini y quitarla de `prod/wayakit-app`.

## Apify — Planeado (F6)

- Amazon KSA: `junglee/amazon-crawler` con la URL `https://www.amazon.sa/s?k=…`. **No pasar `countryCode` ni `zipCode`** (cobran USD 0.05 extra por resultado).
- Noon KSA: `saswave/noon-product-scraper`, **una corrida por keyword** (no devuelve la keyword ni la moneda; se asume SAR por `/saudi-en/`).
- Cuenta: la de VdC. Hoy el único token está en `voz-del-cliente/api-keys` (de VdC); PI no debe leer ese secreto. Dónde vive el token de PI está por decidir: el plan propone una llave nueva en `prod/wayakit-app` (pregunta 4 de `docs/plan.md`).

## Cómo se desarrolla (equipo y verificación)

- **Equipo:** para un ticket con varias partes, la sesión principal es la líder (única que hace commits) y dirige a `pi-dev`, `pi-tester` y `pi-reviewer` (`.claude/agents/`). Manual: skill [`equipo`](.claude/skills/equipo/SKILL.md), con la tabla de quién es dueño de cada archivo.
- **Verificación:** skill [`verify`](.claude/skills/verify/SKILL.md) y `scripts/verify.sh rapido|completo` (unit, property, e2e, differential; sin red). `completo` antes de cada commit que toque `ml_model/`, `scraper/` o `run_pipeline.sh`.
- **Reglas de revisión:** [REVIEW.md](REVIEW.md) y [docs/checklist-confiabilidad.md](docs/checklist-confiabilidad.md), leídos **desde la rama base** (`git show "${PI_BASE_REF:-origin/server-improvement}:REVIEW.md"`). Los checks van en PR aparte del código; `scripts/hooks/protect_checks.sh` los bloquea salvo con `PI_EDITAR_CHECKS=1` (solo la líder, en un cambio de solo checks).
- **3 rondas:** si el mismo check sigue fallando (o el mismo hallazgo Importante sigue abierto) después de 3 intentos, se para y se escala a Mafer con el error, lo intentado y la decisión que hace falta.
- **Sello del commit:** trailer `Verificado: <rapido|completo|ninguna> (<detalle>)`. El hook `commit-msg` (`scripts/install_githooks.sh`, una vez por clon) lo valida contra `.claude/state/ultima_verificacion.json` del mismo árbol.

## Entorno

Local: conda, entorno **`wayakit-price-prediction`** (`conda env create -f environment.yml`; para las pruebas, además `pip install -r requirements-dev.txt`). En la instancia: conda, entorno `wayakit_env` (fijo en `run_pipeline.sh:6`) hasta F3; si hay que recrearlo allí, `conda env create -f environment.yml -n wayakit_env` (sin `-n` se crearía `wayakit-price-prediction` y `run_pipeline.sh` no lo encontraría). `requirements.txt` no fija versiones; en la instancia: Python 3.10.20, conda en `/home/ec2-user/miniconda`, Chrome 146. [environment.yml](environment.yml) fija las versiones de la instancia (`pip freeze`, 2026-11-17); para Bedrock con `outputConfig` quizá haya que subir boto3 en F2 (local se probó con 1.43.115). Datos de la última corrida para diagnóstico: `data/f1_antes_2026-11-15/` (ignorada por git). Los scripts usan rutas relativas: correrlos desde la raíz del repo.

```bash
python ml_model/odoo_api_products.py                     # lee Odoo → ml_model/wayakit_products.csv
python scraper/odoo_api_connection_products.py --input_odoo_products_file ml_model/wayakit_products.csv --output_analysis_file scraper/analysis-odoo.csv
python scraper/main.py --analysis_file scraper/analysis-odoo.csv --output_mode overwrite --output_file scraper/competitors_complete.csv
python ml_model/odoo_api_cotizations.py && python ml_model/odoo_api_competitor_products.py
python ml_model/1a_preprocess_data.py && python ml_model/1b_preprocess_data.py --run_mode full
python ml_model/2_train_models.py && python ml_model/3_predicted_prices.py
python ml_model/odoo_api_price_suggestion.py --run_mode full   # ESCRIBE en Odoo: solo con OK de Mafer y compañía confirmada
```

## Despliegue — Planeado (F4)

Hoy no hay CI/CD: el código llega por el `git pull` de la etapa 2. Planeado: GitHub Actions (OIDC, sin llaves) publica `releases/<sha>.tar.gz` en S3, mueve el puntero `current` y enciende la instancia en modo "solo deploy" (pull-on-boot, symlink atómico). Rollback con `workflow_dispatch` y un `sha`. Detalle en [docs/runbook.md](docs/runbook.md).
