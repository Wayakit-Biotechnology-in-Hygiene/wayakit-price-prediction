# Recursos

Inventario de lo que existe para Price Intelligence (PI). Cuenta de Wayakit, región **eu-north-1** (Estocolmo) salvo que se indique otra cosa. Actualizado el **2026-11-16**.

`<cuenta>` = ID de la cuenta de AWS de Wayakit (`aws sts get-caller-identity`). Nunca se escribe en el repo.

**Regla:** al agregar un recurso → script en `infra/` + fila aquí + paso en `docs/runbook.md` + fila en `docs/decisiones.md` si aplica. Cada cambio de pipeline o de infraestructura actualiza estos docs en el mismo commit.

Estado general: **todo lo que existe hoy se creó a mano** (consola o CLI) y no hay ningún script en `infra/`. Todavía no se creó ni se cambió nada en AWS como parte del plan ([plan.md](plan.md)). El inventario se hizo en modo solo lectura con el perfil de AWS CLI `wayakit-vdc`. No se verificó qué etiquetas tienen los recursos actuales, además de `Name`. Los recursos nuevos llevarán la etiqueta `project = price-intelligence`.

## Lo que existe hoy

| Recurso | Nombre | Cómo se creó | Estado | Costo aprox. |
|---|---|---|---|---|
| Instancia EC2 | `wayakit-app-server` (tag `Name`). t3.medium, Amazon Linux 2023, key pair `wayakit-stockholm-key`. Sin userData, sin Elastic IP, IMDSv2 no verificado | A mano | `stopped` casi siempre. La encienden los schedules; se apaga sola con `sudo shutdown -h +1` en el `trap EXIT` (`run_pipeline.sh:52`). Corre la rama **`server-improvement`** (verificado en F1, 2026-11-17). Adentro: crontab de `ec2-user` (vie y sáb 04:00 UTC), Python 3.10.20, conda `wayakit_env`, Chrome 146. Registrada en SSM (agente 3.3.4108.0) | Por horas encendida (hoy 2 arranques por semana; la etapa 2 puede durar hasta 30 h). Monto pendiente de medir en Cost Explorer |
| Volumen EBS (raíz de la instancia) | gp3 de 25 GB | A mano, con la instancia | **Sin cifrar.** Es el único lugar donde viven los CSV, los modelos `.joblib`, `last_full_run.txt` y los logs | Pendiente de medir (precio de gp3 por GB-mes) |
| Snapshot del volumen raíz | `pi-f1-antes-inventario` (etiquetas `Name` y `project=price-intelligence`) | A mano, 2026-11-17 (F1, OK de Mafer) | Respaldo antes del inventario y de los cambios del plan | ~USD 1 / mes |
| Security group | `launch-wizard-1` | A mano (asistente de la consola) | Solo abre SSH (22/tcp) a 2 direcciones fijas. Ningún otro puerto | Sin costo |
| Perfil de instancia / rol de IAM | `Wayakit-EC2-Secrets-Role`, con las políticas gestionadas `SecretsManagerReadWrite` y `AmazonSSMManagedInstanceCore` (esta última agregada el 2026-11-17, F1, OK de Mafer) | A mano | **Demasiado amplio**: puede leer y escribir todos los secretos de la cuenta, incluido `voz-del-cliente/api-keys`. Acceso por SSM habilitado | Sin costo |
| Rol de IAM (Scheduler) | `Wayakit-EventBridge-Role`, con la política gestionada `AmazonEC2FullAccess` | A mano | **Demasiado amplio**: solo necesita encender una instancia | Sin costo |
| EventBridge Scheduler | Grupo `default`: `Wayakit-Start-Thursday-Night` (`cron(50 21 ? * 5 *)`) y `Wayakit-Start-Friday-Night` (`cron(50 21 ? * 6 *)`), zona America/Mexico_City. Destino `ec2:startInstances` sobre `wayakit-app-server` | A mano | Activos (se pausaron durante el inventario de F1 y se reactivaron el 2026-11-17). Solo encienden la instancia; adentro, el crontab de `ec2-user` lanza `run_pipeline.sh` a las 04:00 UTC (22:00 CDMX) | Sin costo (capa gratuita) |
| Secreto | `prod/wayakit-app`. Llaves: `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_TOKEN` (`ml_model/odoo_api_products.py:41-44`) y `GEMINI_API_KEY` (`scraper/services/ai_service.py:41`) | A mano | Activo, sin rotación. El nombre y la región están fijos en 6 copias de `get_secret()`. Solo lo usa PI (Mafer, 2026-11-17). **La key de Gemini se filtró a los logs de la instancia** (ver Gemini) | ~USD 0.40 / mes |
| Restos en **me-south-1** | Roles `EC2WayakitSecretsAccessRole` y `EventBridge-EC2-Wayakit-Role`; secreto `wayakit/test/credentials` (el que se usaba antes de 2026-04-05) | A mano | **No inventariados**: el endpoint de la región no respondió. Los roles se usaron por última vez en marzo de 2026. Qué hacer con ellos es la pregunta 10 del plan | Desconocido |

