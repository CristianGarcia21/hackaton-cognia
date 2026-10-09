// Envío del micrófono al backend (#5): conecta el AudioWorklet a la fuente que abre microfono.js y manda cada
// trozo PCM16 16 kHz de 20 ms como frame binario por el WebSocket (contrato: AUDIO_ENTRADA).

import { bus, enviar } from "./app.js";

export const estadisticasCaptura = { trozos: 0, bytes: 0, tasaContexto: 0 };

let nodo = null;
let silencio = null;

bus.addEventListener("microfono:listo", async ({ detail: { contexto, fuente } }) => {
  try {
    await contexto.audioWorklet.addModule("audio-worklet.js");
  } catch (error) {
    if (contexto.state === "closed") return; // se detuvo mientras cargaba (AbortError): no es un error
    console.error("No se pudo cargar el AudioWorklet", error);
    bus.dispatchEvent(new CustomEvent("microfono:stop")); // suelta el micrófono (indicador rojo y onda)
    bus.dispatchEvent(new CustomEvent("microfono:error", { detail: error }));
    return;
  }
  if (contexto.state === "closed") return; // el usuario detuvo mientras cargaba el módulo
  limpiar();
  estadisticasCaptura.tasaContexto = contexto.sampleRate;
  // channelCountMode "explicit": un micrófono estéreo se mezcla a mono (si no, el worklet leería solo el izquierdo).
  nodo = new AudioWorkletNode(contexto, "captura-pcm16", {
    numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: "explicit",
    channelInterpretation: "speakers",
  });
  nodo.port.onmessage = ({ data }) => {
    if (enviar(data)) {
      estadisticasCaptura.trozos++;
      estadisticasCaptura.bytes += data.byteLength;
    }
  };
  // Conectado a la salida con volumen 0: así el navegador procesa el nodo siempre, sin que se oiga el micrófono.
  silencio = new GainNode(contexto, { gain: 0 });
  fuente.connect(nodo).connect(silencio).connect(contexto.destination);
});

function limpiar() {
  if (nodo) {
    nodo.port.onmessage = null;
    nodo.disconnect();
  }
  silencio?.disconnect();
  nodo = silencio = null;
}

bus.addEventListener("microfono:cerrado", limpiar);
