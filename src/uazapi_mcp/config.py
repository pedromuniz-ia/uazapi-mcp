"""Configuração do servidor.

Ordem de precedencia:
1. variáveis de ambiente já exportadas (é o que o cliente MCP injeta via `--env`);
2. `~/.uazapi-mcp/.env`, para configurar uma vez na máquina;
3. `~/.claude/.env`, conveniência para quem já guarda chaves ali.

O `.env` do diretório de trabalho NÃO é lido de propósito: o cliente MCP inicia o
servidor com o cwd do projeto aberto, e um `.env` alheio não pode redirecionar
UAZAPI_SERVER (e com ele o token) para outro lugar. Em desenvolvimento, use
`uv run --env-file .env uazapi-mcp`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HOME_DIR = Path.home() / ".uazapi-mcp"
_ENV_FILES = [
    HOME_DIR / ".env",
    Path.home() / ".claude" / ".env",
]

# Cerquilha é caractere válido dentro de um token, então só abre comentário quando
# vem depois de espaço em branco — é o critério do python-dotenv.
_COMENTARIO_INLINE = re.compile(r"\s+#.*$", re.DOTALL)


def _valor(bruto: str) -> str:
    """Valor de uma linha do `.env`, no mesmo critério do python-dotenv.

    Entre aspas, o valor é o que está dentro delas e o resto da linha é comentário:
    `CHAVE="valor # com cerquilha"` preserva a cerquilha. Sem aspas, corta no primeiro
    `#` precedido de espaço — sem isso, `CHAVE=abc  # comentário` viraria um token com
    o comentário grudado, e `CHAVE=  # sem chave ainda` viraria o comentário inteiro.
    """
    texto = bruto.lstrip()
    aspa = texto[:1]
    if aspa in ("'", '"'):
        fim = texto.find(aspa, 1)
        if fim != -1:
            return texto[1:fim]
    return _COMENTARIO_INLINE.sub("", bruto).strip()


def _carregar_env() -> None:
    for path in _ENV_FILES:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                v = _valor(v)
                if k and v and k not in os.environ:
                    os.environ[k] = v
        except OSError:
            continue


_carregar_env()

SERVER = os.environ.get("UAZAPI_SERVER", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("UAZAPI_ADMIN_TOKEN", "")
INSTANCE_TOKEN = os.environ.get("UAZAPI_TOKEN", "")
DEFAULT_INSTANCE = os.environ.get("UAZAPI_DEFAULT_INSTANCE", "")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
# Modelos da AssemblyAI em ordem de fallback (campo `speech_models`), separados por vírgula,
# ex.: `universal-3-5-pro,universal-2`. Vazio = padrão da AssemblyAI, que já tenta o melhor.
ASSEMBLYAI_SPEECH_MODELS = [m.strip() for m in
                            os.environ.get("ASSEMBLYAI_SPEECH_MODELS", "").split(",") if m.strip()]
ASSEMBLYAI_LANGUAGE = os.environ.get("ASSEMBLYAI_LANGUAGE", "pt")
MEDIA_DIR = Path(os.environ.get("UAZAPI_MCP_MEDIA_DIR") or (HOME_DIR / "media")).expanduser()

# Pastas de onde send_media pode ler arquivos locais (separadas por os.pathsep). Fora
# delas, nada sai: sem isso, uma mensagem recebida com prompt injection faria o agente
# enviar o próprio .env, chaves SSH ou qualquer arquivo do usuário do processo.
SEND_DIRS = [Path(p).expanduser() for p in
             os.environ.get("UAZAPI_MCP_SEND_DIRS", "").split(os.pathsep) if p.strip()] or [MEDIA_DIR]

# Chats (wa_chatid) que recebem envio sem aprovação humana, para bots autônomos.
# Vazio = todo envio pede aprovação ao usuário no cliente MCP.
SEND_ALLOWED_CHATS = frozenset(
    c.strip() for c in os.environ.get("UAZAPI_MCP_SEND_ALLOWED_CHATS", "").split(",") if c.strip())

# Limites de segurança: impedem que uma chamada solta puxe a base inteira.
MAX_MENSAGENS = int(os.environ.get("UAZAPI_MCP_MAX_MENSAGENS", "3000"))
PAGINA = 500
MAX_TRANSCRICOES = int(os.environ.get("UAZAPI_MCP_MAX_TRANSCRICOES", "30"))
MAX_DOWNLOADS = int(os.environ.get("UAZAPI_MCP_MAX_DOWNLOADS", "60"))


def checar_servidor() -> None:
    """Erro útil quando o servidor não foi configurado, em vez de um 404 cru."""
    if not SERVER:
        raise RuntimeError(
            "UAZAPI_SERVER não configurado. Informe o servidor da sua conta uazapi "
            "(ex.: https://suaempresa.uazapi.com) no cliente MCP:\n"
            "  claude mcp add uazapi --env UAZAPI_SERVER=https://suaempresa.uazapi.com "
            "--env UAZAPI_ADMIN_TOKEN=seu_token -- uvx --from "
            "git+https://github.com/pedromuniz-ia/uazapi-mcp uazapi-mcp"
        )


def diagnostico() -> dict[str, str]:
    """Estado da configuração, sem revelar segredos."""
    def marca(v: str) -> str:
        return f"definido ({len(v)} caracteres)" if v else "não definido"

    return {
        "UAZAPI_SERVER": SERVER or "não definido",
        "UAZAPI_ADMIN_TOKEN": marca(ADMIN_TOKEN),
        "UAZAPI_TOKEN": marca(INSTANCE_TOKEN),
        "UAZAPI_DEFAULT_INSTANCE": DEFAULT_INSTANCE or "não definido",
        "ASSEMBLYAI_API_KEY": marca(ASSEMBLYAI_API_KEY),
        "modelo de transcrição": (f"{','.join(ASSEMBLYAI_SPEECH_MODELS) or 'padrão da AssemblyAI'}"
                                  f" ({ASSEMBLYAI_LANGUAGE})"),
        "pasta de mídias": str(MEDIA_DIR),
        "pastas liberadas para send_media": os.pathsep.join(map(str, SEND_DIRS)),
        "chats sem aprovação humana": str(len(SEND_ALLOWED_CHATS)),
    }