Lo que **no existe** hoy (verificado en solo lectura, salvo donde se indica):

- Ningún bucket de S3 de PI (el único bucket de la cuenta es de VdC).
- Ningún grupo de CloudWatch Logs de PI.
- Alarmas, temas SNS y presupuestos de PI: no se revisaron en el inventario; no se conoce ninguno. Se confirma en F1 (y después con `infra/check_pi.py`).
- Ninguna Lambda ni regla de EventBridge de PI.
- **No hay proveedor OIDC de GitHub** en IAM.

Inventario dentro de la instancia: **hecho el 2026-11-17 (F1)**. Ver `docs/runbook.md` (Disparo y Correr a mano), `environment.yml` y las filas del 2026-11-17 en `docs/decisiones.md`. No hay credenciales de git guardadas.

## Lo planeado

Nada de esta tabla existe todavía. Cada script se corre con OK de Mafer, será idempotente y tendrá `--dry-run`, como en VdC. Los nombres marcados "por definir" se fijan en `infra/config.py`.

| Recurso | Nombre | Script | Estado | Costo aprox. |
|---|---|---|---|---|
| Configuración compartida | Perfil, región, etiqueta, nombres y cron | `infra/config.py` | Planeado (F3) | — |
| Bucket S3 de estado y releases | `price-intelligence-<cuenta>`. Prefijos `data/`, `models/`, `state/`, `logs/` y `releases/` (más el puntero `current`). Cifrado, sin acceso público, ciclo de vida | `infra/bucket.py` | Planeado (F3) | Pendiente de estimar; centavos con el volumen esperado |
| Rol de la instancia (mínimo privilegio) | Por definir: rol nuevo o el actual con una política nueva. Reemplaza `SecretsManagerReadWrite` | `infra/instance_role.py` | Planeado (F3); el permiso de Bedrock se necesita desde F2 | Sin costo |
| Instancia EC2 | **Adopta** `wayakit-app-server` o **la crea** (`--create`) desde AL2023 con un userData que corre `infra/bootstrap.sh` (conda, Chrome, systemd, agente SSM). Exige IMDSv2 | `infra/ec2_instance.py` + `infra/bootstrap.sh` | Planeado (F3) | Igual que hoy, menos horas: una corrida al mes más los encendidos de deploy |
| Servicios en la instancia | `pi-deploy.service` (instala el release) y `pi-pipeline.service` (corre el pipeline, salvo en modo "solo deploy"; el mecanismo para indicar ese modo está por definir) | `infra/bootstrap.sh` | Planeado (F3) | — |
| EventBridge Scheduler | Grupo `price-intelligence` con un schedule mensual, **último jueves de cada mes**: `cron(50 21 ? * 5L *)`. Hora y zona horaria pendientes (hoy CDMX; ¿Asia/Riyadh?). Los dos schedules de `default` se retiran con la lista `RETIRED` | `infra/schedule.py` | Planeado (F3) | Sin costo (capa gratuita) |
| Rol del Scheduler | Por definir. Solo `ec2:StartInstances` sobre esta instancia. Reemplaza `Wayakit-EventBridge-Role` | `infra/scheduler_role.py` | Planeado (F3) | Sin costo |
| Proveedor OIDC de GitHub + rol de deploy | Rol por definir, con confianza limitada a `repo:<org>/wayakit-price-prediction:ref:refs/heads/main`. Lo usa `deploy.yml` (sin llaves de largo plazo) | `infra/deploy_role.py` | Planeado (F3, se usa en F4). Falta saber quién es admin de la organización de GitHub (pregunta 8) | Sin costo |
| Acceso a Bedrock | `bedrock:InvokeModel` sobre `deepseek.v3.2` (on-demand en eu-north-1, principal) y sobre el perfil `eu.` de Claude Haiku 4.5 (respaldo), o sobre perfiles de aplicación propios con la etiqueta `project = price-intelligence` | `infra/instance_role.py` (+ `infra/bedrock_profile.py` si se usan perfiles de aplicación) | Planeado (F2). Ver la nota de abajo | Por uso; ver "Servicios externos" |
| Llave de Apify | Una llave nueva dentro de `prod/wayakit-app` (nombre de la llave por definir) | Paso manual (valor en consola, por Mafer) | Planeado (F6) | Sin costo adicional |
| Aviso de fin de corrida | Tema SNS publicado desde el `trap EXIT` (nombre por definir) | Por definir | Planeado (F3) | Sin costo (primeros 1,000 correos) |
| Verificación | Revisión de solo lectura (`[OK]`, `[FALLA]`, `[AVISO]`); cubre el test "cambiar un schedule desde código y verlo en AWS" | `infra/check_pi.py` | Planeado (F3) | — |
| Alarmas y presupuesto | — | `infra/alarms.py`, `infra/budget.py` | Fuera del plazo de los tickets; mientras tanto, el aviso por SNS | — |

