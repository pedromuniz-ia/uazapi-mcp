"""MCP server: WhatsApp via uazapi.

Leitura (chats, mensagens, mídias com áudio já transcrito) e envio com prévia e
aprovação do usuário via elicitation. Multi-instância: cada chamada escolhe a instância, ou usa
UAZAPI_DEFAULT_INSTANCE quando definida.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from typing import Annotated

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from pydantic import BaseModel, Field

from . import __version__, config, formato, uazapi
from .midia import TIPOS_AUDIO, TIPOS_MIDIA, pasta_do_chat, processar_lote, transcrever
from .ranges import fmt, janela, normalizar

mcp = MCPServer(
    "uazapi",
    version=__version__,
    instructions=(
        "Lê e envia mensagens de WhatsApp via uazapi. Áudios voltam transcritos e mídias "
        "viram arquivos locais. Envio: chame com confirmar=False para ver a prévia; com "
        "confirmar=True o servidor pede a aprovação do usuário no cliente antes de enviar."
    ),
)


def _erro(e: Exception) -> str:
    return f"ERRO: {e}"


class _Aprovacao(BaseModel):
    enviar: bool = Field(description="Marque para enviar exatamente esta mensagem")


# A aprovação de envio vem do usuário via elicitation, nunca de um argumento do modelo:
# uma mensagem recebida pode trazer instruções (prompt injection) que o modelo seguiria,
# inclusive passando confirmar=True. O SDK roda o resolver antes do corpo da tool.
_SEM_PERGUNTA = _Aprovacao(enviar=False)
_SEM_ELICITATION = _Aprovacao(enviar=False)


def _cliente_pergunta(ctx: Context) -> bool:
    """Mesmo critério do SDK: elicitation declarada e com modo form."""
    caps = ctx.client_capabilities
    e = caps.elicitation if caps else None
    return e is not None and (e.form is not None or e.url is None)


def _motivo_recusa(aprovacao: ElicitationResult[_Aprovacao]) -> str | None:
    """None libera o envio; senão, o motivo para devolver ao modelo."""
    if not isinstance(aprovacao, AcceptedElicitation):
        return "o usuário não aprovou o envio."
    if aprovacao.data is _SEM_ELICITATION:
        return ("este cliente MCP não pede aprovação ao usuário (elicitation). Use um "
                "cliente que peça, ou libere o chat em UAZAPI_MCP_SEND_ALLOWED_CHATS.")
    return None if aprovacao.data.enviar else "o usuário não aprovou o envio."


def _destino(nome_inst: str, c: dict) -> str:
    tipo = "GRUPO" if c.get("wa_isGroup") else "individual"
    return (f"- Instância: {nome_inst}\n- Destino: {uazapi.nome_do_chat(c)} ({tipo}) · "
            f"`{c['wa_chatid']}`")


def _previa_texto(nome_inst: str, c: dict, texto: str) -> str:
    return f"{_destino(nome_inst, c)}\n- Mensagem:\n\n{texto}"


def _previa_midia(nome_inst: str, c: dict, arquivo: str, tipo: str, legenda: str) -> str:
    origem = arquivo.strip()
    local = None if origem.lower().startswith(("http://", "https://")) else _arquivo_liberado(origem)
    tam = f"{local.stat().st_size/1024:.0f} KB" if local else "URL remota"
    return (f"{_destino(nome_inst, c)}\n- Arquivo: {local or origem} ({tam}) como {tipo}\n"
            f"- Legenda: {legenda or '(sem legenda)'}")


def _pedir(ctx: Context, c: dict, previa: str) -> _Aprovacao | Elicit[_Aprovacao]:
    if c["wa_chatid"] in config.SEND_ALLOWED_CHATS:
        return _Aprovacao(enviar=True)
    if not _cliente_pergunta(ctx):
        return _SEM_ELICITATION
    return Elicit(f"Enviar no WhatsApp?\n\n{previa}", _Aprovacao)


async def _aprovar_texto(ctx: Context, chat: str, texto: str, confirmar: bool, instance: str):
    if not confirmar:
        return _SEM_PERGUNTA
    nome_inst, _, c = await _ctx(instance, chat)
    return _pedir(ctx, c, _previa_texto(nome_inst, c, texto))


async def _aprovar_midia(ctx: Context, chat: str, arquivo: str, tipo: str, legenda: str,
                         confirmar: bool, instance: str):
    if not confirmar:
        return _SEM_PERGUNTA
    nome_inst, _, c = await _ctx(instance, chat)
    return _pedir(ctx, c, _previa_midia(nome_inst, c, arquivo, tipo, legenda))


def _arquivo_liberado(origem: str) -> Path | None:
    """Caminho real do arquivo se ele estiver dentro de config.SEND_DIRS; senão None.

    resolve() desfaz symlinks e `..` antes da checagem. Arquivo inexistente e arquivo fora
    das pastas dão o mesmo None, para o modelo não sondar o que existe no disco.
    """
    try:
        real = Path(origem).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file():
        return None
    for pasta in config.SEND_DIRS:
        try:
            if real.is_relative_to(pasta.expanduser().resolve()):
                return real
        except OSError:
            continue
    return None


async def _ctx(instancia: str | None, chat: str) -> tuple[str, str, dict]:
    nome_inst, token = await uazapi.resolver_instancia(instancia)
    c = await uazapi.resolver_chat(token, chat)
    return nome_inst, token, c


async def _coletar(token: str, chatid: str, *, inicio: int | None, fim: int | None,
                   limite: int, from_me: bool | None = None, tipo: str | None = None) -> list[dict]:
    """Pagina /message/find do mais recente para trás até cobrir a janela.

    A uazapi não filtra por data (testado), então o recorte é feito aqui.
    """
    coletadas: list[dict] = []
    offset = 0
    teto = min(limite, config.MAX_MENSAGENS)
    while len(coletadas) < teto:
        pagina = min(config.PAGINA, teto - len(coletadas) + 50)
        d = await uazapi.buscar_mensagens(token, chatid, limite=pagina, offset=offset,
                                          from_me=from_me, tipo=tipo)
        lote = d.get("messages", [])
        if not lote:
            break
        antigas_demais = False
        for m in lote:
            ts = m.get("messageTimestamp") or 0
            if fim and ts > fim:
                continue
            if inicio and ts < inicio:
                antigas_demais = True
                continue
            coletadas.append(m)
        if antigas_demais or not d.get("hasMore"):
            break
        offset = d.get("nextOffset", offset + len(lote))
    coletadas.sort(key=lambda m: m.get("messageTimestamp") or 0)
    return coletadas[-teto:]


# ------------------------------------------------------------------- leitura

@mcp.tool()
async def check_config() -> str:
    """Mostra o estado da configuração e testa a conexão com o servidor uazapi.

    Use quando as outras tools falharem: diz o que falta configurar sem revelar segredos.
    """
    L = ["# Configuração do uazapi-mcp", ""]
    for k, v in config.diagnostico().items():
        L.append(f"- **{k}**: {v}")
    L.append("")
    if not config.SERVER:
        L.append("UAZAPI_SERVER não definido — nenhuma chamada vai funcionar. "
                 "Configure as variáveis no cliente MCP (veja o README).")
        return "\n".join(L)
    try:
        insts = await uazapi.listar_instancias(forcar=True)
        conectadas = [i for i in insts if i.get("status") == "connected"]
        L.append(f"Conexão OK: {len(insts)} instâncias no servidor, {len(conectadas)} conectadas.")
        if conectadas:
            L.append("Conectadas: " + ", ".join(str(i.get("name")) for i in conectadas))
        else:
            L.append("Nenhuma instância conectada: conecte uma no painel uazapi antes de ler mensagens.")
    except Exception as e:
        L.append(f"Falha ao falar com {config.SERVER}: {e}")
    if not config.ASSEMBLYAI_API_KEY:
        L.append("")
        L.append("ASSEMBLYAI_API_KEY ausente: áudios serão baixados, mas não transcritos.")
    return "\n".join(L)


@mcp.tool()
async def list_instances(apenas_conectadas: bool = True) -> str:
    """Lista as instâncias (números de WhatsApp) disponiveis no servidor uazapi.

    Use para descobrir qual valor passar em `instance` nas outras tools.
    """
    try:
        insts = await uazapi.listar_instancias(forcar=True)
    except Exception as e:
        return _erro(e)
    if apenas_conectadas:
        insts = [i for i in insts if i.get("status") == "connected"]
    if not insts:
        return "Nenhuma instância conectada. Conecte uma no painel uazapi ou use apenas_conectadas=False."
    L = ["| Instância | Numero | Perfil | Status |", "|---|---|---|---|"]
    for i in insts:
        L.append(f"| {i.get('name')} | {i.get('owner') or '-'} | {i.get('profileName') or '-'} | {i.get('status')} |")
    return "\n".join(L)


@mcp.tool()
async def list_chats(busca: str = "", tipo: str = "all", limite: int = 30,
                     instance: str = "") -> str:
    """Lista conversas ordenadas pela mais recente.

    busca: parte do nome do contato/grupo (deixe vazio para as mais recentes).
    tipo: 'all', 'group' (só grupos) ou 'dm' (só individuais).
    instance: nome ou número da instância; vazio usa a padrão.
    """
    try:
        _, token = await uazapi.resolver_instancia(instance)
        chats = await uazapi.buscar_chats(token, busca=busca or None, tipo=tipo, limite=limite)
        return formato.resumo_chats(chats)
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def list_groups(busca: str = "", limite: int = 50, instance: str = "") -> str:
    """Lista os grupos de que o número participa, com o identificador de cada um."""
    try:
        _, token = await uazapi.resolver_instancia(instance)
        d = await uazapi.post("/group/list",
                              {"limit": limite, "noParticipants": True, "search": busca or ""}, token)
        grupos = d.get("groups") or []
        if not grupos:
            return "Nenhum grupo encontrado."
        L = ["| Grupo | Identificador | Participantes |", "|---|---|---|"]
        for g in grupos:
            nome = g.get("Name") or g.get("name") or g.get("subject") or "?"
            jid = g.get("JID") or g.get("jid") or g.get("id") or ""
            n = g.get("participantsCount") or len(g.get("Participants") or g.get("participants") or [])
            L.append(f"| {formato._esc(nome)} | `{jid}` | {n or '-'} |")
        return "\n".join(L)
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def get_messages(chat: str, since: str = "", until: str = "", limite: int = 300,
                       transcrever_audios: bool = True, baixar_midias: bool = True,
                       apenas_nossas: bool = False, tipo_mensagem: str = "",
                       instance: str = "") -> str:
    """Lê as mensagens de uma conversa em uma janela de tempo, já com os áudios transcritos.

    chat: nome do contato/grupo, número (5562...) ou identificador completo.
    since/until: 'hoje', 'ontem', '3d', '2h', '10/09', '10/09 14:30', '2026-09-10'.
                 Tambem aceita intervalo em since: '10/09..12/09'. Vazio = mais recentes.
    transcrever_audios: áudios voltam como texto (AssemblyAI).
    baixar_midias: imagens/vídeos/documentos são gravados em disco e o caminho vem no
                   transcript, pronto para o Read.
    tipo_mensagem: filtra por tipo, ex 'AudioMessage', 'ImageMessage', 'DocumentMessage'.
    """
    try:
        inicio, fim = janela(since or None, until or None)
        nome_inst, token, c = await _ctx(instance, chat)
        chatid = c["wa_chatid"]
        msgs = await _coletar(token, chatid, inicio=inicio, fim=fim, limite=limite,
                              from_me=True if apenas_nossas else None,
                              tipo=tipo_mensagem or None)
        midias: dict[str, dict] = {}
        avisos: list[str] = []
        if msgs and (baixar_midias or transcrever_audios):
            alvo = [m for m in msgs if m.get("messageType") in TIPOS_MIDIA]
            if not baixar_midias:
                alvo = [m for m in alvo if m.get("messageType") in TIPOS_AUDIO]
            if not transcrever_audios:
                alvo = [m for m in alvo if m.get("messageType") not in TIPOS_AUDIO]
            audios = [m for m in alvo if m.get("messageType") in TIPOS_AUDIO]
            if len(audios) > config.MAX_TRANSCRICOES:
                avisos.append(f"{len(audios)} áudios no período; transcrevi os {config.MAX_TRANSCRICOES} mais recentes.")
                manter = {m.get("messageid") for m in audios[-config.MAX_TRANSCRICOES:]}
                alvo = [m for m in alvo if m.get("messageType") not in TIPOS_AUDIO or m.get("messageid") in manter]
            if len(alvo) > config.MAX_DOWNLOADS:
                avisos.append(f"{len(alvo)} mídias no período; baixei as {config.MAX_DOWNLOADS} mais recentes.")
                alvo = alvo[-config.MAX_DOWNLOADS:]
            if alvo:
                destino = pasta_do_chat(nome_inst, uazapi.nome_do_chat(c))
                midias = await processar_lote(token, alvo, destino,
                                              transcrever_audio=transcrever_audios)
        cab = f"Instância: {nome_inst}"
        if inicio or fim:
            cab += f" · Janela: {fmt(inicio) if inicio else 'início'} -> {fmt(fim) if fim else 'agora'}"
        if avisos:
            cab += "\n" + "\n".join(f"AVISO: {a}" for a in avisos)
        return formato.transcript(c, msgs, midias=midias, cabecalho=cab)
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def get_media(chat: str, since: str = "", until: str = "", tipos: str = "all",
                    transcrever_audios: bool = True, limite: int = 40,
                    instance: str = "") -> str:
    """Baixa as mídias de uma conversa para disco e devolve os caminhos locais.

    tipos: 'all', 'audio', 'imagem', 'video' ou 'documento'.
    Áudios voltam também com a transcrição. Use quando precisar dos arquivos em si;
    para ler a conversa inteira prefira get_messages.
    """
    grupos_tipo = {
        "audio": TIPOS_AUDIO,
        "imagem": {"ImageMessage", "StickerMessage"},
        "video": {"VideoMessage", "PtvMessage"},
        "documento": {"DocumentMessage"},
        "all": TIPOS_MIDIA,
    }
    alvo_tipos = grupos_tipo.get(normalizar(tipos))
    if alvo_tipos is None:
        return f"ERRO: tipos inválido ({tipos}). Use: all, audio, imagem, video ou documento."
    try:
        inicio, fim = janela(since or None, until or None)
        nome_inst, token, c = await _ctx(instance, chat)
        msgs = await _coletar(token, c["wa_chatid"], inicio=inicio, fim=fim,
                              limite=config.MAX_MENSAGENS)
        alvo = [m for m in msgs if m.get("messageType") in alvo_tipos][-min(limite, config.MAX_DOWNLOADS):]
        if not alvo:
            return f"Nenhuma mídia do tipo {tipos} nesse período em {uazapi.nome_do_chat(c)}."
        destino = pasta_do_chat(nome_inst, uazapi.nome_do_chat(c))
        res = await processar_lote(token, alvo, destino, transcrever_audio=transcrever_audios)
        L = [f"# Mídias de {uazapi.nome_do_chat(c)} ({len(alvo)} arquivos)", "",
             f"Pasta: {destino}", ""]
        for m in alvo:
            info = res.get(m.get("messageid"), {})
            quem = formato.autor(m, bool(c.get("wa_isGroup")))
            L.append(f"- [{fmt(m.get('messageTimestamp', 0))}] {quem} · {m.get('messageType')}")
            if info.get("file"):
                L.append(f"  arquivo: {info['file']}")
            if info.get("transcricao"):
                L.append(f'  transcrição: "{info["transcricao"]}"')
            if info.get("erro"):
                L.append(f"  {info['erro']}")
        return "\n".join(L)
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def search_messages(termo: str, chat: str = "", since: str = "7d", limite_chats: int = 15,
                          instance: str = "") -> str:
    """Procura um termo nas mensagens recentes (a uazapi não busca texto no servidor).

    chat vazio = varre os chats mais recentes; com chat, busca só nele.
    """
    try:
        nome_inst, token = await uazapi.resolver_instancia(instance)
        inicio, fim = janela(since or None, None)
        alvos = [await uazapi.resolver_chat(token, chat)] if chat else \
            await uazapi.buscar_chats(token, limite=limite_chats)
        termo_l = termo.lower()
        achados: list[str] = []
        for c in alvos:
            msgs = await _coletar(token, c["wa_chatid"], inicio=inicio, fim=fim, limite=500)
            grupo = bool(c.get("wa_isGroup"))
            for m in msgs:
                if termo_l in formato.texto_da_msg(m).lower():
                    achados.append(f"- **{uazapi.nome_do_chat(c)}** · {formato.linha(m, grupo)}")
        if not achados:
            return f"Nada com {termo!r} nos últimos chats desde {since}."
        return f"# {len(achados)} mensagens com {termo!r}\n\n" + "\n".join(achados[:100])
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def get_chat_info(chat: str, instance: str = "") -> str:
    """Detalhes de um contato ou grupo: identificador, nome, se é grupo, última mensagem."""
    try:
        nome_inst, token, c = await _ctx(instance, chat)
        d = await uazapi.post("/chat/details", {"number": c["wa_chatid"], "preview": True}, token)
        alvo = d.get("chat") or d if isinstance(d, dict) else {}
        campos = ["wa_chatid", "wa_contactName", "name", "wa_isGroup", "wa_isGroup_admin",
                  "wa_lastMsgTimestamp", "wa_label", "lead_status", "lead_tags",
                  "wa_isBlocked", "wa_archived", "imagePreview", "phone"]
        L = [f"# {uazapi.nome_do_chat(c)}", "", f"Instância: {nome_inst}", ""]
        for k in campos:
            v = alvo.get(k, c.get(k))
            if v in (None, "", [], {}):
                continue
            if k == "wa_lastMsgTimestamp":
                v = fmt(v)
            L.append(f"- **{k}**: {v}")
        return "\n".join(L)
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def sync_history(chat: str, quantidade: int = 100, instance: str = "") -> str:
    """Pede ao celular o histórico antigo de uma conversa (assincrono).

    Use só quando get_messages não alcança o período pedido: a uazapi guarda uma
    janela curta. Exige o celular online. As mensagens aparecem no get_messages
    depois de alguns segundos/minutos.
    """
    try:
        nome_inst, token, c = await _ctx(instance, chat)
        r = await uazapi.post("/message/history-sync",
                              {"number": c["wa_chatid"], "mode": "history",
                               "count": min(quantidade, 100)}, token)
        return (f"Sync pedido para {uazapi.nome_do_chat(c)} ({nome_inst}): {r}\n"
                "Aguarde ~1 min e rode get_messages de novo. Se voltar HTTP 400, o chat não tem "
                "mensagem âncora conhecida.")
    except Exception as e:
        return _erro(e)


# --------------------------------------------------------------------- envio

@mcp.tool()
async def send_text(chat: str, texto: str,
                    aprovacao: Annotated[ElicitationResult[_Aprovacao], Resolve(_aprovar_texto)],
                    confirmar: bool = False, responder_id: str = "", instance: str = "") -> str:
    """Envia uma mensagem de texto.

    confirmar=False devolve só a prévia com o destino resolvido; nada é enviado.
    confirmar=True pede a aprovação do usuário no cliente e só envia se ele aprovar.
    """
    try:
        nome_inst, token, c = await _ctx(instance, chat)
        destino = uazapi.nome_do_chat(c)
        tipo = "GRUPO" if c.get("wa_isGroup") else "individual"
        if not confirmar:
            return f"PRÉVIA — nada foi enviado ainda.\n\n{_previa_texto(nome_inst, c, texto)}"
        if motivo := _motivo_recusa(aprovacao):
            return f"NÃO ENVIADO: {motivo}"
        body: dict[str, Any] = {"number": c["wa_chatid"], "text": texto}
        if responder_id:
            body["replyid"] = responder_id
        r = await uazapi.post("/send/text", body, token)
        mid = r.get("messageid") or r.get("id") or ""
        return f"Enviado para {destino} ({tipo}) pela instância {nome_inst}. id={mid}"
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def send_media(chat: str, arquivo: str,
                     aprovacao: Annotated[ElicitationResult[_Aprovacao], Resolve(_aprovar_midia)],
                     tipo: str = "document", legenda: str = "", confirmar: bool = False,
                     instance: str = "") -> str:
    """Envia um arquivo local ou URL, com a mesma aprovação de send_text.

    Arquivo local só de dentro das pastas liberadas (UAZAPI_MCP_SEND_DIRS; padrão: a
    pasta de mídias). tipo: image, video, document, audio, ptt (áudio de voz) ou sticker.
    """
    try:
        nome_inst, token, c = await _ctx(instance, chat)
        destino = uazapi.nome_do_chat(c)
        ehgrupo = "GRUPO" if c.get("wa_isGroup") else "individual"
        origem = arquivo.strip()
        local = None
        if not origem.lower().startswith(("http://", "https://")):
            local = _arquivo_liberado(origem)
            if local is None:
                pastas = ", ".join(map(str, config.SEND_DIRS))
                return f"ERRO: só envio arquivos existentes dentro de: {pastas}"
        if not confirmar:
            previa = _previa_midia(nome_inst, c, arquivo, tipo, legenda)
            return f"PRÉVIA — nada foi enviado ainda.\n\n{previa}"
        if motivo := _motivo_recusa(aprovacao):
            return f"NÃO ENVIADO: {motivo}"
        if local:
            dados = base64.b64encode(local.read_bytes()).decode()
            conteudo = f"data:application/octet-stream;base64,{dados}"
        else:
            conteudo = origem
        body: dict[str, Any] = {"number": c["wa_chatid"], "type": tipo, "file": conteudo}
        if legenda:
            body["text"] = legenda
        if local and tipo == "document":
            body["docName"] = local.name
        r = await uazapi.post("/send/media", body, token)
        return f"Enviado para {destino} ({ehgrupo}) pela instância {nome_inst}. id={r.get('messageid') or r.get('id') or ''}"
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def mark_read(chat: str, quantidade: int = 20, instance: str = "") -> str:
    """Marca como lidas as últimas mensagens recebidas de uma conversa."""
    try:
        nome_inst, token, c = await _ctx(instance, chat)
        d = await uazapi.buscar_mensagens(token, c["wa_chatid"], limite=min(quantidade, 100))
        ids = [m["messageid"] for m in d.get("messages", []) if not m.get("fromMe")]
        if not ids:
            return "Nenhuma mensagem recebida para marcar."
        await uazapi.post("/message/markread", {"id": ids}, token)
        return f"{len(ids)} mensagens marcadas como lidas em {uazapi.nome_do_chat(c)} ({nome_inst})."
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def react(chat: str, message_id: str, emoji: str = "👍", instance: str = "") -> str:
    """Reage a uma mensagem. emoji vazio remove a reação."""
    try:
        nome_inst, token, c = await _ctx(instance, chat)
        await uazapi.post("/message/react",
                          {"number": c["wa_chatid"], "id": message_id, "text": emoji}, token)
        return f"Reação {emoji or '(removida)'} aplicada em {uazapi.nome_do_chat(c)}."
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def transcribe_file(caminho: str) -> str:
    """Transcreve um arquivo de áudio local (AssemblyAI).

    Útil para áudios já baixados por get_media ou vindos de outro lugar.
    """
    p = Path(caminho).expanduser()
    if not p.is_file():
        return f"ERRO: arquivo não encontrado: {p}"
    return await transcrever(p)


# ------------------------------------------------------- contatos e etiquetas

@mcp.tool()
async def save_contact(numero: str, nome: str, instance: str = "") -> str:
    """Salva (ou renomeia) um contato na agenda da instância.

    numero: telefone internacional (ex.: 5511999999999) ou JID @s.whatsapp.net.
    nome: nome a gravar. Chamar de novo com o mesmo número troca o nome.
    instance: nome, número ou token da instância; vazio usa a padrão.
    """
    try:
        _, token = await uazapi.resolver_instancia(instance)
        d = await uazapi.post("/contact/add", {"number": numero, "name": nome}, token)
        return f"Contato salvo: {numero} -> {nome!r}. Resposta: {d}"
    except Exception as e:
        return _erro(e)


def _labels_de(d: Any) -> list[dict]:
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("labels") or d.get("data") or []
    return []


def _label_id(l: dict) -> str:
    return str(l.get("labelid") or l.get("LabelID") or l.get("id") or "")


@mcp.tool()
async def list_labels(instance: str = "") -> str:
    """Lista as etiquetas (labels) da instância com id, nome e cor.

    Etiquetas existem só no WhatsApp Business. Use os ids em label_chat.
    """
    try:
        _, token = await uazapi.resolver_instancia(instance)
        labels = _labels_de(await uazapi.get("/labels", token))
        if not labels:
            return "Nenhuma etiqueta encontrada nesta instância."
        L = ["| id | nome | cor |", "|---|---|---|"]
        for l in labels:
            L.append(f"| {_label_id(l)} | {l.get('name') or l.get('Name') or '-'} | "
                     f"{l.get('color', l.get('Color', '-'))} |")
        return "\n".join(L)
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def edit_label(nome: str = "", labelid: str = "new", cor: int = -1,
                     apagar: bool = False, instance: str = "") -> str:
    """Cria, renomeia ou apaga uma etiqueta.

    labelid: 'new' cria (padrão); um id existente edita/apaga.
    nome: nome da etiqueta. cor: 0-19 (-1 mantém/omite). apagar: True apaga labelid.
    Ao criar, se já existir etiqueta com o mesmo nome, não duplica.
    """
    try:
        _, token = await uazapi.resolver_instancia(instance)
        if labelid == "new":
            if apagar:
                return "ERRO: informe o labelid real para apagar."
            if not nome:
                return "ERRO: informe `nome` para criar a etiqueta."
            for l in _labels_de(await uazapi.get("/labels", token)):
                if (l.get("name") or l.get("Name") or "").strip().lower() == nome.strip().lower():
                    return f"Etiqueta {nome!r} já existe (id {_label_id(l)}); nada criado."
        body: dict[str, Any] = {"labelid": labelid, "delete": apagar}
        if nome:
            body["name"] = nome
        if 0 <= cor <= 19:
            body["color"] = cor
        d = await uazapi.post("/label/edit", body, token)
        return f"OK: {d}"
    except Exception as e:
        return _erro(e)


@mcp.tool()
async def label_chat(chat: str, adicionar: str = "", remover: str = "",
                     definir: str = "", instance: str = "") -> str:
    """Aplica ou remove etiquetas de um chat (número, JID ou nome).

    Use só um modo: adicionar=<labelid>, remover=<labelid> ou
    definir='id1,id2' (substitui todas; definir='-' limpa).
    """
    try:
        modos = [m for m in (adicionar, remover, definir) if m]
        if len(modos) != 1:
            return "ERRO: use exatamente um de adicionar, remover ou definir."
        _, token, c = await _ctx(instance, chat)
        body: dict[str, Any] = {"number": c.get("wa_chatid")}
        if adicionar:
            body["add_labelid"] = adicionar
        elif remover:
            body["remove_labelid"] = remover
        else:
            body["labelids"] = [] if definir.strip() == "-" else [x.strip() for x in definir.split(",") if x.strip()]
        d = await uazapi.post("/chat/labels", body, token)
        return f"OK ({uazapi.nome_do_chat(c)}): {d}"
    except Exception as e:
        return _erro(e)


def main() -> None:
    # httpx loga cada URL em INFO; isso gravaria os links publicos de mídia no log do
    # cliente MCP. Só avisos e erros.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
