import os
import select
import sys
import time

from client import playlist, socket_client

# Função pra montar o texto do menu a partir da lista de arquivos do servidor
def montar_menu(arquivos):
    linhas = [" =========== SPOTCIN ==========="]
    for i, nome in enumerate(arquivos, start=1):
        linhas.append(f" [{i}] {nome}")
    linhas.append(" [0] Sair ")
    return "\n".join(linhas)

# Função pra limpar a tela em Windows (cls) e Unix (clear)
def limpar_terminal():
    os.system("cls" if os.name == "nt" else "clear")

# Função que retorna True se o Enter for pressionado até o tempo que foi setado, sem travar como o input()
def enter_pressionado(espera: float) -> bool:
    # Bloco pra sistemas Windows
    if os.name == "nt":
        import msvcrt

        time.sleep(espera)
        while msvcrt.kbhit():
            if msvcrt.getwch() in "\r\n":
                return True
        return False

    # Bloco pra sistemas Linux/macOS
    if select.select([sys.stdin], [], [], espera)[0]:
        sys.stdin.readline()
        return True
    return False

# Função que faz o ciclo completo de uma música;
# Conecta, espera o pré-buffer, toca até o Enter
# for tocado ou a música acabar e garante a limpeza no finally
def tocar_musica(caminho: str):
    sessao = socket_client.iniciar_streaming(caminho)
    reproducao = None 

    try:
        print(" Carregando buffer...")
        sessao.buffer.aguardar_prebuffer()

        # Se a rede falhou antes do pré-buffer encher, aborta aqui
        if sessao.get_erro():
            raise RuntimeError(sessao.get_erro())

        # Começa a tocar. A partir daqui a reprodução roda em uma thread própria do sounddevice;
        # Esse código vai aguardar até o Enter ser pressionado ou o áudio acabar
        reproducao = playlist.iniciar_reproducao(sessao.buffer, sessao.formato)

        print(" Pressione ENTER a qualquer momento para parar a música.")
        while reproducao.active and not enter_pressionado(0.1):
            pass

        # Tratamento de erro
        if sessao.get_erro():
            print(f" --- Transmissão interrompida: {sessao.get_erro()} --- \n")
        print(" --- Áudio encerrado! --- \n")

    finally:
        # Cleanup onde encerra o stream de áudio e avisa o servidor
        if reproducao:
            reproducao.stop()
            reproducao.close()
        sessao.parar()

# Função principal; revela o que o servidor oferece
def main():
    try:
        arquivos = socket_client.listar_arquivos()
    except (OSError, RuntimeError, ConnectionError) as e:
        print(f"\n --- Não foi possível conectar ao servidor: {e} --- \n")
        return

    if not arquivos:
        print("\n --- Servidor não tem nenhum áudio disponível --- \n")
        return

    menu = montar_menu(arquivos)

    while True:
        limpar_terminal()
        print(menu)
        escolha = input(" Escolha uma opção: ")

        if escolha == "0":
            limpar_terminal()
            print("Até logo!!\n")
            time.sleep(2)
            limpar_terminal()
            return

        # Pra validar, o número digitado tem que ser entre 1 e len(arquivos)
        try:
            idx = int(escolha)
        except ValueError:
            idx = -1

        if not (1 <= idx <= len(arquivos)):
            print("\n --- Opção inválida! Tente novamente --- \n")
            time.sleep(2)
            continue

        titulo = arquivos[idx - 1]        # nome do arquivo = "título" exibido
        caminho = arquivos[idx - 1]       # o que vai pro servidor no PLAY

        limpar_terminal()
        print(f" --- Tocando agora: {titulo} --- \n")

        try:
            tocar_musica(caminho)
        except KeyboardInterrupt:
            print("\n --- Interrompido pelo usuário --- \n")
        except (OSError, RuntimeError, ValueError) as e:
            print(f"\n --- Erro: {e} --- \n")

        time.sleep(1)

# Só roda se o arquivo for executado diretamente
if __name__ == "__main__":
    main()