Nota sobre Bedrock: la decisión vigente (2026-11-16) es DeepSeek V3.2 como principal y Haiku 4.5 como respaldo. En F2 se verifica si Bedrock permite crear perfiles de aplicación etiquetados sobre DeepSeek; si no, el costo se separa por modelId. Lo que se decida se anota en `docs/decisiones.md`.

Fuera de los scripts (pasos manuales):

| Recurso | Dónde | Quién | Estado |
|---|---|---|---|
| Acuerdos (EULA) de DeepSeek V3.2 y Qwen3 | Bedrock, eu-north-1 | Aceptados con la primera invocación de la prueba comparativa, con autorización de Mafer | Hecho (2026-11-16) |
| Subir de nivel el plan de Apify (cuenta de VdC) | Apify Console | Mafer | Decidido; nivel y fecha pendientes |
| Valor de la llave de Apify | Secrets Manager, `prod/wayakit-app` | Mafer, consola | Planeado (F6) |
| Snapshot del volumen antes de encender la instancia | EC2 | Con OK de Mafer | Hecho (2026-11-17) |
| **Revocar o rotar la key de Gemini** (filtrada en los logs) y depurar `logs/cron.log` y `logs/pipeline_execution.log` en la instancia | Google (proyecto de la key: pendiente de identificar) e instancia | IT / Mafer | **Urgente** (confirmado en F1, 2026-11-17). Quitar `GEMINI_API_KEY` del secreto al terminar la migración a Bedrock (F2) |
| Environment y permisos de GitHub para OIDC | GitHub, organización de Wayakit | Mafer (admin de la organización) | Pendiente: el repo está en una cuenta personal (pregunta 8) |

## Servicios externos

### Odoo (KSA)

**Confirmar la compañía antes de leer o escribir.** Hoy ninguna consulta filtra por `company_id` ni pasa `allowed_company_ids`: que todo sea KSA se infiere (precios en SAR, scrapers `.sa`, la versión de México vive en otras ramas), pero **no está forzado en el código**. Falta confirmar si la base de `prod/wayakit-app` es solo KSA o compartida con MX, y a qué compañía pertenece el usuario de la API (pregunta 4). Filtrar por la compañía KSA es parte de F2.

Conexión: XML-RPC con API token, credenciales en `prod/wayakit-app`.

| Operación | Modelo | Dónde | Nota |
|---|---|---|---|
| Lee | `product.master` (activos, sin Maritime ni Defense) | `ml_model/odoo_api_products.py:70` | Productos de Wayakit y costos (`unit_cost_sar`) |
| Lee | `sale.order.line` (`order_id.state = sale`, `name like '[FP-%'`) | `ml_model/odoo_api_cotizations.py:68` | **Fechas fijas en 2025** (`:75-76`); pasa a 12 meses móviles en F2 |
| Lee | `competitor.product` (todos los registros) | `ml_model/odoo_api_competitor_products.py:91` | Productos de competencia cargados en Odoo |
| Escribe | `product.price.suggestion.history` (`create`) | `ml_model/odoo_api_price_suggestion.py:19`, `:144` | Copia de las sugerencias anteriores (solo en modo completo) |
| Escribe | `product.price.suggestion` (en modo completo, `unlink` de todo y luego `create`; en modo parcial, solo `create`) | `ml_model/odoo_api_price_suggestion.py:18`, `:151`, `:172` | Si el `create` falla en modo completo, la tabla queda vacía. Planeado (F2): primero `create`, después `unlink`, con `--dry-run` |
| Solo autentica | — | `scraper/odoo_api_connection_products.py` | Se conecta pero no consulta nada |

La subida final de Pets la revisan Puya y Luisa antes de escribir en Odoo KSA (F8).

