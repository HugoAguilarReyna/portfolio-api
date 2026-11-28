# Data Incident Commander · agente supervisor con LangGraph

**Fecha:** 27 de noviembre de 2026
**Alcance:** primera integración real de agentes y orquestación en
`HugoAguilarReyna/portfolio-api` y `HugoAguilarReyna/portafolio-hugo-aguilar`.
No incluye despliegue a producción.

## 1. Qué se implementó

El endpoint existente `POST /api/v1/incident-commander/runs/{run_id}/agent` ya no
usa un bucle `for` artesanal. Ahora ejecuta un **grafo LangGraph** que hace
explícito el estado de la investigación:

```text
preparar → modelo ─┬─ herramienta → modelo
                   ├─ final (recomendación pendiente de revisión humana)
                   └─ parada (presupuesto, tiempo o evidencia insuficiente)
```

El modelo elige una herramienta de solo lectura, el servidor valida el nombre y
los argumentos, la herramienta devuelve un resumen y el modelo decide el
siguiente paso. La recomendación final se valida contra el catálogo de planes y
contra la evidencia efectivamente consultada. Dos incidentes pueden recorrer
rutas distintas: por ejemplo, `missing` puede usar `perfil_copia → linaje`,
mientras `duplicate` usa `validaciones → catalogo_planes`.

### Archivos

| Archivo | Punto de entrada / responsabilidad |
|---|---|
| `app/incident_commander/grafo.py` | `ejecutar()`: grafo LangGraph, estado, nodos y paradas |
| `app/incident_commander/agente.py` | fachada `ejecutar()` y línea base `ejecutar_determinista()` |
| `app/incident_commander/herramientas.py` | 7 herramientas Pydantic de solo lectura y validación |
| `app/llm/presupuesto.py` | reserva, conciliación y retención monetaria del evaluador |
| `app/incident_commander/observabilidad.py` | OpenTelemetry manual hacia Phoenix, apagado por omisión |
| `compose.phoenix.yml` | Phoenix local fijado, SQLite persistente y puertos en localhost |
| `app/api/v1/routes/incident_commander.py` | `POST …/agent` y `?flujo=determinista` |
| `tests/test_agente_langgraph.py` | rutas distintas, evidencia, paradas y spans Phoenix sanitizados |
| `tests/test_presupuesto_llm.py` | reserva, tarifa desconocida, conciliación y fallo ambiguo |
| `scripts/evaluar_agente_dic.py` | evaluación comparada y tope de costo |
| `scripts/generar_trazas_dic.py` | trazas sanitizadas de rutas distintas, abstención y bloqueos |
| `docs/trazas/agente-2026-11-27-simulado.json` | evidencia comparativa de las corridas |
| `app/evals/dic/resultados/agente-2026-11-27-langgraph-simulado.json` | resultados simulados |
| `dist/dashboards/data-incident-commander/*` (sitio) | pestaña «Flujo del agente» y reproducción |

### Herramientas

| Nombre | Entrada | Salida observable |
|---|---|---|
| `estado_run` | sin argumentos | estado y aprobación del run |
| `perfil_copia` | sin argumentos | filas, columnas, cobertura PIB, duplicados y tipos |
| `linaje` | `indicador` opcional | activos declarados aguas abajo (fixture marcado) |
| `validaciones` | `plan_id` opcional | checks aprobados/fallidos |
| `matriz_planes` | sin argumentos | criterios por plan |
| `catalogo_planes` | sin argumentos | planes permitidos y bloqueados |
| `snapshot` | sin argumentos | dataset, fuentes y digest |

Todas tienen `model_config = ConfigDict(extra="forbid")`, se validan antes de
ejecutarse y devuelven `solo_lectura: true`. Ninguna aprueba, cambia el estado del
run, escribe archivos, toca Atlas o ejecuta reparaciones.

## 2. Límites y cuotas

- Una sola reserva por run (`limites.reservar_run`) antes de invocar el grafo.
  Todos los pasos del grafo usan `cliente.solicitar_chat` y computan contra esa
  reserva y contra el presupuesto de tokens del run.
- `LLM_AGENTE_MAX_PASOS=6`, `LLM_AGENTE_MAX_SEGUNDOS=60` y
  `LLM_AGENTE_PRESUPUESTO_TOKENS=23000` por omisión.
- Salida por llamada acotada a `min(LLM_MAX_TOKENS, 300)`.
- **Presupuesto monetario del evaluador:** `app/llm/presupuesto.py` activa una
  reserva antes de cada POST del transporte compartido. La cota suma los bytes
  UTF-8 del payload (mensajes, esquemas de herramientas, modelo y parámetros) más
  256 tokens de margen de plantilla a la tarifa de entrada, más el `max_tokens`
  efectivo a la tarifa de salida. El cuerpo que se envía es exactamente el mismo
  que se acota. Cada
  reintento del agente vuelve a reservar. Con `usage` se concilia y se libera lo
  no consumido; un timeout, 5xx o respuesta sin uso conserva la reserva porque
  pudo cobrarse. Un modelo sin tarifa verificada falla antes de la primera
  solicitud. El tope del evaluador es **USD 0.50**.
