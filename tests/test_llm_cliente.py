"""Pruebas del cliente LLM compartido, sin red y sin credenciales reales."""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.llm import cliente


def ajustes(**extra) -> Settings:
    base = {
        "_env_file": None,
        "llm_base_url": "https://proveedor.invalid/v1",
        "llm_api_key": "clave-ficticia-de-prueba",
        "llm_model": "modelo-de-prueba",
    }
    base.update(extra)
    return Settings(**base)


def sobre(texto: str, **extra) -> dict:
    datos = {
        "choices": [{"message": {"role": "assistant", "content": texto}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    datos.update(extra)
    return datos


def test_configurado_exige_las_tres_variables() -> None:
    assert cliente.configurado(ajustes()) is True
    for vacio in ("llm_base_url", "llm_api_key", "llm_model"):
        assert cliente.configurado(ajustes(**{vacio: None})) is False
        assert cliente.configurado(ajustes(**{vacio: "   "})) is False
    assert cliente.modelo(ajustes()) == "modelo-de-prueba"
    assert cliente.modelo(ajustes(llm_model=None)) is None


def test_post_chat_construye_la_peticion_y_no_filtra_la_clave(monkeypatch) -> None:
    capturado = {}

    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(sobre('{"ok":true}')).encode("utf-8")

    def falso_urlopen(peticion, timeout=None):
        capturado["url"] = peticion.full_url
        capturado["cabeceras"] = dict(peticion.header_items())
        capturado["cuerpo"] = peticion.data.decode("utf-8")
        capturado["metodo"] = peticion.get_method()
        capturado["timeout"] = timeout
        return Respuesta()

    monkeypatch.setattr(cliente, "urlopen", falso_urlopen)
    resultado = cliente.post_chat(ajustes(), {"model": "modelo-de-prueba", "messages": []})

    assert resultado["choices"][0]["message"]["content"] == '{"ok":true}'
    assert capturado["url"] == "https://proveedor.invalid/v1/chat/completions"
    assert capturado["metodo"] == "POST"
    assert capturado["cabeceras"]["Authorization"].startswith("Bearer ")
    assert "clave-ficticia-de-prueba" not in capturado["cuerpo"]
    assert capturado["timeout"] == 30.0


def test_post_chat_traduce_fallos_del_proveedor(monkeypatch) -> None:
    def falla(peticion, timeout=None):
        raise TimeoutError("sin respuesta")

    monkeypatch.setattr(cliente, "urlopen", falla)
    with pytest.raises(cliente.LlmUpstreamError):
        cliente.post_chat(ajustes(), {"model": "x"})

    def devuelve_basura(peticion, timeout=None):
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"no es json"

        return R()

    monkeypatch.setattr(cliente, "urlopen", devuelve_basura)
    with pytest.raises(cliente.LlmUpstreamError):
        cliente.post_chat(ajustes(), {"model": "x"})


def test_contenido_y_uso() -> None:
    assert cliente.contenido(sobre("hola")) == "hola"
    assert cliente.uso(sobre("hola")) == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
    assert cliente.uso({}) is None
    for malo in ({}, {"choices": []}, {"choices": [{"message": {"content": "  "}}]}):
        with pytest.raises(cliente.LlmUpstreamError):
            cliente.contenido(malo)


def test_recortar_avisa_cuando_corta() -> None:
    corto = {"a": 1}
    assert json.loads(cliente.recortar(corto)) == corto

    largo = {"texto": "x" * 10_000}
    recortado = cliente.recortar(largo, maximo=200)
    assert len(recortado) <= 200
    assert json.loads(recortado)["__recortado__"] is True


def test_validaciones_de_texto_y_lista() -> None:
    assert cliente.texto("  hola   mundo ", campo="x") == "hola mundo"
    assert cliente.lista(["a", "b"], campo="y") == ["a", "b"]
    for valor in (None, "", "   "):
        with pytest.raises(cliente.LlmRespuestaInvalida):
            cliente.texto(valor, campo="x")
    with pytest.raises(cliente.LlmRespuestaInvalida):
        cliente.texto("x" * 50, campo="x", maximo=10)
    for valor in ({}, [], "no", ["a"] * 9):
        with pytest.raises(cliente.LlmRespuestaInvalida):
            cliente.lista(valor, campo="y", maximo=6)


def test_ventana_limita_las_consultas(monkeypatch) -> None:
    cliente.VENTANA.reiniciar()
    settings = ajustes(llm_consultas_por_minuto=2)
    cliente.consumir(settings, "demo")
    cliente.consumir(settings, "demo")
    with pytest.raises(cliente.LlmLimiteExcedido):
        cliente.consumir(settings, "demo")
    # Otra demo (otra clave) mantiene su propio cupo.
    cliente.consumir(settings, "otra")
    cliente.VENTANA.reiniciar()


def test_solicitar_json_sin_configuracion() -> None:
    with pytest.raises(cliente.LlmNoConfigurado):
        cliente.solicitar_json(
            Settings(_env_file=None, llm_base_url=None, llm_api_key=None, llm_model=None),
            clave_limite="demo",
            sistema="sistema",
            datos={},
            validar=lambda datos: datos,
        )


def test_solicitar_json_valida_con_el_llamador(monkeypatch) -> None:
    cliente.VENTANA.reiniciar()
    capturado = {}

    def falso_post_chat(settings, payload, *, timeout=None):
        capturado["payload"] = payload
        return sobre('{"campo":"valor"}')

    monkeypatch.setattr(cliente, "post_chat", falso_post_chat)

    def validar(datos):
        return {"limpio": cliente.texto(datos.get("campo"), campo="campo")}

    salida, usado = cliente.solicitar_json(
        ajustes(),
        clave_limite="demo",
        sistema="Instrucciones.",
        datos={"entrada": 1},
        validar=validar,
    )
    assert salida == {"limpio": "valor"}
    assert usado["usage"]["total_tokens"] == 30
    payload = capturado["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0
    assert payload["messages"][0]["content"].endswith("Responde en español.")
    assert json.loads(payload["messages"][1]["content"]) == {"entrada": 1}

    # JSON inválido del modelo
    monkeypatch.setattr(cliente, "post_chat", lambda *a, **k: sobre("no es json"))
    with pytest.raises(cliente.LlmRespuestaInvalida):
        cliente.solicitar_json(
            ajustes(), clave_limite="demo", sistema="s", datos={}, validar=validar
        )
    cliente.VENTANA.reiniciar()
