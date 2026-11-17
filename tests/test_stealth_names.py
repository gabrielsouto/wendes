from __future__ import annotations

from conftest import make_cfg
from wendes.facilitator.stealth import canonical_name, is_bot_display


def test_canonical_name_por_nome_e_alias():
    cfg = make_cfg()
    assert canonical_name(cfg, "Fulana da Silva") == "Fulana da Silva"
    assert canonical_name(cfg, "  fsilva ") == "Fulana da Silva"
    assert canonical_name(cfg, "Beltrano (celular)") == "Beltrano Souza"


def test_canonical_name_visitante_mantem_display():
    assert canonical_name(make_cfg(), "  Ciclano Visitante ") == "Ciclano Visitante"


def test_is_bot_display():
    cfg = make_cfg()
    assert is_bot_display(cfg, "Wendes")
    assert not is_bot_display(cfg, "Fulana da Silva")
