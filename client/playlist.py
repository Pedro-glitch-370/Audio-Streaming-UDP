import sounddevice as sd

# Função pra criar e iniciar um stream de áudio que consome do buffer
def iniciar_reproducao(buffer, formato):
    """
    OBS: O sounddevice chama `callback` sozinho em uma thread própria,
    pedindo frames a cada vez. Nunca devemos bloquear aqui
    (por isso `buffer.ler` é non-blocking: se não tem dado, devolve menos).
    """

    # Quantos bytes formam 1 frame (1 amostra de todos os canais juntos).
    # Ex.: estéreo 16-bit → 2 canais * 2 bytes = 4 bytes por frame.
    bytes_por_frame = formato["canais"] * formato["bytes_amostra"]

    # Valor que representa silêncio no formato:
    # - uint8 é unsigned, então 0 seria o mínimo negativo; 128 é o "zero".
    # - int16/int24/int32/float32 usam 0 como centro.
    silencio = b"\x80" if formato["bytes_amostra"] == 1 else b"\x00"

    def callback(outdata, frames, _time_info, _status):
        n = frames * bytes_por_frame
        dados = buffer.ler(n)

        # Se o buffer devolveu menos que o pedido, completa com silêncio.
        # Isso acontece no começo (antes do buffer encher) e no fim (último frame).
        if len(dados) < n:
            dados += silencio * (n - len(dados))

            # Se a rede já acabou e o buffer esvaziou, para o stream.
            if buffer.terminou():
                outdata[:] = dados
                raise sd.CallbackStop  # exceção usada pelo sounddevice pra encerrar

        outdata[:] = dados

    stream = sd.RawOutputStream(
        samplerate=formato["taxa_amostragem"],
        channels=formato["canais"],
        dtype=formato["dtype"],
        latency="high",
        callback=callback,
    )
    stream.start()
    return stream