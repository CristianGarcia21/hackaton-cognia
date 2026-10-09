// Captura del micrófono: dueño único del MediaStream y del AudioContext de entrada.
// Se activa con el botón (app.js emite "microfono:start" / "microfono:stop") y alimenta la onda en vivo.
//
// El contexto se crea a 16 kHz (AUDIO_ENTRADA del contrato): el navegador remuestrea con antialiasing y el
// AudioWorklet de #5 solo convierte Float32 → PCM16. Si el navegador no acepta 16 kHz, se usa su tasa por
// defecto y #5 debe remuestrear (contexto.sampleRate lo dice).
//
// Para #5: escucha "microfono:listo" y conecta su worklet a la misma fuente, sin volver a pedir permiso:
//     bus.addEventListener("microfono:listo", async ({ detail: { contexto, fuente } }) => {
//       await contexto.audioWorklet.addModule("audio-worklet.js");
//       if (contexto.state === "closed") return;   // el usuario detuvo mientras cargaba
//       ...
//     });
//     bus.addEventListener("microfono:cerrado", limpiar);   // se emite siempre que se suelta el micrófono
// Si algo falla se emite "microfono:error" (app.js revierte el botón, envía stop y muestra el motivo).
// El TTS a 24 kHz se reproduce en OTRO AudioContext de salida, no en este.

import { bus } from "./app.js";
import { crearVisualizador } from "./visualizador.js";

const RESTRICCIONES = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
};

const estado = document.getElementById("estado");
const voz = document.getElementById("voz");
const onda = crearVisualizador(document.getElementById("onda"), {
  alDetectarVoz(hablando) {
    voz.hidden = !hablando;
    estado.classList.toggle("con-voz", hablando);
  },
});

let flujo = null;
let contexto = null;
let intento = 0; // si el usuario pulsa stop mientras el navegador pide permiso, el permiso tardío se descarta

function crearContexto() {
  try {
    return new AudioContext({ sampleRate: 16000, latencyHint: "interactive" });
  } catch {
    return new AudioContext({ latencyHint: "interactive" });
  }
}

async function abrir() {
  if (flujo) cerrar();
  const mio = ++intento;
  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw Object.assign(new Error("Sin acceso al micrófono"), { name: "NotSupportedError" });
    }
    const nuevo = await navigator.mediaDevices.getUserMedia(RESTRICCIONES);
    if (mio !== intento) {
      nuevo.getTracks().forEach((t) => t.stop());
      return;
    }
    flujo = nuevo;
    contexto = crearContexto();
    // Se crea después del permiso (fuera del gesto del click): algunos navegadores lo dejan suspendido.
    if (contexto.state === "suspended") await contexto.resume();
    if (mio !== intento) return; // cerrar() ya soltó todo
    const fuente = contexto.createMediaStreamSource(flujo);
    const analizador = contexto.createAnalyser();
    analizador.fftSize = 1024;
    analizador.smoothingTimeConstant = 0.5;
    fuente.connect(analizador); // solo análisis: no se conecta a los altavoces (evita eco)
    onda.conectar(analizador);
    // Si el usuario quita el permiso o desconecta el micrófono a mitad de la demo.
    flujo.getAudioTracks()[0]?.addEventListener("ended", () => {
      cerrar();
      bus.dispatchEvent(new CustomEvent("microfono:error", { detail: new Error("El micrófono se desconectó") }));
    });
    bus.dispatchEvent(new CustomEvent("microfono:listo", { detail: { flujo, contexto, fuente } }));
  } catch (error) {
    if (mio !== intento) return;
    cerrar(); // suelta lo que alcanzó a abrirse (pista y contexto) para que no quede el indicador rojo
    console.warn("No se pudo abrir el micrófono", error);
    bus.dispatchEvent(new CustomEvent("microfono:error", { detail: error }));
  }
}

function cerrar() {
  intento++;
  onda.desconectar();
  const habia = flujo || contexto;
  flujo?.getTracks().forEach((t) => t.stop());
  if (contexto && contexto.state !== "closed") contexto.close();
  flujo = contexto = null;
  if (habia) bus.dispatchEvent(new CustomEvent("microfono:cerrado"));
}

bus.addEventListener("microfono:start", abrir);
bus.addEventListener("microfono:stop", cerrar);
