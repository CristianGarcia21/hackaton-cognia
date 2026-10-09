// Carita SVG de la emoción detectada, junto a la esfera. Solo cambia atributos del SVG (d, fill, ry); la
// transición la hace CSS (Chrome interpola `d`). Escucha `emotion` del bus (la del último hablante humano).
//
// Para la demo, desde la consola del navegador:   kognia.emocion("alegria")   (o "enojo", "tristeza", ...)
// Solo cambia la carita (no toca el gráfico ni la adaptación).

import { bus } from "./app.js";

// Bocas: misma estructura de comandos (M + 2 Q, forma cerrada) para que la transición de `d` sea suave.
const BOCA = {
  amplia: "M18 38 Q32 54 46 38 Q32 44 18 38",
  suave: "M22 41 Q32 48 42 41 Q32 44 22 41",
  recta: "M22 43 Q32 43 42 43 Q32 44 22 43",
  abajo: "M22 47 Q32 37 42 47 Q32 40 22 47",
  ondulada: "M22 45 Q28 40 42 44 Q30 43 22 45",
  pequena: "M27 44 Q32 51 37 44 Q32 38 27 44",
  tensa: "M22 45 Q32 41 42 45 Q32 43 22 45",
  o: "M26 44 Q32 55 38 44 Q32 33 26 44",
};
// Cejas [izquierda, derecha]: misma estructura (M + L).
const CEJAS = {
  normal: ["M18 20 L28 21", "M36 21 L46 20"],
  caidas: ["M18 22 L28 19", "M36 19 L46 22"],
  v: ["M18 18 L28 23", "M36 23 L46 18"],
  altas: ["M18 16 L28 15", "M36 15 L46 16"],
  inclinadas: ["M18 17 L28 20", "M36 21 L46 19"],
};
// Ojos: alto de la elipse (curvos ≈ 1.6, cerrados ≈ 0.8, normales 3.5, grandes 5).
const OJOS = { curvos: 1.6, cerrados: 0.8, normales: 3.5, grandes: 5 };

/** Claves = emociones del contrato (server/events.py). Para ajustar la demo, edita solo este objeto. */
export const EMOCIONES = {
  alegria: { etiqueta: "Alegría", color: "#FACC15", boca: "amplia", ojos: "curvos", cejas: "normal" },
  calma: { etiqueta: "Calma", color: "#2DD4BF", boca: "suave", ojos: "cerrados", cejas: "normal" },
  neutral: { etiqueta: "Neutral", color: "#94A3B8", boca: "recta", ojos: "normales", cejas: "normal" },
  tristeza: { etiqueta: "Tristeza", color: "#60A5FA", boca: "abajo", ojos: "normales", cejas: "caidas" },
  confusion: { etiqueta: "Confusión", color: "#FB923C", boca: "ondulada", ojos: "normales", cejas: "inclinadas" },
  ansiedad: { etiqueta: "Ansiedad", color: "#A78BFA", boca: "pequena", ojos: "grandes", cejas: "caidas" },
  frustracion: { etiqueta: "Frustración", color: "#FB7185", boca: "tensa", ojos: "normales", cejas: "v" },
  enojo: { etiqueta: "Enojo", color: "#F87171", boca: "tensa", ojos: "normales", cejas: "v" },
  urgencia: { etiqueta: "Urgencia", color: "#F472B6", boca: "o", ojos: "grandes", cejas: "altas" },
};
// Sinónimos que pueden llegar (el backend ya normaliza, esto es por si acaso).
const ALIAS = { feliz: "alegria", tranquilo: "calma", miedo: "ansiedad", preocupacion: "confusion", ira: "enojo",
                sorpresa: "urgencia", triste: "tristeza" };

const $ = (id) => document.getElementById(id);
const svg = $("carita");
const figura = svg.closest(".carita");
const partes = {
  cara: $("carita-cara"), cejaI: $("carita-ceja-i"), cejaD: $("carita-ceja-d"),
  ojoI: $("carita-ojo-i"), ojoD: $("carita-ojo-d"), boca: $("carita-boca"), texto: $("carita-texto"),
};

function clave(nombre) {
  const k = String(nombre ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
  return EMOCIONES[k] ? k : ALIAS[k] ?? "neutral";
}

export function mostrarEmocion(nombre, intensidad) {
  const k = clave(nombre);
  const e = EMOCIONES[k];
  partes.cara.setAttribute("fill", e.color);
  partes.boca.setAttribute("d", BOCA[e.boca]);
  const [i, d] = CEJAS[e.cejas];
  partes.cejaI.setAttribute("d", i);
  partes.cejaD.setAttribute("d", d);
  for (const ojo of [partes.ojoI, partes.ojoD]) ojo.setAttribute("ry", String(OJOS[e.ojos]));
  const pct = typeof intensidad === "number" ? ` · ${Math.round(intensidad * 100)} %` : "";
  partes.texto.textContent = `${e.etiqueta}${pct}`;
  svg.setAttribute("aria-label", `Emoción detectada: ${e.etiqueta.toLowerCase()}`);
  figura.dataset.emocion = k;
  // Rebote breve al cambiar (CSS; desactivado con prefers-reduced-motion).
  figura.classList.remove("cambio");
  void figura.offsetWidth;
  figura.classList.add("cambio");
}

bus.addEventListener("emotion", ({ detail: e }) => {
  if (e.speaker !== "Agente") mostrarEmocion(e.emotion, e.intensity);
});

bus.addEventListener("ready", () => {
  mostrarEmocion("neutral");
  partes.texto.textContent = "Sin emoción aún";
  svg.setAttribute("aria-label", "Emoción detectada: ninguna aún");
});

// Atajo para la demo desde la consola: kognia.emocion("enojo")
window.kognia = Object.assign(window.kognia ?? {}, {
  emocion: (nombre, intensidad = 0.7) => mostrarEmocion(nombre, intensidad),
  emociones: Object.keys(EMOCIONES),
});

mostrarEmocion("neutral");
partes.texto.textContent = "Sin emoción aún";
