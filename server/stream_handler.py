"""
Contrato com o cliente (definido em client/socket_client.py):
  - `obter_info_streaming` retorna o JSON que o cliente espera receber antes do WAV/MP3.
    Os campos obrigatórios são: nome_arquivo, tamanho, taxa_bytes, tamanho_chunk

  - `enviar_arquivo_streaming` envia o WAV/MP3 inteiro (cabeçalho + dados),
     agora em datagramas UDP no formato [seq (4 bytes, big-endian)][até `tamanho_chunk` bytes do arquivo].
    No final, envia um datagrama de fim (seq = SEQ_FIM, sem payload) 3 vezes.
    O cliente faz o parsing do cabeçalho sozinho.

O total de bytes de arquivo enviados é exatamente "tamanho".
"""

import socket
import struct
import threading
import time
import wave
from pathlib import Path

import miniaudio

from config import (
    DIRETORIO_AUDIO,
    EXTENSOES_SUPORTADAS,
    FALLBACK_BITRATE_KBPS,
    SEGUNDOS_PRE_BUFFER,
    TAMANHO_BUFFER,
)

# Cabeçalho de cada datagrama;
CABECALHO = struct.Struct("!I")
# Número de sequência pro cliente detectar detectar perda, duplicata e afins (UDP n faz isso sozinho)
SEQ_FIM = 0xFFFFFFFF

# Payload máximo por datagrama;
# Mais que 1400 bytes o IP fragmenta o pacote, perdendo o datagrama todo
TAMANHO_DATAGRAMA = min(TAMANHO_BUFFER, 1400)

# No pré-buffer o envio é até N vezes mais rápido que o tempo real;
# Em UDP uma rajada pode estourar o buffer de recepção do SO do cliente
# e os datagramas acabam sendo descartados
_FATOR_PREBUFFER = 4

# Função que retorna nomes dos arquivos de áudio disponíveis (ordem alfabética)
def listar_arquivos_audio():
    if not DIRETORIO_AUDIO.is_dir():
        return []
    return sorted(
        p.name
        for p in DIRETORIO_AUDIO.iterdir()
        if p.is_file() and p.suffix.lower() in EXTENSOES_SUPORTADAS
    )

# Função que encontra o caminho absoluto de um arquivo dentro de DIRETORIO_AUDIO;
def obter_caminho_arquivo(name: str) -> Path | None:
    """
    OBS: Essa função rejeita nomes vazios, extensões não suportadas, path
    traversal (ex.: "../../etc/passwd") e arquivos inexistentes
    """
    if not name:
        return None
    path = (DIRETORIO_AUDIO / name).resolve()

    # Confina o arquivo dentro de DIRETORIO_AUDIO (proteção contra traversal).
    try:
        path.relative_to(DIRETORIO_AUDIO.resolve())
    except ValueError:
        return None

    if path.suffix.lower() in EXTENSOES_SUPORTADAS and path.is_file():
        return path
    return None

# Função que retorna os bytes por segundo que o áudio consome
def obter_taxa_transmissao(path: Path) -> int:
    """
    Para .wav: lê do cabeçalho.
    Para .mp3: tamanho do arquivo / duração.
    Se falhar: usa FALLBACK_BITRATE_KBPS.
    """
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                rate = w.getframerate() * w.getnchannels() * w.getsampwidth()
                if rate > 0:
                    return rate
        except (wave.Error, EOFError):
            pass
    elif path.suffix.lower() == ".mp3":
        try:
            duracao = miniaudio.mp3_get_file_info(str(path)).duration
            if duracao > 0:
                return int(path.stat().st_size / duracao)
        except miniaudio.MiniaudioError:
            pass
    return FALLBACK_BITRATE_KBPS * 1000 // 8

# Função que retorna um JSON de metadados que o cliente recebe antes do WAV
def obter_info_streaming(path: Path) -> dict:
    """
    OBS: O cliente usa:
      - `taxa_bytes` - dimensionar o Buffer (taxa_bytes * SEGUNDOS_PRE_BUFFER)
      - `tamanho` - detectar fim de transferência truncada
      - `tamanho_chunk` - agora obrigatório: é o tamanho do payload de cada datagrama,
        usado pelo cliente para preencher com silêncio o espaço de um datagrama perdido
    """
    return {
        "nome_arquivo": path.name,
        "tamanho": path.stat().st_size,
        "taxa_bytes": obter_taxa_transmissao(path),
        "tamanho_chunk": TAMANHO_DATAGRAMA,
    }

# Função que envia o arquivo em datagramas de TAMANHO_DATAGRAMA, em ritmo contínuo
def enviar_arquivo_streaming(
    sock: socket.socket,
    addr,
    path: Path,
    taxa_bytes: int,
    stop_event: threading.Event,
) -> int:
    """
    Estratégia:
      - os primeiros `bytes_prebuffer` bytes são enviados a até _FATOR_PREBUFFER vezes
        a velocidade real, pra preencher o buffer do cliente sem atrasar o início
      - depois, cada chunk é liberado de acordo com um relógio absoluto
        (comeco + bytes_já_enviados / taxa); Com isso, atrasos pontuais são
        compensados no chunk seguinte e o cliente fica sempre `pré-buffer`
        segundos à frente, sem desvio acumulado.
    """
    bytes_prebuffer = int(taxa_bytes * SEGUNDOS_PRE_BUFFER)
    bytes_enviados = 0
    seq = 0
    terminou_arquivo = False
    comeco = time.monotonic()

    with open(path, "rb") as f:
        while not stop_event.is_set():
            chunk = f.read(TAMANHO_DATAGRAMA)
            if not chunk:
                terminou_arquivo = True
                break

            if bytes_enviados < bytes_prebuffer:
                liberar_em = comeco + bytes_enviados / (taxa_bytes * _FATOR_PREBUFFER)
            else:
                liberar_em = comeco + (bytes_enviados - bytes_prebuffer) / taxa_bytes
            delay = liberar_em - time.monotonic()
            if delay > 0 and stop_event.wait(delay):
                break  # caso do servidor sendo desligado ou cliente enviou STOP

            sock.sendto(CABECALHO.pack(seq) + chunk, addr)
            seq += 1
            bytes_enviados += len(chunk)

    # Aqui o fim é avisado com um datagrama especial;
    # ele tá sendo enviado 3 vezes porque ele próprio pode ser perdido
    if terminou_arquivo:
        for _ in range(3):
            sock.sendto(CABECALHO.pack(SEQ_FIM), addr)
            time.sleep(0.05)

    return bytes_enviados
