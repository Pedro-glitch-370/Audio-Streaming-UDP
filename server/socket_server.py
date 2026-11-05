"""
Visão Geral do Protocolo de Comunicação (um datagrama é uma mensagem):

    UDP não tem conexão, então o cliente é identificado pelo endereço de origem (ip, porta)

    As solicitações de reprodução e os dados retornados são:
    Cliente → Servidor:   PLAY <arquivo>
    Servidor → Cliente:   {"status":"ok", "nome_arquivo":..., "tamanho":...,
                           "taxa_bytes":..., "tamanho_chunk":...} (1 datagrama)
    Servidor → Cliente:   [seq(4 bytes, big-endian)][pedaço do WAV/MP3] (N datagramas)
    Servidor → Cliente:   [seq = 0xFFFFFFFF] (fim, enviado 3x)

    Cliente → Servidor:   STOP (cancela o streaming em andamento)

    As listagens de arquivos e os dados retornados são:
    Cliente → Servidor:   LIST
    Servidor → Cliente:   {"status":"ok", "files":[...]}

    Tratamento de erro:
    Erros:                {"status":"error", "message":"..."}
"""
import json
import socket
import threading

from config import DEFAULT_AUDIO, HOST, MAX_CLIENTS, PORT
from server import stream_handler

# Limita o número de clientes simultâneos.
vagas_clientes = threading.BoundedSemaphore(MAX_CLIENTS)

# Sinaliza o loop principal para parar.
server_evento_stop = threading.Event()

# Esses substituem o "fechar a conexão" do TCP
_sessoes: dict[tuple, threading.Event] = {}
_trava_sessoes = threading.Lock()

# Função que serializa um dict como JSON e envia num só datagrama
# Substitui o "_enviar_linha_json" do TCP
def _enviar_json(sock: socket.socket, addr, payload: dict):
    sock.sendto(json.dumps(payload).encode("utf-8"), addr)

# Função que roda em thread própria transmitindo um arquivo para um cliente
def atender_play(sock: socket.socket, addr, nome: str, parar: threading.Event):
    tag = f"[{addr[0]}:{addr[1]}]"
    print(f"{tag} conectado")

    try:
        nome = nome or DEFAULT_AUDIO
        path = stream_handler.obter_caminho_arquivo(nome)

        if path is None:
            _enviar_json(sock, addr, {"status": "error", "message": f"Arquivo não encontrado: {nome}"})
            return

        info = stream_handler.obter_info_streaming(path)
        _enviar_json(sock, addr, {"status": "ok", **info})
        print(f"{tag} enviando {path.name} ({info['tamanho']} bytes)")

        enviados = stream_handler.enviar_arquivo_streaming(sock, addr, path, info["taxa_bytes"], parar)
        status = "concluída" if enviados == info["tamanho"] else "interrompida"
        print(f"{tag} transmissão {status}, {enviados} bytes")

    except OSError as e:
        print(f"{tag} erro de rede ({e.__class__.__name__})")
    except Exception as e:
        print(f"{tag} erro: {e!r}")
    finally:
        with _trava_sessoes:
            _sessoes.pop(addr, None)
        vagas_clientes.release()
        print(f"{tag} encerrado")

# Função que interpreta um datagrama recebido e age conforme o comando
def _tratar_requisicao(sock: socket.socket, dados: bytes, addr):
    requisicao = dados.decode("utf-8", "replace").strip()
    comando, _, argumento = requisicao.partition(" ")
    comando = comando.upper()
    argumento = argumento.strip()

    if comando == "LIST":
        arquivos = stream_handler.listar_arquivos_audio()
        _enviar_json(sock, addr, {"status": "ok", "files": arquivos})

    elif comando == "PLAY":
        with _trava_sessoes:
            if addr in _sessoes:
                return  # caso de cliente já tá sendo atendido
            if not vagas_clientes.acquire(blocking=False):
                _enviar_json(sock, addr, {"status": "error", "message": "Servidor lotado"})
                print(f"[{addr[0]}:{addr[1]}] recusado: servidor lotado")
                return
            parar = threading.Event()
            _sessoes[addr] = parar

        threading.Thread(target=atender_play, args=(sock, addr, argumento, parar), daemon=True).start()

    elif comando == "STOP":
        with _trava_sessoes:
            parar = _sessoes.get(addr)
        if parar:
            parar.set()

    else:
        _enviar_json(
            sock,
            addr,
            {"status": "error", "message": "Comando inválido. Use LIST ou PLAY <arquivo>"},
        )


# Função que representa o loop principal;
# Ela recebe datagramas num único socket e delega cada PLAY para uma thread
def rodar_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))

    # Timeout curto permite checar server_evento_stop entre recebimentos.
    server.settimeout(1.0)

    print(f"Server escutando em {HOST}:{PORT} (UDP).")

    try:
        while not server_evento_stop.is_set():
            try:
                dados, addr = server.recvfrom(1024)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue  # caso de Windows quando a porta do cliente supostamente já saiu
            except OSError:
                break  # socket fechado por outro motivo; encerra

            try:
                _tratar_requisicao(server, dados, addr)
            except OSError as e:
                print(f"[{addr[0]}:{addr[1]}] erro ao responder ({e.__class__.__name__})")

    finally:
        server_evento_stop.set()
        with _trava_sessoes:
            for parar in _sessoes.values():
                parar.set()
        server.close()
        print("Servidor desligado")
