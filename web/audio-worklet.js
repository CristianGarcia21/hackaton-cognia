// AudioWorklet de captura (#5): audio del micrófono → PCM16 LE mono 16 kHz en trozos de 20 ms (AUDIO_ENTRADA).
// Corre en el hilo de audio; cada trozo se transfiere al hilo principal (captura.js), que lo envía como frame binario.
// Si el contexto no quedó a 16 kHz (navegadores que no aceptan esa tasa), remuestrea con interpolación lineal,
// con un promedio de 3 muestras antes (filtro pasa-bajos simple contra el aliasing al bajar de 44,1/48 kHz).

const TASA_DESTINO = 16000;
const MUESTRAS_POR_TROZO = 320; // 20 ms a 16 kHz → 640 bytes

class CapturaPCM16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.paso = sampleRate / TASA_DESTINO; // muestras de entrada por muestra de salida (1 si ya es 16 kHz)
    this.posicion = 0; // posición fraccionaria pendiente en la entrada (remuestreo)
    this.anterior = 0; // última muestra (filtrada) del bloque previo, para interpolar entre bloques
    this.filtrar = this.paso >= 2;
    this.previas = [0, 0]; // dos últimas muestras crudas del bloque previo (para el promedio)
    this.filtrado = new Float32Array(128);
    this.trozo = new Int16Array(MUESTRAS_POR_TROZO);
    this.lleno = 0;
  }

  empujar(valor) {
    const v = Math.max(-1, Math.min(1, valor));
    this.trozo[this.lleno++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.lleno === MUESTRAS_POR_TROZO) {
      this.port.postMessage(this.trozo.buffer, [this.trozo.buffer]);
      this.trozo = new Int16Array(MUESTRAS_POR_TROZO);
      this.lleno = 0;
    }
  }

  process(entradas) {
    const canal = entradas[0]?.[0];
    if (!canal) return true; // sin entrada todavía (o micrófono silenciado): seguir vivo
    if (this.paso === 1) {
      for (let i = 0; i < canal.length; i++) this.empujar(canal[i]);
      return true;
    }
    let datos = canal;
    if (this.filtrar) {
      if (this.filtrado.length !== canal.length) this.filtrado = new Float32Array(canal.length);
      const [p2, p1] = this.previas;
      for (let i = 0; i < canal.length; i++) {
        const a = i >= 2 ? canal[i - 2] : i === 1 ? p1 : p2;
        const b = i >= 1 ? canal[i - 1] : p1;
        this.filtrado[i] = (a + b + canal[i]) / 3;
      }
      this.previas = [canal.length >= 2 ? canal[canal.length - 2] : p1, canal[canal.length - 1]];
      datos = this.filtrado;
    }
    this.remuestrear(datos);
    return true;
  }

  remuestrear(canal) {
    // Remuestreo lineal: posicion recorre la entrada en pasos de `paso`; el índice -1 es `anterior`.
    while (this.posicion < canal.length - 1) {
      const i = Math.floor(this.posicion);
      const f = this.posicion - i;
      const a = i < 0 ? this.anterior : canal[i];
      this.empujar(a + (canal[i + 1] - a) * f);
      this.posicion += this.paso;
    }
    this.posicion -= canal.length;
    this.anterior = canal[canal.length - 1];
  }
}

registerProcessor("captura-pcm16", CapturaPCM16);
