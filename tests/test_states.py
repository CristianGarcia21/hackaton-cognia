"""Tests de la máquina de estados de la sesión (issue #10, spec §5.4)."""

import pytest

from server.events import EstadoConversacion as E
from server.states import Evento as V
from server.states import Maquina


def recorrer(*eventos):
    m = Maquina()
    return m, [m.aplicar(e) for e in eventos]


def test_turno_completo_con_tool():
    m, cambios = recorrer(V.USUARIO_HABLA, V.USUARIO_TERMINA, V.TOOL_PEDIDA, V.TOOL_RESPONDIDA,
                          V.AGENTE_HABLA, V.AUDIO_TERMINADO)
    assert [c.estado for c in cambios] == [E.ESCUCHANDO, E.PENSANDO, E.EJECUTANDO_TOOL, E.PENSANDO,
                                           E.HABLANDO, E.INACTIVO]
    assert all(c.turn_id == 1 for c in cambios) and not any(c.interrumpido for c in cambios)


def test_el_saludo_habla_sin_turno_de_usuario():
    m, cambios = recorrer(V.AGENTE_HABLA, V.AUDIO_TERMINADO)
    assert [c.estado for c in cambios] == [E.HABLANDO, E.INACTIVO] and m.turn_id == 0


@pytest.mark.parametrize("previos", [[V.AGENTE_HABLA],
                                     [V.USUARIO_HABLA, V.USUARIO_TERMINA],
                                     [V.USUARIO_HABLA, V.USUARIO_TERMINA, V.TOOL_PEDIDA]])
def test_hablar_encima_del_agente_es_interrupcion_y_abre_turno(previos):
    m, _ = recorrer(*previos)
    turno = m.turn_id
    c = m.aplicar(V.USUARIO_HABLA)
    assert c.interrumpido and c.estado is E.ESCUCHANDO and c.turn_id == turno + 1


def test_hablar_estando_inactivo_no_es_interrupcion():
    m, cambios = recorrer(V.AGENTE_HABLA, V.AUDIO_TERMINADO, V.USUARIO_HABLA)
    assert not cambios[-1].interrumpido and cambios[-1].turn_id == 1


def test_tool_cancelada_vuelve_a_escuchar():
    m, cambios = recorrer(V.USUARIO_HABLA, V.USUARIO_TERMINA, V.TOOL_PEDIDA, V.TOOL_CANCELADA)
    assert cambios[-1].estado is E.ESCUCHANDO


def test_eventos_fuera_de_orden_se_ignoran():
    m = Maquina()
    assert m.aplicar(V.AUDIO_TERMINADO) is None and m.aplicar(V.TOOL_RESPONDIDA) is None
    assert m.estado is E.INACTIVO


def test_eventos_repetidos_no_emiten_otro_cambio():
    m, cambios = recorrer(V.AGENTE_HABLA, V.AGENTE_HABLA)
    assert cambios[1] is None
    m, cambios = recorrer(V.USUARIO_HABLA, V.USUARIO_TERMINA, V.TOOL_PEDIDA, V.TOOL_PEDIDA)
    assert cambios[-1] is None
