# Plan de mejoras — Price Intelligence (nov–dic 2026)

Estado: **borrador para revisar con Mafer**, en la rama `pi-mejoras`, que sale de `main`. **Corrección (F1, 2026-11-17): la instancia corre `server-improvement` (e25e2c9), no `main`.** Cambios en AWS hasta hoy: calibración de Apify y prueba de modelos (EULA de Qwen y DeepSeek aceptados, 2026-11-16); en F1 (2026-11-17) un snapshot del disco y la política `AmazonSSMManagedInstanceCore` en el rol de la instancia. Nada en Odoo.

Decidido por Mafer (2026-11-16):
- Amazon KSA y Noon KSA se sacan **con Apify, con los mismos actores que VdC**: `junglee/amazon-crawler` y `saswave/noon-product-scraper`.
- Mumzworld y SACO se quedan igual.
- El CI/CD propuesto (pull-on-boot más encendido en modo "solo deploy") está aprobado.
- **Gemini se reemplaza por Bedrock: DeepSeek V3.2 como principal y Haiku 4.5 como respaldo** (prueba comparativa del 2026-11-16, sección 5).
- No se pide a nadie que defina qué cuenta como "local".
- **Corrida mensual el último jueves de cada mes.** No choca con la de VdC, que corre el primer domingo.
- Apify va en la cuenta de VdC. El plan Starter se sube de nivel para cubrir el gasto.
- Corrida de calibración de Apify aprobada (2026-11-16).

