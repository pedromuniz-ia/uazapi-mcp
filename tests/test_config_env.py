"""Testes do leitor de `~/.uazapi-mcp/.env` (sem rede e sem tocar no ambiente real).

O que se prova aqui é o tratamento de comentário inline: sem ele, `CHAVE=abc  # nota`
grava a nota junto do token e `CHAVE=  # sem chave ainda` grava o comentário como se
fosse chave — aí o diagnóstico diz "definido (N caracteres)" e a API responde 401 sem
que nada aponte para a causa.
"""
import os

import pytest

from uazapi_mcp import config


@pytest.mark.parametrize("bruto,esperado", [
    # Comentário inline: só conta quando vem depois de espaço em branco.
    ("abc123   # chave de producao", "abc123"),
    ("abc123\t# com tab", "abc123"),
    ("abc123 #", "abc123"),
    # Valor vazio seguido de comentário: não sobra nada, e é isso que corrige o 401 mudo.
    ("   # sem chave ainda", ""),
    # Sem espaço antes, a cerquilha é parte do token.
    ("abc#123", "abc#123"),
    ("sk#live#42", "sk#live#42"),
    # Entre aspas, a cerquilha fica e o que vem depois do fechamento é comentário.
    ('"valor # com cerquilha"', "valor # com cerquilha"),
    ("'valor # com cerquilha'", "valor # com cerquilha"),
    ('"abc123"   # chave de producao', "abc123"),
    ('"  espaco preservado  "', "  espaco preservado  "),
    ('""', ""),
    # Sem cerquilha, o comportamento de sempre.
    ("https://x.uazapi.com", "https://x.uazapi.com"),
    ("  abc123  ", "abc123"),
])
def test_valor(bruto, esperado):
    assert config._valor(bruto) == esperado


def _isolar(monkeypatch, arquivos) -> dict[str, str]:
    """Ambiente vazio e descartável, com `_ENV_FILES` apontando para o tmp_path."""
    ambiente: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", ambiente)
    monkeypatch.setattr(config, "_ENV_FILES", arquivos)
    return ambiente


def _env(tmp_path, conteudo: str):
    arquivo = tmp_path / ".env"
    arquivo.write_text(conteudo, encoding="utf-8")
    return arquivo


def test_comentario_inline_nao_entra_no_valor(monkeypatch, tmp_path):
    arquivo = _env(tmp_path, (
        "# chaves da maquina\n"
        "UAZAPI_SERVER=https://x.uazapi.com   # producao\n"
        "ASSEMBLYAI_API_KEY=abc123   # chave de producao\n"
        "UAZAPI_ADMIN_TOKEN=tok#en-com-cerquilha\n"
        'UAZAPI_TOKEN="tok # entre aspas"\n'
        "export UAZAPI_DEFAULT_INSTANCE=principal   # a de sempre\n"
    ))
    ambiente = _isolar(monkeypatch, [arquivo])

    config._carregar_env()

    assert ambiente == {
        "UAZAPI_SERVER": "https://x.uazapi.com",
        "ASSEMBLYAI_API_KEY": "abc123",
        "UAZAPI_ADMIN_TOKEN": "tok#en-com-cerquilha",
        "UAZAPI_TOKEN": "tok # entre aspas",
        "UAZAPI_DEFAULT_INSTANCE": "principal",
    }


def test_chave_so_com_comentario_fica_indefinida(monkeypatch, tmp_path):
    """Hoje o comentário virava valor e `check_config` reportava a chave como definida."""
    arquivo = _env(tmp_path, "ASSEMBLYAI_API_KEY=   # sem chave ainda\n")
    ambiente = _isolar(monkeypatch, [arquivo])

    config._carregar_env()

    assert "ASSEMBLYAI_API_KEY" not in ambiente


def test_ambiente_exportado_vence_o_arquivo(monkeypatch, tmp_path):
    arquivo = _env(tmp_path, "ASSEMBLYAI_API_KEY=do-arquivo\n")
    ambiente = _isolar(monkeypatch, [arquivo])
    ambiente["ASSEMBLYAI_API_KEY"] = "do-cliente-mcp"

    config._carregar_env()

    assert ambiente["ASSEMBLYAI_API_KEY"] == "do-cliente-mcp"


def test_arquivo_inexistente_nao_quebra(monkeypatch, tmp_path):
    ambiente = _isolar(monkeypatch, [tmp_path / "nao-existe.env"])

    config._carregar_env()

    assert ambiente == {}
