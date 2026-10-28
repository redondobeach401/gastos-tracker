"""Tests de las piezas del banco de pruebas (scripts/evaluar_categorizacion.py), sin base de datos."""
import random

import categorize
from scripts import evaluar_categorizacion as ev

ROWS = [("manual", "Aldi", "Comida")] * 6 + [("manual", "Amazon", "Compras")] * 6 + [("manual", "", "Ocio")] * 3


def test_validacion_cruzada_predice_cada_gasto_una_vez_y_sin_verse_a_si_mismo():
    res = ev.validacion_cruzada(ROWS, pliegues=5, semilla=0, soporte=2, pureza=0.9, allowed={"Comida", "Compras", "Ocio"})
    assert len(res) == len(ROWS)
    total, decididos, aciertos = ev.resumen(res)
    assert (total, decididos, aciertos) == (15, 12, 12)  # los 3 sin descripción no se pueden decidir


def test_la_validacion_cruzada_es_repetible_con_la_misma_semilla():
    a = ev.validacion_cruzada(ROWS, 3, 7, 2, 0.9, {"Comida", "Compras", "Ocio"})
    b = ev.validacion_cruzada(ROWS, 3, 7, 2, 0.9, {"Comida", "Compras", "Ocio"})
    assert a == b


def test_el_ruido_neutro_nunca_cambia_las_palabras_utiles():
    rng = random.Random(0)
    for desc in ("ALDI SUPERMERCADOS", "Frutería Pepe", "OVPAY AMSTERDAM"):
        for _ in range(50):
            assert categorize.words(ev.ruido_neutro(desc, rng)) == categorize.words(desc)


def test_el_ruido_realista_anade_una_ciudad():
    sucia = ev.ruido_realista("ALDI", random.Random(0))
    assert categorize.words(sucia) - {"ALDI"} <= set(ev.CIUDADES)
