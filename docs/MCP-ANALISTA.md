# MCP en el portafolio · decisión y herramienta propuesta

**Fecha:** 27 de noviembre de 2026
**Decisión:** no se implementa un servidor MCP en esta iteración.

## Por qué no

«Analista asistido por IA» aparece en el portafolio como **Próximamente**, sin
repositorio ni implementación utilizable. Tampoco existe hoy un cliente que
consuma herramientas MCP: la demo de Incident Commander usa su propio catálogo
Pydantic y el Briefing de Banxico se sirve por su ruta HTTP. Añadir un servidor
MCP sin consumidor sería una interfaz decorativa, no una reutilización
demostrable. Se priorizó terminar y evaluar el agente de Incident Commander.

## Herramienta que quedaría disponible

Si se construye «Analista asistido por IA», la primera herramienta MCP de solo
lectura sería **`banxico_buscar_fragmentos`**, porque el corpus y la recuperación
léxica ya existen en `app/rag_banxico/service.py` y ya tienen contrato probado.

### Entrada

```json
{
  "consulta": "¿cuál es la tasa objetivo?",
  "limite": 3
}
```

- `consulta`: texto de 1 a 500 caracteres, obligatorio.
- `limite`: entero entre 1 y 3; por omisión 3.

### Salida

```json
{
  "abstenido": false,
  "fragmentos": [
    {
      "chunk_id": "BM-2026-11-05#3",
      "source_id": "BM-2026-11-05",
      "titulo": "Anuncio de Política Monetaria",
      "fecha": "2026-11-05",
      "extracto": "…texto acotado…",
      "score": 0.82
    }
  ],
  "limite_aplicado": 3,
  "solo_lectura": true
}
```

- Devuelve como máximo tres fragmentos y un extracto acotado (máximo 1 200
  caracteres por fragmento; 3 600 en total).
- Si no hay coincidencia léxica, `abstenido: true` y `fragmentos: []`; no inventa
  texto ni rellena con documentos ajenos.
- No devuelve vectores, rutas del sistema, credenciales ni la pregunta original
  almacenada.

### Permisos y límites

- Solo lectura sobre el corpus versionado (`data/snapshots/banxico/corpus.json`)
  y, si está configurada, la colección `portfolio_demos.rag_banxico_chunks`.
- Sin escrituras, sin aprobación, sin ejecución de SQL ni de remediaciones.
- La herramienta no llama al LLM; si un cliente la usa como contexto, la llamada
  al modelo y su costo se contabilizan en la ruta del cliente (misma cuota y
  presupuesto del agente).
- El servidor MCP, cuando exista, debe declarar esta herramienta con nombre
  explícito, `readOnlyHint: true` y sin argumentos libres fuera del esquema.
- El transporte y la autenticación se definirán con el consumidor; no se publica
  un endpoint MCP anónimo en producción.

## Quién la usaría

El consumidor previsto es **«Analista asistido por IA»**: un cliente MCP separado
que recibe una pregunta, consulta `banxico_buscar_fragmentos`, decide si la
evidencia es suficiente y devuelve una respuesta con citas. Ese cliente no existe
todavía; esta nota define el contrato para cuando se implemente.

## Cómo se demostraría (siguiente iteración)

1. Crear el servidor MCP en un proceso separado, con la herramienta anterior.
2. Crear un cliente MCP mínimo que haga una pregunta y muestre `chunk_id` y
   `source_id`.
3. Verificar que el resultado coincide con `BriefingBanxico.consultar()` para la
   misma consulta y que una abstención se propaga como tal.
4. Añadir una prueba sin red y otra con el corpus real; registrar la llamada y el
   costo del cliente, no atribuirlos a Incident Commander.

Hasta entonces, la integración MCP queda **documentada, no implementada**. No se
presenta como capacidad demostrada en la ficha del proyecto.
