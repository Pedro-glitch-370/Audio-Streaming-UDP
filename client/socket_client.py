import json
import socket
import threading
import wave
from dataclasses import dataclass

import miniaudio

from config import HOST, PORT, TAMANHO_BUFFER, SEGUNDOS_PRE_BUFFER
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

# Classe interna que entrega os bytes ao decodificador MP3 conforme eles chegam do do socket
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

# Classe que carrega tudo que a main precisa pra tocar e parar um áudio
@dataclass
class Sessao:
    """
    OBS: Os elementos que essa classe agrupa são:
      - o Buffer (consumido pelo player)
      - os metadados de formato
      - o socket (para fechar quando a main pedir)
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

    # Função que encerra a thread de rede e o socket;
    # A main chama ela no finally
    def parar(self):
        self._parado = True
        try:
            # shutdown força o read() em andamento a retornar imediatamente.
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # socket já tá fechado
        self.sock.close()

# Função que conecta ao servidor, envia PLAY, lê cabeçalho
# e dispara a thread que baixa o áudio em segundo plano
def iniciar_streaming(nome_arquivo: str) -> Sessao:

    sock = socket.create_connection((HOST, PORT), timeout=30)

    try:
        # Comando do protocolo: "PLAY <nome_arquivo>\n"
        sock.sendall(f"PLAY {nome_arquivo}\n".encode())
        fp = sock.makefile("rb")

        # 1ª linha da resposta: JSON com status e metadados (taxa_bytes, tamanho).
        linha = fp.readline()
        if not linha:
            raise ConnectionError("Servidor fechou conexão sem responder")

        info = json.loads(linha)
        if info.get("status") != "ok":
            raise RuntimeError(info.get("message", "Erro desconhecido do servidor"))

        eh_mp3 = info["nome_arquivo"].lower().endswith(".mp3")

        if eh_mp3:
            # No MP3 o arquivo inteiro é dado de áudio.
            formato = dict(_FORMATO_MP3, tamanho_dados=info["tamanho"])
        else:
            # Depois do JSON vem o WAV cru; pulamos o cabeçalho dele.
            formato = _ler_cabecalho_wav(fp)

    except Exception:
        # Qualquer falha aqui significa que não vamos mais usar o socket.
        sock.close()
        raise

    # Quantos bytes queremos acumular antes de liberar a reprodução.
    # No MP3 o buffer guarda o áudio já decodificado, então usa a taxa do PCM.
    taxa_buffer = _TAXA_PCM_MP3 if eh_mp3 else info["taxa_bytes"]
    buffer = Buffer(int(taxa_buffer * SEGUNDOS_PRE_BUFFER))
    sessao = Sessao(buffer=buffer, formato=formato, sock=sock)

    # Quantos bytes ainda faltam receber. Se o servidor informou um tamanho
    # de dados válido e menor que o total anunciado, usamos ele para saber
    # exatamente quando parar (evita ler lixo ou ficar esperando EOF).
    restante = formato["tamanho_dados"] or None
    if restante is None or restante > info["tamanho"]:
        restante = None  # não confiável; lê até a conexão fechar

    # Função que baixa blocos e joga no buffer até acabar ou parar
    def receber():
        nonlocal restante  # modificamos o valor de fora do escopo

        try:
            while not sessao._parado and restante != 0:
                # Nunca pedir mais do que resta, se sabemos o total.
                n = TAMANHO_BUFFER if restante is None else min(TAMANHO_BUFFER, restante)
                chunk = fp.read(n)

                if not chunk:
                    # EOF inesperado: conexão fechou antes do previsto.
                    if restante and not sessao._parado:
                        sessao._erro = f"Conexão encerrada com {restante} bytes faltando"
                    break

                buffer.adicionar_chunk(chunk)
                if restante is not None:
                    restante -= len(chunk)

        except OSError as e:
            # Erro de rede (reset, timeout, etc.). Só registra se não fomos nós
            # que pedimos para parar (nesse caso, o erro é esperado).
            if not sessao._parado:
                sessao._erro = f"Erro ao receber dados: {e}"

        finally:
            # Em qualquer caso (sucesso, erro, parada), avisa o buffer que acabou
            # e libera os recursos. Sem isso o player pode travar esperando dados.
            buffer.finalizar()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            fp.close()
            sock.close()

    # Função que lê do socket sem passar do total;
    # Ela é chamada pelo decodificador MP3
    def ler_mp3(n: int) -> bytes:
        nonlocal restante

        if sessao._parado or restante == 0:
            return b""
        if restante is not None:
            n = min(n, restante)

        try:
            # read1 devolve o que já chegou, sem esperar juntar n bytes.
            chunk = fp.read1(n)
        except OSError as e:
            # O decodificador engoliria a exceção, então registramos aqui.
            if not sessao._parado:
                sessao._erro = f"Erro ao receber dados: {e}"
            return b""

        if not chunk:
            if restante and not sessao._parado:
                sessao._erro = f"Conexão encerrada com {restante} bytes faltando"
            return b""

        if restante is not None:
            restante -= len(chunk)
        return chunk

    #Função que decodifica o MP3 conforme os bytes chegam
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
            buffer.finalizar()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            fp.close()
            sock.close()

    threading.Thread(target=receber_mp3 if eh_mp3 else receber, daemon=True).start()
    return sessao

# Função que pergunta ao servidor quais arquivos ele tem disponíveis (comando LIST);
# Ela retorna uma lista de nomes ou [] se o servidor responder erro
def listar_arquivos() -> list[str]:
    sock = socket.create_connection((HOST, PORT), timeout=10)
    try:
        sock.sendall(b"LIST\n")
        linha = sock.makefile("rb").readline()
        if not linha:
            raise ConnectionError("Servidor fechou conexão sem responder")
        info = json.loads(linha)
        if info.get("status") != "ok":
            raise RuntimeError(info.get("message", "Erro desconhecido do servidor"))
        return info["files"]
    finally:
        sock.close()
