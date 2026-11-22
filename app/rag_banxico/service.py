"""Servicio de recuperación y respuesta para «Briefing con evidencia».

La recuperación es **léxica** y reproducible: puntúa fragmentos del corpus por
coincidencia de términos y devuelve como máximo tres evidencias. No hay
embeddings ni búsqueda vectorial.

Atlas es opcional. Si existe una colección dedicada con fragmentos, se usa; si
no responde o está vacía, se usa ``corpus.json``. El modelo también es opcional:
sin proveedor configurado, la respuesta es extractiva y se etiqueta como tal.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any
from app.config import Settings
from app.llm import cliente


class CorpusError(RuntimeError):
    """El corpus local no existe o no tiene el formato mínimo."""


def normalizar(valor: str) -> str:
    """Minúsculas sin acentos, para comparar términos del español."""
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", valor.lower())
        if unicodedata.category(caracter) != "Mn"
    )


def terminos(texto: str) -> list[str]:
    """Términos útiles de una pregunta; descarta palabras vacías."""
    vacias = {
        "que", "como", "cuando", "desde", "entre", "cual", "cuales", "sobre",
        "para", "por", "con", "del", "las", "los", "una", "uno", "fue", "hay",
        "dice", "dijo", "segun", "cambio", "menciona", "banxico",
    }
    return [
        termino
        for termino in re.findall(r"[a-z0-9.%]+", normalizar(texto))
        if len(termino) > 2 and termino not in vacias
    ]


def puntuar(consulta: str, fragmentos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordena fragmentos por coincidencia léxica; no inventa coincidencias."""
    consulta_terminos = set(terminos(consulta))
    if not consulta_terminos:
        return []
    ranked: list[dict[str, Any]] = []
    for fragmento in fragmentos:
        cuerpo = normalizar(
            " ".join(
                [
                    str(fragmento.get("heading") or ""),
                    str(fragmento.get("content") or ""),
                    " ".join(str(k) for k in fragmento.get("keywords") or []),
                ]
            )
        )
        palabras_clave = [normalizar(str(k)) for k in fragmento.get("keywords") or []]
        puntaje = sum(
            2 if any(termino in clave for clave in palabras_clave) else 1
            for termino in consulta_terminos
            if termino in cuerpo
        )
        if puntaje:
            ranked.append({**fragmento, "score": puntaje})
    return sorted(ranked, key=lambda fila: (-fila["score"], fila["chunk_id"]))[:3]


def _objetivo_mongo(uri: str) -> str:
    """Clasifica el destino sin devolver ni registrar la cadena de conexión."""
    texto = (uri or "").lower()
    if "mongodb+srv://" in texto or "mongodb.net" in texto:
        return "atlas"
    return "local"


