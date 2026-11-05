import io
import json
import socket
import struct
import threading
import wave
from dataclasses import dataclass

import miniaudio

from config import HOST, PORT, SEGUNDOS_PRE_BUFFER
from client.playback_buffer import Buffer

# Mapeia bytes por amostra → dtype do sounddevice.
# Só cobre PCM inteiro; WAV float32 exige parser manual (wave da stdlib não lê).
_DTYPES_PCM = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}

# MP3 é decodificado no cliente para PCM neste formato fixo.
# Se o arquivo tiver outra taxa/canais, o miniaudio converte.
_FORMATO_MP3 = {
    "canais": 2,
    "taxa_amostragem": 44100,
    "bytes_amostra": 2,
    "dtype": "int16",
}
_TAXA_PCM_MP3 = 44100 * 2 * 2  # bytes por segundo do MP3 já decodificado

# Mesmo formato do servidor: [seq (4 bytes)][payload]; seq = SEQ_FIM marca o fim.
_CAB = struct.Struct("!I")
_SEQ_FIM = 0xFFFFFFFF

_TIMEOUT_SOCKET = 0.5        # cada recv espera no máximo isso
_TIMEOUT_INATIVIDADE = 5.0   # timeout de erro se n tem nenhum datagrama por tanto tempo

# Classe interna que entrega os bytes ao decodificador MP3 conforme eles chegam do socket
class _FonteRede(miniaudio.StreamableSource):

    def __init__(self, ler):
        self.ler = ler

    def read(self, num_bytes: int) -> bytes:
        return self.ler(num_bytes)

# Função que lê o cabeçalho do WAV e deixa `fp` posicionado no primeiro byte de áudio
def _ler_cabecalho_wav(fp):
    wav = wave.open(fp, "rb")

    if wav.getcomptype() != "NONE":
        raise ValueError(f"WAV comprimido não suportado: {wav.getcomptype()}")

    canais = wav.getnchannels()
    taxa = wav.getframerate()
    bytes_amostra = wav.getsampwidth()

    if bytes_amostra not in _DTYPES_PCM:
        raise ValueError(f"WAV com {bytes_amostra * 8} bits não suportado")

    return {
        "canais": canais,
        "taxa_amostragem": taxa,
        "bytes_amostra": bytes_amostra,
        "dtype": _DTYPES_PCM[bytes_amostra],
        "tamanho_dados": wav.getnframes() * canais * bytes_amostra,
    }

# Função que envia um comando e devolve o primeiro datagrama de resposta;
# Como o comando ou a resposta podem se perder, ela transmite dnv se nada chegar
def _handshake(sock: socket.socket, comando: bytes, tentativas=3, espera=2.0) -> bytes:
    sock.settimeout(espera)
    for _ in range(tentativas):
        sock.send(comando)
        try:
            return sock.recv(65535)
        except socket.timeout:
            continue
    raise ConnectionError("Servidor não respondeu")

# Classe que carrega tudo que a main precisa pra tocar e parar um áudio
@dataclass
class Sessao:
    """
    OBS: Os elementos que essa classe agrupa são:
      - o Buffer (consumido pelo player)
      - os metadados de formato
      - o socket (UDP "conectado" ao servidor)
      - o estado interno da thread de rede (parado / erro)
    """

    buffer: Buffer
    formato: dict
    sock: socket.socket
    _parado: bool = False
    _erro: str | None = None

    # Função que retorna uma mensagem de erro ou None;
    # A main chama ela após o streaming do áudio
    def get_erro(self):
        return self._erro

    # Função que encerra a thread de rede;
    # A main chama ela no finally
    def parar(self):
        self._parado = True
        try:
            # Em UDP fechar o socket não avisa o servidor: é preciso pedir o fim.
            # A thread de rede percebe _parado em até _TIMEOUT_SOCKET e fecha o socket.
            self.sock.send(b"STOP")
        except OSError:
            pass  # socket já foi fechado pela thread

