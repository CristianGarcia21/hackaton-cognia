// Paneles de cognición (#14): sentimiento por turno, emoción actual, adaptación vigente, verificación de cada
// respuesta del agente y acciones realizadas. Solo escucha el bus de app.js (eventos emotion, adaptation,
// verification, action y transcript). Todo dato del servidor se pinta con textContent (nunca innerHTML).

import { bus, claseHablante } from "./app.js";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

// Nombres para mostrar (el contrato usa claves sin tildes, spec §9.2 y §9.3).
const EMOCION = {
  alegria: "Alegría", calma: "Calma", neutral: "Neutral", confusion: "Confusión", ansiedad: "Ansiedad",
  frustracion: "Frustración", enojo: "Enojo", tristeza: "Tristeza", urgencia: "Urgencia",
};
const REGLA = {
  urgencia: "Urgencia", frustracion: "Frustración", ansiedad: "Ansiedad", confusion: "Confusión", normal: "Normal",
};
const VERIFICACION = {
  respaldado: { marca: "✔", texto: "Respaldado por los datos" },
  parcial: { marca: "⚠", texto: "Parcialmente respaldado" },
  no_respaldado: { marca: "⚠", texto: "No respaldado por los datos" },
  fuera_de_datos_ok: { marca: "ⓘ", texto: "Fuera de los datos (lo dijo con honestidad)" },
};

const decimal = (n, d = 2) => n.toLocaleString("es-CO", { minimumFractionDigits: d, maximumFractionDigits: d });
const porcentaje = (n) => `${Math.round(n * 100)} %`;

function nodo(etiqueta, clase, texto) {
  const el = document.createElement(etiqueta);
  if (clase) el.className = clase;
  if (texto != null) el.textContent = texto;
  return el;
}

// ============================ Sentimiento por turno (SVG sin librerías) ============================

const grafico = $("grafico-sentimiento");
const resumen = $("sentimiento-resumen");
const ANCHO = 320;
const ALTO = 130;
const M = { izq: 28, der: 10, arr: 10, aba: 18 }; // márgenes del área de datos
const MAX_PUNTOS = 12; // los últimos N análisis (la demo tiene pocos turnos)

let puntos = []; // { speaker, sentiment, emotion, turno }

function svg(etiqueta, atributos, texto) {
  const el = document.createElementNS(SVG, etiqueta);
  for (const [k, v] of Object.entries(atributos)) el.setAttribute(k, v);
  if (texto != null) el.textContent = texto;
  return el;
}

function dibujarGrafico() {
  const y = (s) => M.arr + ((1 - s) / 2) * (ALTO - M.arr - M.aba);
  const visibles = puntos.slice(-MAX_PUNTOS);
  const huecos = Math.max(5, visibles.length - 1); // con pocos turnos los puntos no quedan apretados
  const paso = (ANCHO - M.izq - M.der) / huecos;
  const x = (i) => M.izq + i * paso;
  const hijos = [];

  // Ejes: +1, 0 y −1, con la zona negativa sombreada para leerla de un vistazo.
  hijos.push(svg("rect", { x: M.izq, y: y(0), width: ANCHO - M.izq - M.der, height: y(-1) - y(0), class: "g-negativo" }));
  for (const [valor, etiqueta] of [[1, "+1"], [0, "0"], [-1, "−1"]]) {
    hijos.push(svg("line", { x1: M.izq, x2: ANCHO - M.der, y1: y(valor), y2: y(valor), class: valor === 0 ? "g-cero" : "g-guia" }));
    hijos.push(svg("text", { x: M.izq - 6, y: y(valor) + 4, "text-anchor": "end", class: "g-etiqueta" }, etiqueta));
  }
  hijos.push(svg("text", { x: M.izq, y: ALTO - 3, class: "g-etiqueta" }, "turnos →"));

  // Una línea por hablante, con marcadores (forma además de color).
  const porHablante = new Map();
  visibles.forEach((p, i) => {
    if (!porHablante.has(p.speaker)) porHablante.set(p.speaker, []);
    porHablante.get(p.speaker).push({ ...p, i });
  });
  for (const [speaker, serie] of porHablante) {
    const color = claseHablante(speaker);
    if (serie.length > 1) {
      hijos.push(svg("polyline", {
        points: serie.map((p) => `${x(p.i)},${y(p.sentiment)}`).join(" "), class: "g-linea", style: `stroke:${color}`,
      }));
    }
    for (const p of serie) {
      const punto = svg("circle", { cx: x(p.i), cy: y(p.sentiment), r: 4.5, class: "g-punto", style: `fill:${color}` });
      punto.append(svg("title", {}, `${speaker}: ${EMOCION[p.emotion] ?? p.emotion}, sentimiento ${decimal(p.sentiment)}`));
      hijos.push(punto);
    }
  }
  grafico.replaceChildren(...hijos);
}

