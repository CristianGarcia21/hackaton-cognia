// Panel del brief (#12, paso P3): de qué trata la fuente, cifras reales, puntos clave y preguntas pulsables.
// Cada pregunta se envía como text_input (el agente la procesa como si la hubieran dicho por voz).
// Todo dato del servidor se pinta con textContent.

import { bus, enviar } from "./app.js";

const $ = (id) => document.getElementById(id);
const resumen = $("brief-resumen");
const cifras = $("brief-cifras");
const puntos = $("brief-puntos");
const bloquePreguntas = $("brief-preguntas-bloque");
const preguntas = $("brief-preguntas");

// Cifras que se muestran como tarjetas (las demás claves de stats se ignoran).
const CIFRAS = [
  ["registros", "registros"],
  ["sedes", "sedes de IPS"],
  ["departamentos", "departamentos"],
  ["municipios", "municipios"],
];
const numero = (n) => Number(n).toLocaleString("es-CO");

let conectado = false;

function nodo(etiqueta, clase, texto) {
  const el = document.createElement(etiqueta);
  if (clase) el.className = clase;
  if (texto != null) el.textContent = texto;
  return el;
}

function habilitarPreguntas() {
  for (const b of preguntas.querySelectorAll("button")) b.disabled = !conectado;
}

bus.addEventListener("brief", ({ detail: b }) => {
  resumen.className = "";
  resumen.textContent = b.summary;

  cifras.replaceChildren(...CIFRAS.filter(([clave]) => typeof b.stats?.[clave] === "number").map(([clave, texto]) => {
    const li = nodo("li");
    li.append(nodo("strong", null, numero(b.stats[clave])), nodo("span", null, texto));
    return li;
  }));
  if (typeof b.stats?.sin_nivel_pct === "number") {
    const li = nodo("li");
    li.append(nodo("strong", null, `${b.stats.sin_nivel_pct} %`), nodo("span", null, "sin nivel de atención"));
    cifras.append(li);
  }

  puntos.replaceChildren(...(b.key_points ?? []).map((p) => nodo("li", null, p)));

  preguntas.replaceChildren(...(b.questions ?? []).map((texto) => {
    const boton = nodo("button", "pregunta", texto);
    boton.type = "button";
    boton.addEventListener("click", () => {
      if (enviar({ type: "text_input", text: texto })) boton.classList.add("enviada");
    });
    return boton;
  }));
  bloquePreguntas.hidden = !(b.questions ?? []).length;
  habilitarPreguntas();
});

bus.addEventListener("ready", () => {
  conectado = true;
  habilitarPreguntas();
});
bus.addEventListener("desconectado", () => {
  conectado = false;
  habilitarPreguntas();
});
