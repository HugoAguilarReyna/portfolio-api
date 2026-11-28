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
| `app/incident_commander/observabilidad.py` | LangSmith opcional y metadatos sanitizados |
| `app/api/v1/routes/incident_commander.py` | `POST …/agent` y `?flujo=determinista` |
| `tests/test_agente_langgraph.py` | rutas distintas, evidencia, paradas, LangSmith |
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

## 5. LangSmith

La integración está **apagada por omisión**. Solo se activa con:

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...          # secreto del entorno, nunca del repositorio
LANGSMITH_PROJECT=portafolio-dic
```

También se acepta el alias `LANGCHAIN_TRACING_V2` + `LANGCHAIN_API_KEY`. El grafo
recibe únicamente `run_name`, tags y metadatos agregados (run, escenario, flujo,
`advisory_only`, `external_writes`). Además, cada llamada crea un span hijo:
`modelo-dic` (paso, herramientas elegidas, tokens, final) y `herramienta-dic`
(nombre, duración, resumen y `evidencia_id`). Así la ruta, las herramientas, sus
tiempos y el resultado son inspeccionables sin enviar mensajes internos.

El estado que traza LangGraph contiene el contexto sanitizado del incidente y
resúmenes acotados de herramientas; no incluye filas crudas, credenciales, la IP
del visitante ni la respuesta completa del proveedor. No se usa el contenido como
texto libre de traza. Para un entorno especialmente restrictivo se pueden activar
`LANGSMITH_HIDE_INPUTS=true` y `LANGSMITH_HIDE_OUTPUTS=true`. La respuesta expone
`observabilidad.langsmith: "activado" | "desactivado"` sin valores secretos.

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

# MCP local: descubrimiento e invocación con el cliente separado
python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json

# flujo del agente en navegador
node tools/verificar-agent-flow.mjs
```

En la ejecución local: **530 passed, 11 skipped** en la suite completa; **48
pruebas específicas** del agente/evaluación/presupuesto/MCP y **31 comprobaciones**
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
| **LangSmith** | `app/incident_commander/observabilidad.py` y `grafo.py` | `pytest` del modo apagado y de la configuración saneada; spans `modelo-dic` y `herramienta-dic` | La demo funciona con la traza apagada; con bandera y clave envía ruta, herramientas, tiempos y resúmenes sanitizados | Verificación remota: falta `LANGSMITH_API_KEY`; no se envió ninguna traza real |
| **MCP** | `app/mcp_banxico/server.py` + `scripts/mcp_cliente_banxico.py` | `python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json` | Descubrimiento de `banxico_buscar_fragmentos`, invocación real con `BM-2026-11-05-TASA`, abstención y límite de 3 fragmentos; servidor stdio no público | Analista de producto, autenticación y aislamiento si se expone fuera de local |

La tabla distingue lo ejecutado de lo pendiente. LangGraph y LangChain están
integrados en el código y probados; LangSmith y MCP tienen integración local
verificada, pero no una verificación remota ni un despliegue público.

## 10. Bloqueos y pendientes

- Corrida real con un modelo: faltan credenciales del entorno seguro; no se
  inventaron métricas ni se usó una clave del repositorio. La corrida queda
  pendiente con tope USD 0.50 y solo debe ejecutarse con credenciales aisladas.
- Presupuesto monetario: la lógica y las pruebas con proveedor doblado están
  completas; la conciliación contra un `usage` real del proveedor necesita esa
  misma corrida pendiente.
- Despliegue: fuera de alcance; los cambios se entregan como ramas/PRs revisables.
- LangSmith: no se ejecutó una traza real contra el servicio porque no hay
  `LANGSMITH_API_KEY`; la prueba cubre el apagado y la configuración saneada.
- La interfaz etiqueta la traza como reproducción; un streaming SSE queda como
  siguiente mejora, no se presenta como hecho.
- El linaje es una fixture declarada de la demo, no un catálogo corporativo.