# Função que envia PLAY ao servidor, lê o cabeçalho
# e dispara a thread que recebe o áudio em segundo plano
def iniciar_streaming(nome_arquivo: str) -> Sessao:

    # connect no UDP não abre conexão, só fixa o destino
    # e faz o SO descartar datagramas de outro endereço
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((HOST, PORT))

    inicial = b"" # bytes de áudio que vieram com o cabeçalho do .wav
    proximo_seq = 0 # próximo número de sequência que é esperado

    try:
        # JSON datagrama de resposta, vem com status e metadados
        info = json.loads(_handshake(sock, f"PLAY {nome_arquivo}".encode()))
        if info.get("status") != "ok":
            raise RuntimeError(info.get("message", "Erro desconhecido do servidor"))

        eh_mp3 = info["nome_arquivo"].lower().endswith(".mp3")

        if eh_mp3:
            # Se for .mp3, o arquivo inteiro é dado de áudio
            formato = dict(_FORMATO_MP3, tamanho_dados=info["tamanho"])
        else:
            # Se for .wav, o cabeçalho vai tá no datagrama de seq 0
            sock.settimeout(_TIMEOUT_INATIVIDADE)
            datagrama = sock.recv(65535)
            seq, payload = _CAB.unpack_from(datagrama)[0], datagrama[_CAB.size:]
            if seq != 0:
                raise ConnectionError("Datagrama com o cabeçalho do WAV foi perdido")
            cabecalho = io.BytesIO(payload)
            formato = _ler_cabecalho_wav(cabecalho)
            inicial = payload[cabecalho.tell():]  # áudio que sobrou no msm datagrama
            proximo_seq = 1

    except Exception:
        sock.close()
        raise

    # Quantos bytes queremos acumular antes de liberar a reprodução.
    # No MP3 o buffer guarda o áudio já decodificado, então usa a taxa do PCM.
    taxa_buffer = _TAXA_PCM_MP3 if eh_mp3 else info["taxa_bytes"]
    buffer = Buffer(int(taxa_buffer * SEGUNDOS_PRE_BUFFER))
    sessao = Sessao(buffer=buffer, formato=formato, sock=sock)

    # Tamanho do payload de cada datagrama, bom pra preencher as perdas
    tam_payload = info["tamanho_chunk"]

    # Quantos bytes ainda faltam receber;
    # se o servidor informou um tamanho de dados válido e menor
    # que o total, ele é usado pra saber quando parar
    restante = formato["tamanho_dados"] or None
    if restante is None or restante > info["tamanho"]:
        restante = None  # espera o datagrama de fim

    if restante is not None:
        inicial = inicial[:restante]
        restante -= len(inicial)

    sock.settimeout(_TIMEOUT_SOCKET)

    # Função que devolve os próximos bytes do arquivo na ordem;
    # retorna None quando o stream acaba
    def proximo_payload() -> bytes | None:
        nonlocal proximo_seq, restante
        ocioso = 0.0

        while not sessao._parado and restante != 0:
            try:
                datagrama = sock.recv(65535)
            except socket.timeout:
                ocioso += _TIMEOUT_SOCKET
                if ocioso >= _TIMEOUT_INATIVIDADE:
                    sessao._erro = "Servidor parou de enviar dados"
                    return None
                continue
            except OSError as e:
                if not sessao._parado:
                    sessao._erro = f"Erro ao receber dados: {e}"
                return None

            if len(datagrama) < _CAB.size:
                continue
            seq = _CAB.unpack_from(datagrama)[0]
            payload = datagrama[_CAB.size:]

            if seq == _SEQ_FIM:
                if restante:
                    sessao._erro = f"Transmissão encerrada com {restante} bytes faltando"
                return None
            if seq < proximo_seq:
                continue  # duplicado ou atrasado demais

            # Datagramas que vieram entre o esperado e o recebido foram perdidos
            perdidos = (seq - proximo_seq) * tam_payload
            proximo_seq = seq + 1

            if restante is not None:
                perdidos = min(perdidos, restante)
                restante -= perdidos
                payload = payload[:restante]
                restante -= len(payload)

            # se for .wav, preenche a perda com silêncio pra manter o alinhamento
            # se for .mp3, não dá pra inventar nada pra tapar
            return (b"" if eh_mp3 else bytes(perdidos)) + payload

        return None

    # Função que avisa o fim ao buffer e libera os recursos
    def encerrar():
        buffer.finalizar()
        try:
            sock.send(b"STOP")  # garante que o servidor não vai continuar enviando
        except OSError:
            pass
        sock.close()

    # Função que recebe os datagramas e joga no buffer até acabar ou parar
    def receber():
        try:
            if inicial:
                buffer.adicionar_chunk(inicial)
            while True:
                dados = proximo_payload()
                if dados is None:
                    break
                if dados:
                    buffer.adicionar_chunk(dados)
        finally:
            encerrar()

    # Função que entrega os bytes pro decodificador MP3;
    # qualquer sobra de datagrama fica nesse "resto"
    resto = bytearray()

    def ler_mp3(n: int) -> bytes:
        if sessao._parado:
            return b""
        if not resto:
            dados = proximo_payload()
            if not dados:
                return b""
            resto.extend(dados)
        saida = bytes(resto[:n])
        del resto[:n]
        return saida

    # Função que decodifica o MP3 conforme os bytes chegam
    def receber_mp3():
        try:
            decodificador = miniaudio.stream_any(
                _FonteRede(ler_mp3),
                miniaudio.FileFormat.MP3,
                nchannels=_FORMATO_MP3["canais"],
                sample_rate=_FORMATO_MP3["taxa_amostragem"],
            )
            for pcm in decodificador:
                if sessao._parado:
                    break
                buffer.adicionar_chunk(pcm.tobytes())

        except miniaudio.MiniaudioError as e:
            if not sessao._parado:
                sessao._erro = f"Erro ao decodificar MP3: {e}"

        finally:
            encerrar()

    threading.Thread(target=receber_mp3 if eh_mp3 else receber, daemon=True).start()
    return sessao

# Função que pergunta ao servidor quais arquivos ele tem disponíveis (comando LIST);
# Ela retorna uma lista de nomes ou levanta erro se o servidor responder erro
def listar_arquivos() -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((HOST, PORT))
        info = json.loads(_handshake(sock, b"LIST"))
        if info.get("status") != "ok":
            raise RuntimeError(info.get("message", "Erro desconhecido do servidor"))
        return info["files"]
    finally:
        sock.close()
