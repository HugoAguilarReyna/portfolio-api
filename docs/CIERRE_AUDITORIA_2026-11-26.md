# Cierre de hallazgos · 2026-11-26

Este documento registra solo verificaciones ejecutadas. No se desplegó al VPS ni
se modificó producción.

## Estado de partida comprobado

| Repo | `main` | Rama revisada | SHA |
|---|---|---|---|
| `HugoAguilarReyna/portfolio-api` | `ccb3b9f` | `fix/telemetria-endurecida` | `12fcc97` |
| `HugoAguilarReyna/portafolio-hugo-aguilar` | `3d420a1` | `feat/catalogo-ids` | `9d46d93` |

La copia principal del API y las copias del sitio con remote correcto tenían
cambios locales; no se usaron `reset --hard`, `clean` ni `add --renormalize`.
El trabajo se hizo en worktrees aislados:

- API: `D:\Portafolio\work\audit-cierre-api`, rama
  `audit/cierre-auditoria-2026-11-26`.
- Sitio: `D:\Portafolio\work\audit-cierre-sitio`, rama
  `audit/cierre-auditoria-2026-11-26`.

## Telemetría y catálogo

Integración compatible de `fix/telemetria-endurecida` (merge `968c11d`):

- Catálogo cerrado de 19 proyectos y 5 áreas; el exportador del sitio produce
  exactamente el mismo JSON que `app/data/catalogo_ids.json`.
- Deduplicación diaria, exigencia de registro previo, tope por huella, límite
  por minuto y documento global acotado.
- Fallo cerrado 503 sin catálogo o sin HMAC; sin IP/User-Agent en claro.
- Pruebas: **31 simuladas + 10 de integración con MongoDB real aislado**.
  Comando real usado:
  `MONGODB_TEST_DB=hugo_portfolio_audit_test pytest tests/test_visitas.py -m integracion -q`
  → 10 passed. La base de prueba terminó sin documentos de telemetría.
- La prueba con MongoDB real **no** se sustituye por el simulador: se ejecutó y
  pasó. No se usó la base de producción.

## Evaluación del agente

Se separaron las categorías en `app/evals/dic/incidentes.jsonl`:

- 10 casos `valido`, 1 `fuera_alcance` y 1 `adversarial`.
- Resultado simulado (2026-11-26): **10/10 válidos**, **2/2 bloqueos de
  seguridad correctos**, 2,9 pasos de media y 0 escrituras.
- Baseline determinista: 7/10 válidos.
- Modo de perturbación reproducible: `--perturbacion --semilla 20261126`;
  duplica una lectura y exige invariantes de plan, herramientas y bloqueo.
  Resultado: invariantes OK.
- Resultados versionados:
  `app/evals/dic/resultados/agente-2026-11-26-simulado.json` y
  `agente-2026-11-26-simulado-perturbacion-s20261126.json`.
- La corrida real **no se ejecutó**: no hay `LLM_API_KEY`, `LLM_MODEL` ni
  `LLM_CUOTA_HMAC_KEY` en el entorno. El script exige el tope de USD 0.50 y
  rechaza modelos sin tarifa verificada; no se pidió ni se registró ninguna
  clave. Por tanto, la calidad de un LLM real queda pendiente.

## Presupuesto y límites

- `LLM_AGENTE_PRESUPUESTO_TOKENS=23000` es un presupuesto **provisional** por
  run, en tokens de entrada+salida.
- `LLM_MAX_TOKENS_DIA=230000` = **10 × 23000**, no 10 × 300. `LLM_MAX_TOKENS=300`
  es solo la salida máxima de una consulta normal.
- Se corrigieron README y comentarios que decían 3 000; se añadieron pruebas de
  defaults, rechazo de configuración inválida, reserva y compensación.
- No se declara definitivo el presupuesto hasta medir un run real.

## Briefing de Banxico

- Corpus ampliado de 7 a **12 fragmentos**; se añadieron 5 fragmentos del
  resumen visual oficial del Informe trimestral abril–junio 2026 de Banco de
  México, consultado el **2026-11-26**:
  https://www.banxico.org.mx/publicaciones-y-prensa/resumenes-visuales/informe-trimestral-html/informe-trimestral-abr-jun-00005.html
- Fuente versionada con `date`, `url` y `consultado`; no se añadieron fuentes
  secundarias ni texto inventado.
- Preguntas: **36** (32 con respaldo y 4 sin respaldo). Resultados simulados:
  `recall@3 = 1.0` en los tres modos; MRR 0.9375 léxico, 0.9167 embeddings
  locales y 0.9323 híbrido; abstención 1.0 en los 4 casos sin respaldo.
- Citas: la prueba de cita inventada degrada a extractiva; las citas emitidas
  pertenecen a los fragmentos recuperados.
- `embeddings-locales` sigue siendo una bolsa de términos, no un modelo de
  embeddings; no se declara mejora semántica.

## Pendientes y límites

- Evaluación con LLM real: **BLOQUEADA** por ausencia de credenciales y de
  control de gasto verificable en el entorno.
- 11 pruebas de CNBV se omiten porque faltan los boletines reales; no son
  fallos ocultos.
- No se desplegó ni se cambió el VPS.