Tickets:
- **A**: [corregir mascotas y migrar B2C a Apify](https://app.notion.com/p/41abe65f8c1381ffb1dad8a51e4eb87a). Estimación 8, vence el 2026-11-30.
- **B**: [infraestructura como código y CI/CD a EC2](https://app.notion.com/p/41abe65f8c1381e88806e23603e434b3). Estimación 5, vence el 2026-12-03.

Referencia: repo `voz-del-cliente` (VdC). De ahí se copian las convenciones de `infra/` y de `docs/`.

---

## 1. Cómo funciona hoy (lo verificado)

**Flujo de datos:** `scraper/` (Selenium + Gemini para decidir relevancia) → `competitors_complete.csv` → `ml_model/` (precio por litro o por pieza, recorte p10–p90, RandomForest) → `product.price.suggestion` en Odoo.

**Ejecución:**
- Instancia EC2 `wayakit-app-server` (t3.medium, AL2023, eu-north-1). Casi siempre está apagada.
- La encienden dos schedules de EventBridge Scheduler del grupo `default`: jueves y viernes a las 21:50.
- Dentro de la instancia, el crontab de `ec2-user` lanza `run_pipeline.sh` el viernes 04:00 UTC y `--stage 2` el sábado 04:00 UTC (jueves y viernes 22:00 CDMX):
  - la **etapa 1** prepara la lista de búsqueda;
  - entre etapas se hace una revisión manual por git;
  - la **etapa 2** hace `git pull`, scrapea, entrena y sube a Odoo.
- Al terminar, la instancia se apaga sola con `trap EXIT`.

**Hallazgos que condicionan el plan:**

| # | Hallazgo | Dónde |
|---|---|---|
| 1 | **No existe un scraper de Noon.** En B2C hoy solo hay Amazon (amazon.sa, Selenium), Mumzworld y SACO. Para Pets **solo se usa Amazon** | `scraper/config.py:9-24` |
| 2 | El modelo **no usa** la fuente; desde e25e2c9 sí usa `company` (la marca en Amazon, Mumzworld y SACO). No se captura el vendedor ni el país de origen. Hoy no hay forma de separar importado de local | `ml_model/2_train_models.py:160-169` |
| 3 | En la foto de 2024, la mediana de Pets era de unos 262 SAR/L, contra unos 50 en Home. El 39 % de los tamaños venían en fl oz (indicio de importado). Con n ≤ 2 por tipo, el recorte p10–p90 no hace nada. **Corrección (2026-11-17):** eso vale con 1 precio o con 2 iguales; con **2 precios distintos el recorte deja el grupo vacío** (hallazgo 12, bug 6) | `2_train_models.py:46-60` |
| 4 | Las ventas de Odoo están **fijas a 2025** | `ml_model/odoo_api_cotizations.py:75-76` |
| 5 | El estado (CSV, `last_full_run.txt`, modelos y logs) vive solo en el disco de la EC2. Hay un snapshot manual del 2026-11-17 (F1) | `run_pipeline.sh:7-24` |
| 6 | La etapa 2 hace `git pull` sin verificar el resultado. Muchos errores terminan con código 0 (`exit()`, `except` que se tragan el error) | `run_pipeline.sh:133`, `odoo_api_price_suggestion.py:52-109` |
| 7 | Hay 6 copias de `get_secret()`. Todas leen `prod/wayakit-app` | `ml_model/odoo_api_*.py`, `scraper/...` |
| 8 | La instancia no tiene IP fija ni userData. Desde F1 (2026-11-17) está registrada en SSM. No existe un proveedor OIDC de GitHub en IAM | AWS |
| 9 | La consulta a Odoo **no filtra por compañía (KSA/MX)** | `ml_model/odoo_api_*.py` |
| 10 | **La instancia corre `server-improvement`** (e25e2c9, "model improvement"), que cambia el entrenamiento, la predicción y `run_pipeline.sh` (modo parcial deshabilitado). `pi-mejoras` se adelantó a e25e2c9 el 2026-11-17 | F1 (instancia) |
| 11 | **La última corrida completa (13–15 nov 2026) falló en el scraping y aun así subió a Odoo.** El scraping llegó al timeout de 30 h; `competitors_complete.csv` quedó con 53 filas (SACO Automotive 36, AeroSense 14, Fine Airports 3; nada de Amazon, Home ni Pets). El pipeline entrenó, subió en modo full (borró las sugerencias anteriores) y terminó con código 0. Las sugerencias actuales de Odoo salen casi solo de `competitor.product`. GoGreen falló muchas veces con "Could not set language to English" | F1, `logs/pipeline_execution.log` |
| 12 | **Las pruebas de propiedades (2026-11-17) encontraron 11 bugs** en los parsers de tamaño y en el preprocesado y entrenamiento. Cada uno tiene una prueba `xfail(strict=True)` con su contraejemplo. Detalle y fase en [Bugs de las pruebas de propiedades](#bugs-de-las-pruebas-de-propiedades-2026-11-17) | `tests/unit/`, `tests/property/` |
| 13 | **La corrida local no reproduce el reporte de producción.** Con los datos congelados de F1 y el mismo código (e25e2c9), cambian 520 de 738 `predicted_price`, hasta 51.86 SAR; los procesados salen iguales y `to_predict` solo difiere en ruido de flotantes (~1e-14). Causa en investigación: CPU/BLAS, versiones de scikit-learn o numpy en la instancia, o que esa corrida no fuera e25e2c9. Por eso las pruebas diferenciales comparan **base contra versión nueva en la misma máquina**, nunca contra salidas de producción | `data/f1_antes_2026-11-15/`, `tests/differential/` |

### Bugs de las pruebas de propiedades (2026-11-17)

Encontrados por `pi-tester` al escribir las pruebas unitarias y de propiedades del código actual (e25e2c9). No se arreglaron: cada uno queda como `xfail(strict=True)` con el contraejemplo mínimo; cuando se arregle, la prueba pasa y hay que quitar el `xfail`. Los parsers de `scraper/utils.py` los usan Amazon, Mumzworld, SACO, Fine, GoGreen y OfficeSupply: arreglarlos cambia filas de Mumzworld y SACO, así que necesita la prueba de regresión de F6 y una decisión de Mafer (pregunta 13).

| # | Bug | Contraejemplo | Dónde | Fase |
|---|---|---|---|---|
| 1 | `litre` no se reconoce como litro (la rama busca "liter") | `'1 litre'` → unit `litre`, normalized 1.0 (debe ser L y 1000); `'6 x 1 litre'` → normalized 6 | `scraper/utils.py:3` (`parse_volume_string`), `:64` (`parse_volume_with_multiplier`) | F7 (regresión F6) |
| 2 | `kilograms` se lee como gramos ("gram" está dentro de "kilograms" y esa rama va antes) | `'2 kilograms'` → unit `g`, normalized 2.0 (debe ser kg y 2000) | `scraper/utils.py:3` | F7 (regresión F6) |
| 3 | Separador de miles | `'Shampoo 1,000 ml'` → quantity 0.0 (la regex toma solo "000") | `scraper/utils.py:3` | F7 (regresión F6) |
| 4 | Decimal sin cero inicial | `'Bleach .5 L'` → quantity 5.0 (5 L en lugar de 0.5 L) | `scraper/utils.py:3` | F7 (regresión F6) |
| 5 | `1a` manda `kg` y `litre` al modelo por pieza: solo reconoce `ml`, `l`, `fl oz` y `g` como volumen | `'Powder 2 kg'` de 30 SAR → `competitor_unit_processed.csv` con total_quantity 2 y price_per_item 15 | `ml_model/1a_preprocess_data.py:79-80` | F7 (correcciones chicas) |
| 6 | **El recorte p10–p90 vacía los grupos con 2 precios distintos** (límites 1.1 y 1.9 para [1.0, 2.0]; el filtro es inclusivo y no queda nada) | En los datos de F1, **14 de 151 tipos volumétricos y 1 de 15 unitarios se quedan sin ningún competidor** en el entrenamiento | `ml_model/2_train_models.py:46-60` | **F7, directo para Pets** (n chico por tipo; "recorte robusto cuando n es pequeño") y F5 (contar cuántos tipos de Pets lo sufren) |
| 7 | Los SKU repetidos duplican cotizaciones (el `merge` por `Product_ID`) | Una cotización de `P1` con `P1` dos veces en productos → 2 filas. En F1: 4 SKU repetidos, 162 cotizaciones → 163 filas | `ml_model/2_train_models.py:37` (`prepare_wayakit_training_data`) | F7 (y la limpieza de SKU de la sección 7.3) |
| 8 | `process_volumetric_data` no descarta cantidades ≤ 0 (el unitario sí) | `ml`, total_quantity −500, precio 1 → volume_liters −0.5 y price_per_liter −2.0, que entran al entrenamiento | `ml_model/1a_preprocess_data.py:39-59` | F7 (correcciones chicas, C18) |
| 9 | **Posible bug (confirmar con Mafer):** el "3X" de concentración se toma como 3 piezas | `'Detergent 3X concentrate 500 ml'` → 1500 ml | `scraper/utils.py:64` | F7, según la pregunta 12 |
| 10 | Menor: notación científica en el multiplicador (solo con totales < 1e-4 o ≥ 1e16, poco realista) | `'(2 Bottles x 0.00001 ml)'` → quantity 2e-05 pero normalized 5.0 | `scraper/utils.py:64` | Sin fase (anotado) |
| 11 | Menor: números gigantes en los parsers de conteo lanzan `ValueError` (límite de dígitos de `int()` en Python ≥ 3.10.7); irreal en títulos | `'9'*4301 + ' wipes'` | `scraper/utils.py:31` (`parse_count_string`) | Sin fase (anotado) |

### Seguridad: avisar a IT

- **Confirmado en F1:** la key de Gemini está en texto plano en `logs/cron.log` (72 líneas) y `logs/pipeline_execution.log` (87 líneas) de la instancia, por los errores HTTP de `requests` con la URL (`scraper/services/ai_service.py:54-55`). En `~/logs/wayakit_app.log*` no aparece. Acción: **revocar o rotar la key en Google ya**, depurar o borrar esos logs y, al migrar a Bedrock, quitarla de `prod/wayakit-app`.
- El rol de la instancia tiene `SecretsManagerReadWrite`: puede leer y escribir **todos** los secretos de la cuenta, incluido `voz-del-cliente/api-keys`.
- ~~Credenciales de push en el servidor~~: verificado en F1, no hay `credential.helper` ni `~/.git-credentials`, y el remoto https no lleva credenciales.

---

## 2. Decisiones de diseño (propuestas)

| Tema | Propuesta | Por qué |
|---|---|---|
| Etapas | **Una sola corrida mensual y siempre completa.** Sin etapa 2, sin modo parcial y sin `git pull`. La lista de búsqueda sale de `modifiers_mapping.csv` versionado, y cada cambio va por PR | El ticket A pide corrida mensual. Así desaparecen el conflicto de git, la mezcla de versiones entre etapas y un schedule. `server-improvement` ya va en esa dirección |
| IaC | Igual que VdC: **un script boto3 por recurso** en `infra/`, que se puede correr varias veces, con `--dry-run`, docstring en español, `config.py` compartido y etiqueta `project=price-intelligence` | Misma herramienta que VdC, como pide el ticket B |
| CI/CD | **Pull-on-boot.** GitHub Actions (OIDC, sin llaves) corre las pruebas, publica `releases/<sha>.tar.gz` en S3 y actualiza el puntero `current`. Después **enciende la instancia en modo "solo deploy"**: la instancia instala el release, verifica, cambia el symlink de forma atómica y se apaga | Funciona con la instancia apagada. Un push llega sin pasos manuales. Si el deploy falla, el symlink no se toca y la versión anterior sigue funcionando |
| Rollback | `workflow_dispatch` con un `sha`, que mueve el puntero y vuelve a correr el deploy. En la instancia se guardan los últimos 5 releases | Mismo patrón que `--rollback` de VdC |
| Estado | S3 `price-intelligence-<cuenta>`, con los prefijos `data/`, `models/`, `state/`, `logs/` y `releases/` | Para recrear la instancia sin perder nada (criterio de B) |
| Secretos | **Mantener `prod/wayakit-app`**, agregar la llave de Apify, centralizar en un solo módulo (`pi_secrets.py`) y limitar el rol al ARN de ese secreto | Es lo mínimo. Aplica si nadie más usa ese secreto (pregunta 4) |
| Subida a Odoo | Primero `create` del lote nuevo y, solo si sale bien, `unlink` del anterior (IDs guardados, se puede reintentar). Agregar `--dry-run`. Filtrar por la compañía KSA | Si algo falla, la tabla nunca queda vacía |
| Pets | Ver F7. Las correcciones salen de los datos (estadística robusta y, si el actor lo trae, el país de origen). No se usan listas que tenga que mantener una persona | Decisión de Mafer |
| IA de relevancia | **Bedrock: DeepSeek V3.2 como principal y Haiku 4.5 como respaldo**, con la API Converse, JSON schema y **siempre por lotes**. Se mantiene la interfaz de `RelevanceAgent` para que los scrapers no cambien | Decisión de Mafer y prueba comparativa del 2026-11-16 (sección 5) |

---

## 3. Fases

Fecha de referencia: 2026-11-16. Quedan unas 2 semanas para los dos tickets, así que el alcance ya viene recortado.

### F0 · Documentación base (1–1.5 días) — primero

Se usa la misma estructura de VdC. La regla: **cada cambio de pipeline o de infraestructura actualiza los docs en el mismo commit.**

- `CLAUDE.md`, para Claude:
  - qué es el proyecto;
  - reglas que no se rompen: credenciales, compañía de Odoo, OK de Mafer, Mumzworld y SACO no se tocan, ningún CSV en git;
  - mapa del código y contrato de un scraper;
  - esquema del CSV;
  - trampas conocidas;
  - convenciones de `infra/`.
- `README.md`, para personas: qué hace, arquitectura, cómo correrlo y enlaces.
- `docs/runbook.md`: operar, correr a mano, desplegar, rollback, revisar alertas, volver a subir a Odoo y una tabla de problemas conocidos (`Síntoma | Causa | Solución`).
- `docs/recursos.md`: tabla de qué existe en AWS (`Recurso | Nombre | Script | Costo`).
- `docs/decisiones.md`: tabla `Fecha | Decisión | Motivo` a la que solo se agregan filas, y una sección `## Pendiente`.
- `environment.yml` con versiones fijas, sacadas de `pip freeze` en la instancia.

### F1 · Inventario de la instancia — **hecho el 2026-11-17**

Resultado: crontab de `ec2-user` (vie y sáb 04:00 UTC), rama `server-improvement`, Python 3.10.20 (fijado en `environment.yml`), sin credenciales de git guardadas, key de Gemini en los logs, última corrida fallida (hallazgo 11). Datos "antes" en `data/f1_antes_2026-11-15/` (ignorada por git): `last_full_run` 2026-11-13 04:00:09, `run_mode` full. Los schedules se pausaron durante el inventario y se reactivaron.

Plan original:

1. Tomar un **snapshot del volumen**.
2. Deshabilitar los schedules.
3. Encender la instancia.
4. Revisar el cron (si tiene `@reboot`, apagarlo antes de que dispare), la rama en checkout, los cambios locales, el entorno de conda, las credenciales de git y el log de Gemini.
5. Copiar los CSV y los modelos actuales a S3. Esa es la **foto "antes"** para el ticket A.

### F2 · Endurecer el pipeline (2 días)

- `|| exit 1` en cada paso (no usar `set -u`, porque rompe conda) y `sys.exit(1)` en los scripts de Odoo.
- No borrar `competitors_complete.csv` antes de tener el nuevo: escribir en un archivo temporal y renombrarlo.
- Una sola etapa mensual. Quitar `git pull`.
- `pi_secrets.py`.
- `ai_service.py` pasa de Gemini a Bedrock, por lotes y con la misma interfaz. Necesita `infra/bedrock_profile.py` y el permiso `bedrock:InvokeModel` en el rol de la instancia.
- Subida segura a Odoo con `--dry-run` y filtro por compañía.
- Ventas de Odoo de los últimos 12 meses móviles, en lugar de 2025 fijo.
- **Antes de empezar F2:** subir a la base, en un PR aparte que revisa Mafer, el setup de verificación (`REVIEW.md`, `.claude/`, `scripts/`, `tests/`, `pytest.ini`, `docs/checklist-confiabilidad.md`), sin código de producción. Sin eso, "las reglas se leen de la base" no funciona (skill `equipo`).
- **Paridad del entorno (hallazgo 13):** comparar las versiones de la instancia (numpy, scikit-learn, threadpoolctl/BLAS, CPU) con `environment.yml` y confirmar qué código hizo la corrida del 13–15 nov, hasta explicar por qué la corrida local da otros 520 precios. Mientras tanto, ninguna prueba compara contra salidas de producción.
- Los bugs del hallazgo 12 no se arreglan en F2 (no son de endurecimiento); sus pruebas `xfail` estrictas quedan como recordatorio.

### F3 · `infra/` (3 días) — cada script se corre con OK de Mafer

| Script | Qué deja |
|---|---|
| `config.py` | Perfil, región, etiqueta, nombres y cron |
| `bucket.py` | Bucket de estado y releases: cifrado, sin acceso público, ciclo de vida |
| `instance_role.py` | Mínimo privilegio: el secreto de PI, el bucket, `AmazonSSMManagedInstanceCore` y `PutMetricData`. Reemplaza `SecretsManagerReadWrite` |
| `ec2_instance.py` | **Adopta** la instancia actual o **la crea** (`--create`) desde AL2023 con un userData que corre `bootstrap.sh` (conda, Chrome, systemd, agente SSM). Exige IMDSv2 |
| `bootstrap.sh` | `pi-deploy.service` (instala el release) y `pi-pipeline.service` (corre el pipeline, salvo en modo "solo deploy") |
| `schedule.py` | Grupo `price-intelligence` y schedule mensual. Los dos schedules de `default` se retiran por medio de la lista `RETIRED` |
| `scheduler_role.py` | Solo `ec2:StartInstances` sobre esta instancia |
| `deploy_role.py` | Proveedor OIDC de GitHub y un rol limitado a `repo:<org>/wayakit-price-prediction:ref:refs/heads/main` |
| `check_pi.py` | Revisión de solo lectura: `[OK]`, `[FALLA]` o `[AVISO]`. Cubre el test del ticket "cambiar un schedule desde código y verlo en AWS" |

Para después, fuera del plazo: `alarms.py` y `budget.py`. Mientras tanto se usa un aviso por SNS desde el trap de salida.

### F4 · CI/CD (2 días)

- `ci.yml`:
  - lint;
  - `scripts/verify.sh rapido` (pytest con los marcadores `unit` y `property`: convertidores de Apify, contrato de scrapers, infra con MagicMock como hace VdC);
  - smoke del ML con datos sintéticos;
  - gitleaks.
- `deploy.yml` (solo en main y con CI en verde): empaqueta → S3 → puntero → enciende la instancia en "solo deploy" → espera la señal de OK → listo.
- Pruebas del ticket:
  - un cambio menor llega a EC2;
  - un deploy roto a propósito no cambia `current`;
  - el rollback a un SHA anterior funciona.

### F5 · Diagnóstico de Pets (1 día), en paralelo a F3/F4

Con la foto de F1 (`data/f1_antes_2026-11-15/`; ojo: esa corrida no trajo filas de Pets del scraping, así que Pets se explica casi solo con `competitor_products_from_odoo.csv`), para cada tipo de producto de Pets:
- n antes y después del recorte;
- mediana, p75 y máximo de SAR/L;
- % de tamaños en fl oz;
- número de marcas;
- % de multipacks;
- filas con precio 0 o con 5 mL o menos;
- volumen de los SKU de Wayakit frente al rango de la competencia;
- razón entre el precio sugerido y el costo y el precio actual en Odoo **KSA**.

### F6 · Apify para Amazon y Noon (3 días)

1. **Elegir el actor** de Amazon KSA y el de Noon KSA (investigación del 2026-11-16; resultado en la sección 5).
2. **Corrida de calibración** (con OK por el gasto): 1 término de Pets, unos 20 resultados y tope de USD 0.50 por actor. Sirve para fijar el mapeo de campos (precio, tamaño, marca, vendedor, origen) y el costo real.
3. Copiar de VdC (`pipeline/apify.py`) el cliente con reintentos, paginación y tope de gasto. Escribir `amazon_apify.py` y `noon_apify.py` con la firma actual `scrape(keyword, search_mode) -> list[dict]`. Filtros: precio > 0, multiplicador de pack, `is_relevant`.
4. Columnas nuevas `seller` y `origin` en `CSV_COLUMNS` y `row_data`.
5. **Prueba de regresión:** Mumzworld y SACO con las mismas keywords, antes y después. Deben salir iguales.

### F7 · Ajuste de Pets y ML (2 días)

- `origin` = `local`, `imported` o `unknown`, **solo con lo que trae el dato**: país de origen o fabricante en las especificaciones del actor, o el texto "Made in KSA" / "صنع في السعودية". Si no viene, queda `unknown`. No hay listas de marcas mantenidas a mano.
- **Solo en Pets:**
  - si hay al menos N filas locales por tipo, se usan esas filas;
  - si no, se usa la estadística robusta del mercado (mediana, sin valores extremos, sin multipacks ni tamaños de 5 mL o menos) con un techo de `p75 × volumen`;
  - el precio nunca baja del piso de margen de 1.30.
- Correcciones chicas: precio 0, `kg` tratado como volumétrico, recorte robusto cuando n es pequeño. Las cubren los bugs 5, 6, 7 y 8 del hallazgo 12 (con sus pruebas `xfail` estrictas). **El bug 6 es directo para Pets:** con 2 precios distintos por tipo, el recorte p10–p90 deja el tipo sin competidores (14 de 151 tipos volumétricos en F1). Los bugs de parsers (1–4 y 9) entran aquí solo si Mafer decide cambiar el parser compartido (pregunta 13), con la regresión de F6.
- **Decisión documentada sobre el modelo** (lo pide el ticket): las demás mejoras (target en log, GroupKFold) quedan para una fase 2. Mueven los precios de todas las líneas. La fuga de `approved_quote_price` como feature ya se quitó en e25e2c9 (`2_train_models.py:160`), junto con el recorte al rango de mercado con 3 competidores o más (`3_predicted_prices.py:126-132`).
- Comparación antes/después por subindustria: mediana de la razón nuevo/viejo y % de sugerencias por encima del p75 local.

### F8 · Corrida final y Odoo KSA (1 día + revisión)

1. Corrida completa con `--dry-run`.
2. Puya y Luisa revisan la lista de Pets: precio anterior, nuevo, p75 local y costo.
3. Con su visto bueno, y con la compañía KSA confirmada, se sube con el esquema seguro.

**Orden:** F0 → F1 → F2 → (F3 → F4) en paralelo con (F5 → F6 → F7) → F8.

---

## 4. Preguntas abiertas (para Mafer)

1. ~~¿Qué rama corre la instancia?~~ **Corrección (F1):** corre `server-improvement` (e25e2c9), no `main`. Ver la pregunta 11.
2. ~~¿Eliminar las 2 etapas?~~ Mafer quiere una corrida mensual, pero **la revisión de keywords sí hace falta**: los sitios no encuentran el producto con el nombre genérico (ej. en SACO hay que buscar "car cleaner" en lugar de "car disinfectant"). Pendiente: elegir el diseño de la revisión (propuesta en la sección 6).
3. ~~¿Qué día?~~ El último jueves de cada mes: `cron(50 21 ? * 5L *)` en EventBridge Scheduler. Hora 21:50 en horario de CDMX (`America/Mexico_City`), confirmado por Mafer el 2026-11-17.
4. ~~¿Secreto y compañía?~~ Solo PI usa `prod/wayakit-app`. La base de Odoo que usa PI es solo de KSA (Mafer, 2026-11-17).
5. ~~¿Cuenta de Apify?~~ La de VdC, con un plan de mayor nivel. Pendiente: tope de USD por corrida (propuesta: USD 20).
6. ¿Alcance de Apify: Amazon solo para Pets, o también para Home y Automotive? ¿Noon en qué subindustrias?
7. ~~¿Qué cuenta como "local"?~~ No se define a mano (ver F7).
8. ~~¿Admin de GitHub?~~ Mafer es admin de la organización. El repo ya está en la organización (`Wayakit-Biotechnology-in-Hygiene/wayakit-price-prediction`). Pendiente: ¿El deploy necesita aprobación manual?
9. ~~¿Snapshot y encendido?~~ Hecho el 2026-11-17 (F1): snapshot, acceso por SSM, inventario y datos copiados a `data/f1_antes_2026-11-15/`. La instancia corre `server-improvement`, no `main`.
11. ¿Se rebasa `pi-mejoras` sobre `server-improvement` (lo que corre en producción) o se descarta e25e2c9?
10. ¿Qué hacemos con los restos en me-south-1 (2 roles y el secreto `wayakit/test/credentials`)? No se pudieron inventariar desde aquí.
12. ¿Un "3X" de concentración en el título ("Detergent 3X concentrate 500 ml") debe multiplicar el volumen? Hoy da 1500 ml (hallazgo 12, bug 9). Es un supuesto de las pruebas, no una regla confirmada.
13. Los bugs de parsers del hallazgo 12 (1–4) están en `scraper/utils.py`, que también usan Mumzworld y SACO. ¿Se arreglan (cambian filas de Mumzworld y SACO, con la regresión de F6 y las diferencias aprobadas) o se dejan y se corrigen solo en los adaptadores nuevos de Apify?

---

## 5. Actores de Apify y cambio a Bedrock (investigación del 2026-11-16)

La información sale de la Store pública de Apify y de la API pública de cada actor. No se corrió ningún actor ni se usó ningún token. Cada recomendación la revisó por separado un segundo agente.

### Amazon KSA: `junglee/amazon-crawler` (confirmado)

- **Por qué este actor:**
  - Es el mismo actor que VdC usa para Amazon MX y lo mantiene Apify.
  - En 30 días tuvo un 99.5 % de corridas exitosas, unos 3,600 usuarios y LIMITED_PERMISSIONS.
  - Para amazon.sa basta con pasar la URL `https://www.amazon.sa/s?k=…`. El changelog tiene un arreglo explícito para `.sa`.
- **Campos que trae:**
  - `price{value,currency}`, que es el precio actual, y `listPrice` por separado;
  - `brand`, `seller.name`, `asin` y `url`;
  - `attributes` / `attributesMapped`: Manufacturer, Volume, Unit Count y Country of Origin cuando Amazon los muestra;
  - `categoryPageData.isSponsored` y `loadedCountryCode`.
- **Input:**
  - `categoryOrProductUrls`: una URL de búsqueda por keyword;
  - `maxItemsPerStartUrl: 50`, `scrapeProductDetails: true`, `language: "en"`;
  - `maxOffers: 0`, `scrapeSellers: false`.
  - **No pasar `countryCode` ni `zipCode`**: suman USD 0.05 por resultado, unos USD 137 al mes.
- **Costo:**
  - USD 0.004 por resultado en el plan Starter (BRONZE). Con o sin detalle cuesta lo mismo.
  - 55 keywords × 50 resultados ≈ **USD 11 por corrida mensual**.
- **En el convertidor:**
  - descartar los patrocinados (`categoryPageData.isSponsored`);
  - validar `loadedCountryCode == "SA"`;
  - normalizar la moneda, que puede llegar como "SAR", "ر.س" o un símbolo.

### Noon KSA: `saswave/noon-product-scraper` (decisión de Mafer: el mismo de VdC, que ya funciona)

- Busca con `search_urls` (`https://www.noon.com/saudi-en/search/?q=…`) y saca el detalle con `product_urls`, igual que VdC.
- No trae la keyword ni la moneda. Por eso se corre **una vez por keyword**, y la moneda se toma como SAR por el dominio `/saudi-en/`.
- Costo: unos USD 0.0009 por resultado, unos USD 2.5 al mes solo con la búsqueda.
- Lo que trae cada fila se confirma con la corrida de calibración.

Alternativa que se evaluó y **no se usa**: `memo23/noon-scraper`.

- **Lo que tenía a favor:**
  - Busca por keyword con `country: "saudi"`. El valor por defecto es UAE, así que hay que pasarlo siempre.
  - Trae `currency`, `salePrice` y `price`; el precio actual es `salePrice ?? price`.
  - También trae `brand`, `storeName` (vendedor), `sku`, `productUrl` y `sourceQuery`, la keyword que produjo cada fila. El actor de VdC no trae ese último dato.
  - Con `scrapeDetails: true` agrega `specifications`.
  - En 30 días tuvo un 99.8 % de corridas exitosas.
  - Riesgo: es un actor nuevo (de septiembre de 2026) y sin reseñas.
- **Input:** `{"searchQueries": ["<keyword>"], "country": "saudi", "maxItems": 50, "scrapeDetails": true}`, con **una corrida por keyword**, porque `maxItems` es un tope para toda la corrida.
- **Costo:** USD 0.0008 por fila más USD 0.005 por arranque, unos **USD 2.5 por corrida mensual**.
- **País de origen:** no hay un campo propio. Solo puede venir dentro de `specifications`.

### Corrida de calibración (2026-11-16, keyword "pet urine odor remover")

Los datos crudos quedaron fuera del repo, en el scratchpad de la sesión. Costo: unos USD 0.08.

**Amazon (`junglee`, 20 resultados, USD 0.08):**

| Qué se revisó | Resultado |
|---|---|
| País del sitio | 20/20 con `loadedCountryCode = SA` |
| Moneda | 18/20 en `SAR`. Las otras 2 venían sin precio y se descartan |
| Patrocinados | 0 |
| Marca y vendedor | Marca 20/20, vendedor 18/20 |
| Tamaño | Viene en `Item Volume`, `Unit Count` y `Size`, pero **muchas veces no coinciden entre sí** (ej. "5 Liters" contra "907 Grams"). Hay que conservar la regla actual de que 2 de 3 fuentes coincidan, o resolverlo con el modelo de IA |
| `Country of Origin` | **Solo 3 de 20** (Saudi Arabia, China, Germany) |

**Lo más útil para el diagnóstico de Pets:**
- Los resultados están dominados por marcas de EE. UU. (Simple Solution, Rocco & Roxie, Nature's Miracle). Se venden por revendedores de importación como **Desertcart SA** y MigaStore, a precios de **140 a 720 SAR por 0.9–5 L**.
- El producto de Wayakit sale a 48.83 SAR por 700 mL.
- El vendedor es mejor señal de importado que el país de origen, porque casi siempre viene lleno.
- **El propio producto de Wayakit sale en los resultados**: hay que excluir marca y vendedor Wayakit, como ya hace VdC.

**Noon (`saswave`, búsqueda más 5 detalles):**

| Qué se revisó | Resultado |
|---|---|
| Filas | Devolvió 50 filas aunque el tope era 20: el actor entrega la página completa |
| Campos de la búsqueda | `price` y `sale_price` (precio actual = `sale_price` o, si no hay, `price`), `brand` (41/50), `store_name` y `sku`/`url` |
| Detalle | `specifications` vacío en 3 de 5. No trae país de origen |
| Importado | En el detalle aparece `shipping_banner_details.global` (envío "global", es decir, importado). Hay que confirmar si sirve como bandera de importado |
| Relevancia | Hay resultados fuera de tema (purificadores de aire, corrales para perro), así que el filtro de IA es indispensable |
| Otros vendedores | Wayakit y SACO también venden en Noon |

**Conclusión para F7:** el país de origen casi nunca viene. Para separar importado de local se usan señales que sí vienen siempre: el vendedor (revendedores de importación conocidos, sacados de los datos), el envío "global" de Noon, las unidades en fl oz y el país de origen cuando aparece. Todo sale de los datos, sin listas a mano.

**Gasto en Apify:** unos **USD 14 al mes** (Amazon unos USD 11 y Noon unos USD 2.5). Va en la cuenta de VdC, con un plan de mayor nivel.

### Bedrock en lugar de Gemini

- **Interfaz:** `RelevanceAgent` conserva `is_relevant()` y `extract_wipes_units()`, así que los scrapers de Selenium (Mumzworld, SACO, B2B) no cambian. Se agregan `filter_relevant(titles, query)` y `extract_wipes_units_batch(titles)`, que solo usan los adaptadores de Apify.
- **Llamada a Bedrock:**
  - Converse en eu-north-1, con `temperature=0`;
  - salida JSON forzada con `outputConfig` (JSON schema). DeepSeek no admite `toolConfig` en Converse; se probó en la prueba comparativa;
  - **25 títulos por llamada**;
  - caché por (keyword, título).
- **Modelos (investigación del 2026-11-16, solo lectura en la cuenta).** Mafer prefiere uno barato que no sea de Claude.

  | Modelo | Dónde corre | USD por millón de tokens (entrada / salida) | Al mes | JSON schema en Converse | Nota |
  |---|---|---|---|---|---|
  | **DeepSeek V3.2** (`deepseek.v3.2`) | eu-north-1, on-demand | 0.74 / 2.22 | ~USD 3.9 | Sí | **Elegido como principal** (prueba del 2026-11-16) |
  | Qwen3 235B A22B 2507 (`qwen.qwen3-235b-a22b-2507-v1:0`) | eu-north-1, on-demand | 0.22 / 0.88 | ~USD 1.2 | Sí | No pasó la regla: 3 puntos debajo de Haiku y falló en casos claros |
  | gpt-oss-20b (`openai.gpt-oss-20b-1:0`) | eu-north-1 | 0.07 / 0.30 | ~USD 0.5 | Sí | Opción de costo mínimo. Razona y cobra esos tokens |
  | Gemma 3 | **No está en eu-north-1** por Converse; solo en eu-west-1 | — | — | 12B sí | Descartado por ahora |
  | Nova Micro/Lite | Perfil `eu.*` | 0.04–0.07 / 0.15–0.26 | ~USD 0.3 | No | Muy barato; reglas finas dudosas |
  | Claude Haiku 4.5 (referencia) | Perfil `eu.*` | 1.10 / 5.50 | ~USD 6.3 | Sí | — |

  - Qwen y DeepSeek corren solo en Estocolmo, no necesitan Marketplace y ya aparecen como `AVAILABLE/AUTHORIZED`. **La primera invocación acepta el EULA del proveedor.**
  - Cada uno tiene cuota propia: 10,000 RPM.
  - Corrección: en eu-north-1, Haiku 4.5 también tiene 10,000 RPM. Los 10 RPM que menciona VdC son de eu-west-1.
  - **Prueba comparativa (2026-11-16, aprobada por Mafer; con ella se aceptaron los EULA de Qwen y DeepSeek).**
    - Datos: 66 títulos únicos de la calibración (keyword "pet urine odor remover", Amazon y Noon). Claude los etiquetó con las reglas del prompt actual y marcó 7 casos dudosos.
    - Modos: lotes de 22, un título por llamada (20 títulos) y 10 títulos de wipes armados a mano con conteo conocido.
    - Regla de decisión: se queda el más barato que quede a 2 puntos o menos de exactitud de Haiku, con 100 % de JSON válido.

    | Modelo | Exactitud | Precisión | Recall | JSON válido | Wipes exactos | Lote = un título | Latencia del lote (p50) | Costo de la prueba |
    |---|---|---|---|---|---|---|---|---|
    | Haiku 4.5 | 98.5 % | 96.9 % | 100 % | 24/24 | 10/10 | 100 % | 1.3 s | USD 0.024 |
    | **DeepSeek V3.2** | **97.0 %** | **100 %** | 93.5 % | 24/24 | 10/10 | 85 % | 5.2 s | USD 0.010 |
    | Qwen3 235B | 95.5 % | 96.7 % | 93.5 % | 24/24 | 10/10 | 85 % | 4.6 s | USD 0.004 |

    - DeepSeek **pasa la regla** (1.5 puntos debajo de Haiku). Sus 2 errores son casos dudosos: un removedor para patio y otro para piso duro, que rechazó por la regla de superficie especializada. **Nunca aceptó un producto irrelevante.** Eso es lo que más importa, porque un falso positivo contamina el precio de referencia.
    - Qwen rechazó 2 productos que claramente sí eran relevantes y aceptó un limpiador de tapicería genérico.
    - DeepSeek y Qwen no siempre dan la misma respuesta en lote que título por título (85 %). Por eso conviene usar **siempre el modo lote**, también en B2B (lotes de 1 a N).
    - **Decisión:** DeepSeek V3.2 como principal. Haiku 4.5 como respaldo, solo si DeepSeek falla o devuelve JSON inválido; ya está habilitado en la cuenta para VdC.
    - Limitaciones: 1 keyword, solo títulos en inglés, etiquetas hechas por Claude y wipes armados a mano. En la primera corrida real hay que revisar una muestra.
    - Scripts y resultados crudos: `scratchpad/calib/bakeoff.py` y `bakeoff_results.json` (temporales). El script se pasa al repo como `tests/bakeoff_relevance.py` en F2.
  - La primera llamada con un schema nuevo puede tardar hasta unos 90 s, porque compila la gramática: hay que poner un timeout de 120 s.
- **Cuota:**
  - Ya no es una restricción: los modelos candidatos tienen 10,000 RPM en eu-north-1. Aun así se usan lotes de 25 títulos para ahorrar tiempo y tokens.
  - Con lotes, Apify baja de unas 5,500 llamadas a unas 220. B2B también va por lotes: los títulos de cada búsqueda se juntan, porque el modelo responde distinto en lote y título por título.
  - La mensual de PI (último jueves) no coincide con la de VdC (primer domingo).
- **Infraestructura:**
  - `infra/bedrock_profile.py`: perfiles de aplicación con la etiqueta `project=price-intelligence` para DeepSeek V3.2 y Haiku 4.5, si Bedrock lo permite para DeepSeek (se verifica en F2). Si no lo permite, el costo se separa por modelId en Cost Explorer;
  - en `instance_role.py`: `bedrock:InvokeModel` solo sobre `deepseek.v3.2`, el perfil de Haiku y sus modelos.
- **Errores:**
  - reintentos con backoff ante `ThrottlingException`;
  - **fallar en voz alta**: hoy, si falta la key, todo sale "no relevante" y el pipeline sigue. Si los fallos de la IA pasan del 5 %, el pipeline debe salir con código 1.
- **Costo:** unos USD 4 al mes con DeepSeek V3.2.
- **Después del corte:** quitar `GEMINI_API_KEY` de `prod/wayakit-app` y revocar la key.

---

## 6. Revisión de keywords sin frenar la corrida (propuesta del 2026-11-17)

**Estado: en revisión.** Respuesta de Mafer (2026-11-17): no le funciona "una sola fuente de verdad", porque se agregan productos nuevos todo el tiempo (baby, home, etc.) y hay que revisar cómo buscar cada uno en cada sitio activo. El problema es de recall: el producto sí existe en el sitio (ej. SACO), pero con el nombre de nuestro producto no sale y hay que buscarlo de otra forma. Le gustó el reporte de cobertura y pidió una propuesta mejor que el PR automático: es la v2 de abajo.

**El problema:**
- Cada sitio encuentra el producto con palabras distintas. Por ejemplo, en SACO "car disinfectant" no sale y "car cleaner" sí.
- Hoy eso se resuelve así:
  - `scraper/modifiers_mapping.csv` guarda la keyword por sitio (`sitio:keyword`, 149 filas; hoy solo para fine, gogreen, officesupply y aerosense);
  - además, alguien edita `analysis-odoo.csv` entre la etapa 1 y la 2, lo que frena la corrida hasta que esa revisión termine.
- Las ediciones hechas en `analysis-odoo.csv` se pierden en la siguiente corrida completa si no se copian a `modifiers_mapping.csv`.

**Propuesta v1 (descartada el 2026-11-17):** keywords en un solo CSV, reintento durante la corrida y un PR con lo que funcionó. Mafer la rechazó porque reacciona tarde: los productos nuevos se buscan mal en la primera corrida y la revisión llega después.

**Propuesta v2: descubrir cómo buscar cada producto *antes* de la corrida mensual.**

1. **Una semana antes (el jueves anterior al último jueves), chequeo automático.** La instancia se enciende en modo "descubrimiento" y arma la lista de pendientes:
   - **solo los tipos de producto nuevos en Odoo** desde la corrida anterior (baby, home, etc.). Así el costo mensual se queda bajo (decisión de Mafer, 2026-11-17);
   - los producto × sitio que dieron 0 a 2 resultados relevantes **no** se revisan cada mes: solo se marcan en el reporte de cobertura.
   - **Revisión completa cada 6 meses:** todos los productos del mapping, en todos los sitios activos, con el mismo proceso. Cubre los cambios de vocabulario de los sitios y los casos marcados en el reporte.
2. **Candidatos de búsqueda de tres fuentes,** por cada producto × sitio activo:
   - **el vocabulario del propio sitio:** sugerencias del buscador (autocompletar) y nombres de categoría de productos parecidos que ya se encontraron. Es cómo el sitio llama a las cosas; ej. SACO dice "car cleaner";
   - **DeepSeek:** sinónimos en inglés y árabe, a partir del nombre y la descripción del producto en Odoo;
   - **lo que ya funcionó** en ese sitio para productos parecidos (memoria de meses anteriores).
3. **Se prueban de verdad.** Cada candidato se busca en el sitio (solo la primera página, que es barato) y se cuentan los resultados que DeepSeek juzga equivalentes a nuestro producto. Gana el que trae más.
4. **Hoja de revisión** (solo con los pendientes; se esperan pocos al mes). Por cada producto × sitio muestra la keyword actual, la ganadora, cuántos relevantes trajo y 3 ejemplos con liga. Quien revisa aprueba, cambia la keyword o marca "no existe en este sitio".
5. **El día de la corrida:**
   - se usa lo aprobado;
   - si nadie revisó, la ganadora automática si trajo 3 o más relevantes; si no, la keyword de siempre;
   - todo queda marcado en el reporte de cobertura, que alimenta el chequeo del mes siguiente.
6. **Las keywords se guardan por producto × sitio,** con historial y quién aprobó. Hoy eso es `modifiers_mapping.csv`, pero ya nadie lo edita a mano: se llena desde la hoja de revisión.

**Dónde revisar (por decidir con Mafer):**
- **(a) GitHub:** la hoja como PR o como CSV. Gratis e inmediato, pero solo es cómodo para IT.
- **(b) Hoja compartida:** Google Sheets o similar. Cómoda para Ventas; necesita una cuenta de servicio y un secreto.
- **(c) Odoo:** un campo "keywords por sitio" en el tipo de producto, donde ya trabaja quien crea los productos. Es lo mejor a largo plazo, pero requiere desarrollo de un módulo.

Mafer prefiere Odoo (2026-11-17). Antes de diseñarlo se estudia el módulo `price_intelligence` de Odoo (repo `wayakit/wayakit`).

**Fases:** reporte de cobertura (paso 1) en F2 · descubrimiento y prueba de candidatos (2–3) en F6, junto con Apify · hoja de revisión y modo "descubrimiento" (4–5) en F4. Esfuerzo adicional estimado: unos 3–4 días. Parte de esto queda fuera del alcance del ticket A, así que conviene un ticket aparte.

---

## 7. Odoo: keywords de búsqueda y enlace con los productos (propuesta del 2026-11-17)

Sale del estudio del módulo `price_intelligence` (repo `wayakit/wayakit`, rama `mainTest`; abajo, `PIM/`), con un diseño revisado por un segundo agente contra el código. Hasta que Mafer lo apruebe es solo propuesta: no se tocó Odoo.

### 7.1 Cómo está hoy

**Taxonomía**
- Industry → Sub-industry → **Type of product** (`product.classification`).
- El nombre del tipo es la única llave única (`PIM/models/product_classification.py:49-51`).
- Las columnas Sub industry, Industry y Generic product type que lee PI se derivan del tipo. Generic product type es texto libre (`:21`).

**`product.master`, el SKU de PI.** Vive aparte de los productos de Odoo y se enlaza de tres formas distintas:

| Enlace | Dónde se usa | Problema |
|---|---|---|
| `product_id` (Char) = SKU = `default_code` | PI, pestaña Financials (`wayakit_customization/models/sale_order_financials.py:38-56`), `product.price.suggestion.product_id_str` | **No es único**: hay 6 SKU repetidos (12 de 1,927 masters, según los datos de F1). Al ser texto, si el SKU cambia en un lado, se rompe |
| `external_id` (Char, xml-id de `product.template`) | Solo el precio del e-commerce y la búsqueda por nombre (`PIM/models/product_master.py:151-191`, `:255-360`, `:802`) | Se resuelve a mano; si el xml-id cambia, falla en silencio |
| `uploaded_in_odoo` (Bool manual) | Informativo | Nadie lo mantiene; se puede calcular |

- `product.price.suggestion` tampoco tiene relación: todo es texto (`PIM/models/product_price_suggestion.py:5-56`).
- Solo `competitor.product` tiene `company_id`.

**Granularidad de las keywords:** **por tipo de producto × sitio**, no por SKU.
- Hay 1,921 SKU en 410 tipos (mediana: 4 SKU por tipo).
- Dar de alta un SKU de un tipo que ya existe no pide keywords nuevas. Solo un **tipo nuevo** las necesita.

### 7.2 Propuesta A: keywords en Odoo

**Modelos nuevos en `PIM/`**
- **`pi.search.site`:** reemplaza `TARGET_MAP`. Campos: `code` (amazon, saco…), `name`, `active` y una relación M2M con `product.subindustry`.
  - Como enlaza por id, desaparece el error de mayúsculas de "Land Transportation".
  - Los 7 sitios iniciales se cargan como datos `noupdate`.
- **`pi.search.keyword`:** una fila por tipo × sitio × keyword.
  - **Estado:** `proposed`, `approved`, `rejected`, `not_on_site` o `legacy_base`.
  - **Origen:** `manual`, `discovery` o `csv_import`.
  - **Medición:** `relevant_count`, ejemplos como texto ("título — URL") y `last_discovery`, que **es lo que decide el barrido semestral**.
  - La keyword se guarda **tal cual** (sin pasarla a minúsculas, porque eso cambia las búsquedas de hoy) y se deduplica sin distinguir mayúsculas.

**En el tipo de producto**
- Una pestaña **"Search Keywords"** en el formulario.
- Un badge **pendiente / completo** en la lista, con un filtro "Keywords pendientes".

**Menú "Price Intelligence > Keywords por revisar"**
- Es una lista editable de propuestas: se corrige la keyword en la misma fila y se aprueba, rechaza o marca "No existe en el sitio" con un clic.
- El descubrimiento deja **una actividad por tipo** al revisor configurado.

**No es obligatorio llenarla al dar de alta.** Quien crea un tipo puede escribir keywords si ya sabe cómo se busca. Si no, el descubrimiento las propone. Así no se frena a Ventas.

**Qué es "nuevo" en el descubrimiento mensual**
- Un tipo sin ninguna keyword.
- Un sitio recién activado.

Para que la migración no dispare un barrido completo el primer mes, se crea una fila base por cada par activo: `approved` con el nombre base en retail, y `legacy_base` en B2B sin keyword.

**Integración con PI**
- **Lectura:** PI lee sitios, tipos y keywords por XML-RPC y arma el mismo `analysis-odoo.csv`. Así `modifiers_mapping.csv` y la edición manual se retiran.
- **Escritura:** PI solo escribe propuestas, con un método del servidor que nunca aprueba ni borra.
- **Si Odoo no responde, PI sale con código 1.**

**Migración:** un script exporta las 213 keywords actuales del CSV como archivo de importación. Mafer lo importa primero en staging y luego en producción.

**Esfuerzo:** unos 3–4 días (módulo 1.5–2 días, migración 0.5, PI lee de Odoo 1). El descubrimiento ya está contado en la sección 6.

### 7.3 Propuesta B: enlace limpio entre `product.master` y los productos de Odoo

- **Campo nuevo `product_tmpl_id`** (M2O a `product.template`) en `product.master`:
  - se llena automáticamente por SKU (`default_code`) con un backfill;
  - también se llena cuando el producto se crea en Odoo después que el master, que hoy es lo normal (`uploaded_in_odoo`).
  - Los masters que no encuentren su producto quedan en un filtro "Sin enlazar".
- **`uploaded_in_odoo` pasa a calcularse** (`bool(product_tmpl_id)`).
- **`external_id` se retira:** el precio del e-commerce y la búsqueda se hacen por el M2O. Antes se valida que el backfill cubra a todos los que hoy tienen `external_id`.
- **SKU único:**
  - primero un reporte de los 6 SKU repetidos para que Mafer decida cuál queda;
  - luego `unique(product_id)`.
- **`product.price.suggestion` gana `master_id`** (M2O). PI lo escribe y se conserva `product_id_str` por compatibilidad. La pestaña Financials puede seguir por SKU o pasar al M2O.
- **No se fusiona `product.master` con `product.template`.** El master tiene costos, tiers y productos que todavía no existen en Odoo. Lo correcto es enlazarlos, no mezclarlos.

**Esfuerzo:** unos 2 días, más la limpieza de los duplicados por parte de Mafer.

### 7.4 Dónde vive cada pieza y riesgos

- **Código del módulo:** `wayakit/wayakit`, rama `mainTest` → staging en odoo.sh → `main`.
- **Datos** (revisor, grupos, importación, limpieza de SKU): en producción, a mano por Mafer.
- **Cambios de PI:** esta rama.

**Riesgos**
- Subir la versión del módulo y probar el upgrade en staging antes de producción, en una ventana sin corrida de PI.
- El Viewer necesita lectura en los modelos nuevos.
- Los permisos del usuario de la API siguen sin verificar.
- Mumzworld y SACO cambian de dónde leen su configuración, así que hace falta la prueba de regresión de F6.
- Al activar Noon, se descubren de golpe unos 120 pares (costo de Apify).

**Tickets abiertos el 2026-11-17** (aprobados por Mafer):
- [Price Intelligence en Odoo: keywords de búsqueda por sitio](https://app.notion.com/p/41bbe65f8c1381dd8f54c3b0077953c3) (propuesta A): estimación 8, vence el 2026-12-10.
- [Price Intelligence en Odoo: enlazar product.master con productos](https://app.notion.com/p/41bbe65f8c1381248fd3fbb2a177d536) (propuesta B): estimación 5, vence el 2026-12-15.

Las fechas quedan después de los tickets A y B, porque las semanas de noviembre ya están cargadas.
