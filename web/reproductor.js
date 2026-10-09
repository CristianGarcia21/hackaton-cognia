// Reproducción de la voz del agente (#5): frames binarios PCM16 LE mono 24 kHz (AUDIO_SALIDA) en cola sobre un
// AudioContext propio a 24 kHz. Cada trozo se programa justo donde termina el anterior (sin cortes) y con un
// pequeño colchón inicial para absorber el jitter de la red. Con `audio_flush` se detiene todo de inmediato.

import { bus } from "./app.js";

const TASA = 24000;
const COLCHON_S = 0.06; // 60 ms de margen al empezar (o tras quedarse sin datos): evita cortes por jitter

export const estadisticasReproduccion = { trozos: 0, cortes: 0, ultimoFlushMs: null, sonando: 0 };

let contexto = null;
let siguiente = 0; // instante (en contexto.currentTime) donde debe empezar el próximo trozo
const activos = new Set();

function asegurarContexto() {
  if (!contexto) {
    try {
      contexto = new AudioContext({ sampleRate: TASA, latencyHint: "interactive" });
    } catch {
      contexto = new AudioContext({ latencyHint: "interactive" }); // el navegador remuestrea el AudioBuffer
    }
  }
  if (contexto.state === "suspended") contexto.resume().catch(() => {});
  return contexto;
}

// Los navegadores solo dejan sonar audio tras un gesto del usuario: se prepara el contexto en el primer click/tecla.
for (const tipo of ["pointerdown", "keydown"]) {
  addEventListener(tipo, asegurarContexto, { once: true, capture: true });
}

bus.addEventListener("audio", ({ detail: datos }) => {
  const ctx = asegurarContexto();
  // Sin un gesto previo el navegador no deja sonar audio: se descarta en vez de acumularlo y que suene tarde.
  if (ctx.state !== "running") return;
  const pcm = new Int16Array(datos, 0, Math.floor(datos.byteLength / 2));
  if (!pcm.length) return;
  const buffer = ctx.createBuffer(1, pcm.length, TASA);
  const canal = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) canal[i] = pcm[i] / 0x8000;

  const fuente = new AudioBufferSourceNode(ctx, { buffer });
  fuente.connect(ctx.destination);
  const ahora = ctx.currentTime;
  if (siguiente < ahora) {
    // Un hueco corto es un corte a mitad de una frase (faltaron datos); uno largo es el silencio entre turnos.
    if (siguiente > 0 && ahora - siguiente < 0.5) estadisticasReproduccion.cortes++;
    siguiente = ahora + COLCHON_S;
  }
  fuente.start(siguiente);
  siguiente += buffer.duration;
  activos.add(fuente);
  fuente.onended = () => {
    activos.delete(fuente);
    estadisticasReproduccion.sonando = activos.size;
  };
  estadisticasReproduccion.trozos++;
  estadisticasReproduccion.sonando = activos.size;
});

/** Detiene ya todo lo que suena o está en cola (interrupción del usuario). */
export function vaciar() {
  const inicio = performance.now();
  for (const fuente of activos) {
    fuente.onended = null;
    try {
      fuente.stop();
    } catch {
      // ya había terminado
    }
    fuente.disconnect();
  }
  activos.clear();
  siguiente = 0;
  estadisticasReproduccion.sonando = 0;
  estadisticasReproduccion.ultimoFlushMs = performance.now() - inicio;
}

bus.addEventListener("audio_flush", vaciar);
// Si se cae la conexión no debe quedar audio viejo sonando.
bus.addEventListener("desconectado", vaciar);
