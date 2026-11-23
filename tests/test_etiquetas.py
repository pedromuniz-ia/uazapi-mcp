"""label_chat: `definir` substitui todas as etiquetas, então lista vazia por engano não passa."""
import asyncio

import pytest

from uazapi_mcp import server, uazapi


@pytest.fixture
def enviados(monkeypatch):
    saida = []

    async def ctx(_instancia, _chat):
        return "teste", "tok", {"wa_chatid": "5511999990000@s.whatsapp.net"}

    async def post(path, body, _token):
        saida.append(body)
        return {"ok": True}

    monkeypatch.setattr(server, "_ctx", ctx)
    monkeypatch.setattr(uazapi, "post", post)
    return saida


@pytest.mark.parametrize("definir", [" , ", ",", "  "])
def test_lista_vazia_por_engano_nao_apaga(enviados, definir):
    out = asyncio.run(server.label_chat("x", definir=definir))
    assert out.startswith("ERRO") and enviados == []


def test_traco_limpa_de_proposito(enviados):
    asyncio.run(server.label_chat("x", definir="-"))
    assert enviados[0]["labelids"] == []


def test_ids_substituem(enviados):
    asyncio.run(server.label_chat("x", definir="10, 20"))
    assert enviados[0]["labelids"] == ["10", "20"]
