// Inspector (#20): cascada de tiempos por turno, tools con sus argumentos y resultado, contexto usado, más la caja
// de texto de respaldo (plan B si falla la voz). Solo escucha el bus de app.js (trace, tool, transcript, ready,
// desconectado). Todo dato del servidor se pinta con textContent (nunca innerHTML).

import { bus, enviar } from "./app.js";

const $ = (id) => document.getElementById(id);

const MAX_TURNOS = 10;
const ms = (n) => `${Math.round(n).toLocaleString("es-CO")} ms`;

const ESTADO_TOOL = {
  running: "en curso", ok: "ok", error: "error", timeout: "tiempo agotado", cancelled: "cancelada",
};

function nodo(etiqueta, clase, texto) {
  const el = document.createElement(etiqueta);
  if (clase) el.className = clase;
  if (texto != null) el.textContent = texto;
  return el;
}

/** Valor legible: arreglos unidos con coma, objetos como JSON, vacío como «—». */
function legible(v) {
  if (v == null || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.map(legible).join(", ") : "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function argumentos(args) {
  const pares = Object.entries(args ?? {});
  return pares.length ? pares.map(([k, v]) => `${k}=${legible(v)}`).join(", ") : "sin argumentos";
}

// ============================ Estado: una tarjeta por turno ============================

const contenido = $("inspector-contenido");
const inspector = $("inspector");
const resumenSummary = $("inspector-resumen");
const RESUMEN_BASE = resumenSummary.textContent;
const fondo = $("inspector-fondo");
const abrirBoton = $("abrir-inspector");
const cerrarBoton = $("cerrar-inspector");
const insignia = $("inspector-insignia");
let sinVer = 0; // pasos (tools y trazas) que llegaron con el panel cerrado

const abierto = () => !inspector.hidden;

function pintarInsignia() {
  insignia.hidden = sinVer === 0;
  insignia.textContent = sinVer > 99 ? "99+" : String(sinVer);
  abrirBoton.setAttribute("aria-label", sinVer ? `Inspector, ${sinVer} pasos nuevos` : "Inspector");
}

function contarPaso() {
  if (abierto()) return;
  sinVer++;
  pintarInsignia();
}

function abrirInspector() {
  inspector.hidden = false;
  fondo.hidden = false;
  abrirBoton.setAttribute("aria-expanded", "true");
  sinVer = 0;
  pintarInsignia();
  pintarResumen();
  cerrarBoton.focus();
}

function cerrarInspector() {
  if (!abierto()) return;
  inspector.hidden = true;
  fondo.hidden = true;
  abrirBoton.setAttribute("aria-expanded", "false");
  pintarResumen();
  abrirBoton.focus(); // el foco vuelve al botón que lo abrió
}

abrirBoton.addEventListener("click", () => (abierto() ? cerrarInspector() : abrirInspector()));
cerrarBoton.addEventListener("click", cerrarInspector);
fondo.addEventListener("click", cerrarInspector);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") cerrarInspector();
});
const VACIO = contenido.firstElementChild; // «Las trazas de los turnos aparecerán aquí.»

const turnos = new Map(); // turn_id -> { raiz, pregunta, total, listaTools, cascada, contexto, tools: [], nTools, ms }
const preguntas = new Map(); // turn_id -> pregunta, cuando el transcript trae su turn_id
let ultimaPregunta = null; // último texto del usuario (transcript final o text_input enviado)
let ultimoResumen = null;

function asignarPregunta(t, texto) {
  t.pregunta.hidden = !texto;
  t.pregunta.replaceChildren(nodo("span", "tenue", "Pregunta: "), document.createTextNode(texto ?? ""));
}

function crearTurno(id) {
  const raiz = nodo("article", "turno");
  const cabecera = nodo("header", "turno-cabecera");
  const total = nodo("span", "turno-total tenue");
  cabecera.append(nodo("h3", "turno-titulo", `Turno ${id}`), total);
  const pregunta = nodo("p", "turno-pregunta");
  const listaTools = nodo("ul", "turno-tools");
  const cascada = nodo("div", "cascada");
  const contexto = nodo("dl", "turno-contexto");
  raiz.append(cabecera, pregunta, listaTools, cascada, contexto);
  const t = { raiz, pregunta, total, listaTools, cascada, contexto, tools: [], nTools: 0, ms: null };
  asignarPregunta(t, preguntas.get(id) ?? ultimaPregunta);
  turnos.set(id, t);
  VACIO.remove();
  contenido.prepend(raiz); // lo más reciente arriba
  while (turnos.size > MAX_TURNOS) {
    const [viejoId, viejo] = turnos.entries().next().value;
    viejo.raiz.remove();
    turnos.delete(viejoId);
  }
  return t;
}

const turno = (id) => turnos.get(id) ?? crearTurno(id);

function actualizarResumen(id) {
  const t = turnos.get(id);
  ultimoResumen = [`Turno ${id}`, t.ms != null ? ms(t.ms) : null,
    `${t.nTools} ${t.nTools === 1 ? "tool" : "tools"}`].filter(Boolean).join(" · ");
  pintarResumen();
}

