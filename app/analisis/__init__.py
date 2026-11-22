"""Análisis consultivo de las demos de decisión.

Cada módulo recibe un resumen **acotado y validado** del escenario activo de su
demo, pide al proveedor una salida JSON y la valida contra el catálogo de la
propia demo. Ninguno ejecuta acciones: la respuesta es una recomendación que la
interfaz etiqueta como generada por IA y que la persona decide si aplica en el
simulador (que sigue siendo local y determinista).
"""