// ============================ Emoción actual por hablante ============================

const actuales = $("emociones-actuales");
const filasEmocion = new Map(); // speaker -> { li, nombre, barra, valor }

function pintarEmocionActual(e) {
  let fila = filasEmocion.get(e.speaker);
  if (!fila) {
    const li = nodo("li", "emocion");
    li.style.setProperty("--c", claseHablante(e.speaker));
    const cabecera = nodo("div", "emocion-cabecera");
    const hablante = nodo("span", "emocion-hablante", e.speaker);
    const nombre = nodo("span", "emocion-nombre");
    cabecera.append(hablante, nombre);
    const barra = nodo("div", "emocion-barra");
    barra.setAttribute("role", "meter");
    barra.setAttribute("aria-valuemin", "0");
    barra.setAttribute("aria-valuemax", "100");
    const relleno = nodo("div", "emocion-relleno");
    barra.append(relleno);
    const senales = nodo("p", "emocion-senales tenue");
    li.append(cabecera, barra, senales);
    actuales.append(li);
    fila = { li, nombre, barra, relleno, senales };
    filasEmocion.set(e.speaker, fila);
  }
  const emocion = EMOCION[e.emotion] ?? e.emotion;
  fila.nombre.textContent = `${emocion} · ${porcentaje(e.intensity)}`;
  fila.relleno.style.width = porcentaje(e.intensity).replace(" ", "");
  fila.barra.setAttribute("aria-valuenow", String(Math.round(e.intensity * 100)));
  fila.barra.setAttribute("aria-label", `Intensidad de ${emocion.toLowerCase()} en ${e.speaker}`);
  fila.senales.textContent = e.signals?.length ? `Señales: ${e.signals.join(", ")}` : "";
}

bus.addEventListener("emotion", ({ detail: e }) => {
  puntos.push({ speaker: e.speaker, sentiment: e.sentiment, emotion: e.emotion, turno: e.turn_id });
  dibujarGrafico();
  pintarEmocionActual(e);
  resumen.textContent = `Último: ${e.speaker}, ${(EMOCION[e.emotion] ?? e.emotion).toLowerCase()} `
    + `(intensidad ${porcentaje(e.intensity)}), sentimiento ${decimal(e.sentiment)}.`;
});

// ============================ Adaptación vigente ============================

const adaptacion = $("adaptacion");
bus.addEventListener("adaptation", ({ detail: a }) => {
  adaptacion.dataset.activa = String(a.active);
  adaptacion.dataset.regla = a.rule;
  $("adaptacion-titulo").textContent = a.active
    ? `Adaptación activa: ${REGLA[a.rule] ?? a.rule} · ${a.style} · velocidad ${decimal(a.speed, 2)}×`
    : `Sin adaptación · ${a.style || "estilo estándar"}`;
  $("adaptacion-motivo").textContent = `Motivo: ${a.reason}`;
  // Pulso breve para que el jurado note el cambio (respeta prefers-reduced-motion vía CSS).
  adaptacion.classList.remove("cambio");
  void adaptacion.offsetWidth;
  adaptacion.classList.add("cambio");
});

// ============================ Verificación de cada respuesta ============================

const pendientes = new Map(); // turn_id -> verification que llegó antes que la burbuja del agente

function burbujaAgente(turno) {
  // Si la respuesta llegó en varios segmentos, el veredicto va en el último.
  const todas = document.querySelectorAll(`#transcripcion .segmento.agente[data-turno="${Number(turno)}"]`);
  return todas[todas.length - 1] ?? null;
}

function marcarVerificacion(li, v) {
  li.querySelector(".verificacion")?.remove();
  li.querySelector(".verificacion-detalle")?.remove();
  const info = VERIFICACION[v.status] ?? VERIFICACION.parcial;
  const marca = nodo("span", "verificacion", `${info.marca} ${info.texto}`);
  marca.dataset.estado = v.status;
  li.querySelector(".segmento-meta").append(marca);
  if (v.issues?.length || v.correction) {
    const detalle = nodo("div", "verificacion-detalle");
    detalle.dataset.estado = v.status;
    for (const problema of v.issues ?? []) detalle.append(nodo("p", null, problema));
    if (v.correction) detalle.append(nodo("p", "verificacion-correccion", `Corrección: ${v.correction}`));
    li.append(detalle);
  }
}

