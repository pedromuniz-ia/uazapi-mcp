"""Travas de envio: pasta liberada em send_media e aprovação fora do modelo. Sem rede."""
import asyncio

import mcp.types as types
import pytest
from mcp import Client

from uazapi_mcp import config, server, uazapi


@pytest.fixture
def pasta(tmp_path, monkeypatch):
    liberada = tmp_path / "liberada"
    liberada.mkdir()
    monkeypatch.setattr(config, "SEND_DIRS", [liberada])
    return liberada


def test_arquivo_dentro_da_pasta_passa(pasta):
    f = pasta / "foto.jpg"
    f.write_bytes(b"x")
    assert server._arquivo_liberado(str(f)) == f.resolve()


def test_arquivo_fora_da_pasta_bloqueia(pasta, tmp_path):
    segredo = tmp_path / ".env"
    segredo.write_text("UAZAPI_TOKEN=abc")
    assert server._arquivo_liberado(str(segredo)) is None


def test_ponto_ponto_nao_escapa(pasta, tmp_path):
    (tmp_path / ".env").write_text("x")
    assert server._arquivo_liberado(str(pasta / ".." / ".env")) is None


def test_symlink_para_fora_bloqueia(pasta, tmp_path):
    segredo = tmp_path / "id_ed25519"
    segredo.write_text("chave")
    link = pasta / "inocente.jpg"
    try:
        link.symlink_to(segredo)
    except OSError:
        pytest.skip("sistema sem symlink")
    assert server._arquivo_liberado(str(link)) is None


def test_inexistente_e_pasta_dao_none(pasta):
    assert server._arquivo_liberado(str(pasta / "nao-existe.pdf")) is None
    assert server._arquivo_liberado(str(pasta)) is None


# --- aprovação de envio, ponta a ponta com o cliente MCP do SDK (uazapi simulada)

CHAT = "5511999990000@s.whatsapp.net"


@pytest.fixture
def enviados(monkeypatch):
    saida = []

    async def instancia(_):
        return "teste", "tok"

    async def chat(_token, _nome):
        return {"wa_chatid": CHAT, "name": "Cliente"}

    async def post(path, body, _token):
        saida.append((path, body))
        return {"messageid": "m1"}

    monkeypatch.setattr(uazapi, "resolver_instancia", instancia)
    monkeypatch.setattr(uazapi, "resolver_chat", chat)
    monkeypatch.setattr(uazapi, "post", post)
    return saida


def _responde(resposta, perguntas):
    async def callback(_context, params):
        perguntas.append(params.message)
        return resposta
    return callback


def _chamar(tool, args, **cliente):
    async def rodar():
        async with Client(server.mcp, **cliente) as c:
            r = await c.call_tool(tool, args)
            return " ".join(getattr(b, "text", "") for b in r.content)
    return asyncio.run(rodar())


def test_envio_aprovado_pelo_usuario(enviados):
    perguntas = []
    ok = types.ElicitResult(action="accept", content={"enviar": True})
    out = _chamar("send_text", {"chat": "x", "texto": "oi", "confirmar": True},
                  elicitation_callback=_responde(ok, perguntas))
    assert "Enviado" in out and len(enviados) == 1
    assert "oi" in perguntas[0] and CHAT in perguntas[0]


@pytest.mark.parametrize("resposta", [
    types.ElicitResult(action="decline"),
    types.ElicitResult(action="cancel"),
    types.ElicitResult(action="accept", content={"enviar": False}),
])
def test_recusa_nao_envia(enviados, resposta):
    out = _chamar("send_text", {"chat": "x", "texto": "oi", "confirmar": True},
                  elicitation_callback=_responde(resposta, []))
    assert "NÃO ENVIADO" in out and enviados == []


def test_cliente_sem_elicitation_nao_envia(enviados):
    out = _chamar("send_text", {"chat": "x", "texto": "oi", "confirmar": True})
    assert "NÃO ENVIADO" in out and "elicitation" in out and enviados == []


def test_previa_nao_pergunta_nem_envia(enviados):
    perguntas = []
    out = _chamar("send_text", {"chat": "x", "texto": "oi"},
                  elicitation_callback=_responde(types.ElicitResult(action="decline"), perguntas))
    assert "PRÉVIA" in out and perguntas == [] and enviados == []


def test_chat_liberado_envia_sem_perguntar(enviados, monkeypatch):
    monkeypatch.setattr(config, "SEND_ALLOWED_CHATS", frozenset({CHAT}))
    out = _chamar("send_text", {"chat": "x", "texto": "oi", "confirmar": True})
    assert "Enviado" in out and len(enviados) == 1


def test_send_media_fora_da_pasta_nao_envia(enviados, pasta, tmp_path):
    (tmp_path / ".env").write_text("UAZAPI_TOKEN=abc")
    ok = types.ElicitResult(action="accept", content={"enviar": True})
    out = _chamar("send_media", {"chat": "x", "arquivo": str(tmp_path / ".env"), "confirmar": True},
                  elicitation_callback=_responde(ok, []))
    assert "ERRO" in out and enviados == []


def test_send_media_da_pasta_aprovado_envia(enviados, pasta):
    (pasta / "foto.jpg").write_bytes(b"jpg")
    ok = types.ElicitResult(action="accept", content={"enviar": True})
    out = _chamar("send_media", {"chat": "x", "arquivo": str(pasta / "foto.jpg"),
                                 "tipo": "image", "confirmar": True},
                  elicitation_callback=_responde(ok, []))
    assert "Enviado" in out and enviados[0][0] == "/send/media"
