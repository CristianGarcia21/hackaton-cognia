// Frontend base (issue #4): conexión al WebSocket /ws/voz, indicador de estado y transcripción diarizada.
// Contrato de eventos: server/events.py (fuente de verdad) → web/contrato.json.
//
// Cada evento del servidor se republica en `bus` con su `type` (y el audio TTS como "audio"; al cerrarse el
// socket, "desconectado"), así los
// demás módulos (#5 audio, #12 brief, #14 emociones/acciones, #20 inspector) se suscriben sin tocar este archivo:
//     import { bus, enviar } from "./app.js";
//     bus.addEventListener("emotion", (e) => pintar(e.detail));
//
// Importa siempre "./app.js" con esa URL exacta: los módulos ES se evalúan una vez por URL (otra URL abriría
// un segundo WebSocket). La conexión NO arranca sola: la abre inicio.js con iniciar() después de importar todos
// los módulos, así cada uno ya está suscrito cuando llegan ready, source_status, brief... Un módulo nuevo se
// agrega como import en inicio.js (no como otra etiqueta <script> en index.html).
//
// Servidor alternativo:  /?ws=ws://127.0.0.1:8001/ws/voz   (por defecto, el mismo host que sirve la página).
// Todo lo que llega del servidor se pinta con textContent (nunca innerHTML).

export const bus = new EventTarget();

const $ = (id) => document.getElementById(id);
const ui = {
  conexion: $("conexion"), microfono: $("microfono"), microfonoTexto: $("microfono-texto"),
  avisoNavegador: $("aviso-navegador"), avisoDetalle: $("aviso-detalle"),
  avisoError: $("aviso-error"), avisoErrorTexto: $("aviso-error-texto"), reconectar: $("reconectar"),
  cerrarError: $("cerrar-error"),
  estado: $("estado"), estadoTexto: $("estado-texto"), estadoTurno: $("estado-turno"),
  transcripcion: $("transcripcion"), hablantes: $("hablantes"), irAlFinal: $("ir-al-final"),
  carga: $("carga"), cargaProgreso: $("carga-progreso"), cargaTexto: $("carga-texto"),
};

// ============================ Conexión ============================