function pintarResumen() {
  // Abierto, el detalle ya está a la vista; cerrado, el resumen del último turno sustituye al texto genérico.
  // El resumen del último turno se ve en la cabecera del panel (y como contador en el botón si está cerrado).
  resumenSummary.textContent = ultimoResumen ? `· ${ultimoResumen}` : RESUMEN_BASE;
}

// ============================ Tools ============================

function pintarTool(e) {
  const t = turno(e.turn_id);
  // El servidor emite la tool al iniciar (running) y al terminar: se actualiza la misma fila. Con dos llamadas
  // idénticas en un turno, se cierra la que sigue en curso.
  const clave = `${e.name}|${JSON.stringify(e.args ?? {})}`;
  let fila = t.tools.find((f) => f.clave === clave && f.estadoValor === "running");
  if (!fila) {
    const li = nodo("li", "tool");
    const linea = nodo("div", "tool-linea");
    const estado = nodo("span", "tool-estado");
    const tiempo = nodo("span", "tool-ms tenue");
    linea.append(nodo("code", "tool-nombre", e.name), estado, tiempo);
    const resultado = nodo("p", "tool-resultado");
    li.append(linea, nodo("p", "tool-args tenue", argumentos(e.args)), resultado);
    t.listaTools.append(li);
    fila = { clave, estado, tiempo, resultado, estadoValor: null };
    t.tools.push(fila);
    t.nTools += 1;
  }
  fila.estado.textContent = ESTADO_TOOL[e.status] ?? e.status;
  fila.estado.dataset.estado = e.status;
  fila.estadoValor = e.status;
  fila.tiempo.textContent = e.ms != null ? ms(e.ms) : "";
  fila.resultado.textContent = e.summary ?? "";
  fila.resultado.hidden = !e.summary;
  actualizarResumen(e.turn_id);
}

// ============================ Traza: cascada y contexto ============================

function pintarTraza(e) {
  const t = turno(e.turn_id);
  const total = e.spans.reduce((s, x) => s + x.ms, 0);
  t.ms = total;
  t.total.textContent = `Total ${ms(total)}`;

  // Cascada: cada etapa empieza donde termina la anterior (las del turno son secuenciales).
  let acumulado = 0;
  const filas = e.spans.map((s) => {
    const fila = nodo("div", "cascada-fila");
    const etiqueta = nodo("div", "cascada-etiqueta");
    etiqueta.append(nodo("span", "cascada-stage", s.stage), nodo("span", "cascada-ms", ms(s.ms)));
    const pista = nodo("div", "cascada-pista");
    const barra = nodo("div", s.stage.startsWith("tool:") ? "cascada-barra es-tool" : "cascada-barra");
    barra.style.marginLeft = `${total ? (acumulado / total) * 100 : 0}%`;
    barra.style.width = `${total ? Math.max((s.ms / total) * 100, 1) : 0}%`;
    pista.append(barra);
    fila.append(etiqueta, pista);
    if (s.detail) fila.append(nodo("p", "cascada-detalle tenue", s.detail));
    acumulado += s.ms;
    return fila;
  });
  t.cascada.replaceChildren(...filas);

  const pares = Object.entries(e.context ?? {}).flatMap(([k, v]) => [nodo("dt", null, k), nodo("dd", null, legible(v))]);
  t.contexto.replaceChildren(...pares);
  t.contexto.hidden = pares.length === 0;
  actualizarResumen(e.turn_id);
}

// ============================ Pregunta del usuario ============================

function recordarPregunta(texto, turnId) {
  ultimaPregunta = texto;
  if (turnId == null) return;
  preguntas.set(turnId, texto);
  const t = turnos.get(turnId);
  if (t) asignarPregunta(t, texto);
}

bus.addEventListener("transcript", (ev) => {
  const e = ev.detail;
  if (e.is_final && e.speaker !== "Agente") recordarPregunta(e.text, e.turn_id);
});
bus.addEventListener("tool", (ev) => {
  pintarTool(ev.detail);
  if (ev.detail.status === "running") contarPaso();
});
bus.addEventListener("trace", (ev) => {
  pintarTraza(ev.detail);
  contarPaso();
});
bus.addEventListener("ready", () => {
  turnos.clear();
  preguntas.clear();
  ultimaPregunta = null;
  ultimoResumen = null;
  contenido.replaceChildren(VACIO);
  sinVer = 0;
  pintarInsignia();
  pintarResumen();
});

// ============================ Caja de texto de respaldo ============================

const formulario = $("texto-respaldo");
const campo = $("texto-entrada");
const boton = $("texto-enviar");
const aviso = $("texto-aviso");

function sinConexion() {
  aviso.textContent = "Sin conexión con el agente.";
  aviso.hidden = false;
}

function conectado(si) {
  campo.disabled = !si;
  boton.disabled = !si;
  if (si) aviso.hidden = true;
  else sinConexion();
}

formulario.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = campo.value.trim();
  if (!text) return;
  if (!enviar({ type: "text_input", text })) {
    sinConexion(); // el texto se conserva para reintentar
    return;
  }
  aviso.hidden = true;
  recordarPregunta(text.slice(0, 2000), null);
  campo.value = "";
  campo.focus();
});

bus.addEventListener("ready", () => conectado(true));
bus.addEventListener("desconectado", () => conectado(false));
