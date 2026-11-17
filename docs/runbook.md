# Runbook — Price Intelligence

Guía práctica para operar Price Intelligence (PI). Cada sección resuelve una tarea concreta.
Para saber *qué* existe en AWS, ver [recursos.md](recursos.md). Para saber *por qué* está así, ver [decisiones.md](decisiones.md). El plan de mejoras está en [plan.md](plan.md).

Estado al 2026-11-16: la operación descrita aquí es la de **hoy** (rama `main` en la instancia). Lo que dice **Planeado (F#)** todavía no existe; el número es la fase de [plan.md](plan.md). En AWS no se ha creado ni cambiado nada para el plan.

Los comandos de AWS se corren desde la raíz del repo, en una Mac con AWS CLI v2. Los del pipeline se corren dentro de la instancia EC2.

---

## Antes de empezar

### Una sola vez por máquina

1. Tener usuario en IAM Identity Center con acceso a la cuenta de Wayakit.
2. Configurar el perfil SSO `wayakit-vdc`, igual que en VdC (ver `voz-del-cliente/docs/runbook.md`, sección *Antes de empezar*):

   | Pregunta | Respuesta |
   |---|---|
   | SSO region | `eu-north-1` |
   | Default client Region | `eu-north-1` (la CLI propone `us-east-1`; no aceptarlo) |
   | Profile name | `wayakit-vdc` |

3. Para entrar a la instancia se usa **SSM** (desde 2026-11-17). Instalar el [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) de la AWS CLI. Ya no hace falta la llave SSH (`wayakit-stockholm-key`).

### Cada día, o cuando expire la sesión

```bash
aws sso login --profile wayakit-vdc
aws sts get-caller-identity --profile wayakit-vdc
```

- **Usar siempre `--profile wayakit-vdc`, nunca el perfil `default`**: en las Mac del equipo `default` usa llaves de acceso de largo plazo.
- Región: **`eu-north-1` (Stockholm)**. En la consola, revisar el selector de región antes de buscar algo. Hay restos de la región anterior, me-south-1 (2 roles y el secreto `wayakit/test/credentials`), que no se pudieron inventariar (pregunta 10 del plan).
- Las credenciales de Odoo y de Gemini viven en el secreto `prod/wayakit-app` (llaves `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_TOKEN` y `GEMINI_API_KEY`). Nunca imprimir ni copiar los valores.

---

## Cómo corre hoy

### Disparo

| Schedule (EventBridge Scheduler, grupo `default`) | Expresión | Zona horaria | Qué hace |
|---|---|---|---|
| `Wayakit-Start-Thursday-Night` | `cron(50 21 ? * 5 *)` | America/Mexico_City | Enciende la instancia `wayakit-app-server` (jueves 21:50 CDMX) |
| `Wayakit-Start-Friday-Night` | `cron(50 21 ? * 6 *)` | America/Mexico_City | Enciende la instancia (viernes 21:50 CDMX) |

- Los schedules **solo encienden** la instancia (destino `ec2:startInstances`, rol `Wayakit-EventBridge-Role`). No hay Lambda ni reglas de EventBridge de PI.
- Dentro de la instancia, el **crontab de `ec2-user`** (no está en el repo; verificado en F1, 2026-11-17) lanza las etapas. El repo está en `/home/ec2-user/wayakit-price-prediction`, rama **`server-improvement`**:
  ```
  0 4 * * 5 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh >> .../logs/cron.log 2>&1
  0 4 * * 6 /home/ec2-user/wayakit-price-prediction/run_pipeline.sh --stage 2 >> .../logs/cron.log 2>&1
  ```
  En UTC: viernes y sábado 04:00 = **jueves y viernes 22:00 CDMX**, 10 minutos después de que los schedules encienden la instancia. No hay `@reboot`, timers de systemd propios ni `rc.local`: encender la instancia a otra hora no lanza nada.
- Al terminar, cualquier salida del script (éxito o error) dispara `trap EXIT`, que programa `sudo shutdown -h +1` (`run_pipeline.sh:52`). No hay `StopInstances` por API.
- Si la etapa 1 sigue corriendo cuando llega el schedule del viernes, ese `StartInstances` no hace nada (la instancia ya está encendida). Como el cron es a hora fija, la etapa 2 se lanza igual el sábado 04:00 UTC aunque la etapa 1 no haya terminado (las dos correrían a la vez).
- Planeado (F3): un solo schedule mensual, el último jueves de cada mes (`cron(50 21 ? * 5L *)`), en el grupo `price-intelligence`. A las 21:50 en horario de CDMX (decisión de Mafer, 2026-11-17).

### Etapa 1: preparar la lista de búsqueda (`run_pipeline.sh:65-109`)

1. La corrida es **siempre completa**: desde e25e2c9 el modo parcial está deshabilitado (`IS_FULL_RUN="true"`, `run_pipeline.sh:67-68`). `logs/last_full_run.txt` se sigue escribiendo, pero ya no decide nada.
2. Activa conda (`wayakit_env`) y corre `ml_model/odoo_api_products.py` → `ml_model/wayakit_products.csv` (`run_pipeline.sh:86`).
3. `scraper/odoo_api_connection_products.py` genera `scraper/analysis-odoo.csv` desde los productos de Odoo y `scraper/modifiers_mapping.csv`, y escribe `run_mode.txt=full` y `last_full_run.txt` (`run_pipeline.sh:90-93`).
4. La rama parcial (`ml_model/filter_new_products.py` → `ml_model/wayakit_new_products_temp.csv`, `run_mode.txt=partial`, `run_pipeline.sh:95-100`) sigue en el código, pero ya no se alcanza.
5. Sale y la instancia se apaga.

En la etapa 1, cada paso lleva `|| exit 1`: si algo falla, la etapa termina con error (y se apaga igual).

### Etapa 2: scraping, entrenamiento y subida a Odoo (`run_pipeline.sh:116-215`)

1. Lee `logs/run_mode.txt`; si no existe, sale con error (`run_pipeline.sh:122-128`).
2. `git pull` de la rama que esté en checkout, **sin revisar el resultado** (`run_pipeline.sh:133`).
3. Completa (`run_pipeline.sh:144-167`):
   - **borra** `scraper/competitors_complete.csv` (línea 151) y hace scraping con `timeout 30h` (línea 154);
   - lee de Odoo las cotizaciones y los productos de competencia (155-156);
   - corre `1a_preprocess_data.py`, `1b_preprocess_data.py --run_mode full`, `2_train_models.py` y `3_predicted_prices.py` (163-166), cada uno con `timeout 1h`;
   - sube a Odoo con `odoo_api_price_suggestion.py --run_mode full` (167).
4. Parcial (`run_pipeline.sh:172-202`). Sigue en el código, pero desde e25e2c9 la etapa 1 siempre escribe `run_mode.txt=full`, así que no se alcanza:
   - si `wayakit_new_products_temp.csv` tiene 1 línea o menos, no hace nada (178-180);
   - si no, genera `scraper/analysis-odoo_partial.csv` (187), hace scraping en modo `append` con `timeout 4h` (188) y sigue igual que la completa, pero con `--run_mode partial` (189-201).
5. Sale y la instancia se apaga.

En la etapa 2, **los pasos no llevan `|| exit 1`**: si uno falla, los siguientes corren igual con los archivos que haya. Ver [Problemas conocidos](#problemas-conocidos).

La rama `origin/server-improvement` (e25e2c9) desactiva el modo parcial y cambia el entrenamiento y la predicción. La instancia corre **`server-improvement`** (verificado en F1, 2026-11-17), y `pi-mejoras` ya está en e25e2c9: las líneas de este documento son de ese commit.

### Archivos que produce

Todo vive **solo en el disco de la instancia** (volumen sin snapshots). Las rutas son relativas a la raíz del repo; los scripts asumen que el directorio de trabajo es esa raíz.

| Archivo | Lo escribe | Etapa |
|---|---|---|
| `logs/pipeline_execution.log` | `run_pipeline.sh` (salida de todos los pasos, línea 12) | 1 y 2 |
| `logs/last_full_run.txt`, `logs/run_mode.txt` | `run_pipeline.sh` (líneas 10-11) | 1 |
| `../logs/wayakit_app.log` (rotación 5 MB × 3) | `log_config.py:7-8,32-34`. Sube un nivel desde la raíz del repo: queda **fuera del repo**, en el directorio padre | 1 y 2 |
| `ml_model/wayakit_products.csv` | `odoo_api_products.py` | 1 |
| `ml_model/wayakit_new_products_temp.csv` | `filter_new_products.py` (solo parcial) | 1 |
| `scraper/analysis-odoo.csv` (versionado en git) | `odoo_api_connection_products.py` (solo completa) | 1 |
| `scraper/analysis-odoo_partial.csv` | `odoo_api_connection_products.py` (solo parcial) | 2 |
| `scraper/competitors_complete.csv` | `scraper/main.py` | 2 |
| `ml_model/wayakit_cotizations.csv` | `odoo_api_cotizations.py` | 2 |
| `ml_model/competitor_products_from_odoo.csv` | `odoo_api_competitor_products.py` | 2 |
| `ml_model/competitor_volumetric_processed.csv`, `competitor_unit_processed.csv` | `1a_preprocess_data.py` | 2 |
| `ml_model/wayakit_products_to_predict_odoo.csv` | `1b_preprocess_data.py` | 2 |
| `ml_model/trained_models/*.joblib` (modelos, columnas y `*_market_stats.joblib`) | `2_train_models.py:110-111,171-178` | 2 |
| `ml_model/wayakit_prediction_report.csv` | `3_predicted_prices.py` (lo sube `odoo_api_price_suggestion.py`) | 2 |

No hay grupos de CloudWatch Logs ni bucket de S3 de PI. La foto "antes" (CSV de la corrida del 13–15 nov 2026) se copió el 2026-11-17 a `data/f1_antes_2026-11-15/` en el repo local, ignorada por git; los modelos `.joblib` quedan en el snapshot del disco. Planeado (F3): bucket `price-intelligence-<cuenta>` con `data/`, `models/`, `state/`, `logs/` y `releases/`.

### Saber si la última corrida funcionó

No hay alertas ni correos hoy. Planeado (F3): aviso por SNS desde el trap de salida.

1. **Sin encender la instancia:** en Odoo (compañía KSA, ver [Revisar resultados en Odoo](#revisar-resultados-en-odoo)), revisar la fecha de creación de los registros de `product.price.suggestion`. Si es de la última corrida completa, la subida se hizo.
2. **CloudTrail** (eu-north-1): eventos `StartInstances` de los jueves y viernes a las 21:50 CDMX confirman que los schedules dispararon. No dicen si el pipeline terminó bien.
3. **Con la instancia encendida** (ver [Correr a mano](#correr-a-mano)): leer `logs/pipeline_execution.log` completo. **No basta con buscar "Éxito"**: varios scripts terminan con código 0 aunque fallen.

---

## Revisión manual entre etapas

Cómo funciona hoy (solo en las corridas **completas**):

1. La etapa 1 (jueves) regenera `scraper/analysis-odoo.csv` en el disco de la instancia y registra "listo para revisión manual" (`run_pipeline.sh:93`).
2. Antes de la etapa 2 (viernes 21:50 CDMX), alguien revisa la lista de búsqueda (columna `Search Modifiers`), hace commit en `main` de `scraper/analysis-odoo.csv` y, para que el cambio dure, también de `scraper/modifiers_mapping.csv`. En el historial hay commits "weekly analysis-odoo edition" (6be62f5 y 7a5b506). Algunos se hicieron desde el servidor (autor "Ubuntu", noviembre de 2025).
3. La etapa 2 hace `git pull` y usa la lista revisada.

Quién revisa hoy y cómo es una pregunta abierta (pregunta 2 del plan).

Riesgos:

- La etapa 1 **reescribe en la instancia un archivo versionado**. Si alguien hizo push de su revisión, `git pull` falla con "local changes would be overwritten", el error no se revisa y la etapa 2 sigue con la lista **sin revisar**.
- Las ediciones que se hacen solo en `analysis-odoo.csv` se pierden en la siguiente completa, porque ese archivo se regenera desde `modifiers_mapping.csv`.
- En las corridas **parciales** (deshabilitadas desde e25e2c9) no había revisión: `analysis-odoo_partial.csv` se genera dentro de la etapa 2 (`run_pipeline.sh:187`).
- El `git pull` también trae cualquier cambio de código que haya en la rama en checkout (`server-improvement`), a mitad de la corrida.
- Verificado en F1: la instancia no tiene credenciales de git guardadas (sin `credential.helper` ni `~/.git-credentials`).

Mafer confirmó que **la revisión de keywords hace falta** (los sitios no encuentran algunos productos con el nombre genérico). El nuevo diseño, que no frene la corrida mensual, está en revisión (`plan.md`, sección 6).

---

## Correr a mano

> **Aviso.** Encender la instancia a otra hora no lanza el pipeline (el cron es viernes y sábado 04:00 UTC, sin `@reboot`). Pero la etapa 2 en modo completo **escribe en Odoo** y borra las sugerencias anteriores, y correr `run_pipeline.sh` apaga la instancia al terminar. Antes de encenderla:
> 1. Tener el OK de Mafer.
> 2. Si se va a trabajar cerca de los horarios del cron, [pausar la corrida](#pausar-la-corrida) (deshabilitar los dos schedules) y comentar el crontab.
>
> Hay un snapshot del disco del 2026-11-17 (`pi-f1-antes-inventario`, ver `docs/recursos.md`).

1. **Encender la instancia.** Consola de EC2 (Stockholm) → *Instances* → filtrar por `Name = wayakit-app-server` → *Instance state → Start*. La IP pública cambia en cada encendido (no tiene Elastic IP). Para verla sin la consola:
   ```bash
   aws ec2 describe-instances --profile wayakit-vdc \
     --filters "Name=tag:Name,Values=wayakit-app-server" \
     --query "Reservations[].Instances[].[InstanceId,State.Name]" --output table
   ```
2. **Entrar por SSM** (desde 2026-11-17; el rol tiene `AmazonSSMManagedInstanceCore`). Esperar 1–2 minutos a que la instancia quede "Online" en SSM. No hace falta llave ni abrir el security group:
   ```bash
   aws ssm describe-instance-information --profile wayakit-vdc --query "InstanceInformationList[].[InstanceId,PingStatus]" --output table
   aws ssm start-session --profile wayakit-vdc --target <instance-id>   # requiere el Session Manager plugin
   sudo su - ec2-user
   ```
   Para un comando suelto sin sesión interactiva, usar Run Command (`aws ssm send-command --document-name AWS-RunShellScript ...`). Así se hizo el inventario de F1.
3. **Revisar el cron y cualquier corrida en curso:**
   ```bash
   crontab -l; sudo crontab -l; ls /etc/cron.d; systemctl list-timers --all
   ps aux | grep -E "run_pipeline|python|chrome" | grep -v grep
   sudo shutdown -c    # cancela un apagado programado por el trap (tiene 1 minuto)
   ```
   Para pausar el cron, comentar sus líneas con `crontab -e` y anotar qué se cambió para restaurarlo.
4. **Preparar el entorno.** Repo en `/home/ec2-user/wayakit-price-prediction` (rama `server-improvement`), conda en `/home/ec2-user/miniconda`, entorno `wayakit_env` con Python 3.10.20 (versiones en `environment.yml`). `run_pipeline.sh:14-18` busca conda en `conda info --base` o, si no, en `/home/ec2-user/miniconda` (o en `/home/ubuntu/miniconda3` si existe `/home/ubuntu`, resto del servidor anterior; la AMI actual es Amazon Linux 2023, usuario `ec2-user`).
   ```bash
   cd /home/ec2-user/wayakit-price-prediction
   source "$(conda info --base)/etc/profile.d/conda.sh"
   conda activate wayakit_env
   git status; git branch --show-current; git log -1 --oneline
   ```
5. **Correr los pasos**, siempre desde la raíz del repo (las rutas de los scripts son relativas). Es la corrida completa de `run_pipeline.sh`, paso por paso y **sin** el apagado automático:
   ```bash
   # Etapa 1
   python ml_model/odoo_api_products.py
   python scraper/odoo_api_connection_products.py --input_odoo_products_file ml_model/wayakit_products.csv --output_analysis_file scraper/analysis-odoo.csv
   # Etapa 2 (respaldar antes el CSV de competencia: el modo overwrite lo reemplaza)
   cp scraper/competitors_complete.csv scraper/competitors_complete.bak.csv
   timeout 30h python scraper/main.py --analysis_file scraper/analysis-odoo.csv --output_mode overwrite --output_file scraper/competitors_complete.csv
   python ml_model/odoo_api_cotizations.py
   python ml_model/odoo_api_competitor_products.py
   python ml_model/1a_preprocess_data.py
   python ml_model/1b_preprocess_data.py --run_mode full
   python ml_model/2_train_models.py
   python ml_model/3_predicted_prices.py
   # Subida a Odoo: solo con OK de Mafer y la compañía KSA confirmada (ver "Volver a subir sugerencias")
   python ml_model/odoo_api_price_suggestion.py --run_mode full
   ```
   - Para probar **sin escribir en Odoo**, detenerse después de `3_predicted_prices.py` y revisar `ml_model/wayakit_prediction_report.csv`. Hoy no existe `--dry-run`. Planeado (F2).
   - Después de cada paso, revisar el final de `../logs/wayakit_app.log`: un paso puede terminar con código 0 aunque haya fallado.
   - El scraping completo dura hasta 30 h. Si se corta la sesión, el proceso muere: correrlo con `nohup … > <archivo>.log 2>&1 &` (o en `tmux`, si está instalado).
   - Correr los pasos a mano **no** actualiza `logs/last_full_run.txt` ni `logs/run_mode.txt`. Desde e25e2c9 no importa para el modo: la etapa 1 siempre escribe `run_mode.txt=full`.
   - La etapa 1 reescribe `scraper/analysis-odoo.csv`, que está versionado: `git status` lo mostrará modificado.
6. **Al terminar:** cerrar los Chrome que queden (`pkill -f chrome`), restaurar el cron, apagar la instancia (`sudo shutdown -h now` o desde la consola) y volver a habilitar los schedules si corresponde.

Correr el script completo (`bash run_pipeline.sh` o `bash run_pipeline.sh --stage 2`) también funciona, pero **apaga la instancia al terminar**.

---

## Pausar la corrida

Hoy, desde la consola: **Amazon EventBridge → Scheduler → Schedules** (Stockholm) → seleccionar `Wayakit-Start-Thursday-Night` y `Wayakit-Start-Friday-Night` → **Disable**. Para reanudar, **Enable**. Con los schedules deshabilitados, la instancia no se enciende y el cron interno no corre.

Revisar el estado (solo lectura):

```bash
aws scheduler list-schedules --group-name default --profile wayakit-vdc
aws scheduler get-schedule --group-name default --name Wayakit-Start-Thursday-Night --profile wayakit-vdc
```

- No tocar los schedules del grupo `voz-del-cliente`: son de VdC.
- Si hay que detener una corrida en curso: entrar por SSM, detener el proceso y revisar si ya empezó la subida a Odoo (paso 9 del log). Si se detiene a mitad de la subida en modo completo, la tabla de sugerencias puede quedar vacía (ver [Volver a subir sugerencias](#volver-a-subir-sugerencias)).
- Planeado (F3): `python infra/schedule.py --disable` / `python infra/schedule.py`, como en VdC. Los dos schedules de `default` se retiran con la lista `RETIRED`.

---

## Revisar resultados en Odoo

> **Confirmar la compañía antes de leer o escribir.** PI es de **KSA** (precios en SAR, scrapers de sitios .sa), pero **hoy ninguna consulta filtra por compañía**: ningún dominio usa `company_id` ni `allowed_company_ids`. No se sabe si la base de `prod/wayakit-app` es solo KSA o compartida con MX, ni a qué compañía pertenece el usuario de la API (pregunta 4 del plan). Al consultar Odoo, a mano o con el conector de Odoo desde Claude, confirmar primero que se trata de la compañía KSA.

Qué escribe PI:

| Modelo | Qué guarda |
|---|---|
| `product.price.suggestion` | La sugerencia vigente por producto: `product_id_str`, `product_name`, `product_type`, `generic_product_type`, `subindustry`, `industry`, `volume_units`, `production_cost`, `suggested_price`, `profit`, `predicted_price_per_unit`, `model_confidence`, `market_min_found`, `market_max_found`, `competitors_count` (`odoo_api_price_suggestion.py:86-102`) |
| `product.price.suggestion.history` | Copia de las sugerencias anteriores, con `original_create_date`, que se archiva antes de cada subida completa (`odoo_api_price_suggestion.py:111-146`) |

Qué revisar:

- Después de una corrida **completa**, que el número de registros de `product.price.suggestion` coincida con las filas de `ml_model/wayakit_prediction_report.csv`. Si hay del doble, se subió además en modo parcial (ver Problemas conocidos). Después de una parcial, la tabla tiene las sugerencias anteriores más las de los productos nuevos: el CSV parcial solo trae los nuevos (`1b_preprocess_data.py:23-27`).
- Que la fecha de creación sea la de la última corrida.
- `suggested_price` contra `production_cost`: el precio nunca baja de costo × 1.30 (`3_predicted_prices.py:134-136`). Desde e25e2c9, si hay 3 competidores o más, la predicción se recorta al rango `market_min_found`–`market_max_found` antes del piso (`3_predicted_prices.py:126-132`); el piso gana, así que puede quedar arriba del máximo del mercado. Con menos de 3 competidores **no tiene techo**. En Pets suele salir alto (ver [plan.md](plan.md), F5 y F7).
- `market_min_found` y `market_max_found` salen solo de competidores, sin precios de Wayakit (`2_train_models.py:177-178`).
- `competitors_count` bajo (1 o 2) quiere decir que la sugerencia se apoya en muy pocos datos y que no se aplicó el recorte al rango de mercado.
- El reporte trae además `clamped_to_market` y `volume_outside_market` (`3_predicted_prices.py:150-151`), pero `odoo_api_price_suggestion.py` no las sube: solo se ven en `ml_model/wayakit_prediction_report.csv`.

Lo que lee PI de Odoo: `product.master` (activos), `sale.order.line` (ventas confirmadas, **fijas a 2025**) y `competitor.product` (todos los registros).

---

## Volver a subir sugerencias

Sube el contenido de `ml_model/wayakit_prediction_report.csv` a `product.price.suggestion`. Se corre dentro de la instancia, desde la raíz del repo, con `wayakit_env` activo. **Escribe en Odoo: solo con OK de Mafer y con la compañía KSA confirmada.**

```bash
python ml_model/odoo_api_price_suggestion.py --run_mode full
```

| Modo | Qué hace |
|---|---|
| `--run_mode full` | Lee **todos** los registros de `product.price.suggestion` (dominio `id != 0`, sin filtro de compañía), los copia a `product.price.suggestion.history`, hace `unlink` de todos y después un `create` masivo con el CSV (`odoo_api_price_suggestion.py:110-182`) |
| `--run_mode partial` (o **sin** `--run_mode`: es el valor por defecto, línea 68) | Solo hace `create`, sin borrar. Si ya había sugerencias, quedan duplicadas |

Riesgos de hoy:

- En modo completo se **borra primero y se crea después**. Si el `create` falla, `product.price.suggestion` queda **vacía** (el historial sí queda guardado), y el script solo lo registra en el log: sale con código 0 (líneas 179-181).
- Si falla el archivado o el `unlink`, el script sale con `exit(1)` sin crear nada (líneas 158-161); si el historial ya se creó, al reintentar se vuelve a copiar.
- Para recuperar una tabla vacía con el mismo CSV: correr con `--run_mode partial` (solo crea).
- Si el CSV no existe o no tiene filas válidas, el script termina con `exit()` (código 0) sin subir nada (líneas 78 y 109).

Planeado (F2): primero `create` del lote nuevo y, solo si sale bien, `unlink` del anterior por IDs guardados (se puede reintentar), con `--dry-run` y filtro por la compañía KSA. XML-RPC no es transaccional: habrá unos segundos con duplicados, y si falla el `unlink`, se reintenta con los IDs guardados.

---

## Desplegar y rollback

### Hoy

- No hay CI/CD ni deploy. El código llega a la instancia con el `git pull` del inicio de la etapa 2 (`run_pipeline.sh:133`): la rama en checkout (`server-improvement`), sin fijar versión y sin revisar el resultado.
- Un push a `server-improvement` llega en la siguiente etapa 2 programada (viernes), si el pull no falla.
- **Rollback hoy:** `git revert` del commit en `server-improvement` y push; la siguiente etapa 2 lo trae. No hacer `git checkout <sha>` dentro de la instancia sin OK de Mafer: deja la instancia fuera de la rama y los siguientes `git pull` fallan sin aviso.

### Planeado (F4): pull-on-boot

1. GitHub Actions (OIDC, sin llaves) corre las pruebas en cada push a `main`.
2. Con CI en verde, publica `releases/<sha>.tar.gz` en S3 y actualiza el puntero `current`.
3. Enciende la instancia en modo **"solo deploy"**: la instancia instala el release, lo verifica, cambia el symlink de forma atómica y se apaga, sin correr el pipeline.
4. Si el deploy falla, el symlink no se toca y la versión anterior sigue funcionando.
5. **Rollback:** `workflow_dispatch` con un `sha`, que mueve el puntero y vuelve a correr el deploy. En la instancia se guardan los últimos 5 releases.

Esto depende de F3 (bucket, roles, proveedor OIDC de GitHub, agente SSM, `bootstrap.sh`), que todavía no existe. Los pasos exactos se escriben aquí cuando se implementen.

---

## Calibración de Apify y prueba de modelos

Las dos corridas se hicieron el 2026-11-16 desde la Mac de Mafer, con su aprobación. Los resultados están en [plan.md](plan.md), sección 5. Los scripts y los datos crudos quedaron **fuera del repo**, en el scratchpad temporal de esa sesión.

| Corrida | Cómo se corrió | Costo |
|---|---|---|
| Calibración de Apify (`calibrate.py`) | Cliente de VdC (`voz-del-cliente/pipeline/apify.py`), token desde `voz-del-cliente/api-keys` (nunca se imprime). Keyword "pet urine odor remover". `junglee~amazon-crawler` con la URL de búsqueda de amazon.sa, 20 resultados, `scrapeProductDetails: true`, sin `countryCode` ni `zipCode`, tope USD 0.15. `saswave~noon-product-scraper` con la búsqueda de `noon.com/saudi-en`, 1 página, tope USD 0.10, más el detalle de 5 productos (tope USD 0.05) | ~USD 0.08 |
| Prueba de modelos de Bedrock (`bakeoff.py`) | boto3 con el perfil `wayakit-vdc` en eu-north-1, API Converse con `temperature=0` y salida JSON con schema. DeepSeek V3.2, Qwen3 235B y Haiku 4.5 sobre 66 títulos de la calibración: lotes de 22, un título por llamada y 10 títulos de wipes. Con ella se aceptaron los EULA de DeepSeek y Qwen | ~USD 0.04 en total |

- Volver a correrlas cuesta dinero (Apify y Bedrock): pedir OK de Mafer y fijar topes por actor (`maxItems` y `maxTotalChargeUsd`).
- La cuenta de Apify es la de VdC: el gasto de PI compite con el límite mensual de esa cuenta (ver `voz-del-cliente/docs/runbook.md`, sección *Apify*).
- Planeado (F2): el script de la prueba de modelos pasa al repo como `tests/bakeoff_relevance.py`. [plan.md](plan.md) no dice si el script de calibración también se pasa al repo; pendiente de decidir (sería útil para repetir la calibración con otras keywords en F6).

---

## Cambiar una llave

Todas las credenciales de PI están en el secreto `prod/wayakit-app` (eu-north-1), como pares llave/valor. Las llaves nunca pasan por el chat, el repo, un archivo ni Notion.

1. Consola de AWS (Stockholm) → **Secrets Manager → `prod/wayakit-app` → Retrieve secret value → Edit** → pestaña *Key/value*.
2. Cambiar solo el valor de la llave que corresponde. **No borrar las otras filas.** → **Save**.
3. Si fue un reemplazo, revocar la llave vieja en su servicio.

- Hoy hay 6 copias de `get_secret()` con el nombre del secreto y la región fijos en el código. Planeado (F2): un solo módulo, `pi_secrets.py`, y la llave de Apify en el mismo secreto.
- Planeado (después de F2): al terminar el cambio a Bedrock, quitar `GEMINI_API_KEY` del secreto, revocar la key y limpiar los logs (ver Problemas conocidos, *Seguridad*).

---

## Problemas conocidos

Errores que ya aparecieron o que se ven en el código, y qué hacer. Las líneas son de e25e2c9 (`server-improvement`, lo que corre la instancia; `pi-mejoras` ya está en ese commit).

### Ejecución

| Síntoma | Causa | Solución |
|---|---|---|
| El log dice "Éxito" en un paso que falló | Varios scripts terminan con `exit()`, que devuelve 0: `odoo_api_products.py:48,54,67,126`, `odoo_api_cotizations.py:49,55,66,101`, `odoo_api_price_suggestion.py:52,56,65,78,109`. Otros atrapan la excepción y siguen (`1a_preprocess_data.py:87-90`, `2_train_models.py:182-183`) | Leer el log completo, no solo "Éxito". Planeado (F2): `sys.exit(1)` |
| La etapa 2 entrena y sube a Odoo aunque el scraping falló | Los pasos de la etapa 2 no llevan `\|\| exit 1` (`run_pipeline.sh:154-167,187-201`), y la completa borra `competitors_complete.csv` antes de scrapear (línea 151) | Revisar el log antes de confiar en las sugerencias. Planeado (F2): `\|\| exit 1` por paso y escribir el CSV nuevo en un temporal |
| La etapa 2 usó la lista sin revisar o código viejo | `git pull` falló ("local changes would be overwritten") porque la etapa 1 reescribe `scraper/analysis-odoo.csv`, y nadie revisa el resultado (`run_pipeline.sh:133`) | Ver [Revisión manual entre etapas](#revisión-manual-entre-etapas). Planeado (F2): quitar `git pull` |
| La etapa 1 y la etapa 2 corren a la vez (posible, no observado) | El cron de la etapa 2 es a hora fija (sábado 04:00 UTC) y no revisa si la etapa 1 sigue corriendo | Revisar `logs/pipeline_execution.log`. Planeado (F3): un solo schedule mensual |
| **La corrida del 13–15 nov 2026 subió sugerencias a Odoo con el scraping fallido** | El scraping llegó al `timeout 30h` y salió con error, pero `competitors_complete.csv` ya tenía solo 53 filas (SACO Automotive 36, AeroSense 14, Fine Airports 3; nada de Amazon, Home ni Pets). Los pasos siguientes no revisan el error: entrenó, subió en modo full (borró las anteriores) y terminó con código 0 | Las sugerencias actuales de Odoo salen casi solo de `competitor.product`. Planeado (F2): `\|\| exit 1` por paso, mínimo de filas por fuente y subida segura |
| `wayakit_app.log` no está en `logs/` del repo | `log_config.py:7` sube un nivel desde la raíz del repo | Buscarlo en `../logs/wayakit_app.log` |
| Un `.joblib` no carga después de actualizar paquetes | `requirements.txt` no fija versiones de pandas, numpy ni scikit-learn | No actualizar paquetes en `wayakit_env` a mano. `environment.yml` (2026-11-17) fija las versiones de la instancia (Python 3.10.20, scikit-learn 1.7.2, pandas 2.3.3, numpy 2.2.6) |

### Scraping

| Síntoma | Causa | Solución |
|---|---|---|
| La corrida termina con 0 productos y sin error | Falta `GEMINI_API_KEY` en `prod/wayakit-app` o no se pudo leer: `is_relevant` devuelve `False` para todo (`scraper/services/ai_service.py:138-139`) y solo queda el aviso "API KEY not found" | Revisar que la llave exista en el secreto (sin imprimir el valor). Planeado (F2): Bedrock y fallar si los errores de la IA pasan del 5 % |
| Se descartan productos en silencio | Ante un 429, Gemini espera 60 s; al tercer intento devuelve `False` (`ai_service.py:151-154`) | Buscar "RATE LIMIT HIT" en `wayakit_app.log`. Planeado (F2): Bedrock por lotes |
| Land transportation no tiene datos de competencia | `config.py:19` dice `'Land Transportation'` y `analysis-odoo.csv` dice `Land transportation` (31 filas). La comparación distingue mayúsculas (`main.py:333`) y no avisa | Pendiente: corregir la clave en `scraper/config.py:19` (cambio de código, con OK de Mafer) |
| 4 exclusiones de SACO no se aplican | Faltan comas en `SACO_EXCLUSIONS` (`config.py:65,70`): Python une los textos | Pendiente: agregar las comas |
| GoGreen: "Could not set language to English" muchas veces | Visto en la corrida del 13–15 nov 2026 (`gogreen_scraper.py:48`); el cambio de idioma del sitio falla | Pendiente: revisar el scraper de GoGreen (cambio de código, con OK de Mafer) |
| Quedan procesos de Chrome abiertos | En SACO, `driver.get` y `_handle_overlays` están fuera del `try` (`saco_scraper.py:122-123`) y los errores que no son Timeout no cierran el navegador | `pkill -f chrome` al correr a mano. La instancia se apaga al final de la corrida programada |
| El scraping no arranca: "No se pudo instalar ChromeDriver" | `ChromeDriverManager().install()` resuelve el driver en cada corrida y lo descarga si no está en caché (`main.py:263`) y depende de la red y de la versión de Chrome | Reintentar. En la instancia: Google Chrome 146.0.7680.177 y webdriver-manager 4.0.2 (F1) |
| Precio por litro inflado en Amazon | Sin multiplicador de pack: un "Pack of 3, 500 ml" se guarda como 500 ml con el precio de 3 (`amazon_scraper.py:80-109`). En Mumzworld, la regex de pack toma el volumen como multiplicador (`mumzworld_scraper.py:58`) | Planeado (F6/F7): multiplicador de pack en los adaptadores de Apify y estadística robusta en Pets |
| Productos de Wayakit en los resultados de Amazon y Noon | Ni el scraper de Amazon ni los actores excluyen la marca o el vendedor Wayakit (se vio en la calibración) | Planeado (F6): excluir marca y vendedor Wayakit, como VdC |

### ML y Odoo

| Síntoma | Causa | Solución |
|---|---|---|
| Las ventas de 2026 no influyen en las sugerencias | Fechas fijas de 2025-01-01 a 2025-12-31 (`odoo_api_cotizations.py:75-76`) | Planeado (F2): últimos 12 meses móviles |
| Filas con precio 0 o productos en `kg` con precios raros | Las filas `0.00` no se descartan y `kg` no está en `volumetric_units`, así que cae en precio por pieza (`1a_preprocess_data.py:55-57,79`) | Planeado (F7): correcciones chicas |
| `product.price.suggestion` vacía | En modo completo se hace `unlink` y después `create`; si el `create` falló, la tabla queda vacía (`odoo_api_price_suggestion.py:148-182`) | Volver a subir con `--run_mode partial` (ver [Volver a subir sugerencias](#volver-a-subir-sugerencias)). Planeado (F2): `create` y luego `unlink` |
| Sugerencias duplicadas | Se corrió `odoo_api_price_suggestion.py` sin `--run_mode` (por defecto `partial`) o dos veces en parcial | Volver a subir con `--run_mode full`, solo si `wayakit_prediction_report.csv` es de una corrida completa (el parcial solo trae productos nuevos) |
| Datos de otra compañía en ventas o sugerencias | Ninguna consulta filtra por compañía (`ml_model/odoo_api_*.py`) | Confirmar la compañía KSA antes de leer o escribir. Planeado (F2): filtro por compañía |

### Apify (Planeado, F6; visto en la calibración)

| Síntoma | Causa | Solución |
|---|---|---|
| Noon devuelve 50 filas aunque `maxItems` era 20 | `saswave/noon-product-scraper` entrega la página completa | Correr una vez por keyword, con tope de gasto (`maxTotalChargeUsd`), y cortar en el convertidor |
| En Amazon, los tamaños no coinciden entre `Item Volume`, `Unit Count` y `Size` (ej. "5 Liters" contra "907 Grams") | Amazon llena esos campos de forma inconsistente | Conservar la regla de que 2 de 3 fuentes coincidan, o resolverlo con el modelo de IA |
| Casi nunca viene el país de origen | `Country of Origin` vino en 3 de 20 en Amazon y no viene en Noon | Usar señales que sí vienen: vendedor, envío "global" de Noon, unidades en fl oz (F7) |

### Seguridad

| Síntoma | Causa | Solución |
|---|---|---|
| **La key de Gemini está en texto plano** en `logs/cron.log` (72 líneas) y `logs/pipeline_execution.log` (87 líneas) de la instancia (confirmado en F1). En `~/logs/wayakit_app.log*` no aparece | La key va en la URL (`ai_service.py:54-55`) y los `requests.exceptions.HTTPError` se registran con la URL completa | No copiar ni imprimir esos logs sin redactar `key=`. **Revocar o rotar la key en Google ya**, depurar o borrar los logs y avisar a IT (Mafer). Planeado (F2): Bedrock y quitar la llave de `prod/wayakit-app` |
| El rol de la instancia puede leer y escribir todos los secretos de la cuenta, incluido `voz-del-cliente/api-keys` | `Wayakit-EC2-Secrets-Role` tiene `SecretsManagerReadWrite` | Avisar a IT. Planeado (F3): `instance_role.py` con mínimo privilegio |
