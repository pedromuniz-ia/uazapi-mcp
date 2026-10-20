"""Download e transcrição das mídias das mensagens.

Motivo de existir: o fluxo manual (baixar áudio -> mover para pasta -> rodar skill)
é o gargalo. Aqui o áudio volta como TEXTO e a imagem como caminho local pronto
para o Read. O fileURL da uazapi é público sem autenticação, então nada de link:
o arquivo é gravado em disco e só o caminho circula.
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import certifi
import httpx

from . import config
from .uazapi import baixar_arquivo, baixar_midia

TIPOS_AUDIO = {"AudioMessage", "PttMessage", "VoiceMessage"}
TIPOS_IMAGEM = {"ImageMessage", "StickerMessage"}
TIPOS_VIDEO = {"VideoMessage", "PtvMessage"}
TIPOS_DOC = {"DocumentMessage"}
TIPOS_MIDIA = TIPOS_AUDIO | TIPOS_IMAGEM | TIPOS_VIDEO | TIPOS_DOC | {"AlbumMessage"}

_cache_transcricao: dict[str, str] = {}
_transcricoes_em_andamento: dict[str, asyncio.Future] = {}

ASSEMBLY_BASE = "https://api.assemblyai.com/v2"
_POLL_INTERVALO = 3.0   # teto do backoff entre consultas ao job
_POLL_TETO = 300.0      # desiste depois disso, em vez de segurar a chamada do agente


class ErroTranscricao(Exception):
    """Falha da API de transcrição, virada em texto pelo chamador."""


def _cliente() -> httpx.AsyncClient:
    """Única saída de rede da transcrição. Os testes trocam isto por um MockTransport."""
    return httpx.AsyncClient(verify=certifi.where(), timeout=120.0)


def _slug(texto: str, limite: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", str(texto), flags=re.UNICODE).strip().replace(" ", "-")
    return (re.sub(r"-+", "-", s)[:limite] or "chat").lower()


def pasta_do_chat(instancia: str, chat_nome: str) -> Path:
    p = config.MEDIA_DIR / _slug(instancia, 30) / _slug(chat_nome)
    p.mkdir(parents=True, exist_ok=True)
    return p


def nome_do_arquivo(msg: dict, ext: str) -> str:
    """Nome local da mídia de uma mensagem: horário, id e rótulo.

    Só o horário não basta: áudios encaminhados juntos chegam com o mesmo
    messageTimestamp, e um arquivo sobrescrevia o outro.
    """
    conteudo = msg.get("content") if isinstance(msg.get("content"), dict) else {}
    nome_original = conteudo.get("fileName") or ""
    tipo = msg.get("messageType", "")
    base = _slug(Path(nome_original).stem, 30) if nome_original else _slug(tipo.replace("Message", ""), 20)
    mid = _slug(msg.get("messageid") or msg.get("id") or "", 64)[-12:]
    return f"{msg.get('messageTimestamp', 0)}-{mid}-{base}{ext}"


async def _esperar_job(cli: httpx.AsyncClient, headers: dict[str, str], job_id: str) -> str:
    """Espera o job fechar.

    A AssemblyAI é assíncrona: o POST /transcript só enfileira e devolve um id.
    Quem consome isso é o `processar_lote`, com seis áudios em paralelo — daí o
    backoff, para não virar uma consulta por segundo por áudio.
    """
    limite = time.monotonic() + _POLL_TETO
    espera = 1.0
    while True:
        r = await cli.get(f"{ASSEMBLY_BASE}/transcript/{job_id}", headers=headers)
        if r.status_code >= 400:
            raise ErroTranscricao(f"consulta HTTP {r.status_code} {r.text[:150]}")
        dados = r.json()
        estado = dados.get("status")
        if estado == "completed":
            return (dados.get("text") or "").strip()
        if estado == "error":
            raise ErroTranscricao(str(dados.get("error") or "erro sem detalhe")[:200])
        if time.monotonic() >= limite:
            raise ErroTranscricao(
                f"tempo esgotado em {int(_POLL_TETO)}s (job {job_id}, estado {estado})")
        await asyncio.sleep(espera)
        espera = min(espera * 1.5, _POLL_INTERVALO)


async def transcrever(caminho: Path) -> str:
    """Transcreve um áudio com AssemblyAI.

    A uazapi tem `transcribe: true` no /message/download, mas o servidor não tem
    chave OpenAI configurada e devolve só o arquivo — testado. Daí a transcrição
    local.

    O cache é pelo conteúdo: o mesmo áudio encaminhado duas vezes é transcrito uma
    vez só, mesmo quando as duas cópias chegam juntas. Falha não entra no cache.
    """
    if not config.ASSEMBLYAI_API_KEY:
        return "[transcrição indisponível: ASSEMBLYAI_API_KEY não configurada]"
    chave = hashlib.sha256(caminho.read_bytes()).hexdigest()
    if chave in _cache_transcricao:
        return _cache_transcricao[chave]
    tarefa = _transcricoes_em_andamento.get(chave)
    if tarefa is None:
        tarefa = asyncio.ensure_future(_chamar_assemblyai(caminho))
        _transcricoes_em_andamento[chave] = tarefa
    try:
        ok, texto = await tarefa
    finally:
        _transcricoes_em_andamento.pop(chave, None)
    if ok:
        _cache_transcricao[chave] = texto
    return texto


async def _chamar_assemblyai(caminho: Path) -> tuple[bool, str]:
    """Uma transcrição na AssemblyAI. Devolve (sucesso, texto ou mensagem de falha).

    São três passos, não um: a API não aceita os bytes no mesmo POST que cria o
    job, então sobe o arquivo, enfileira e faz polling. O `upload_url` que volta é
    interno da AssemblyAI e não circula daqui para fora.
    """
    headers = {"authorization": config.ASSEMBLYAI_API_KEY}
    try:
        async with _cliente() as cli:
            r = await cli.post(f"{ASSEMBLY_BASE}/upload", headers=headers,
                               content=caminho.read_bytes())
            if r.status_code >= 400:
                raise ErroTranscricao(f"upload HTTP {r.status_code} {r.text[:150]}")
            audio_url = r.json().get("upload_url")
            if not audio_url:
                raise ErroTranscricao("upload aceito mas sem upload_url na resposta")

            r = await cli.post(f"{ASSEMBLY_BASE}/transcript", headers=headers, json={
                "audio_url": audio_url,
                "language_code": config.ASSEMBLYAI_LANGUAGE,
                "speech_model": config.ASSEMBLYAI_MODEL,
            })
            if r.status_code >= 400:
                raise ErroTranscricao(f"job HTTP {r.status_code} {r.text[:150]}")
            job_id = r.json().get("id")
            if not job_id:
                raise ErroTranscricao("job criado mas sem id na resposta")

            texto = await _esperar_job(cli, headers, job_id)
    except Exception as e:
        return False, f"[falha na transcrição: {e}]"
    return True, texto or "[áudio sem fala detectada]"


async def processar_midia(token: str, msg: dict, destino: Path, *,
                          transcrever_audio: bool = True) -> dict[str, Any]:
    """Baixa a mídia de uma mensagem e, se for áudio, transcreve.

    Devolve {'file': caminho|None, 'transcricao': str|None, 'erro': str|None}.
    """
    tipo = msg.get("messageType", "")
    mid = msg.get("messageid") or msg.get("id") or ""
    saida: dict[str, Any] = {"file": None, "transcricao": None, "erro": None}
    if not mid:
        saida["erro"] = "mensagem sem id"
        return saida
    try:
        r = await baixar_midia(token, mid, mp3=tipo in TIPOS_AUDIO)
    except Exception as e:
        saida["erro"] = f"mídia indisponível ({e})"
        return saida
    url = r.get("fileURL")
    if not url:
        saida["erro"] = r.get("error") or "mídia expirada no WhatsApp"
        return saida

    ext = (Path(urlparse(url).path).suffix
           or mimetypes.guess_extension(r.get("mimetype", "") or "") or ".bin")
    arquivo = destino / nome_do_arquivo(msg, ext)
    try:
        if not arquivo.exists():
            await baixar_arquivo(url, arquivo)
    except Exception as e:
        saida["erro"] = f"falha no download: {e}"
        return saida
    saida["file"] = str(arquivo)
    if tipo in TIPOS_AUDIO and transcrever_audio:
        saida["transcricao"] = await transcrever(arquivo)
    return saida


async def processar_lote(token: str, msgs: list[dict], destino: Path, *,
                         transcrever_audio: bool = True, concorrencia: int = 6) -> dict[str, dict]:
    """Processa várias mídias em paralelo. Devolve {messageid: resultado}."""
    sem = asyncio.Semaphore(concorrencia)

    async def uma(m: dict):
        async with sem:
            return m.get("messageid"), await processar_midia(
                token, m, destino, transcrever_audio=transcrever_audio)

    pares = await asyncio.gather(*(uma(m) for m in msgs), return_exceptions=True)
    out: dict[str, dict] = {}
    for p in pares:
        if isinstance(p, Exception):
            continue
        mid, res = p
        if mid:
            out[mid] = res
    return out
