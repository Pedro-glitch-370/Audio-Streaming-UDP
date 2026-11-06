from pathlib import Path

DIRETORIO_BASE = Path(__file__).resolve().parent

# ── Rede ─────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8080
TAMANHO_BUFFER = 8192            # bytes por chunk enviado/recebido
MAX_CLIENTS = 50                 # conexões simultâneas no servidor
HANDSHAKE_TIMEOUT = 10           # segundos para o cliente enviar o comando
SEND_TIMEOUT = 30                # segundos sem conseguir enviar → derruba

# ── Áudio ────────────────────────────────────────────────────────────
DIRETORIO_AUDIO = DIRETORIO_BASE / "audio_files"
EXTENSOES_SUPORTADAS = (".wav", ".mp3")

# Nome deve bater exatamente com um arquivo real (case-sensitive no Linux).
DEFAULT_AUDIO = "(Zelda) Kokiri Forest.wav"

# ── Streaming ────────────────────────────────────────────────────────
# O cliente usa este valor para dimensionar o pré-buffer local.
SEGUNDOS_PRE_BUFFER = 4.0
VELOCIDADE_PREBUFFER = 1_000_000 # bytes por segundos no pré-buffer

# Usado para estimar taxa_bytes quando o formato não permite calcular
# (ex.: MP3 ou WAV corrompido). Deve ser compatível com o arquivo real,
# senão o servidor envia mais rápido/lento do que o cliente consome.
FALLBACK_BITRATE_KBPS = 128
