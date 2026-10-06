"""Patrones de orquestación multi-agente. Todos tienen la misma interfaz que Agent:
`.name` y `.run(task, history=None, on_step=None) -> str`, así que son intercambiables.

- Router:      elige UN especialista según la petición. Rápido y barato.
- PlanExecute: un planificador divide la tarea, los especialistas ejecutan en orden
               y un sintetizador une los resultados. Bueno para tareas largas.
- Supervisor:  un agente que llama a los especialistas como herramientas, cuando quiera.
               El más flexible.
"""

from pydantic import BaseModel, Field

from core import llm
from core.agent import Agent, OnStep, Step


def _roster(agents: list[Agent]) -> str:
    return "\n".join(f"- {a.name}: {a.description}" for a in agents)


class RouteDecision(BaseModel):
    agent: str = Field(description="nombre exacto del agente elegido")
    reason: str = Field(description="por qué, en una frase")


class Router:
    name = "router"

    def __init__(self, agents: list[Agent], model: str | None = None):
        self.agents = {a.name: a for a in agents}
        self.model = model

    def run(self, task: str, history: list[dict] | None = None, on_step: OnStep = None) -> str:
        emit = on_step or (lambda s: None)
        decision = llm.structured(
            f"Agentes disponibles:\n{_roster(list(self.agents.values()))}\n\nPetición del usuario:\n{task}",
            RouteDecision, model=self.model,
            system="Eres un enrutador. Elige el agente más adecuado para atender la petición.",
        )
        agent = self.agents.get(decision.agent) or next(iter(self.agents.values()))
        emit(Step(self.name, "info", f"→ {agent.name}: {decision.reason}"))
        return agent.run(task, history, on_step)


class PlanStep(BaseModel):
    agent: str = Field(description="nombre exacto del agente que ejecuta este paso")
    task: str = Field(description="instrucción concreta y autocontenida para ese agente")


class Plan(BaseModel):
    steps: list[PlanStep]


class PlanExecute:
    name = "planner"

    def __init__(self, agents: list[Agent], model: str | None = None, max_steps: int = 5):
        self.agents = {a.name: a for a in agents}
        self.model = model
        self.max_steps = max_steps

    def run(self, task: str, history: list[dict] | None = None, on_step: OnStep = None) -> str:
        emit = on_step or (lambda s: None)
        plan = llm.structured(
            f"Agentes disponibles:\n{_roster(list(self.agents.values()))}\n\nTarea:\n{task}",
            Plan, model=self.model,
            system=f"Eres un planificador. Divide la tarea en como máximo {self.max_steps} pasos, "
                   "cada uno asignado a un agente. Usa los menos pasos posibles.",
        )
        steps = plan.steps[: self.max_steps]
        emit(Step(self.name, "info", "Plan:\n" + "\n".join(f"{i + 1}. [{s.agent}] {s.task}" for i, s in enumerate(steps))))

        results: list[str] = []
        for i, s in enumerate(steps, 1):
            agent = self.agents.get(s.agent) or next(iter(self.agents.values()))
            context = "\n\n".join(results)
            prompt = f"{s.task}\n\nResultados de pasos anteriores:\n{context}" if context else s.task
            emit(Step(self.name, "info", f"Paso {i}/{len(steps)} → {agent.name}"))
            results.append(f"## Paso {i} ({agent.name})\n{agent.run(prompt, on_step=on_step)}")

        answer = llm.ask(
            f"Tarea original:\n{task}\n\nResultados de cada paso:\n\n" + "\n\n".join(results),
            system="Integra los resultados en una respuesta final clara y completa para el usuario. "
                   "No menciones los pasos internos salvo que aporten.",
            model=self.model,
        )
        emit(Step(self.name, "answer", answer))
        return answer


class Supervisor:
    name = "supervisor"

    def __init__(self, agents: list[Agent], model: str | None = None, instructions: str = ""):
        self.agents = agents
        self.model = model
        self.instructions = (
            "Eres un supervisor que coordina un equipo de agentes especialistas. Delega subtareas "
            "llamando a sus herramientas ask_<agente>, revisa lo que devuelven y da una respuesta final completa.\n\n"
            f"Equipo:\n{_roster(agents)}\n\n{instructions}"
        ).strip()

    def run(self, task: str, history: list[dict] | None = None, on_step: OnStep = None) -> str:
        boss = Agent(self.name, self.instructions, model=self.model, max_steps=12,
                     tools=[a.as_tool(on_step) for a in self.agents])
        return boss.run(task, history, on_step)
