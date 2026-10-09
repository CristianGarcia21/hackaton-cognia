// Página /citas: solicitudes agrupadas por IPS (GET /api/citas). Se refresca sola para que una solicitud
// registrada por voz aparezca mientras se mira la página. Todo el texto se inserta con textContent.

const REFRESCO_MS = 5000;
let vistos = null; // ids ya mostrados: las solicitudes nuevas se resaltan

function el(tag, clase, texto) {
  const n = document.createElement(tag);
  if (clase) n.className = clase;
  if (texto !== undefined) n.textContent = texto;
  return n;
}

function enlace(texto, href, etiqueta, descarga = false) {
  const a = el("a", "", texto);
  a.href = href;
  a.setAttribute("aria-label", etiqueta);
  if (descarga) a.setAttribute("download", "");
  else { a.target = "_blank"; a.rel = "noopener"; }
  return a;
}

function fila(s, nueva) {
  const li = el("li", "citas-fila" + (nueva ? " nueva" : ""));
  li.append(el("div", "citas-cuando", s.cuando));
  const detalle = el("div", "citas-detalle");
  detalle.append(el("div", "citas-paciente", `#${s.id} · ${s.paciente}`), el("div", "citas-motivo", s.motivo),
                 el("span", "citas-estado", s.estado));
  li.append(detalle);
  const acciones = el("div", "citas-acciones");
  if (s.google) acciones.append(enlace("Agregar a Google Calendar", s.google, `Agregar la solicitud ${s.id} a Google Calendar`));
  if (s.ics) acciones.append(enlace("Descargar .ics", s.ics, `Descargar la solicitud ${s.id} como evento de calendario`, true));
  li.append(acciones);
  return li;
}

function grupo(g, nuevos) {
  const sec = el("section", "panel citas-sede");
  sec.setAttribute("aria-label", g.sede_nombre || `Sede ${g.sede_codigo}`);
  const cab = el("div", "citas-sede-cabecera");
  const titulo = el("div");
  titulo.append(el("h2", "", g.sede_nombre || "Sede sin nombre"), el("span", "citas-sede-id", `id ${g.sede_codigo}`));
  const n = g.solicitudes.length;
  cab.append(titulo, el("span", "citas-contador", `${n} solicitud${n === 1 ? "" : "es"} para enviar`));
  const lista = el("ul", "citas-lista");
  for (const s of g.solicitudes) lista.append(fila(s, nuevos.has(s.id)));
  sec.append(cab, lista);
  return sec;
}

async function cargar() {
  try {
    const r = await fetch("/api/citas", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const datos = await r.json();
    const ids = new Set(datos.grupos.flatMap((g) => g.solicitudes.map((s) => s.id)));
    const nuevos = vistos === null ? new Set() : new Set([...ids].filter((i) => !vistos.has(i)));
    vistos = ids;
    const cont = document.getElementById("grupos");
    cont.replaceChildren(...(datos.grupos.length ? datos.grupos.map((g) => grupo(g, nuevos))
      : [el("p", "panel citas-vacio", "Todavía no hay solicitudes. Pídele al agente que registre una.")]));
    const sedes = datos.grupos.length;
    document.getElementById("resumen").textContent =
      `${datos.total} solicitud${datos.total === 1 ? "" : "es"} en ${sedes} IPS · se actualiza sola`;
  } catch (e) {
    document.getElementById("resumen").textContent = "No pude cargar las solicitudes; reintento en unos segundos.";
  }
}

async function excelDisponible() {
  // El Excel lo sirve el servidor MCP de Excel (#17); el botón aparece solo cuando existe.
  try {
    const r = await fetch("/api/citas/excel", { method: "HEAD" });
    document.getElementById("excel").hidden = !r.ok;
  } catch { /* sin Excel: el botón queda oculto */ }
}

cargar();
excelDisponible();
setInterval(cargar, REFRESCO_MS);