bus.addEventListener("verification", ({ detail: v }) => {
  const li = burbujaAgente(v.turn_id);
  if (li) marcarVerificacion(li, v);
  else pendientes.set(v.turn_id, v);
});

// app.js ya pintó la burbuja cuando el bus reemite el transcript: aquí se aplica un veredicto que llegó antes.
bus.addEventListener("transcript", ({ detail: t }) => {
  if (t.speaker !== "Agente" || t.turn_id == null || !pendientes.has(t.turn_id)) return;
  const li = burbujaAgente(t.turn_id);
  if (li) {
    marcarVerificacion(li, pendientes.get(t.turn_id));
    pendientes.delete(t.turn_id);
  }
});

// ============================ Acciones ============================

const acciones = $("acciones");
const TITULO_ACCION = { cita: "Solicitud de cita", evento: "Evento en el calendario", excel: "Exportación a Excel" };
const TEXTO_ENLACE = { evento: "Descargar .ics", excel: "Descargar Excel", cita: "Ver" };
const CAMPOS = {
  cita: [["paciente", "Paciente"], ["sede", "Sede"], ["motivo", "Motivo"], ["fecha_preferida", "Fecha preferida"]],
  evento: [["titulo", "Título"], ["inicio", "Inicio"], ["duracion_min", "Duración (min)"]],
  excel: [["filas", "Filas"]],
};

const ISO = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

function valorLegible(valor) {
  // "2026-10-13T08:00" → "lun, 13 de oct., 8:00 a. m." (las fechas del backend vienen en ISO).
  if (typeof valor === "string" && ISO.test(valor)) {
    const fecha = new Date(valor.replace(" ", "T"));
    if (!Number.isNaN(fecha.getTime())) {
      return fecha.toLocaleString("es-CO", { weekday: "short", day: "numeric", month: "short", hour: "numeric",
                                               minute: "2-digit" });
    }
  }
  return String(valor);
}

function enlaceSeguro(link) {
  // Solo archivos del propio servidor (/api/...): nunca javascript:, data: ni otro dominio (p. ej. //otro.com).
  try {
    const url = new URL(link, location.href);
    return url.origin === location.origin ? url.href : null;
  } catch {
    return null;
  }
}

bus.addEventListener("action", ({ detail: a }) => {
  acciones.querySelector(":scope > li.tenue")?.remove();
  const li = nodo("li", "accion");
  li.dataset.tipo = a.kind;
  const cabecera = nodo("div", "accion-cabecera");
  const id = a.data?.id ?? a.data?.solicitud_id;
  cabecera.append(nodo("strong", null, TITULO_ACCION[a.kind] ?? a.kind));
  if (id != null) cabecera.append(nodo("span", "tenue", ` #${id}`));
  li.append(cabecera);

  const datos = nodo("dl", "accion-datos");
  for (const [campo, etiqueta] of CAMPOS[a.kind] ?? []) {
    if (a.data?.[campo] == null) continue;
    datos.append(nodo("dt", null, etiqueta), nodo("dd", null, valorLegible(a.data[campo])));
  }
  if (datos.childElementCount) li.append(datos);
  if (a.data?.estado) li.append(nodo("p", "accion-estado", a.data.estado)); // p. ej. pendiente de confirmación

  const href = a.link && enlaceSeguro(a.link);
  if (href) {
    const enlace = nodo("a", "accion-enlace", TEXTO_ENLACE[a.kind] ?? "Abrir");
    enlace.href = href;
    enlace.download = "";
    li.append(enlace);
  }
  acciones.prepend(li); // lo más reciente arriba
});

// ============================ Nueva sesión ============================

bus.addEventListener("ready", () => {
  // Las emociones y la adaptación son de la sesión; las acciones son registros reales y se conservan.
  puntos = [];
  pendientes.clear();
  filasEmocion.clear();
  actuales.replaceChildren();
  dibujarGrafico();
  resumen.textContent = "Sentimiento por turno: aún sin análisis.";
  adaptacion.dataset.activa = "false";
  delete adaptacion.dataset.regla;
  $("adaptacion-titulo").textContent = "Sin adaptación · estilo estándar";
  $("adaptacion-motivo").textContent = "El agente ajusta su forma de hablar según las emociones detectadas.";
});

dibujarGrafico();
