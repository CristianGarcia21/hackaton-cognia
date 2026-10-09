// Punto de entrada de la UI: importa todos los módulos (cada uno se suscribe al bus al evaluarse) y solo
// entonces abre el WebSocket. Con varias etiquetas <script type="module"> el navegador puede evaluar un módulo
// después de que lleguen los primeros eventos y ese panel se los pierde; los imports estáticos lo evitan.
// Para agregar un módulo nuevo (p. ej. inspector.js de #20): un import más aquí.

import { iniciar } from "./app.js";
import "./microfono.js";
import "./captura.js";
import "./reproductor.js";
import "./paneles.js";
import "./brief.js";
import "./inspector.js";

iniciar();
