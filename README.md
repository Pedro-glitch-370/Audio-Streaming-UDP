# SPOTCIN - Audio Streaming TCP

Streaming de áudio WAV ou MP3 via sockets TCP.
O servidor divide o arquivo em blocos e envia em ritmo de reprodução (tempo real) para cada cliente.
O cliente guarda os blocos num buffer, espera um pré-buffer encher e, depois, toca na placa de som.

## Estrutura

```
audio-streaming-tcp/
├── config.py
├── requirements.txt
├── audio_files/
│   ├── Audio1.wav
│   ├── Audio2.mp3
│   └── (...)
├── server/
│   ├── main.py
│   ├── socket_server.py
│   └── stream_handler.py
└── client/
    ├── main.py
    ├── socket_client.py
    ├── playback_buffer.py
    └── playlist.py
```

## 1. Instalação

Requisito: Python 3.10 ou superior.

### Sobre o PortAudio

O `sounddevice` precisa da biblioteca PortAudio:

- **Windows e macOS:** vem embutida no `pip install sounddevice`.
- **Linux:** instale antes :
  - Debian/Ubuntu: `sudo apt install libportaudio2`
  - Fedora: `sudo dnf install portaudio`
  - Arch: `sudo pacman -S portaudio`

## 2. Ambiente Virtual

```
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Execução

Abra **dois terminais na raiz do projeto** (com o ambiente ativado no do cliente).

**Terminal 1 - Servidor:**
```
python -m server.main
```
Deve aparecer: `Server escutando em 127.0.0.1:8080.`

**Terminal 2 - Cliente:**
```
python -m client.main
```
Deve aparecer o seguinte menu:
```
 =========== SPOTCIN ===========
 [1] (Castlevania) Dance of Illusions.mp3
 [2] (FFVII) One-Winged Angel.mp3
 [3] (Kirby) Sky Tower.mp3
 [4] (Kirby) The Adventure Begins.mp3
 [5] (Mario) Ground Theme.mp3
 [6] (Undertale) MEGALOVANIA.wav
 [7] (Zelda) Hyrule Field.mp3
 [8] (Zelda) Kokiri Forest.wav
 [9] Especial de Natal.wav
 [0] Sair 
 Escolha uma opção: 
```

Escolha uma música digitando o número correspondente a ela. Depois de uma mensagem "Carregando buffer..." a música começa.
Aperte **Enter** para parar e voltar ao menu. No menu, aperte `0` para sair.
