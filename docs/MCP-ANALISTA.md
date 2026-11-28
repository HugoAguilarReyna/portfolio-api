# MCP en el portafolio · piloto local de Banxico

**Fecha:** 27 de noviembre de 2026
**Estado:** piloto local implementado y verificado con un cliente MCP separado.
**No** hay servidor MCP expuesto por HTTP ni integrado en la interfaz pública.

## Por qué un piloto local

«Analista asistido por IA» sigue siendo un caso **Próximamente**, sin repositorio
ni interfaz. Para no añadir una arquitectura sin consumidor, se implementó un
**cliente MCP local separado** que usa la misma recuperación léxica real del
Briefing de Banxico. El cliente arranca el servidor por `stdio`, hace
descubrimiento de herramientas, invoca una búsqueda y comprueba una abstención.
El servidor no se abre en ningún puerto.

## Archivos

| Archivo | Responsabilidad |
|---|---|
| `app/mcp_banxico/server.py` | servidor FastMCP por `stdio` y herramienta `banxico_buscar_fragmentos` |
| `scripts/mcp_cliente_banxico.py` | cliente MCP separado: descubrimiento e invocación |
| `docs/evidencia-mcp.json` | salida reproducible del cliente |
| `tests/test_mcp_banxico.py` | validación de parámetros, límites y abstención |

## Herramienta

### `banxico_buscar_fragmentos`

Entrada:

```json
{"consulta": "¿cuál es la tasa objetivo de Banco de México?", "limite": 2}
```

- `consulta`: texto de 3 a 500 caracteres; se normalizan espacios.
- `limite`: entero de 1 a 3; por omisión 3.
- El servidor rechaza tipos incorrectos y valores fuera de rango con `ValueError`;
  el SDK MCP devuelve el error al cliente.

Salida (máximo tres fragmentos, extracto de 1 200 caracteres):

```json
{
  "abstenido": false,
  "fragmentos": [
    {
      "chunk_id": "BM-2026-11-05-TASA",
      "source_id": "BM-2026-11-05",
      "titulo": "Anuncio de Política Monetaria",
      "fecha": "2026-11-05",
      "extracto": "…",
      "score": 3
    }
  ],
  "limite_aplicado": 2,
  "modo_recuperacion": "corpus-local",
  "solo_lectura": true
}
```

- Reutiliza `BriefingBanxico.recuperar()` y `puntuar()`; no reimplementa la
  recuperación ni inventa coincidencias.
- **No** llama al LLM, no consulta Atlas (`BriefingBanxico(None, …)`) y no escribe.
- Si no hay coincidencia léxica: `abstenido: true` y `fragmentos: []`.
- No expone rutas del sistema, URI de Mongo, credenciales, el corpus completo ni
  el contenido de todos los documentos.

## Demostración reproducible

```bash
python scripts/mcp_cliente_banxico.py --salida docs/evidencia-mcp.json
```

El cliente:

1. arranca `python -m app.mcp_banxico.server` con `stdio`;
2. inicializa la sesión MCP;
3. descubre el catálogo y confirma la herramienta;
4. invoca la pregunta de tasa objetivo con `limite=2`;
5. invoca una consulta sin coincidencias para comprobar la abstención.

Resultado local observado:

- herramienta descubierta: `banxico_buscar_fragmentos`;
- fragmento real: `BM-2026-11-05-TASA` (Anuncio de Política Monetaria, 5 nov 2026);
- dos fragmentos como máximo y abstención para una consulta sin términos;
- `servidor_expuesto_publicamente: false`.

## Permisos y límites

- Solo lectura sobre el corpus versionado de Banxico; sin escrituras ni
  aprobaciones.
- Transporte `stdio` local; no hay endpoint HTTP, autenticación pública ni CORS.
- La herramienta no consume cuota de LLM porque no llama a un proveedor. Si un
  cliente futuro la usa para aumentar un prompt, esa llamada debe contabilizarse
  en la ruta del cliente con las cuotas existentes.
- El servidor debe permanecer fuera de exposición pública en este piloto. Antes
  de publicarlo harían falta autenticación, aislamiento por proceso y límites de
  tasa propios.

## Consumidor y pendientes

El consumidor de producto sigue pendiente: «Analista asistido por IA» no existe
todavía. El cliente local demuestra el caso de reutilización, no un producto
terminado. Pendiente para una siguiente iteración:

1. integrar el cliente en el flujo del analista;
2. definir autenticación y aislamiento si el servidor deja de ser local;
3. añadir una prueba con el corpus real en CI (hoy la prueba directa es local);
4. medir latencia y tamaño de salida del cliente en el entorno de ejecución.
