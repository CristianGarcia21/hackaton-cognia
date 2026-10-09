"""Tests del frontend estático (issue #4): archivos servidos y coherencia entre index.html y app.js."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from server import mock_ws

WEB = Path(__file__).resolve().parent.parent / "web"


def test_cada_id_que_usan_los_js_existe_en_index_html():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = "\n".join(f.read_text(encoding="utf-8") for f in WEB.glob("*.js"))
    ids_html = set(re.findall(r'\bid="([^"]+)"', html))
    ids_js = set(re.findall(r'(?:\$|getElementById)\("([^"]+)"\)', js))
    assert ids_js and ids_js <= ids_html, f"faltan en index.html: {ids_js - ids_html}"


def test_index_tiene_una_sola_entrada_y_todo_modulo_se_importa():
    # Una sola etiqueta <script>: con varias, un módulo puede evaluarse tarde y perderse los primeros eventos.
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script\b[^>]*>", html) == ['<script type="module" src="inicio.js">']
    js = "\n".join(f.read_text(encoding="utf-8") for f in WEB.glob("*.js"))
    for modulo in WEB.glob("*.js"):
        if modulo.name == "inicio.js":
            continue
        usos = (f'"./{modulo.name}"' in js, f'addModule("{modulo.name}")' in js)
        assert any(usos), f"{modulo.name} no se importa (agrégalo en inicio.js)"


def test_cada_estado_del_contrato_tiene_texto_y_color():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    for estado in mock_ws.ev.EstadoConversacion:
        assert re.search(rf"\b{estado.value}:", js), f"sin texto para {estado.value}"
        assert f'[data-estado="{estado.value}"]' in css, f"sin color para {estado.value}"


def test_el_servidor_sirve_la_ui():
    with TestClient(mock_ws.create_app(velocidad=0)) as client:
        html = client.get("/")
        assert html.status_code == 200 and 'src="inicio.js"' in html.text and 'href="styles.css"' in html.text
        assert "javascript" in client.get("/app.js").headers["content-type"]
        assert client.get("/styles.css").headers["content-type"].startswith("text/css")
