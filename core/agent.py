"""Agente base: instrucciones + herramientas + bucle de tool-calling.

    agente = Agent("analista", "Eres un analista de datos...", tools=[read_file, run_python])
    respuesta = agente.run("¿Cuál es el promedio de ventas en data.csv?")
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from core import llm
from core.tools import Tool

log = logging.getLogger("cognia.agent")


@dataclass
class Step:
    """Evento que emite un agente mientras trabaja (para mostrar el progreso en la UI)."""

    agent: str
    kind: Literal["info", "tool_call", "tool_result", "answer", "error"]
    content: str


OnStep = Callable[[Step], None] | None


class Agent:
    def __init__(self, name: str, instructions: str, tools: list[Tool] | None = None,
                 model: str | None = None, description: str = "", max_steps: int = 10,
                 temperature: float = 0.3):
        self.name = name
        self.instructions = instructions
        self.tools = {t.name: t for t in tools or []}
        self.model = model
        self.description = description or instructions.split("\n")[0]
        self.max_steps = max_steps
        self.temperature = temperature

    def run(self, task: str, history: list[dict] | None = None, on_step: OnStep = None) -> str:
        emit = on_step or (lambda s: None)
        messages = [{"role": "system", "content": self.instructions}, *(history or []),
                    {"role": "user", "content": task}]
        openai_tools = [t.to_openai() for t in self.tools.values()] or None

        for _ in range(self.max_steps):
            response = llm.chat(messages, model=self.model, tools=openai_tools, temperature=self.temperature)
            msg = response.choices[0].message
            if not msg.tool_calls:
                answer = msg.content or ""
                emit(Step(self.name, "answer", answer))
                return answer

            messages.append(_assistant_message(msg))
            for call in msg.tool_calls:
                messages.append(self._execute(call, emit))

        # Se acabaron los pasos: pedir una respuesta final sin herramientas.
        messages.append({"role": "user", "content": "Ya no puedes usar más herramientas. Da tu mejor respuesta final."})
        answer = llm.chat(messages, model=self.model, temperature=self.temperature).choices[0].message.content or ""
        emit(Step(self.name, "answer", answer))
        return answer

    def _execute(self, call, emit: Callable[[Step], None]) -> dict:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        emit(Step(self.name, "tool_call", f"{name}({json.dumps(args, ensure_ascii=False)[:300]})"))

        tool = self.tools.get(name)
        try:
            result = str(tool(**args)) if tool else f"Error: la herramienta '{name}' no existe."
        except Exception as e:  # noqa: BLE001 — el error vuelve al modelo para que se corrija
            log.exception("Tool %s falló", name)
            result = f"Error ejecutando {name}: {type(e).__name__}: {e}"
        emit(Step(self.name, "tool_result", result[:500]))
        return {"role": "tool", "tool_call_id": call.id, "name": name, "content": result}

    def as_tool(self, on_step: OnStep = None) -> Tool:
        """Expone el agente como herramienta, para que otro agente (supervisor) lo use."""
        return Tool(
            name=f"ask_{self.name}",
            description=f"Delega una tarea al agente '{self.name}': {self.description}",
            fn=lambda task: self.run(task, on_step=on_step),
            parameters={
                "type": "object",
                "properties": {"task": {"type": "string", "description": "tarea detallada y autocontenida"}},
                "required": ["task"],
            },
        )


def _assistant_message(msg) -> dict:
    tool_calls = []
    for c in msg.tool_calls:
        entry = {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
        # Gemini guarda aquí su "thought signature"; hay que devolverla para que siga razonando.
        extra = getattr(c, "provider_specific_fields", None)
        if extra:
            entry["provider_specific_fields"] = extra
        tool_calls.append(entry)
    return {"role": "assistant", "content": msg.content, "tool_calls": tool_calls}
