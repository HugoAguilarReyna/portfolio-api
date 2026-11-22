# Portfolio API · backend del portafolio de datos

[![Pruebas](https://github.com/Dev-HugoAguilar/portfolio-api/actions/workflows/tests.yml/badge.svg)](https://github.com/Dev-HugoAguilar/portfolio-api/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](requirements.txt)
[![API](https://img.shields.io/badge/API-solo%20lectura-2f855a)](#endpoints)

> API de solo lectura que sirve a los tableros publicados por el portafolio.
> Este repositorio **no** incluye volúmenes de datos, `.env` ni credenciales:
> la configuración llega por variables de entorno y el ejemplo está en
> [`.env.example`](.env.example).


API de solo lectura para construir **dashboards empresariales interactivos** sobre
MongoDB Atlas. Sirve a los tableros que el portafolio publica:

| Dashboard | Datos | Endpoints |
|---|---|---|
| **Marketing y clientes** | Muestra pública de GA4 en BigQuery (5 % de usuarios) | `/api/v1/marketing/*` |
| **Atlas vial · Ciudad de México** | Públicos reales de la SSC | `/api/v1/atlas-vial/*` |
| **Comparativo bancario (CNBV)** | Públicos reales de la CNBV (Boletín Estadístico de Banca Múltiple) | `/api/v1/cnbv/*` |
| **Briefing con evidencia · Banco de México** | Corpus versionable de dos publicaciones oficiales; recuperación léxica | `/api/v1/briefing-banxico/*` |
| **Copiloto NL-to-SQL** | Snapshot WDI de México y SQLite de solo lectura | `/api/v1/copiloto-nl-sql/*` |
| **Data Incident Commander** | Runs efímeros en memoria; sin persistencia | `/api/v1/incident-commander/*` |

La persistencia de los tableros es **una sola**: el clúster gratuito de MongoDB
Atlas (512 MB, compartido con otros proyectos). La API no consulta BigQuery ni la
CNBV para servir: lee la versión publicada. La regla de qué se publica, qué se
calcula fuera y con qué presupuesto está en
`docs/ALMACENAMIENTO.md` (documento interno del monorepo, no publicado); el detalle de la
conciliación de cada migración, en los documentos de cada tablero.

> Dos demos nuevas no usan Atlas como almacén principal: el Copiloto NL-to-SQL
> usa SQLite local de solo lectura (Atlas solo recibe metadatos opcionales) y
> Data Incident Commander mantiene runs efímeros en memoria. Ninguna requiere
> credenciales para funcionar.

> El Briefing de Banco de México y el Copiloto NL-to-SQL pueden usar un modelo de
> lenguaje opcional. Sin las variables `LLM_BASE_URL`, `LLM_API_KEY` y `LLM_MODEL`,
> el briefing responde de forma extractiva y el copiloto usa solo su parser local.
> El briefing usa `portfolio_demos.rag_banxico_chunks`; instrucciones en
> [`banxico-evidence-briefing`](https://github.com/Dev-HugoAguilar/banxico-evidence-briefing).
> Data Incident Commander es determinista y, por omisión, no llama a ningún modelo.
> Opcionalmente puede pedir una **segunda opinión consultiva** a un proveedor
> compatible con OpenAI (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY): valida el JSON,
> admite solo quarantine, ackfill y ullreload —orward sigue bloqueado— y
> nunca aprueba ni ejecuta. Sin las tres variables, esa ruta responde 503.

> El dataset sintético (`demo_rentabilidad_2024_2025`) **se archivó fuera del
> clúster** (`data/ventas/sales.jsonl.gz`): el sitio publicado no lo consulta y sus
> 30 000 líneas ocupaban 20 MiB en un clúster al límite. Se puede recargar con
> `python -m scripts.seed_demo --replace`; sus endpoints
> (`/api/v1/dashboards/profitability/*`) siguen existiendo para probar la
> infraestructura y responden sin datos mientras la colección no se recargue.

> El sitio del portafolio se sirve desde `dist/`; los módulos de cada proyecto y
> el contenido editorial viven allí. Esta API no genera ni modifica el sitio:
> entrega los datos y la recuperación que consumen los tableros.

---

## 1. Stack

| Pieza | Versión | Para qué |
|---|---|---|
| Python | 3.12 | Lenguaje |
| FastAPI | 0.118.3 | API HTTP y validación |
| Uvicorn | 0.37.0 | Servidor ASGI |
| Pydantic / pydantic-settings | 2.12.4 / 2.12.0 | Modelos y configuración |
| PyMongo | 4.15.4 | Acceso a MongoDB (síncrono) |
| openpyxl | 3.1.5 | Lectura de los boletines XLSX de la CNBV |
| MongoDB | MongoDB Atlas, plan M0 (512 MB, compartido) | Base de datos documental |
| pytest + httpx | 8.4.2 / 0.28.1 | Pruebas |

No hay Redis, colas, ORM ni segundo motor de base de datos: no hay ninguna
necesidad medida que lo justifique en este alcance. El backend no entrena modelos:
la demo de Briefing puede llamar a un proveedor compatible con OpenAI solo si se
configuran las tres variables del modelo; no usa embeddings ni búsqueda vectorial.

## 2. Estructura

```
./   (raíz de este repositorio)
├── app/
│   ├── config.py                 # ajustes desde entorno (.env), límites y CORS
│   ├── main.py                   # app FastAPI, ciclo de vida (Mongo + índices)
│   ├── api/
│   │   ├── deps.py               # dependencias y NORMALIZACIÓN única de filtros
│   │   └── v1/
│   │       ├── router.py
│   │       └── routes/           # health, datasets, cnbv, briefing, copiloto, incidentes
│   ├── db/
│   │   ├── mongo.py              # cliente compartido, ping, colecciones
│   │   └── indexes.py            # índices e idempotencia
│   ├── cnbv/                     # DATOS REALES: fuente, parser e importador
│   │   ├── sources.py            # URL oficial, periodos verificados, condiciones de uso
│   │   ├── parser.py             # lectura del XLSX (encabezado variable, notas al pie)
│   │   ├── importer.py           # importación idempotente con revisiones y hash
│   │   └── conceptos.py          # catálogo curado y grupos de cartera
│   ├── rag_banxico/              # recuperación léxica y respuesta con evidencia
│   │   └── service.py            # corpus, Atlas opcional, LLM opcional, salud
│   ├── nl_sql/                   # parser léxico, SQL parametrizado y SQLite
│   │   └── service.py            # snapshot WDI, plantillas y auditoría opcional
│   ├── incident_commander/       # AgentOps determinista con runs en memoria
│   │   └── service.py            # escenarios, validaciones y recibos locales
│   ├── models/
│   │   ├── filters.py            # filtros y orden permitido (validación)
│   │   ├── responses.py          # contrato de salida (importes en centavos)
│   │   └── cnbv.py               # filtros y respuestas del comparativo bancario
│   ├── repositories/
│   │   ├── sales_repository.py   # agregaciones en MongoDB ($match/$group/$facet)
│   │   ├── cnbv_repository.py    # consultas a los hechos de la CNBV
│   │   └── dataset_repository.py # catálogo e metadatos del dataset
│   └── services/
│       ├── metrics.py            # REGLAS DE NEGOCIO de las métricas (sintético)
│       ├── dashboard_service.py  # resumen, detalle y comparación (sintético)
│       ├── cnbv_service.py       # comparativo bancario (datos reales)
│       ├── catalog_service.py    # datasets disponibles
│       ├── export_service.py     # CSV con protección de fórmulas
│       └── demo_data.py          # generador sintético (SOLO desarrollo)
├── scripts/
│   ├── create_indexes.py
│   ├── seed_demo.py              # carga del dataset sintético (manual)
│   ├── importar_cnbv.py          # descarga e importa boletines reales de la CNBV
│   ├── verificar_cnbv.py         # concilia los hechos contra el archivo original
│   ├── reiniciar_cnbv.py         # vacía solo las colecciones cnbv_* (reimportar)
│   └── seed_rag_banxico.py       # carga idempotente del corpus del briefing (Atlas)
├── tests/                        # unitarias + integración contra MongoDB real
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
```

La separación es deliberada: la configuración no conoce modelos, los modelos no
conocen MongoDB, las agregaciones viven en el repositorio y **las reglas de
negocio de las métricas viven en un solo sitio** (`services/metrics.py`).

## 3. Puesta en marcha

### 3.1 Variables de entorno

```bash
cp .env.example .env      # en la raíz del proyecto (Portafolio_Hugo_Aguilar/)
```

`.env` no se versiona. No hay credenciales en el código: el único valor por
defecto es una URI local sin autenticación, pensada para desarrollo.

### 3.2 Opción A — local (Python + MongoDB en el host)

Requisitos: Python 3.12 y la URI de MongoDB Atlas en `.env` (`MONGODB_URI`). Ya no
se levanta un MongoDB local: la versión publicada vive en el clúster y la API lee
de ahí. Se comprueba con `python -m scripts.verificar_atlas`.

```bash
# desde la raíz del repositorio clonado
python -m venv .venv
.venv\Scripts\activate                       # Windows
pip install -r requirements-dev.txt

python -m scripts.create_indexes             # crea los índices que falten
python -m scripts.importar_cnbv --descargar   # DATOS REALES: 7 boletines de la CNBV
python -m scripts.seed_demo                  # opcional: dataset sintético (desarrollo)
python -m uvicorn app.main:app --reload --port 8000
```

* API: <http://127.0.0.1:8010/api/v1>
* Documentación interactiva: <http://127.0.0.1:8010/api/docs>
* Salud: `GET /api/v1/health/ready`

### 3.3 Opción B — imagen Docker

La imagen incluye código, dependencias **y los snapshots de runtime**. El
despliegue completo (sitio + API bajo `/api` + MongoDB) se declara en el
repositorio del sitio, en `deploy/docker-compose.yml`; aquí solo se construye y
se prueba la imagen.

```bash
docker build -t portfolio-api:local .
docker run --rm -p 8000:8000 \
  -e MONGODB_URI="$MONGODB_URI" -e MONGODB_DB="$MONGODB_DB" \
  -e ALLOWED_ORIGINS="http://127.0.0.1:8080" \
  portfolio-api:local
curl http://127.0.0.1:8000/api/v1/health/ready
```

* El proceso corre como usuario sin privilegios (`uid 10001`) y escribe un único
  archivo propio: el índice SQLite del Copiloto en
  `/app/data/copiloto_nl_sql/wdi.sqlite3`, que se genera al arrancar a partir del
  snapshot. Todo lo demás es de solo lectura.
* `EXPOSE 8000` es el puerto interno esperado por el proxy del sitio.
* La imagen **no** trae credenciales: `MONGODB_URI`, `MONGODB_DB` y
  `ALLOWED_ORIGINS` se pasan por entorno.
* El job `imagen` del workflow construye la imagen, comprueba que los snapshots
  están dentro, que los tres servicios que los leen los abren, y que
  `/api/v1/health/live` responde.

#### Snapshots de runtime

Los tres archivos son pequeños, versionados y deterministas; la imagen los copia
(`COPY data/snapshots ./data/snapshots`):

| Archivo | Servicio | Tamaño | SHA-256 (16) |
|---|---|---|---|
| `data/snapshots/banxico/corpus.json` | Briefing BANXICO | 6 915 B | `35d35e48a510281f` |
| `data/snapshots/copiloto/data.json` | Copiloto NL → SQL | 4 416 B | `2d241c70db41ac6a` |
| `data/snapshots/incident-commander/data.json` | Data Incident Commander | 5 482 B | `8483d4ee6c5427e8` |

Tamaños y hashes corresponden al contenido versionado (LF), que es el que copia
la imagen. Para comprobarlos:

```bash
for p in banxico/corpus.json copiloto/data.json incident-commander/data.json; do
  git cat-file blob "HEAD:data/snapshots/$p" | wc -c
  git cat-file blob "HEAD:data/snapshots/$p" | sha256sum | cut -c1-16
done
```

El resto de `data/` (boletines de la CNBV, Atlas vial, marketing, M5) son
entradas de importación: no se versionan ni se copian a la imagen, y se generan
con los scripts `scripts/importar_*` a partir de sus fuentes públicas.

### 3.4 Pruebas

```bash
# desde la raíz del repositorio clonado
.venv\Scripts\python -m pytest -q
```

Las pruebas de integración usan un **MongoDB real** en una base aislada
`hugo_portfolio_test` (el suite aborta si el nombre no termina en `_test` y nunca
borra bases de datos ni colecciones de otros proyectos). Si MongoDB no está
disponible, esas pruebas se **omiten** con un mensaje explícito en lugar de
falsear resultados con mocks.

Las pruebas del comparativo bancario importan **boletines reales** desde
`data/cnbv`; si el archivo no está descargado, se omiten con un mensaje
que indica el comando para obtenerlo. Nunca se sustituyen por datos sintéticos.

Además de las pruebas, hay una comprobación de ida y vuelta contra el archivo
original:

```bash
python -m scripts.verificar_cnbv     # 56.248 hechos conciliados, 0 discrepancias
```

## 4. Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/health/live` | Vitalidad del proceso (sin tocar MongoDB) |
| GET | `/api/v1/health/ready` | Disponibilidad; incluye `ping` a MongoDB (503 si cae) |
| GET | `/api/v1/datasets` | Catálogo: origen, periodo, moneda, versión |
| GET | `/api/v1/datasets/{dataset_id}` | Metadatos de un dataset |
| GET | `/api/v1/dashboards/profitability/summary` | KPIs, comparación, serie, desgloses, dispersión y opciones de filtro |
| GET | `/api/v1/dashboards/profitability/sales` | Detalle paginado y ordenable con las mismas métricas |
| GET | `/api/v1/dashboards/profitability/sales.csv` | Exportación CSV del detalle filtrado |

**Comparativo bancario con datos reales de la CNBV:**

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/cnbv/banca-multiple/catalogo` | Fuente oficial, boletines importados (URL, hash, revisión), periodos, 158 conceptos, instituciones |
| GET | `/api/v1/cnbv/banca-multiple/resumen` | KPI del sector, comparación comparable, tabla por institución, serie, ausentes y advertencias |
| GET | `/api/v1/cnbv/banca-multiple/series` | Serie por concepto e institución |
| GET | `/api/v1/cnbv/banca-multiple/detalle` | Hechos paginados con archivo y fila de origen |
| GET | `/api/v1/cnbv/banca-multiple/indicadores` | ROA y ROE publicados por la CNBV (no se recalculan) |

**Briefing con evidencia · Banco de México (RAG léxico opcional):**

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/briefing-banxico/health` | Corpus, recuperación léxica, estado de Atlas y modelo; sin URI ni claves |
| GET | `/api/v1/briefing-banxico/corpus` | Dos fuentes y siete fragmentos versionables |
| GET | `/api/v1/briefing-banxico/query?question=...` | Evidencias recuperadas y respuesta extractiva o LLM, según configuración |

**Copiloto NL-to-SQL · indicadores WDI de México:**

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/copiloto-nl-sql/health` | Snapshot, SQLite, Atlas opcional y modelo opcional |
| GET | `/api/v1/copiloto-nl-sql/data` | Snapshot WDI versionable |
| GET | `/api/v1/copiloto-nl-sql/query?question=...` | Plan léxico, SQL parametrizado y filas |

**Data Incident Commander · AgentOps determinista:**

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/v1/incident-commander/health` | Snapshot, digest y estado efímero |
| GET | `/api/v1/incident-commander/snapshot` | Metadatos de las 21 observaciones |
| POST | `/api/v1/incident-commander/runs` | Inicia un escenario en copia temporal |
| POST | `/api/v1/incident-commander/runs/{run_id}/approve` | Rechaza o simula una reparación |
| POST | `/api/v1/incident-commander/runs/{run_id}/analysis` | Segunda opinión consultiva del modelo (no aprueba ni ejecuta) |

Contratos: `docs/CNBV-COMPARATIVO-BANCARIO.md` (documento interno del monorepo, no publicado)
(datos reales) y [`docs/API-DASHBOARD-RENTABILIDAD.md`(documento interno del monorepo, no publicado)
(dataset sintético de desarrollo). Instrucciones de las demos:
[Briefing Banxico](https://github.com/Dev-HugoAguilar/banxico-evidence-briefing),
[Copiloto NL-to-SQL](https://github.com/Dev-HugoAguilar/nl2sql-economic-copilot) y
[Data Incident Commander](https://github.com/Dev-HugoAguilar/data-incident-commander).

## 5. Decisiones que conviene conocer

1. **Importes en centavos enteros** (`*_cent`). Nunca se usan flotantes para
   dinero: no hay errores de redondeo acumulados. La moneda es única (MXN) y no
   se mezclan impuestos.
2. **Importes derivados precalculados al escribir** (`venta_bruta_cent`,
   `venta_neta_cent`, `coste_cent`, `margen_cent`). MongoDB solo suma; la fórmula
   se aplica en un único lugar (`services/metrics.py`) y el cargador la verifica
   línea a línea antes de insertar.
3. **Porcentajes desde sumas**: `margen_pct = 100 · Σmargen / Σventa_neta`, nunca
   el promedio de porcentajes de línea. Si la base es 0 → `null` con motivo.
4. **Un índice compuesto** `sales(dataset_id, fecha)` cubre todas las consultas
   implementadas, porque todas filtran por dataset y rango de fechas. Se prueba
   con `explain` que la consulta representativa usa `IXSCAN` y no `COLLSCAN`.
   Añadir un índice por dimensión sería sobreindexar con 30.000 documentos.
5. **Dataset inmutable**: un `dataset_id` cargado no se modifica. Esto permite
   usar `$facet` sin pretender una instantánea transaccional.
6. **Filtros normalizados una sola vez** en `app/api/deps.py`: resumen, detalle y
   exportación comparten validación, límites y rango temporal.
7. **Opciones de filtro dependientes**: cada dimensión se calcula aplicando el
   resto de filtros y excluyendo el suyo; una selección activa se mantiene
   visible aunque se quede con 0 filas.
8. **Datos reales sin inventar nada** (CNBV): el periodo se toma del nombre del
   archivo y se contrasta con el título; la unidad se lee del propio boletín y si
   falta se rechaza el archivo; los periodos descargables están en una lista
   verificada (`sources.PERIODOS_VERIFICADOS`) y cualquier otro se rechaza en vez
   de construir una URL a mano.
9. **Saldos vs flujos acumulados** (CNBV): cada concepto declara `naturaleza`. Los
   saldos se comparan con el periodo anterior; los flujos del estado de resultado
   integral, solo con el mismo mes del año anterior, porque el boletín los publica
   acumulados del ejercicio.
10. **La columna «Sistema» es un agregado publicado**, no una suma nuestra: se usa
    como denominador de participaciones y nunca se suma con las instituciones.
11. **Ausente ≠ cero** (CNBV): las celdas vacías no generan hechos; se cuentan y se
    informan. Las revisiones del boletín se conservan: solo se sirve la vigente.

## 6. Límites y seguridad

* El proceso no expone credenciales ni detalles internos: `health/ready` solo
  responde `ok`/`down` y la versión.
* CORS restringido a `ALLOWED_ORIGINS`; no se permiten credenciales.
* No hay endpoints públicos de escritura: la API solo lee. El pipeline de
  MongoDB nunca llega desde el cliente (no se aceptan agregaciones arbitrarias).
* Límites de consulta configurables: `page_size ≤ 200`, `max_export_rows`
  (50.000 por defecto), `max_query_ms` (8 s), `max_filter_values` (50 por
  dimensión), `max_date_range_days` (2.000) y `default_range_days` (365).
* Qué falta para publicarlo en Internet: autenticación y cuotas por cliente,
  TLS en el borde, límite de tasa, registro de auditoría de consultas, backend
  privado sin puerto publicado y almacenamiento de secretos fuera del `.env`.
* Tiempos medidos con las 30.000 líneas del dataset demostrativo (mismo equipo,
  en caliente): resumen de 365 días ~0,65 s, detalle paginado ~0,05 s,
  exportación CSV de 24 meses ~0,85 s. El desglose está en el
  el contrato de la API (documento interno del monorepo, no publicado) (§9.1).
* Tiempos medidos con los 56.248 hechos reales de la CNBV: `resumen` ~0,12 s,
  `series` ~0,01 s, `detalle` ~0,01 s, `indicadores` ~0,01 s y `catalogo`
  ~0,41 s (lo más caro, porque agrega 56.248 hechos para listar 158 conceptos y
  51 instituciones; se pide una vez por sesión).

## 7. Alcance y limitaciones de esta entrega

**Comparativo bancario (datos reales de la CNBV):**

* Cobertura: 7 boletines importados (2023-10, 2024-01, 2024-04, 2024-07,
  2024-08, 2024-09, 2024-10). El XLSX deja de publicarse después de 2024-10 y hay
  huecos reales (p. ej. 2024-06 no existe); no se inventan periodos.
* Unidades: solo las del boletín (millones de pesos y porcentajes). No hay
  conversión de unidad.
* ROA y ROE se muestran **como los publica la CNBV**; no se calculan indicadores
  propios de rentabilidad ni morosidad.
* La cartera se publica por **etapa** (1, 2, 3); el corte por tipo con monto y
  porcentaje está en la hoja `Cartera y calif`, pendiente de importar.
* El boletín no se actualiza en el tiempo según su propia nota; para series
  revisadas la CNBV remite al Portafolio de Información.
* Sin licencia de datos abiertos explícita: se atribuye la fuente y no se
  redistribuyen los archivos (viven en `data/`, fuera de Git).
* Los archivos originales no se sirven por la API: solo hechos normalizados y sus
  metadatos (URL, hash, fila de origen).

**Rentabilidad comercial (dataset sintético, solo desarrollo):**

* Datos **sintéticos** (semilla fija `20260401`), reproducible y sin datos
  personales reales; nunca se presentan como información de una empresa real.
* No se modelan devoluciones, impuestos ni gastos operativos: no se devuelve
  utilidad operativa y la venta neta es venta bruta menos descuento de línea.
* Un solo dataset activo por consulta; el alias de moneda es fijo (MXN).
* Serie temporal con granularidad mensual; sin comparación año contra año ni
  proyecciones.
* Sin caché ni preagregados: con 30.000 líneas las consultas responden en
  milisegundos. Si el volumen crece, el siguiente paso es materializar agregados
  mensuales, no añadir un motor nuevo.
* Los gráficos del frontend no están construidos: solo el contrato y ejemplos.

## Pruebas

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Estado verificado en el repositorio: **362 pruebas pasan y 11 se omiten**. Las
omisiones no son fallos ocultos: requieren los boletines reales de la CNBV en
`data/cnbv/` (periodos 202410, 202409 y 202310) y lo declaran con ese motivo. Los
archivos se obtienen con `python -m scripts.importar_cnbv --descargar`; no se
versionan.

La integración continua ejecuta `pytest -q` completo, no una recolección. No se
ejecuta ninguna ingesta contra Atlas: las pruebas usan MongoDB local o fixtures, y
los snapshots públicos de las demos viven en `data/snapshots/`.

### Cuotas de uso y costo (consultas LLM)

Las seis demos que consultan al modelo (Briefing Banxico, NL-to-SQL, Data
Incident Commander, Kilómetro Cero, The Ripple Room y DockMind) comparten un
único control, aplicado en `app/llm/cliente.py` (el transporte común): cualquier
llamada pasa por la reserva de cupo **antes** de tocar el proveedor.

| Límite | Valor inicial | Variable |
|---|---|---|
| Consultas por visitante y día (compartido entre las seis demos) | 1 | `LLM_MAX_SOLICITUDES_POR_IP_DIA` |
| Consultas globales por día | 10 | `LLM_MAX_SOLICITUDES_DIA` |
| Tokens de salida por consulta | 300 | `LLM_MAX_TOKENS` |
| Tokens reservados por día | 3 000 | `LLM_MAX_TOKENS_DIA` |
| Intervalo mínimo entre consultas del visitante | 600 s | `LLM_INTERVALO_MINIMO_S` |

- **Días UTC**; al agotarse el cupo se responde **429** con `Retry-After` (segundos
  hasta el intervalo o hasta la medianoche UTC) y un mensaje claro.
- **No se cuentan visitas sin llamada**: la reserva ocurre justo antes del POST.
- **Atómico y compartido**: contadores en `portfolio_guard.llm_limites` con
  `find_one_and_update` + `upsert` e índice TTL; valen para varios workers y para
  contenedores recreados. No se usa memoria ni el Redis de la plataforma.
- **Una llamada que falla o agota el tiempo consume cupo**: el proveedor pudo
  procesarla y facturarla; el objetivo es proteger el gasto.
- **Sin IP en claro**: el identificador es `HMAC-SHA256(LLM_CUOTA_HMAC_KEY,
  ip|día)`; la clave vive solo en el entorno del `api` y **si falta, las rutas LLM
  fallan cerrado (503)** mientras el resto del sitio sigue sirviendo.
- **IP real**: la de la conexión; `X-Real-IP`/`X-Forwarded-For` solo se aceptan si
  el par está en `LLM_PROXIES_CONFIABLES` (Traefik/nginx del despliegue). Un
  visitante no puede falsificar su IP.
- El intervalo de 10 minutos es una protección adicional: con 1 consulta diaria
  por visitante, el límite diario ya es más estricto.

> Esta protección **no sustituye** el límite monetario del proyecto de OpenAI:
> configúralo aparte (US$ 1/mes, alerta en US$ 0.50 y «Enforce a hard limit»). Es
> un tope de facturación del proveedor, no una garantía de gasto cero.

### Evaluación reproducible de NL-to-SQL

Conjunto sintético y versionado en `app/evals/nl_sql/casos.jsonl` (51 preguntas:
directas, parafraseadas, no soportadas y adversariales). El resultado esperado se
deriva del contrato del sistema —catálogo de tres indicadores, años 2019–2025,
operaciones `trend`/`comparison`/`max`/`min` y solo México—, nunca de respuestas
del modelo.

```bash
# Modo simulado (por omisión, sin red y sin coste)
python scripts/evaluar_nl_sql.py

# Llamadas reales: avisa de los cargos y exige confirmación explícita
PORTFOLIO_EVAL_LIVE=1 python scripts/evaluar_nl_sql.py --live
```

Compara tres modos: `parser` (léxico), `llm` (solo planificador del modelo) y
`llm_con_fallback` (modelo con respaldo léxico). Guarda un JSON con fecha,
versión del conjunto, modo, modelo, métricas por modo y por categoría en
`app/evals/nl_sql/resultados/`.

Qué mide: acierto exacto del plan, precisión por campo (indicador, años,
operación), detección de preguntas no soportadas, planes inválidos bloqueados,
latencia y tokens. **El costo en USD figura como «no calculable»**: no hay tabla
de precios verificada en el repositorio y no se estima sin base.

Límites conocidos: en modo simulado el proveedor es un doble determinista (sus
cifras no representan al modelo real) y la latencia mide el arnés, no la red. El
conjunto documenta cuatro casos (`n12`, `n13`, `a09`, `a10`) donde el parser
léxico asocia el término y devuelve un plan de lectura aunque el contrato diga
«no soportado»: la evaluación los reporta como falsos soportados; el sistema
sigue siendo de solo lectura porque solo construye `SELECT`.

### Evaluación del briefing de Banxico (recuperación y respuestas)

Conjunto sintético de 32 preguntas con el fragmento relevante marcado
(`app/evals/banxico/preguntas.jsonl`), sobre el corpus real versionado (7
fragmentos de 2 publicaciones oficiales del Banco de México).

```bash
python scripts/evaluar_banxico.py     # simulado: sin red y sin coste
```

Compara tres modos de recuperación y mide `recall@3` y MRR; además evalúa las
respuestas: citas verificables contra el corpus y abstención cuando no hay
respaldo.

| Modo | Qué es |
|---|---|
| `lexica` | Baseline actual (`puntuar`): coincidencia de términos, sin embeddings |
| `embeddings-locales` | Bolsa de términos ponderada (TF, L2) con NumPy y coseno — **no** es un modelo de embeddings |
| `hibrida` | Mezcla lineal 0.5 · léxica normalizada + 0.5 · coseno |

Resultado medido (simulado, 2026-11-22): los tres modos alcanzan `recall@3 = 1.0`
en las 28 preguntas con respaldo; el léxico obtiene MRR 0.9464 y la variante de
embeddings locales 0.9286. **No se afirma que los embeddings mejoren** la
recuperación: con este corpus, la señal léxica es igual o mejor. La abstención
sin respaldo depende del umbral (coseno ≥ 0.20; híbrido ≥ 0.25), fijado a priori.

Vectores reales: `scripts/generar_embeddings_banxico.py --modelo text-embedding-3-small`
llama al proveedor de embeddings, **genera cargos**, exige
`PORTFOLIO_EVAL_LIVE=1` y escribe `app/evals/banxico/vectores-banxico.json`. No se
ejecutó ni se versionaron vectores; antes de hacerlo revisa formato, tamaño y
conveniencia de publicarlos.

El corpus **no se amplió** en esta fase: no es posible verificar en línea la
procedencia de nuevas publicaciones oficiales desde este entorno. Para añadir un
fragmento, incluye `chunk_id`, `source_id`, `heading`, `content` y la fuente en
`corpus.json/sources` con `title`, `date`, `kind` y `url` oficial; sin esa
procedencia verificable no debe añadirse.

### Agente con herramientas · Data Incident Commander (solo lectura)

`POST /api/v1/incident-commander/runs/{run_id}/agent` ejecuta un bucle de agente
con **herramientas de solo lectura** sobre el run: máximo 6 pasos y presupuesto de
tokens configurable (`LLM_AGENTE_MAX_PASOS`, `LLM_AGENTE_PRESUPUESTO_TOKENS`).

| Herramienta | Devuelve |
|---|---|
| `estado_run` | Estado y si hay aprobación registrada |
| `validaciones` | Validaciones del run sobre la copia de trabajo |
| `matriz_planes` | Matriz de criterios por plan |
| `catalogo_planes` | Planes permitidos y bloqueados |
| `snapshot` | Metadatos del snapshot y su digest |

- Cada herramienta tiene contrato Pydantic con `extra="forbid"`; el nombre y los
  argumentos se validan antes de ejecutar y nunca escriben.
- El modelo **no** puede aprobar, cambiar estados, escribir fuera ni ejecutar
  remediaciones: cualquier herramienta o argumento fuera de catálogo se rechaza
  (422 con la traza) y queda registrado.
- La traza es segura (paso, tipo, herramienta, plan): sin prompts completos, sin
  credenciales y sin datos sensibles.
- La respuesta es consultiva (`advisory_only`, `external_writes: 0`); la
  aprobación humana y la simulación en `POST /runs/{id}/approve` no cambian.
- Ante límite de pasos, presupuesto agotado, error del proveedor o salida
  inválida, la ruta responde 502/503 y el flujo determinista sigue disponible.

Evaluación reproducible con proveedor simulado:

```bash
python scripts/evaluar_agente_dic.py
```

Conjunto sintético de 12 incidentes (`app/evals/dic/incidentes.jsonl`) con las
herramientas esperadas y el plan esperado. Resultado simulado del 2026-11-22:
10/12 cerrados, plan correcto 1.0 y herramientas exactas 1.0 entre los cerrados,
2.9 pasos de media, 2 violaciones de política bloqueadas y **0 escrituras**; el
baseline determinista resuelve 12/12. Son cifras del doble simulado, **no** del
modelo real; no hay coste en USD calculable.
