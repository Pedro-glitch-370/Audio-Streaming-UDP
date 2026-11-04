import queue
import threading
from dataclasses import dataclass, field

# Classe que representa o estado do buffer de reprodução
# O fluxo de dados é: thread de rede  --(adicionar_chunk)-->  fila  --(ler)-->  callback de áudio
@dataclass
class Buffer:
    """ 
    OBS: Usamos dataclass em vez de dict porque:
      - evita erro de digitação em chave ("fila" vs "fial")
      - o editor/autocomplete enxerga os campos
      - os métodos ficam junto do estado que manipulam
    """

    # Quantos bytes precisam ser acumulados antes de liberar a reprodução.
    # Esse número vem do config (SEGUNDOS_PRE_BUFFER * taxa_bytes).
    bytes_prebuffer: int

    # Fila thread-safe onde a thread de rede deposita blocos de áudio
    # e de onde o callback de áudio retira.
    fila: queue.Queue = field(default_factory=queue.Queue)

    # Sobra de bytes de uma leitura anterior que não completou um frame.
    # Ex.: o player pediu 4096 bytes, mas só tínhamos 3000 disponíveis —
    # a diferença fica aqui para a próxima leitura.
    sobra: bytearray = field(default_factory=bytearray)

    # Total de bytes já recebidos da rede (para saber quando atingimos o pré-buffer).
    recebido: int = 0

    # Evento setado quando o pré-buffer é atingido: destrava o player.
    pronto: threading.Event = field(default_factory=threading.Event)

    # Evento setado quando a rede terminou (com sucesso ou erro):
    # avisa o player que não virão mais dados.
    fim: threading.Event = field(default_factory=threading.Event)

    # Função chamada pela thread de rede a cada bloco baixado
    def adicionar_chunk(self, chunk: bytes):
        self.fila.put(chunk)
        self.recebido += len(chunk)

        # Quando o total recebido atinge o pré-buffer, libera o player.
        # Isso evita "engasgo" no começo da música por rede lenta.
        if self.recebido >= self.bytes_prebuffer:
            self.pronto.set()

    # Função chamada quando a rede acaba (seja por fim normal ou por erro)
    def finalizar(self):
        self.fim.set()
        # Também sinaliza 'pronto' pro player não ficar travado
        # esperando um pré-buffer que nunca vai chegar (ex.: conexão caiu cedo).
        self.pronto.set()

    # Função que trava o fluxo principal até o pré-buffer ficar cheio
    def aguardar_prebuffer(self, timeout=None) -> bool:
        # Retorna False se estourar o timeout (o chamador pode checar se quiser).
        return self.pronto.wait(timeout)

    # Função chamada pelo callback de áudio;
    # Ela retorna até n_bytes, podendo ser menos
    def ler(self, n_bytes: int) -> bytes:
        # Vai drenando a fila até ter o suficiente ou até ela esvaziar.
        while len(self.sobra) < n_bytes:
            try:
                self.sobra += self.fila.get_nowait()
            except queue.Empty:
                break

        dados = bytes(self.sobra[:n_bytes])
        del self.sobra[:n_bytes]  # comando del pra remover o que foi consumido
        return dados

    # Função que retorna True quando a rede fecha, a fila fica vazia e não há sobra restante;
    def terminou(self) -> bool:
        return self.fim.is_set() and self.fila.empty() and not self.sobra