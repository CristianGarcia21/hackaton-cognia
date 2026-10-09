// Onda de la voz en vivo dentro del indicador de estado: barras simétricas que siguen el volumen por frecuencia,
// del color del estado actual, y el aviso «Voz detectada» cuando alguien habla.
//
//     const onda = crearVisualizador(canvas, { alDetectarVoz: (hablando) => ... });
//     onda.conectar(analizador);   // cualquier AnalyserNode: el micrófono (microfono.js) o, en #5, el audio del agente
//     onda.desconectar();          // vuelve a la línea en reposo
//
// Dentro de la esfera del micrófono (.esfera) dibuja un ESPECTRO de barras en el centro, en blanco, y `alNivel(0..1)` informa
// el volumen suavizado para escalar la esfera y su brillo.
// Solo lee el AnalyserNode: no modifica el audio que se envía al backend.
// Con prefers-reduced-motion no se anima el espectro: solo se detecta la voz («Voz detectada»).

const ANCHO_BARRA = 4; // px CSS
const SEPARACION = 3;
const FRECUENCIA_MIN = 85; // Hz: banda principal de la voz
const FRECUENCIA_MAX = 4000;
const UMBRAL_VOZ = 0.035; // RMS 0..1 a partir del cual hay alguien hablando
const SOSTENER_VOZ_MS = 350; // evita parpadeo entre palabras

const BARRAS_ESFERA = 14; // par: espectro simétrico (graves al centro)

