// Panel lateral (drawer) reutilizable: se abre desde un botón del menú superior, con fondo oscurecido.
// Se cierra con Esc, con clic en el fondo o con ✕; el foco vuelve al botón. Un solo panel abierto a la vez.
//
//     const fuente = crearCajon("fuente");   // usa #fuente, #fuente-fondo, #abrir-fuente, #cerrar-fuente
//     fuente.avisar();                       // marca el botón con un punto «hay algo nuevo» si está cerrado

const abiertos = new Set();

export function crearCajon(id) {
  const parte = (sufijo) => document.getElementById(sufijo ? `${id}-${sufijo}` : id); // id o id-sufijo
  const panel = parte();
  const fondo = parte("fondo");
  const boton = document.getElementById(`abrir-${id}`);
  const cerrarBoton = document.getElementById(`cerrar-${id}`);
  const punto = parte("nuevo");

  const estaAbierto = () => !panel.hidden;

  function abrir() {
    for (const otro of abiertos) if (otro !== api) otro.cerrar(false);
    panel.hidden = false;
    fondo.hidden = false;
    boton.setAttribute("aria-expanded", "true");
    if (punto) punto.hidden = true;
    abiertos.add(api);
    cerrarBoton.focus();
  }

  function cerrar(devolverFoco = true) {
    if (!estaAbierto()) return;
    panel.hidden = true;
    fondo.hidden = true;
    boton.setAttribute("aria-expanded", "false");
    abiertos.delete(api);
    if (devolverFoco) boton.focus();
  }

  const api = {
    abrir, cerrar, estaAbierto,
    avisar() {
      if (punto && !estaAbierto()) punto.hidden = false;
    },
  };

  boton.addEventListener("click", () => (estaAbierto() ? cerrar() : abrir()));
  cerrarBoton.addEventListener("click", () => cerrar());
  fondo.addEventListener("click", () => cerrar());
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") cerrar();
  });
  return api;
}
