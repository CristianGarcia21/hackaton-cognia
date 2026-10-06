"""UI de demo. Ejecutar con:  uv run python run.py"""

import time
from pathlib import Path

import streamlit as st

from agents import TEAMS
from core import config
from core.agent import Agent, Step
from core.memory import KnowledgeBase
from core.orchestrator import PlanExecute, Router, Supervisor

UPLOADS = Path("data/uploads")
UPLOADS.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Cognia", page_icon="🧠", layout="wide")

state = st.session_state
state.setdefault("messages", [])  # {"role", "content", "steps"}
state.setdefault("kb", KnowledgeBase())
state.setdefault("files", {})  # nombre -> ruta

ICONS = {"info": "🧭", "tool_call": "🔧", "tool_result": "📄", "answer": "✅", "error": "❌"}
MODES = {
    "Agente único": "Habla directo con un agente.",
    "Router": "Un enrutador elige al especialista adecuado.",
    "Supervisor": "Un jefe delega en los especialistas las veces que necesite.",
    "Planner → Workers": "Se planifica la tarea, se ejecuta por pasos y se integra el resultado.",
}

# ---------------- Barra lateral ----------------
with st.sidebar:
    st.title("🧠 Cognia")

    with st.expander("🔑 Proveedores", expanded=not config.available_models()):
        for name, cfg in config.PROVIDERS.items():
            ok = config.has_key(name)
            st.markdown(f"{'🟢' if ok else '⚪'} **{name}**" + ("" if ok else f" · [obtener key]({cfg['signup']})"))

    models = config.available_models()
    if not models:
        st.error("No hay API keys. Copia `.env.example` a `.env`, llénalo y reinicia.")
        st.stop()
    model = st.selectbox("Modelo principal", models, index=models.index(config.default_model()) if config.default_model() in models else 0,
                         help="Si falla, se usa automáticamente el siguiente proveedor disponible.")

    team_name = st.selectbox("Equipo de agentes", list(TEAMS))
    team = TEAMS[team_name](model=model, kb=state.kb)

    mode = st.radio("Modo de orquestación", list(MODES), help="\n\n".join(f"**{k}**: {v}" for k, v in MODES.items()))
    agent_name = st.selectbox("Agente", [a.name for a in team]) if mode == "Agente único" else None

    st.divider()
    uploaded = st.file_uploader("📎 Documentos (se indexan para RAG)", accept_multiple_files=True,
                                type=["pdf", "docx", "txt", "md", "csv", "xlsx", "json"])
    for f in uploaded or []:
        if f.name not in state.files:
            path = UPLOADS / f.name
            path.write_bytes(f.getbuffer())
            with st.spinner(f"Indexando {f.name}..."):
                try:
                    n = state.kb.add_file(path)
                    st.toast(f"{f.name}: {n} fragmentos indexados")
                except Exception as e:  # noqa: BLE001
                    st.warning(f"No se pudo indexar {f.name}: {e}")
            state.files[f.name] = str(path)
            st.rerun()  # recrea los agentes para que reciban la tool de búsqueda
    if state.files:
        search = "embeddings" if state.kb.use_embeddings else "palabras clave"
        st.caption(f"{len(state.kb)} fragmentos · búsqueda por {search}")

    if st.button("🗑️ Nueva conversación", use_container_width=True):
        state.messages = []
        st.rerun()


def build_runner() -> Agent | Router | Supervisor | PlanExecute:
    if mode == "Agente único":
        return next(a for a in team if a.name == agent_name)
    if mode == "Router":
        return Router(team, model=model)
    if mode == "Supervisor":
        return Supervisor(team, model=model)
    return PlanExecute(team, model=model)


def render_steps(steps: list[dict]) -> None:
    for s in steps:
        if s["kind"] == "answer":
            continue
        st.markdown(f"{ICONS[s['kind']]} **{s['agent']}** · `{s['kind']}`")
        st.code(s["content"], language=None, wrap_lines=True)


# ---------------- Chat ----------------
st.header(f"{team_name} · {mode}")
st.caption(MODES[mode])

for m in state.messages:
    with st.chat_message(m["role"]):
        if m.get("steps"):
            with st.expander(f"Ver razonamiento ({len(m['steps'])} pasos)"):
                render_steps(m["steps"])
        st.markdown(m["content"])

if prompt := st.chat_input("Escribe tu petición..."):
    with st.chat_message("user"):
        st.markdown(prompt)
    history = [{"role": m["role"], "content": m["content"]} for m in state.messages]
    state.messages.append({"role": "user", "content": prompt})

    task = prompt
    if state.files:
        task += "\n\n[Archivos disponibles: " + ", ".join(state.files.values()) + "]"

    with st.chat_message("assistant"):
        steps: list[dict] = []
        status = st.status("Pensando...", expanded=True)

        def on_step(step: Step) -> None:
            steps.append(step.__dict__)
            if step.kind != "answer":
                status.markdown(f"{ICONS[step.kind]} **{step.agent}** · {step.content[:300]}")

        start = time.time()
        try:
            answer = build_runner().run(task, history=history, on_step=on_step)
            status.update(label=f"Listo en {time.time() - start:.1f}s · {len(steps)} pasos", state="complete", expanded=False)
        except Exception as e:  # noqa: BLE001
            answer = f"⚠️ Error: {e}"
            status.update(label="Error", state="error")
        st.markdown(answer)
    state.messages.append({"role": "assistant", "content": answer, "steps": steps})
