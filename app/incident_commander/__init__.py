"""Data Incident Commander.

Flujo determinista de respuesta a incidentes con runs efímeros en memoria, más un
agente supervisor LangGraph de solo lectura. El LLM es opcional; sin proveedor se
conserva la línea base determinista. No usa Atlas ni escribe fuera del proceso.
"""