class BriefingBanxico:
    """Caso de uso único: corpus, recuperación, salud y respuesta."""

    def __init__(self, db: Any, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.corpus_path = Path(settings.rag_banxico_corpus_path)
        self.corpus = self._leer_corpus(self.corpus_path)

    @staticmethod
    def _leer_corpus(ruta: Path) -> dict[str, Any]:
        if not ruta.exists():
            raise CorpusError(f"No existe el corpus de la demo: {ruta}")
        try:
            corpus = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorpusError("El corpus de la demo no es un JSON válido.") from exc
        if not corpus.get("sources") or not corpus.get("chunks"):
            raise CorpusError("El corpus debe incluir sources y chunks.")
        if not corpus.get("forecast"):
            raise CorpusError("El corpus debe incluir la serie forecast.")
        return corpus

    @property
    def fuente_por_id(self) -> dict[str, dict[str, Any]]:
        return {fuente["source_id"]: fuente for fuente in self.corpus["sources"]}

    def _coleccion(self):
        base = self.settings.rag_banxico_mongodb_db
        coleccion = self.settings.rag_banxico_collection
        return self.db.client[base][coleccion]

    def _atlas(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Lee la colección dedicada; nunca escribe durante una consulta."""
        info: dict[str, Any] = {
            "target": _objetivo_mongo(self.settings.mongodb_uri),
            "reachable": False,
            "database": self.settings.rag_banxico_mongodb_db,
            "collection": self.settings.rag_banxico_collection,
            "chunks": 0,
        }
        try:
            if self.db is None or not self.db.ping():
                return [], info
            coleccion = self._coleccion()
            documentos = list(coleccion.find({}, {"_id": 0}).limit(500))
            info["reachable"] = True
            info["chunks"] = len(documentos)
            return documentos, info
        except Exception:
            # La demo no falla por Atlas: el corpus local es el respaldo.
            return [], info

    def salud(self) -> dict[str, Any]:
        documentos, atlas = self._atlas()
        return {
            "ok": True,
            "demo": "Briefing con evidencia · Banco de México",
            "corpus_chunks": len(self.corpus["chunks"]),
            "corpus_snapshot": self.corpus.get("snapshot"),
            "retrieval": "lexical",
            "vector_search": False,
            "mongo": atlas,
            "atlas_chunks": len(documentos),
            "llm": {"configured": self.llm_configurado},
            "answer_modes": [
                "extractiva",
                "llm-grounded",
                "extractiva-respaldo",
                "sin-coincidencias",
            ],
        }

    @property
    def llm_configurado(self) -> bool:
        return bool(
            (self.settings.llm_base_url or "").strip()
            and (self.settings.llm_api_key or "").strip()
            and (self.settings.llm_model or "").strip()
        )

    def recuperar(self, pregunta: str) -> tuple[list[dict[str, Any]], str]:
        fragmentos_atlas, atlas = self._atlas()
        if fragmentos_atlas:
            evidencia = puntuar(pregunta, fragmentos_atlas)
            if evidencia:
                return self._con_fuente(evidencia), "atlas"
            # Si la colección existe pero no cubre la pregunta, el corpus local
            # evita convertir una colección incompleta en una respuesta vacía.
            evidencia = puntuar(pregunta, self.corpus["chunks"])
            return self._con_fuente(evidencia), "corpus-local-respaldo"
        return self._con_fuente(puntuar(pregunta, self.corpus["chunks"])), "corpus-local"

    def _con_fuente(self, evidencia: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fuentes = self.fuente_por_id
        return [
            {**fila, "source": fuentes.get(fila.get("source_id"))}
            for fila in evidencia
        ]

    def responder(self, pregunta: str, evidencia: list[dict[str, Any]]) -> tuple[str, str]:
        """Respuesta con evidencia: extractiva o del modelo, siempre acotada.

        El proveedor recibe solo la pregunta y los fragmentos recuperados. Si no
        hay configuración, si el proveedor falla o si devuelve una cita que no
        existe en el corpus, se responde de forma extractiva.
        """
        if not evidencia:
            return (
                "No encontré evidencia suficiente en los documentos cargados para "
                "responder esa pregunta. Prueba con tasa, inflación, riesgos, "
                "pronóstico o actividad económica.",
                "sin-coincidencias",
            )
        base = " ".join(str(fila["content"]) for fila in evidencia)
        if not cliente.configurado(self.settings):
            return base, "extractiva"

        evidencia_prompt = "\n\n".join(
            f"[{fila['source_id']} · {fila['heading']}] {fila['content']}"
            for fila in evidencia
        )
        payload = {
            "model": str(self.settings.llm_model),
            "temperature": 0.1,
            "max_tokens": int(self.settings.llm_max_tokens),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Responde en español usando exclusivamente la evidencia "
                        "proporcionada. No infieras datos ausentes. Si la evidencia "
                        "no alcanza, dilo. Cita los identificadores [BM-...] de las "
                        "fuentes que uses."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Pregunta: {pregunta}\n\nEvidencia recuperada:\n{evidencia_prompt}",
                },
            ],
        }
        try:
            # El límite por minuto protege el presupuesto de la clave.
            cliente.consumir(self.settings, "banxico")
            sobre = cliente.post_chat(self.settings, payload)
            answer = cliente.contenido(sobre).strip()
        except (cliente.LlmUpstreamError, cliente.LlmLimiteExcedido):
            # El proveedor no responde, tarda o se salta el límite: extractiva.
            return base, "extractiva-respaldo"

        citadas = set(re.findall(r"\[(BM-[A-Za-z0-9-]+)\]", answer))
        conocidas = self.ids_del_corpus
        if citadas - conocidas:
            # Una cita inventada invalida la respuesta: se prefiere la evidencia.
            return base, "extractiva-respaldo"

        ids = list(dict.fromkeys(fila["source_id"] for fila in evidencia))
        if not citadas:
            answer += "\n\nFuentes: " + " ".join(f"[{source_id}]" for source_id in ids)
        return answer, "llm-grounded"

    @property
    def ids_del_corpus(self) -> set[str]:
        """Identificadores reales del corpus: única lista blanca de citas."""
        return {str(fila.get("source_id")) for fila in self.corpus["chunks"]}

    def consultar(self, pregunta: str) -> dict[str, Any]:
        evidencia, modo_recuperacion = self.recuperar(pregunta)
        respuesta, modo_respuesta = self.responder(pregunta, evidencia)
        return {
            "question": pregunta,
            "answer": respuesta,
            "mode": modo_respuesta,
            "retrieval_mode": modo_recuperacion,
            "vector_search": False,
            "evidence": evidencia,
        }