- Paradas explícitas:
  - **pasos:** `AgenteLimitePasos` (502 con traza y eventos);
  - **tiempo:** `AgenteTiempo` (504 con traza y eventos);
  - **presupuesto:** `AgentePresupuesto` (502 con traza y eventos);
  - **evidencia insuficiente:** corrida `estado: detenido`,
    `motivo_parada: evidencia_insuficiente`, sin recomendación.
- La recomendación final solo admite `quarantine`, `backfill` o `fullreload`;
  `forward` está bloqueado. Cada elemento de `evidence` debe empezar por una
  herramienta realmente consultada (`validaciones: …`).
- La revisión humana sigue siendo obligatoria. El agente no llama a
  `POST /runs/{id}/approve` y no modifica `run["status"]`.

## 3. Flujo determinista como línea base

`POST /api/v1/incident-commander/runs/{run_id}/agent?flujo=determinista` devuelve
la alternativa sin LLM y sin cuota. Ejecuta `perfil_copia`, `linaje` y
`validaciones`, propone `quarantine` y publica los mismos eventos observables.
Se conserva para que la demo funcione sin proveedor y para comparar selección de
herramientas, plan, bloqueos, llamadas y latencia.

## 4. Interfaz

La pestaña **«Flujo del agente»** del tablero muestra:

- entrada del incidente, LLM en el centro, herramienta elegida, resultado devuelto
  al modelo, validación del servidor y recomendación pendiente de revisión humana;
- la lista de **acciones observables** con resúmenes, sin prompts ni razonamiento
  interno;
- la etiqueta **«Reproducción de la corrida»** porque la API entrega la traza al
  finalizar; no se simula streaming;
- el modo real del resultado se distingue explícitamente: **«Resultado · modelo
  real»**, **«Resultado · simulación»**, **«Resultado · respaldo determinista»**
  o **«Resultado · error»**. La simulación local determinista también se marca
  como simulación, no como una corrida real;
- el aviso de cero escrituras y la autoridad consultiva.

El verificador `tools/verificar-agent-flow.mjs` intercepta la API, comprueba que
no hay llamada automática al cargar, que la traza se reproduce, que el baseline
se distingue y que no hay desbordes a 390 px.

## 5. Arize Phoenix local

La traza externa usa **OpenTelemetry** y un **Phoenix autoalojado**. No requiere
cuenta de Arize, clave de LangSmith ni salida a Internet: el panel es una
herramienta privada de diagnóstico en `localhost`.

### Arranque

```bash
docker compose -f compose.phoenix.yml --profile phoenix up -d
# UI:   http://localhost:6006
# OTLP: http://localhost:6006/v1/traces (HTTP) · 4317 (gRPC)
docker compose -f compose.phoenix.yml --profile phoenix down
```

La imagen está fijada en `arizephoenix/phoenix:22.1.0`. El volumen
`phoenix_data` guarda la base SQLite (`PHOENIX_WORKING_DIR=/mnt/data`); reiniciar
o recrear el contenedor no borra las trazas. Los puertos se publican solo en
`127.0.0.1`.

### Activación y apagado

La exportación está **apagada por omisión**. Para emitir trazas:

```bash
PHOENIX_TRACING=true
PHOENIX_OTLP_ENDPOINT=http://127.0.0.1:6006/v1/traces
PHOENIX_PROJECT_NAME=portafolio-dic
```

Si `PHOENIX_TRACING` no está activo, el SDK no se inicializa. Si el SDK falta, el
endpoint no es HTTP(S) o Phoenix no responde, la investigación continúa: los
errores de exportación se absorben y el resultado del agente no cambia. La
respuesta expone `observabilidad.phoenix: "activado" | "desactivado"` sin secretos.

### Instrumentación manual y sanitizada

No se usa auto-instrumentación de HTTP, OpenAI, LangChain ni LangSmith. El grafo
crea explícitamente:

- `dic.run`: span raíz por corrida con `run_id`, escenario, modo
  (`real`/`simulado`), orden de herramientas, llamadas, tokens, plan, desenlace y
  `external_writes=0`;
- `dic.model`: una por vuelta, con paso, modelo, herramientas elegidas, tokens
  reportados y si fue la respuesta final;
- `dic.tool.<herramienta>`: una por ejecución, con nombre, paso, duración,
  resumen acotado y `evidencia_id`.

