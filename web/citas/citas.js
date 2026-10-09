// Página /citas: solicitudes agrupadas por IPS (GET /api/citas), con Excel por clínica, búsqueda y cifras.
// Se refresca sola: una solicitud registrada por voz aparece resaltada. Todo se inserta con textContent.

const REFRESCO_MS = 5000;
const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
const SVG = "http://www.w3.org/2000/svg";
let vistos = null; // ids ya mostrados: las solicitudes nuevas se resaltan
let datos = { total: 0, grupos: [] };

const $ = (id) => document.getElementById(id);

function el(tag, clase, texto) {
  const n = document.createElement(tag);
  if (clase) n.className = clase;
  if (texto !== undefined && texto !== null) n.textContent = texto;
  return n;
}

function icono(trazos) {
  const s = document.createElementNS(SVG, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "icono");
  s.setAttribute("aria-hidden", "true");
  for (const d of trazos) {
    const p = document.createElementNS(SVG, "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
}
const ICONOS = {
  descargar: ["M12 3v12m0 0l-4-4m4 4l4-4M4 17v3h16v-3"],
  calendario: ["M4 6h16v14H4z", "M4 10h16M8 3v4M16 3v4"],
  hospital: ["M4 21V7l8-4 8 4v14", "M9 21v-5h6v5", "M12 7v6M9 10h6"],
};

function enlace(clase, href, etiqueta, contenido, descarga = false) {
  const a = el("a", clase);
  a.href = href;
  a.setAttribute("aria-label", etiqueta);
  a.title = etiqueta;
  if (descarga) a.setAttribute("download", "");
  else { a.target = "_blank"; a.rel = "noopener"; }
  a.append(...contenido);
  return a;
}

function hoja(fechaHora) {
  const h = el("div", "citas-hoja");
  if (!fechaHora) { h.append(el("span", "citas-hoja-sin", "sin fecha")); return h; }
  const [fecha] = fechaHora.split("T");
  const [, mes, dia] = fecha.split("-").map(Number);
  h.append(el("span", "citas-hoja-mes", MESES[mes - 1]), el("span", "citas-hoja-dia", String(dia)));
  return h;
}

function fila(s, nueva) {
  const li = el("li", "citas-fila" + (nueva ? " nueva" : ""));
  const detalle = el("div", "citas-detalle");
  const linea1 = el("div", "citas-linea1");
  const hora = s.fecha_hora ? s.fecha_hora.split("T")[1].slice(0, 5) : null;
  linea1.append(el("span", "citas-hora", hora ? `${s.cuando.split(" · ")[0]} · ${hora}` : s.cuando),
                el("span", "citas-paciente", `${s.paciente} · #${s.id}`));
  const insignias = el("div", "citas-insignias");
  insignias.append(el("span", "citas-insignia citas-insignia-pendiente", "Pendiente de confirmación por la IPS"));
  if (s.calendario) {
    insignias.append(el("span", "citas-insignia citas-insignia-calendario",
                        s.calendario === "google" ? "✓ En Google Calendar" : "✓ En el calendario"));
  }
  detalle.append(linea1, el("div", "citas-motivo", s.motivo), insignias);

  const acciones = el("div", "citas-acciones");
  if (s.google && s.calendario !== "google") {
    acciones.append(enlace("citas-icono-boton", s.google, `Agregar la solicitud ${s.id} a Google Calendar`,
                           [icono(ICONOS.calendario)]));
  }
  if (s.ics) {
    acciones.append(enlace("citas-icono-boton", s.ics, `Descargar la solicitud ${s.id} como evento (.ics)`,
                           [icono(ICONOS.descargar)], true));
  }
  li.append(hoja(s.fecha_hora), detalle, acciones);
  return li;
}

function grupo(g, nuevos) {
  const sec = el("section", "citas-sede");
  const titulo = g.sede_nombre || `Sede ${g.sede_codigo}`;
  sec.setAttribute("aria-label", titulo);
  const cab = el("div", "citas-sede-cabecera");
  const nombre = el("div", "citas-sede-nombre");
  const ico = el("span", "citas-sede-icono");
  ico.append(icono(ICONOS.hospital));
  const textos = el("div");
  const n = g.solicitudes.length;
  textos.append(el("h3", "", titulo),
                el("div", "citas-sede-meta", `${n} solicitud${n === 1 ? "" : "es"} · id REPS ${g.sede_codigo}`));
  nombre.append(ico, textos);
  const excel = enlace("citas-boton", g.excel, `Descargar el Excel con las solicitudes de ${titulo}`,
                       [icono(ICONOS.descargar), document.createTextNode("Descargar Excel de esta IPS")], true);
  cab.append(nombre, excel);
  const lista = el("ul", "citas-lista");
  for (const s of g.solicitudes) lista.append(fila(s, nuevos.has(s.id)));
  sec.append(cab, lista);
  return sec;
}

function coincide(g, s, q) {
  return !q || [g.sede_nombre, g.sede_codigo, s.paciente, s.motivo].some((t) => (t || "").toLowerCase().includes(q));
}

function pintar(nuevos = new Set()) {
  const q = $("buscar").value.trim().toLowerCase();
  const grupos = datos.grupos
    .map((g) => ({ ...g, solicitudes: g.solicitudes.filter((s) => coincide(g, s, q)) }))
    .filter((g) => g.solicitudes.length);
  const cont = $("grupos");
  if (!datos.total) {
    const vacio = el("div", "citas-vacio");
    const ir = el("a", "citas-boton", "Ir al agente y pedir una cita");
    ir.href = "/";
    vacio.append(el("strong", "", "Todavía no hay solicitudes"),
                 el("span", "", "Pídele al agente por voz: «quiero una cita en …». Aparecerá aquí al confirmarla."), ir);
    cont.replaceChildren(vacio);
  } else if (!grupos.length) {
    cont.replaceChildren(el("div", "citas-vacio", `Nada coincide con «${$("buscar").value}».`));
  } else {
    cont.replaceChildren(...grupos.map((g) => grupo(g, nuevos)));
  }
  const visibles = grupos.reduce((n, g) => n + g.solicitudes.length, 0);
  $("resumen").textContent = q ? `${visibles} de ${datos.total} solicitudes` : "Se actualiza sola";
}

function cifras() {
  $("n-solicitudes").textContent = String(datos.total);
  $("n-ips").textContent = String(datos.grupos.length);
  const ahora = new Date().toISOString().slice(0, 16);
  const proximas = datos.grupos.flatMap((g) => g.solicitudes.map((s) => ({ ...s, sede: g.sede_nombre })))
    .filter((s) => s.fecha_hora && s.fecha_hora >= ahora).sort((a, b) => a.fecha_hora.localeCompare(b.fecha_hora));
  $("proxima").textContent = proximas.length ? proximas[0].cuando : "—";
  $("proxima").title = proximas.length ? proximas[0].sede : "";
}

async function cargar() {
  try {
    const r = await fetch("/api/citas", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    datos = await r.json();
    const ids = new Set(datos.grupos.flatMap((g) => g.solicitudes.map((s) => s.id)));
    const nuevos = vistos === null ? new Set() : new Set([...ids].filter((i) => !vistos.has(i)));
    vistos = ids;
    cifras();
    pintar(nuevos);
  } catch {
    $("resumen").textContent = "No pude cargar las solicitudes; reintento en unos segundos.";
  }
}

$("buscar").addEventListener("input", () => pintar());
cargar();
setInterval(cargar, REFRESCO_MS);
