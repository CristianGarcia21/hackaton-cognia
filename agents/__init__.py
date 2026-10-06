"""Registro de equipos de agentes. Para agregar uno nuevo: crea su archivo y añádelo a TEAMS."""

from agents.base_team import base_team
from agents.reto import reto_team

# nombre visible en la UI -> función (model, kb) -> list[Agent]
TEAMS = {
    "Reto": reto_team,
    "Equipo base": base_team,
}