export function crearVisualizador(canvas, { alDetectarVoz = () => {}, alNivel = () => {} } = {}) {
  const ctx = canvas.getContext("2d");
  const esfera = Boolean(canvas.closest(".esfera"));
  let nivel = 0; // volumen suavizado (lerp) para que la esfera no tiemble
  let analizador = null;
  let frecuencias = null;
  let muestras = null;
  let alturas = [];
  let animacion = 0;
  let vozHasta = 0;
  let hablando = false;
  let colorActual = "#475569";
  const movimientoReducido = matchMedia("(prefers-reduced-motion: reduce)");

  function ajustarTamano() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(canvas.clientWidth * dpr));
    canvas.height = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (!analizador) dibujarReposo();
  }

  function leerColor() {
    // --c es el color del estado actual (styles.css); el canvas no hereda CSS, así que se lee aquí.
    const estado = canvas.closest(".estado");
    colorActual = (estado && getComputedStyle(estado).getPropertyValue("--c").trim()) || "#475569";
  }

  function barras(dpr) {
    if (esfera) return BARRAS_ESFERA;
    return Math.max(8, Math.floor(canvas.width / ((ANCHO_BARRA + SEPARACION) * dpr))) & ~1; // par: simétrica
  }

  function pintarEsfera(niveles) {
    // Barras verticales centradas, recortadas al círculo: el espectro de la voz llena la esfera.
    const { width: w, height: h } = canvas;
    const r = Math.min(w, h) / 2;
    const ancho = (r * 1.2) / niveles.length; // el espectro ocupa ~60 % del diámetro
    const barra = ancho * 0.6;
    const inicio = w / 2 - (ancho * niveles.length) / 2 + (ancho - barra) / 2;
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.beginPath();
    ctx.arc(w / 2, h / 2, r * 0.92, 0, Math.PI * 2);
    ctx.clip();
    ctx.fillStyle = "#ffffff";
    niveles.forEach((v, i) => {
      const alto = Math.max(barra, v * r * 1.1);
      ctx.globalAlpha = 0.55 + 0.45 * Math.min(1, v * 1.4);
      ctx.beginPath();
      ctx.roundRect(inicio + i * ancho, h / 2 - alto / 2, barra, alto, barra / 2);
      ctx.fill();
    });
    ctx.restore();
    ctx.globalAlpha = 1;
  }

  function pintar(niveles) {
    if (esfera) return pintarEsfera(niveles);
    const dpr = window.devicePixelRatio || 1;
    const { width: w, height: h } = canvas;
    const paso = (ANCHO_BARRA + SEPARACION) * dpr;
    const ancho = ANCHO_BARRA * dpr;
    const minimo = 3 * dpr;
    const inicio = (w - niveles.length * paso + SEPARACION * dpr) / 2;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = colorActual;
    niveles.forEach((nivel, i) => {
      const alto = Math.max(minimo, nivel * (h - 2 * dpr));
      const x = inicio + i * paso;
      ctx.globalAlpha = 0.35 + 0.65 * Math.min(1, nivel * 1.5);
      ctx.beginPath();
      ctx.roundRect(x, (h - alto) / 2, ancho, alto, ancho / 2);
      ctx.fill();
    });
    ctx.globalAlpha = 1;
  }

  function dibujarReposo() {
    if (esfera) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    pintar(new Array(barras(window.devicePixelRatio || 1)).fill(0));
  }

  function cuadro() {
    animacion = requestAnimationFrame(cuadro);
    const n = barras(window.devicePixelRatio || 1);
    const mitad = Math.ceil(n / 2);

    // Volumen (RMS) para detectar voz.
    analizador.getByteTimeDomainData(muestras);
    let suma = 0;
    for (const m of muestras) suma += ((m - 128) / 128) ** 2;
    const rms = Math.sqrt(suma / muestras.length);
    const ahora = performance.now();
    if (rms > UMBRAL_VOZ) vozHasta = ahora + SOSTENER_VOZ_MS;
    const hayVoz = ahora < vozHasta;
    if (hayVoz !== hablando) alDetectarVoz((hablando = hayVoz));
    if (movimientoReducido.matches) return;
    nivel += (Math.min(1, rms * 6) - nivel) * 0.2;
    alNivel(nivel);

    // Espectro de la banda de voz, espejado desde el centro (graves al centro, agudos a los lados).
    analizador.getByteFrequencyData(frecuencias);
    const hzPorBin = analizador.context.sampleRate / analizador.fftSize;
    const binMin = Math.max(1, Math.floor(FRECUENCIA_MIN / hzPorBin));
    const binMax = Math.min(frecuencias.length - 1, Math.ceil(FRECUENCIA_MAX / hzPorBin));
    const mitadNiveles = [];
    for (let i = 0; i < mitad; i++) {
      // Escala logarítmica: más barras para los graves, donde está la energía de la voz.
      const a = binMin * (binMax / binMin) ** (i / mitad);
      const b = binMin * (binMax / binMin) ** ((i + 1) / mitad);
      let pico = 0;
      for (let k = Math.floor(a); k <= Math.ceil(b) && k <= binMax; k++) pico = Math.max(pico, frecuencias[k]);
      mitadNiveles.push(pico / 255);
    }
    const objetivo = [...mitadNiveles.slice().reverse(), ...mitadNiveles];
    if (alturas.length !== objetivo.length) alturas = objetivo.map(() => 0);
    // Sube rápido y baja suave, como un vúmetro.
    alturas = alturas.map((actual, i) => actual + (objetivo[i] - actual) * (objetivo[i] > actual ? 0.5 : 0.15));
    pintar(alturas);
  }

  function conectar(nuevo) {
    desconectar();
    leerColor();
    analizador = nuevo;
    frecuencias = new Uint8Array(analizador.frequencyBinCount);
    muestras = new Uint8Array(analizador.fftSize);
    animacion = requestAnimationFrame(cuadro);
  }

  function desconectar() {
    cancelAnimationFrame(animacion);
    analizador = null;
    alturas = [];
    nivel = 0;
    alNivel(0);
    if (hablando) alDetectarVoz((hablando = false));
    dibujarReposo();
  }

  new ResizeObserver(ajustarTamano).observe(canvas);
  // El color del reposo sigue al estado aunque no haya audio.
  new MutationObserver(() => {
    leerColor();
    if (!analizador) dibujarReposo();
  })
    .observe(canvas.closest(".estado") ?? canvas, { attributes: true, attributeFilter: ["data-estado"] });
  leerColor();
  ajustarTamano();
  return { conectar, desconectar };
}
