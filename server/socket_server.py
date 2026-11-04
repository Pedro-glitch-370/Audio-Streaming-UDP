"""
Visão Geral do Protocolo de Comunicação, que é baseado em linha de comando:

    As solicitações de reprodução e os dados retornados são:
    Cliente → Servidor:   PLAY <arquivo>\n
    Servidor → Cliente:   {"status":"ok", "nome_arquivo":..., "tamanho":...,
                           "taxa_bytes":..., "tamanho_chunk":...}\n
    Servidor → Cliente:   <WAV ou MP3 inteiro: cabeçalho + dados>

    As listagens de arquivos e os dados retornados são:
    Cliente → Servidor:   LIST\n
    Servidor → Cliente:   {"status":"ok", "files":[...]}\n

    Tratamento de erro:
    Erros:                {"status":"error", "message":"..."}\n

O cliente que acompanha este servidor (`client/socket_client.py`) faz parsing manual do WAV
e decodificação do MP3, então, depois do JSON enviado, o servidor não deve enviar nada além
do arquivo bruto para o cliente.
"""
import json
import socket
import threading

from config import (
    DEFAULT_AUDIO,
    HANDSHAKE_TIMEOUT,
    HOST,
    MAX_CLIENTS,
    PORT,
    SEND_TIMEOUT,
)
from server import stream_handler

# Limita o número de clientes simultâneos.
vagas_clientes = threading.BoundedSemaphore(MAX_CLIENTS)

# Sinaliza o loop principal (acept) para parar.
server_evento_stop = threading.Event()

# Função que serializa um dict como JSON em uma linha (terminada em \\n).
def _enviar_linha_json(conn: socket.socket, payload: dict):
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")

# Função que roda em thread própria atendendo um cliente até o fim da conexão
def handle_client(conn: socket.socket, addr):
    tag = f"[{addr[0]}:{addr[1]}]"
    print(f"{tag} conectado")

    try:
        # TCP_NODELAY: não espera acumular pacotes (latência menor).
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Timeout para o handshake (cliente enviar o comando inicial).
        conn.settimeout(HANDSHAKE_TIMEOUT)

        requisicao = (
            conn.makefile("rb")
            .readline(1024)
            .decode("utf-8", "replace")
            .strip()
        )

        # A partir daqui o timeout é para envio de dados pesados.
        conn.settimeout(SEND_TIMEOUT)

        comando, _, argumento = requisicao.partition(" ")
        comando = comando.upper()

        if comando == "LIST":
            arquivos = stream_handler.listar_arquivos_audio()
            _enviar_linha_json(conn, {"status": "ok", "files": arquivos})

        elif comando == "PLAY":
            nome = argumento.strip() or DEFAULT_AUDIO
            path = stream_handler.obter_caminho_arquivo(nome)

            if path is None:
                _enviar_linha_json(
                    conn,
                    {"status": "error", "message": f"Arquivo não encontrado: {nome}"},
                )
                return

            info = stream_handler.obter_info_streaming(path)
            _enviar_linha_json(conn, {"status": "ok", **info})
            print(f"{tag} enviando {path.name} ({info['tamanho']} bytes)")

            enviados = stream_handler.enviar_arquivo_streaming(conn, path, info["taxa_bytes"], server_evento_stop)
            status = "concluída" if enviados == info["tamanho"] else "interrompida"
            print(f"{tag} transmissão {status}, {enviados} bytes")

        else:
            _enviar_linha_json(conn,{"status": "error","message": "Comando inválido. Use LIST ou PLAY <arquivo>",})

    except (BrokenPipeError, ConnectionResetError, socket.timeout) as e:
        # Cliente desconectou ou parou de responder (é esperado).
        print(f"{tag} desconectou ou ficou inativo ({e.__class__.__name__})")
    except Exception as e:
        print(f"{tag} erro: {e!r}")
    finally:
        try:
            conn.close()
        finally:
            vagas_clientes.release()
        print(f"{tag} encerrado")

# Função que representa o loop principal;
# Ela aceita conexões e delega cada uma para uma thread
def rodar_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(socket.SOMAXCONN)

    # Timeout curto permite checar server_evento_stop entre aceitações.
    server.settimeout(1.0)

    print(f"Server escutando em {HOST}:{PORT}.")

    try:
        while not server_evento_stop.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket fechado por outro motivo; encerra

            if not vagas_clientes.acquire(blocking=False):
                try:
                    _enviar_linha_json(
                        conn,
                        {"status": "error", "message": "Servidor lotado"},
                    )
                except OSError:
                    pass
                conn.close()
                print(f"[{addr[0]}:{addr[1]}] recusado: servidor lotado")
                continue

            threading.Thread(
                target=handle_client, args=(conn, addr), daemon=True
            ).start()

    finally:
        server_evento_stop.set()
        server.close()
        print("Servidor desligado")
