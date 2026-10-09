// Ficha visual de la cita: aparece cuando el agente propone una cita (antes del "sí") y se actualiza sola
// en cada fase: Por confirmar → Solicitud registrada → En el calendario. Escucha `action` del bus (kind
// "cita" / "evento", con data.fase; ver server/tarjetas.py). Módulo y estilos propios (tarjeta-cita.css) para
// no tocar el CSS principal. Todo dato del servidor se pinta con textContent.

import { bus } from "./app.js";

const estilo = document.createElement("link");
estilo.rel = "stylesheet";
estilo.href = "/tarjeta-cita.css";
document.head.append(estilo);

const FASES = {
  por_confirmar: { etiqueta: "Por confirmar", paso: 1, ayuda: "Di «sí» para registrar la solicitud." },
  registrada: { etiqueta: "Solicitud registrada", paso: 2, ayuda: "Pendiente de confirmación por la IPS." },
  en_calendario: { etiqueta: "En el calendario", paso: 3, ayuda: "Pendiente de confirmación por la IPS." },
};

let ficha = null;   // { raiz, datos }
let ocultar = null;

function el(tag, clase, texto) {
  const n = document.createElement(tag);
  if (clase) n.className = clase;
  if (texto !== undefined && texto !== null) n.textContent = texto;
  return n;
}

function boton(texto, href, descarga = false) {
  const a = el("a", "tc-boton", texto);
  a.href = href;
  if (descarga) a.setAttribute("download", "");
  else { a.target = "_blank"; a.rel = "noopener"; }
  return a;
}

function pintar(d) {
  const fase = FASES[d.fase] || FASES.por_confirmar;
  const raiz = el("aside", `tc-ficha tc-${d.fase}`);
  raiz.setAttribute("role", "status");
  raiz.setAttribute("aria-label", `Cita ${fase.etiqueta}`);

  const cerrar = el("button", "tc-cerrar", "×");
  cerrar.type = "button";
  cerrar.setAttribute("aria-label", "Cerrar la ficha de la cita");
  cerrar.addEventListener("click", () => { raiz.remove(); ficha = null; });

  const fecha = el("div", "tc-fecha");
  fecha.append(el("span", "tc-mes", d.mes || "—"), el("span", "tc-dia", d.dia ?? "?"), el("span", "tc-semana", d.semana || ""));

  const cuerpo = el("div", "tc-cuerpo");
  const estado = el("span", "tc-estado", fase.etiqueta);
  cuerpo.append(estado, el("div", "tc-hora", d.hora ? `${d.hora} · 30 min` : "Hora por definir"),
                el("div", "tc-sede", d.sede || "Sede por definir"));
  const quien = [d.paciente, d.motivo].filter(Boolean).join(" · ");
  if (quien) cuerpo.append(el("div", "tc-paciente", quien));

  const pasos = el("ol", "tc-pasos");
  pasos.setAttribute("aria-label", "Progreso");
  for (const f of Object.values(FASES)) {
    const li = el("li", f.paso <= fase.paso ? "hecho" : "", f.etiqueta);
    pasos.append(li);
  }

  const pie = el("div", "tc-pie");
  pie.append(el("span", "tc-ayuda", d.calendario ? `${d.calendario} · ${fase.ayuda}` : fase.ayuda));
  const acciones = el("div", "tc-acciones");
  if (d.google) acciones.append(boton("Ver en Google Calendar", d.google));
  if (d.ics && d.fase !== "por_confirmar") acciones.append(boton(".ics", d.ics, true));
  if (d.fase !== "por_confirmar") acciones.append(boton("Ver solicitudes", "/citas"));
  pie.append(acciones);

  raiz.append(cerrar, fecha, cuerpo, pasos, pie);
  return raiz;
}

bus.addEventListener("action", ({ detail: a }) => {
  if ((a.kind !== "cita" && a.kind !== "evento") || !a.data || !a.data.fase) return;
  // Una nueva propuesta empieza ficha nueva; las fases siguientes completan la misma (el evento no repite sede ni hora).
  const datos = a.data.fase === "por_confirmar" || !ficha ? { ...a.data } : { ...ficha.datos, ...stripVacios(a.data) };
  const raiz = pintar(datos);
  if (ficha) ficha.raiz.replaceWith(raiz);
  else document.body.append(raiz);
  ficha = { raiz, datos };
  clearTimeout(ocultar);
  if (datos.fase === "en_calendario") ocultar = setTimeout(() => raiz.classList.add("tc-plegada"), 20000);
});

function stripVacios(o) {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== ""));
}
