# Integración de los PR de Data Incident Commander

Instantánea de revisión: **2026-11-28**. Los PR de esta ronda son:

| Repo | PR | Rama | Cabeza revisada | Base |
|---|---|---:|---|---|
| portfolio-api | #4 | `feat/dic-langgraph-agent` | `98ad4f8` | `ccb3b9f` (`main`) |
| portafolio-hugo-aguilar | #8 | `feat/dic-agent-flow` | `e532ef4` | `a1ae6ac` (`main`) |

Los SHA son la cabeza funcional revisada; la rama puede avanzar con commits de
documentación posteriores.

PR de auditoría abiertos que tocan el mismo alcance:

| Repo | PR | Rama | Cabeza revisada | Base |
|---|---|---:|---|---|
| portfolio-api | #3 | `audit/cierre-auditoria-2026-11-26` | `70026ba` | `ccb3b9f` |
| portafolio-hugo-aguilar | #7 | `audit/cierre-auditoria-2026-11-26` | `c61b68e` | `3d420a1` |

## 1. Solapamiento real

### API #3 ↔ API #4

Ambos modifican:

- `scripts/evaluar_agente_dic.py`
- `tests/test_eval_agente_dic.py`
- documentación y conteos de evaluación

#3 aporta métricas por categoría, perturbación reproducible, cambio de conjunto
y un tope duro `MAX_COSTO_REAL_USD = 0.50` con aborto previo. #4 aporta la
reserva monetaria **por solicitud** en `cliente.post_chat`, conciliación con
`usage`, retención ante fallo ambiguo, `app/llm/presupuesto.py` y sus pruebas.

No son alternativas: #3 protege el **presupuesto del run**; #4 protege el
**gasto real de cada POST**, incluidos los reintentos. La integración debe
conservar ambos. El punto delicado es que #3 reescribe `evaluar()` y
`_fila_incidente()` mientras #4 añade la captura de `PresupuestoAgotado` y el
bloque de presupuesto en el JSON: el conflicto se resuelve a mano, no tomando
una rama completa.

#3 también toca `app/evals/dic/*`, `README.md`, `.env.example`, telemetría y
Banxico. Esos cambios no dependen de #4. #4 no debe absorberlos en esta ronda.

### Sitio #7 ↔ sitio #8

Ambos modifican:

- `dist/dashboards/data-incident-commander/app.js`
- `dist/dashboards/data-incident-commander/index.html`
- `dist/dashboards/data-incident-commander/styles.css`
- evidencia (`docs/EVIDENCIA_PROYECTOS.json`, matriz CSV)
- contenido del portafolio y verificadores

#7 añade un panel separado con `agent-button` para ejecutar `/agent`, además de
catálogo de IDs, telemetría y fichas. #8 añade la pestaña **«Flujo del agente»**,
la reproducción de eventos, las etiquetas de modo de resultado y retira la
cabecera superior. El panel de #7 y la pestaña de #8 son dos interfaces para el
mismo endpoint; mantener ambas duplicaría la acción y el aviso de cuota.

Recomendación: #8 es la interfaz que se conserva. Si #7 entra primero, rebasar #8
y retirar de #7 el panel `/agent` duplicado; si #8 entra primero, rebasar #7 y
eliminar su panel `/agent` antes de mergear. Los cambios de #7 ajenos a DIC
(catálogo/telemetría/fichas) sí son independientes y deben conservarse.

## 2. Orden recomendado

### Si los PR de auditoría están listos

1. **API #3** en `main`.
2. Rebasear **API #4** sobre la nueva `main`; integrar el presupuesto por POST en
   el `evaluar()` de #3 y volver a correr las pruebas.
3. **Sitio #7** solo con sus cambios no duplicados de DIC (catálogo, telemetría,
   fichas); retirar su panel `/agent` si #8 ya está en revisión.
4. Rebasear **Sitio #8** sobre la nueva `main`; resolver `app.js`/`index.html`
   conservando la pestaña de flujo y las etiquetas de modo; regenerar evidencia.
5. Mergear **API #4** y después **Sitio #8**. El sitio puede servirse sin #4, pero
   la pestaña de flujo solo obtiene la orquestación LangGraph y la evidencia de
   presupuesto cuando #4 está desplegado.

### Si los PR de auditoría no están listos

1. **API #4** y **Sitio #8** primero, porque son la entrega revisable de esta ronda.
2. Rebasear #3 y #7 después; en #3 conservar sus métricas/telemetría y adaptar el
   presupuesto por POST; en #7 descartar el panel `/agent` duplicado y conservar
   catálogo/telemetría/fichas.

En ningún caso se debe mergear #3 o #7 sin volver a ejecutar sus pruebas después
del rebase: #3 cambia el conjunto de evaluación y #7 cambia la UI/evidencia.

## 3. Dependencias y compatibilidad

- **API #4 → Sitio #8:** el sitio consume `POST /runs/{id}/agent` y el flujo
  determinista. La API de #4 mantiene los campos existentes (`modo`, `modelo`,
  `eventos`, `traza`, `recomendacion`, `observabilidad`) y solo añade
  `ejecucion.presupuesto_monetario` al JSON del evaluador; el sitio no depende de
  ese campo.
- **Sitio #8 → API #4:** la UI funciona con la API anterior, pero la corrida real
  requiere #4 para LangGraph/MCP/LangSmith. Si #4 no está desplegado, el flujo cae
  en respaldo determinista o error, y la UI lo etiqueta como tal.
- **Presupuesto:** el contexto monetario solo se activa en
  `scripts/evaluar_agente_dic.py`; las rutas normales de las demos no cambian de
  comportamiento. `post_chat` conserva las cuotas por IP/global y añade la
  reserva solo si hay un presupuesto activo.
- **Evidencia:** #8 y #7 modifican la evidencia generada. Después de resolver
  conflictos hay que ejecutar `node tools/generar-evidencia.mjs` y
  `npm run verificar`; no se debe editar la matriz a mano.
- **Tope:** la corrida real queda limitada a **USD 0.50** por el validador del
  evaluador y por `PresupuestoMonetario`. Un modelo sin tarifa verificada produce
  cero solicitudes.
- **Producción:** esta ronda no despliega. El hotfix de la cabecera ya está en
  `main` del sitio (`a1ae6ac`); #8 lo incorpora mediante merge y no vuelve a
  tocar producción.

## 4. Estado pendiente

- Corrida real del evaluador: **pendiente**; no hay `LLM_API_KEY`, `LLM_MODEL` ni
  `LLM_CUOTA_HMAC_KEY` en este entorno.
- Traza remota LangSmith: **pendiente**; no hay `LANGSMITH_API_KEY`.
- La verificación de presupuesto se hizo con proveedor doblado (sin red): saldo
  agotado antes de la llamada, tarifa desconocida con cero solicitudes, fallo
  ambiguo que conserva la reserva y conciliación con `usage`.
