"""Testes da transcrição via AssemblyAI.

Sem rede: `midia._cliente` é a única saída HTTP da transcrição e aqui ela vira um
MockTransport. O que importa provar é o que a troca de provedor trouxe de novo —
três passos em vez de um, e o polling do job.
"""
import asyncio

import httpx
import pytest

from uazapi_mcp import config, midia


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(config, "ASSEMBLYAI_API_KEY", "chave-de-teste")
    monkeypatch.setattr(config, "ASSEMBLYAI_MODEL", "best")
    monkeypatch.setattr(config, "ASSEMBLYAI_LANGUAGE", "pt")
    # O polling dorme entre consultas; nos testes não.
    monkeypatch.setattr(midia.asyncio, "sleep", _nao_dorme)
    midia._cache_transcricao.clear()
    yield
    midia._cache_transcricao.clear()


async def _nao_dorme(_segundos):
    return None


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "audio.mp3"
    p.write_bytes(b"\x00\x01bytes-de-audio")
    return p


def _instalar(monkeypatch, handler) -> list[httpx.Request]:
    """Devolve a lista de requisições feitas, para conferir a sequência."""
    chamadas: list[httpx.Request] = []

    def registrando(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        return handler(request)

    monkeypatch.setattr(
        midia, "_cliente",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(registrando)))
    return chamadas


def _fluxo(estados: list[str], texto: str | None = "bom dia, o sistema travou"):
    """Handler que sobe o arquivo, cria o job e devolve `estados` em sequência."""
    fila = list(estados)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": "https://cdn.assemblyai.com/upload/abc"})
        if request.url.path == "/v2/transcript" and request.method == "POST":
            return httpx.Response(200, json={"id": "job-1", "status": "queued"})
        estado = fila.pop(0) if fila else "completed"
        corpo: dict = {"id": "job-1", "status": estado}
        if estado == "completed":
            corpo["text"] = texto
        if estado == "error":
            corpo["error"] = "audio file does not appear to contain audio"
        return httpx.Response(200, json=corpo)

    return handler


def test_transcreve_apos_o_job_fechar(monkeypatch, audio):
    chamadas = _instalar(monkeypatch, _fluxo(["queued", "processing", "completed"]))

    assert asyncio.run(midia.transcrever(audio)) == "bom dia, o sistema travou"

    # upload, criação do job e três consultas — nessa ordem.
    assert [(c.method, c.url.path) for c in chamadas] == [
        ("POST", "/v2/upload"),
        ("POST", "/v2/transcript"),
        ("GET", "/v2/transcript/job-1"),
        ("GET", "/v2/transcript/job-1"),
        ("GET", "/v2/transcript/job-1"),
    ]
    assert chamadas[0].content == b"\x00\x01bytes-de-audio"
    assert chamadas[0].headers["authorization"] == "chave-de-teste"
    import json
    assert json.loads(chamadas[1].content) == {
        "audio_url": "https://cdn.assemblyai.com/upload/abc",
        "language_code": "pt",
        "speech_model": "best",
    }


def test_job_com_erro_virou_texto_e_nao_excecao(monkeypatch, audio):
    _instalar(monkeypatch, _fluxo(["error"]))
    out = asyncio.run(midia.transcrever(audio))
    assert out.startswith("[falha na transcrição:")
    assert "does not appear to contain audio" in out


def test_upload_recusado_nao_cria_job(monkeypatch, audio):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    chamadas = _instalar(monkeypatch, handler)
    out = asyncio.run(midia.transcrever(audio))
    assert "upload HTTP 401" in out
    assert len(chamadas) == 1


def test_audio_sem_fala(monkeypatch, audio):
    _instalar(monkeypatch, _fluxo(["completed"], texto=None))
    assert asyncio.run(midia.transcrever(audio)) == "[áudio sem fala detectada]"


def test_sem_chave_nao_toca_a_rede(monkeypatch, audio):
    monkeypatch.setattr(config, "ASSEMBLYAI_API_KEY", "")
    chamadas = _instalar(monkeypatch, _fluxo(["completed"]))
    out = asyncio.run(midia.transcrever(audio))
    assert out == "[transcrição indisponível: ASSEMBLYAI_API_KEY não configurada]"
    assert chamadas == []


def test_segunda_chamada_sai_do_cache(monkeypatch, audio):
    chamadas = _instalar(monkeypatch, _fluxo(["completed"]))
    primeira = asyncio.run(midia.transcrever(audio))
    segunda = asyncio.run(midia.transcrever(audio))
    assert primeira == segunda
    assert len(chamadas) == 3  # não repetiu upload, job nem consulta


def test_erro_nao_entra_no_cache(monkeypatch, audio):
    _instalar(monkeypatch, _fluxo(["error"]))
    asyncio.run(midia.transcrever(audio))
    assert str(audio) not in midia._cache_transcricao