### Apify

| Campo | Valor |
|---|---|
| Cuenta | La de VdC (decisión de Mafer). El plan Starter se sube de nivel para cubrir el gasto de PI; el nivel nuevo está pendiente |
| Actores | `junglee/amazon-crawler` (Amazon KSA, amazon.sa) y `saswave/noon-product-scraper` (Noon KSA), los mismos que usa VdC |
| Costo aprox. | ~USD 14 / mes: Amazon ~USD 11 (55 keywords × 50 resultados a USD 0.004) y Noon ~USD 2.5. No pasar `countryCode` ni `zipCode` en junglee (sumaría ~USD 137 / mes) |
| Tope por corrida | Pendiente (propuesta: USD 20; pregunta 5) |
| Corrida de calibración | Hecha el 2026-11-16, keyword "pet urine odor remover", ~USD 0.08 |
| Token | Hoy PI no tiene token de Apify. Se guardará en `prod/wayakit-app` (F6). Si es un token separado del de VdC dentro de la misma cuenta está por definir |
| Estado | Planeado (F6). Mumzworld y SACO siguen con Selenium |

Ojo: PI comparte el límite de gasto mensual de la cuenta con VdC. El límite de la cuenta y la guardia mensual están en la sección de Apify de `docs/runbook.md` del repo `voz-del-cliente`.

### Amazon Bedrock (relevancia de productos)

| Uso | Modelo | Dónde | Precio (USD por 1M tokens, entrada / salida) | Al mes (aprox.) |
|---|---|---|---|---|
| Principal | DeepSeek V3.2 (`deepseek.v3.2`) | eu-north-1, on-demand | 0.74 / 2.22 | ~USD 4 |
| Respaldo (solo si DeepSeek falla o devuelve JSON inválido) | Claude Haiku 4.5 (perfil `eu.`) | Perfil de inferencia de la UE | 1.10 / 5.50 | ~USD 6.3 si se usara para todo |

Estado: **planeado (F2)**. Converse con `temperature=0`, salida JSON forzada con `outputConfig` (JSON schema), lotes de 25 títulos y timeout de 120 s. Cuota en eu-north-1: 10,000 peticiones/min por modelo. Detalle de la prueba comparativa en la sección 5 de [plan.md](plan.md).

### Gemini (se retira)

| Campo | Valor |
|---|---|
| Uso hoy | Relevancia (`gemini-2.5-flash-lite`) y extracción de unidades de wipes (`gemini-2.5-pro`), en `scraper/services/ai_service.py:54-55` |
| Credencial | `GEMINI_API_KEY` en `prod/wayakit-app` |
| Riesgo | **Confirmado (F1):** la key está en texto plano en `logs/cron.log` (72 líneas) y `logs/pipeline_execution.log` (87 líneas) de la instancia, porque va en la URL y los errores HTTP se registran con ella. En `~/logs/wayakit_app.log*` no aparece. Revocar o rotar ya y avisar a IT |
| Costo | No documentado (pendiente: identificar el proyecto de Google y su facturación) |
| Estado | Se reemplaza por Bedrock en F2. Después: **revocar la key**, quitar `GEMINI_API_KEY` del secreto y limpiar los logs |

## Permisos por rol

| Rol | Hoy | Planeado |
|---|---|---|
| Rol de la instancia | `Wayakit-EC2-Secrets-Role`: `SecretsManagerReadWrite` (todos los secretos de la cuenta, lectura y escritura) y `AmazonSSMManagedInstanceCore` (desde 2026-11-17) | (F3) `secretsmanager:GetSecretValue` solo sobre el ARN de `prod/wayakit-app`; el bucket de PI; `AmazonSSMManagedInstanceCore`; `cloudwatch:PutMetricData`; publicar en el tema SNS del aviso. (F2) `bedrock:InvokeModel` sobre DeepSeek V3.2 y Haiku 4.5 |
| Rol del Scheduler | `Wayakit-EventBridge-Role`: `AmazonEC2FullAccess` | (F3) Solo `ec2:StartInstances` sobre esta instancia |
| Rol de deploy (GitHub Actions) | No existe | (F3/F4) Confianza OIDC solo para `main` del repo; publicar en `releases/` del bucket, mover el puntero `current` y encender la instancia en modo "solo deploy". Alcance exacto por definir en `infra/deploy_role.py` |
| Roles en me-south-1 | `EC2WayakitSecretsAccessRole`, `EventBridge-EC2-Wayakit-Role`: permisos no inventariados | Por decidir (pregunta 10) |