const URL_WS = new URLSearchParams(location.search).get("ws")
  || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/voz`;

let ws = null;
let reintentos = 0;
let reconexionManual = false; // true tras un error no recuperable: no se reintenta solo
let temporizadorReconexion = null;

function conectar() {
  clearTimeout(temporizadorReconexion);
  ponerConexion("conectando", "Conectando…");
  ws = new WebSocket(URL_WS);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => ponerConexion("conectado", "Conectado");

  ws.onmessage = (mensaje) => {
    if (typeof mensaje.data !== "string") {
      bus.dispatchEvent(new CustomEvent("audio", { detail: mensaje.data })); // PCM16 24 kHz (#5)
      return;
    }
    let evento;
    try {
      evento = JSON.parse(mensaje.data);
    } catch {
      console.warn("Mensaje no JSON ignorado", mensaje.data);
      return;
    }
    try {
      manejadores[evento.type]?.(evento);
    } catch (err) {
      console.error(`Error pintando ${evento.type}`, err); // un panel roto no debe frenar a los demás
    }
    bus.dispatchEvent(new CustomEvent(evento.type, { detail: evento }));
  };

  ws.onclose = () => {
    ponerConexion("desconectado", "Desconectado");
    ponerEstado("desconectado");
    ui.microfono.disabled = true;
    bus.dispatchEvent(new CustomEvent("desconectado")); // reproductor.js corta el audio que quedaba en cola
    if (microfonoActivo) bus.dispatchEvent(new CustomEvent("microfono:stop")); // #5 suelta el micrófono
    marcarMicrofono(false);
    if (reconexionManual) {
      ui.avisoError.hidden = false; // sin reconexión automática: el aviso con «Reconectar» no puede desaparecer
      return;
    }
    const espera = Math.min(10_000, 1000 * 2 ** reintentos++);
    ui.conexion.textContent = `Reconectando en ${Math.round(espera / 1000)} s…`;
    temporizadorReconexion = setTimeout(conectar, espera);
  };
}

/** Envía un mensaje del cliente (Start, Stop, TextInput) o un frame binario de audio. */
export function enviar(mensaje) {
  if (ws?.readyState !== WebSocket.OPEN) return false;
  ws.send(mensaje instanceof ArrayBuffer || ArrayBuffer.isView(mensaje) ? mensaje : JSON.stringify(mensaje));
  return true;
}

function ponerConexion(estado, texto) {
  ui.conexion.dataset.estado = estado;
  ui.conexion.textContent = texto;
}

// ============================ Estado de la conversación ============================

const TEXTO_ESTADO = {
  desconectado: "Sin conexión con el agente",
  inactivo: "En espera",
  escuchando: "Escuchando…",
  pensando: "Pensando…",
  ejecutando_tool: "Consultando datos…",
  hablando: "Hablando…",
  interrumpido: "Interrumpido",
};
// Qué decir mientras corre cada tool (spec §8). Si no está aquí se usa el texto genérico.
const TEXTO_TOOL = {
  describir_datos: "Revisando la fuente de datos…",
  buscar_ips: "Consultando IPS…",
  contar_capacidad: "Contando capacidad instalada…",
  detalle_ips: "Buscando el detalle de la IPS…",
  registrar_solicitud_cita: "Registrando la solicitud de cita…",
  listar_solicitudes: "Revisando solicitudes…",
  exportar_solicitudes_excel: "Exportando a Excel…",
  consultar_fuente_adicional: "Consultando la fuente adicional…",
  crear_evento_cita: "Creando el evento en el calendario…",
};

let estadoActual = "desconectado";

function ponerEstado(estado, turnId) {
  estadoActual = estado;
  ui.estado.dataset.estado = estado;
  ui.estadoTexto.textContent = TEXTO_ESTADO[estado] ?? estado;
  ui.estadoTurno.textContent = turnId ? `Turno ${turnId}` : "";
}

// ============================ Transcripción diarizada ============================

let inicioSesion = performance.now(); // para la marca mm:ss cuando el evento no trae `start`
const segmentos = new Map(); // segment_id -> { li, inicio }
let ultimoTiempo = 0; // segundos: el mayor start/end visto, para que la marca mm:ss nunca retroceda
const hablantesVistos = new Set();

/** Color CSS de un hablante (lo usan la transcripción y el gráfico de sentimiento de paneles.js). */
export function claseHablante(speaker) {
  if (speaker === "Agente") return "var(--hablante-agente)";
  const n = Number(/(\d+)/.exec(speaker)?.[1] ?? 1);
  return `var(--hablante-${(((n - 1) % 4) + 4) % 4 + 1})`; // "Hablante 0" también tiene color
}

function mmss(segundos) {
  const s = Math.max(0, Math.floor(segundos));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function cercaDelFinal() {
  const t = ui.transcripcion;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 80;
}

function irAlFinal() {
  ui.transcripcion.scrollTop = ui.transcripcion.scrollHeight;
  ui.irAlFinal.hidden = true;
}

function registrarHablante(speaker, color) {
  if (hablantesVistos.has(speaker)) return;
  hablantesVistos.add(speaker);
  const chip = document.createElement("span");
  chip.className = "etiqueta-hablante";
  chip.style.setProperty("--c", color);
  chip.textContent = speaker;
  ui.hablantes.append(chip);
}

function pintarTranscript(e) {
  const pegado = cercaDelFinal();
  ui.transcripcion.querySelector(".vacio")?.remove();

  let seg = segmentos.get(e.segment_id);
  if (!seg) {
    const li = document.createElement("li");
    li.className = "segmento";
    li.innerHTML = '<div class="segmento-meta"><span class="segmento-hablante"></span>'
      + '<time class="segmento-tiempo"></time></div><p class="segmento-texto"></p>'; // plantilla fija, sin datos
    // Si no trae start (p. ej. el Agente), la hora de llegada, sin quedar antes de lo ya transcrito.
    seg = { li, inicio: e.start ?? Math.max(ultimoTiempo, (performance.now() - inicioSesion) / 1000) };
    segmentos.set(e.segment_id, seg);
    ui.transcripcion.append(li);
  }
  ultimoTiempo = Math.max(ultimoTiempo, e.end ?? e.start ?? seg.inicio);
  const color = claseHablante(e.speaker);
  registrarHablante(e.speaker, color);

  const { li } = seg;
  li.style.setProperty("--c", color);
  li.classList.toggle("agente", e.speaker === "Agente");
  li.classList.toggle("parcial", !e.is_final);
  if (e.turn_id != null) li.dataset.turno = e.turn_id;
  li.querySelector(".segmento-hablante").textContent = e.speaker;
  const tiempo = li.querySelector(".segmento-tiempo");
  tiempo.textContent = mmss(e.start ?? seg.inicio);
  tiempo.dateTime = `PT${Math.floor(e.start ?? seg.inicio)}S`;
  li.querySelector(".segmento-texto").textContent = e.text;

  if (pegado) irAlFinal();
  else ui.irAlFinal.hidden = false;
}

function marcarInterrumpido(turnId) {
  // Clase y no texto: si luego llega otro transcript del mismo segmento, la marca se conserva.
  for (const li of ui.transcripcion.querySelectorAll(`.segmento.agente[data-turno="${Number(turnId)}"]`)) {
    li.classList.add("interrumpido");
  }
}

function nuevaSesion() {
  // Los segment_id y turn_id se repiten entre sesiones: lo anterior queda como historial, sin que se reescriba.
  segmentos.clear();
  ultimoTiempo = 0;
  ui.transcripcion.querySelectorAll("[data-turno]").forEach((li) => li.removeAttribute("data-turno"));
  if (ui.transcripcion.querySelector(".segmento")) {
    const separador = document.createElement("li");
    separador.className = "separador";
    separador.textContent = "Nueva sesión";
    ui.transcripcion.append(separador);
  }
}

// ============================ Manejadores de eventos ============================

const manejadores = {
  ready(e) {
    inicioSesion = performance.now();
    nuevaSesion();
    reintentos = 0; // aquí y no en onopen: si el servidor acepta y cierra enseguida, el backoff sigue creciendo
    reconexionManual = false;
    ui.microfono.disabled = false;
    ponerEstado("inactivo");
    console.info(`Sesión ${e.session_id} · voz ${e.voice} · fuentes: ${e.sources.join(", ") || "ninguna"}`);
  },
  state(e) {
    ponerEstado(e.state, e.turn_id);
  },
  tool(e) {
    if (e.status === "running" && estadoActual === "ejecutando_tool" && TEXTO_TOOL[e.name]) {
      ui.estadoTexto.textContent = TEXTO_TOOL[e.name];
    }
  },
  transcript: pintarTranscript,
  audio_flush(e) {
    marcarInterrumpido(e.turn_id);
  },
  source_status(e) {
    ui.carga.hidden = e.status === "listo";
    ui.cargaProgreso.style.width = `${Math.round(e.progress * 100)}%`;
    const total = e.total_rows ? ` de ${e.total_rows.toLocaleString("es-CO")}` : "";
    ui.cargaTexto.textContent = `${e.source}: ${e.status} · ${e.rows.toLocaleString("es-CO")}${total} filas`;
  },
  error(e) {
    ui.avisoErrorTexto.textContent = `${e.message} (${e.where})`;
    ui.reconectar.hidden = e.recoverable;
    ui.cerrarError.hidden = !e.recoverable; // si no es recuperable, la única salida es «Reconectar»
    ui.avisoError.hidden = false;
    if (!e.recoverable) reconexionManual = true;
  },
};

// ============================ Micrófono (start/stop; la captura de audio es el issue #5) ============================

let microfonoActivo = false;

function marcarMicrofono(activo) {
  microfonoActivo = activo;
  ui.microfono.setAttribute("aria-pressed", String(activo));
  ui.microfonoTexto.textContent = activo ? "Detener micrófono" : "Activar micrófono";
}

// #5 emite "microfono:error" si getUserMedia falla: se revierte el botón y se avisa al backend.
const MOTIVO_MICROFONO = {
  NotAllowedError: "El permiso del micrófono fue denegado: actívalo en el candado de la barra de direcciones.",
  NotFoundError: "No se encontró ningún micrófono conectado.",
  NotReadableError: "Otra aplicación está usando el micrófono.",
  NotSupportedError: "El navegador bloquea el micrófono: abre la página por HTTPS (o en localhost).",
  SecurityError: "El navegador bloquea el micrófono en esta página.",
  OverconstrainedError: "El micrófono no admite la configuración pedida.",
};

bus.addEventListener("microfono:error", ({ detail }) => {
  enviar({ type: "stop" });
  marcarMicrofono(false);
  ui.avisoDetalle.textContent = MOTIVO_MICROFONO[detail?.name] ?? (detail?.message || "No se pudo abrir el micrófono.");
  ui.avisoNavegador.hidden = false;
});

ui.microfono.addEventListener("click", () => {
  const activar = !microfonoActivo;
  if (enviar({ type: activar ? "start" : "stop" })) {
    marcarMicrofono(activar);
    bus.dispatchEvent(new CustomEvent(activar ? "microfono:start" : "microfono:stop"));
  }
});

// ============================ Avisos ============================

async function revisarNavegador() {
  const ua = navigator.userAgent;
  const esChromium = navigator.userAgentData?.brands?.some((b) => b.brand === "Chromium") || /Chrome\//.test(ua);
  const problemas = [];
  if (!esChromium) problemas.push("Este navegador no es Chrome: la captura de audio puede fallar.");
  if (!window.isSecureContext) problemas.push("La página no es HTTPS: el navegador bloquea el micrófono.");
  else if (!navigator.mediaDevices?.getUserMedia) problemas.push("Este navegador no permite capturar el micrófono.");
  try {
    const permiso = await navigator.permissions?.query({ name: "microphone" });
    if (permiso?.state === "denied") problemas.push("El permiso del micrófono está bloqueado para este sitio.");
    else if (permiso && permiso.state !== "granted") problemas.push("Cuando el navegador lo pida, permite el micrófono.");
  } catch {
    // Firefox/Safari no soportan consultar "microphone": no es un error.
  }
  ui.avisoNavegador.hidden = problemas.length === 0;
  if (problemas.length) ui.avisoDetalle.textContent = problemas.join(" ");
}

ui.cerrarError.addEventListener("click", () => { ui.avisoError.hidden = true; });
ui.reconectar.addEventListener("click", () => {
  ui.avisoError.hidden = true;
  reconexionManual = false;
  reintentos = 0;
  if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
  else conectar();
});
ui.irAlFinal.addEventListener("click", irAlFinal);
ui.transcripcion.addEventListener("scroll", () => { if (cercaDelFinal()) ui.irAlFinal.hidden = true; });

/** Lo llama inicio.js cuando todos los módulos ya se suscribieron al bus. */
export function iniciar() {
  revisarNavegador();
  conectar();
}