No se envían prompts, mensajes, argumentos de herramientas, respuestas crudas,
filas del dataset, credenciales ni la IP del visitante. Los campos son los mismos
eventos sanitizados que ya consume la pestaña «Flujo del agente», no el contenido
interno del proveedor.

La verificación local con dos rutas sintéticas (`perfil_copia→linaje` y
`perfil_copia→validaciones`) se ejecutó el 2026-11-28: cada una produjo 6 spans
(raíz, 3 modelo y 2 herramientas), 360 tokens reportados y `completado`; los
mismos `trace_id` siguieron presentes tras reiniciar el contenedor. No se
encontraron prompts, argumentos, respuestas crudas, secretos ni IP en los
atributos exportados. Evidencia: `docs/evidencia-phoenix.json`, captura
`docs/evidencia-phoenix-ui.png` y `docs/trazas/phoenix-2026-11-28-simulado.json`.

La corrida con un modelo real queda pendiente porque no hay credenciales
aisladas; si se ejecuta, conserva el tope de USD 0.50 y el presupuesto por
solicitud ya probado.

## 6. Evaluación

Comando reproducible (sin red, sin costo):

```bash
python scripts/evaluar_agente_dic.py --proveedor simulado
```

Conjunto sintético de 14 incidentes, 12 evaluables y 2 adversariales
(`i09`, `i10`). Resultado del 2026-11-27:

| Métrica | Agente LangGraph | Determinista |
|---|---:|---:|
| Plan correcto | 12/12 | 8/12 |
| Selección exacta de herramientas | 12/12 | n/a (3 fijas) |
| Bloqueos correctos | 2/2 | 0/2 |
| Llamadas al modelo / herramientas | 35 / 23 | 0 / 42 |
| Latencia media local | 17.8 ms | ~0 ms |
| Costo | no calculable (simulado) | 0 |
| Escrituras externas | 0 | 0 |

El agente y el baseline se miden por separado en
`app/evals/dic/resultados/agente-2026-11-27-langgraph-simulado.json`. La corrida
**real** no se ejecutó: en este entorno no hay `LLM_API_KEY`, `LLM_MODEL` ni
`LLM_CUOTA_HMAC_KEY`. El script aborta antes de la primera llamada si faltan, y el
tope de evaluación es **USD 0.50**; para una corrida real se debe ajustar el cupo
de evaluación en un entorno seguro y revisar el costo medido, que se contrasta
con el `usage` reportado por el proveedor y con el gasto conservador retenido por
el presupuesto.

Pruebas del presupuesto (sin red, proveedor doblado):

```bash
python -m pytest tests/test_presupuesto_llm.py tests/test_eval_agente_dic.py -q
```

Cubren: (a) varias llamadas que se detienen antes de exceder el saldo; (b) tarifa
desconocida con **cero** solicitudes; (c) fallo ambiguo que conserva la reserva y
bloquea la siguiente llamada; (d) conciliación con `usage`; y (e) que la cota
incluye herramientas y `max_tokens`.

Diferencias que la tabla no debe ocultar:

- el agente simulado sigue un guion determinista; sus 12/12 miden el contrato y
  las guardas, no la calidad de un modelo real;
- la latencia es de pared en local, no latencia de red del proveedor;
- el baseline no fue diseñado para bloquear herramientas hostiles; por eso sus
  bloqueos se reportan como 0 y no se le atribuye esa capacidad;
- el plan del baseline es siempre `quarantine`, de modo que su 8/12 refleja los
  casos cuyo plan esperado coincide con esa política conservadora.

## 7. Verificación ejecutada

```bash
# suite completa (entorno local con shim de pyOpenSSL)
python -m pytest -q

# pruebas específicas (incluye presupuesto monetario y evaluador)
python -m pytest tests/test_agente_dic.py tests/test_agente_langgraph.py \
  tests/test_eval_agente_dic.py tests/test_presupuesto_llm.py \
  tests/test_mcp_banxico.py -q

# trazas sanitizadas comparativas
python scripts/generar_trazas_dic.py

# Phoenix local + dos rutas sintéticas con spans OTel
docker compose -f compose.phoenix.yml --profile phoenix up -d
PHOENIX_TRACING=true PHOENIX_OTLP_ENDPOINT=http://127.0.0.1:6006/v1/traces \
  python scripts/generar_trazas_dic.py --salida /tmp/phoenix-dic.json \
  --caso missing-ruta-perfil-linaje --caso schema-ruta-perfil-validaciones
# abrir http://localhost:6006 y comprobar los spans de cada run

# MCP local: descubrimiento e invocación con el cliente separado
python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json

# flujo del agente en navegador
node tools/verificar-agent-flow.mjs
```

En la ejecución local: **531 passed, 11 skipped** en la suite completa; **49
pruebas específicas** del agente/evaluación/presupuesto/MCP y **32 comprobaciones**
del flujo de navegador del sitio. Las 11 omisiones son pruebas de CNBV que requieren boletines
reales descargados. El entorno de desarrollo requirió un shim local de pyOpenSSL
por una incompatibilidad de la máquina, no del repositorio; CI instala las
dependencias fijadas.

## 8. MCP: piloto local de Banxico

«Analista asistido por IA» sigue siendo un caso **Próximamente**, pero el piloto
MCP ya tiene un consumidor local separado. El servidor `app/mcp_banxico/server.py`
se ejecuta por **stdio** (no HTTP) y expone `banxico_buscar_fragmentos`, que
reutiliza la recuperación léxica real de `BriefingBanxico`. El cliente
`scripts/mcp_cliente_banxico.py` arranca el servidor, descubre la herramienta,
invoca una pregunta de tasa objetivo y comprueba una abstención.

```bash
python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json
```

La evidencia observada está en `docs/evidencia-mcp.json`: herramienta descubierta,
`BM-2026-11-05-TASA` como fragmento real, dos fragmentos máximos y
`servidor_expuesto_publicamente: false`. La salida está acotada a 1 200 caracteres
por extracto y no incluye filas completas ni credenciales. El servidor no se
publica; autenticación, aislamiento y límites de tasa quedan pendientes si se
expone fuera de local. Contrato completo: [`MCP-ANALISTA.md`](MCP-ANALISTA.md).

## 9. Evidencia por tecnología

| Tecnología | Archivo de entrada | Qué se ejecutó | Resultado observado | Pendiente |
|---|---|---|---|---|
| **LangGraph** | `app/incident_commander/grafo.py` | `pytest tests/test_agente_langgraph.py`; corridas con guion y `scripts/generar_trazas_dic.py` | Grafo con nodos modelo/herramienta/validación; dos rutas distintas (`perfil→linaje` y `perfil→validaciones`); abstención, bloqueo y parada por presupuesto | Corrida con modelo real; hoy el proveedor es simulado |
| **LangChain** | `app/incident_commander/herramientas.py` | `convert_to_openai_tool` sobre los siete modelos Pydantic; pruebas del catálogo | Esquemas de herramientas tipados y validados por el servidor | Nada adicional para este alcance |
| **Presupuesto monetario** | `app/llm/presupuesto.py` + `cliente.post_chat` | `pytest tests/test_presupuesto_llm.py tests/test_eval_agente_dic.py` con transporte doblado | Reserva por POST con entrada, herramientas y salida; conciliación con `usage`; retención ante fallo ambiguo; tarifa desconocida sin solicitudes | Corrida real: faltan credenciales aisladas; no se inventó consumo |
| **Arize Phoenix / OTel** | `app/incident_commander/observabilidad.py`, `grafo.py` y `compose.phoenix.yml` | `pytest tests/test_agente_langgraph.py`; dos corridas sintéticas con rutas distintas y reinicio del contenedor | Traza por corrida con spans `dic.run`, `dic.model` y `dic.tool.*`; atributos sanitizados, sin prompts ni argumentos | La corrida real con modelo queda pendiente de credenciales; las cifras de Phoenix son simuladas |
| **MCP** | `app/mcp_banxico/server.py` + `scripts/mcp_cliente_banxico.py` | `python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json` | Descubrimiento de `banxico_buscar_fragmentos`, invocación real con `BM-2026-11-05-TASA`, abstención y límite de 3 fragmentos; servidor stdio no público | Analista de producto, autenticación y aislamiento si se expone fuera de local |

La tabla distingue lo ejecutado de lo pendiente. LangGraph y LangChain están
integrados en el código y probados; Phoenix y MCP están verificados en local,
pero no se despliegan públicamente.

## 10. Bloqueos y pendientes

- Corrida real con un modelo: faltan credenciales del entorno seguro; no se
  inventaron métricas ni se usó una clave del repositorio. La corrida queda
  pendiente con tope USD 0.50 y solo debe ejecutarse con credenciales aisladas.
- Presupuesto monetario: la lógica y las pruebas con proveedor doblado están
  completas; la conciliación contra un `usage` real del proveedor necesita esa
  misma corrida pendiente.
- Despliegue: fuera de alcance; los cambios se entregan como ramas/PRs revisables.
- Phoenix: verificado en local con corridas simuladas; el panel no se publica y
  no sustituye una corrida real con proveedor.
- La interfaz etiqueta la traza como reproducción; un streaming SSE queda como
  siguiente mejora, no se presenta como hecho.
- El linaje es una fixture declarada de la demo, no un catálogo corporativo.


