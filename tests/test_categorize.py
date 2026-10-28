"""Tests de la lógica de categorize.py sin base de datos."""
import categorize


def test_words_normaliza_acentos_y_quita_numeros_palabras_cortas_y_genericas():
    assert categorize.words("Frutería Pepe 1234 · COMPRA en ES www.pepe.com") == {"FRUTERIA", "PEPE"}


def test_predice_la_categoria_de_una_palabra_clara():
    model = categorize.train([("Fruteria Pepe", "Comida"), ("FRUTERIA PEPE", "Comida")], {"Comida"})
    assert categorize.predict(model, "FRUTERIA PEPE MADRID ES") == "Comida"


def test_no_decide_con_una_sola_aparicion():
    model = categorize.train([("Fruteria Pepe", "Comida")], {"Comida"})
    assert categorize.predict(model, "FRUTERIA PEPE") is None


def test_no_decide_si_la_palabra_se_reparte_entre_categorias():
    history = [("Mercado", "Comida")] * 5 + [("Mercado", "Compras")] * 5
    model = categorize.train(history, {"Comida", "Compras"})
    assert categorize.predict(model, "MERCADO CENTRAL") is None


def test_tolera_alguna_excepcion_si_la_palabra_es_casi_siempre_de_una_categoria():
    history = [("Aldi", "Comida")] * 9 + [("Aldi", "Compras")]
    model = categorize.train(history, {"Comida", "Compras"})
    assert categorize.predict(model, "ALDI SUPERMERCADOS") == "Comida"


def test_no_decide_si_dos_palabras_indican_categorias_distintas():
    history = [("Pepe", "Comida")] * 2 + [("Zapatos", "Compras")] * 2
    model = categorize.train(history, {"Comida", "Compras"})
    assert categorize.predict(model, "PEPE ZAPATOS") is None


def test_una_ciudad_ignorada_no_decide_ni_crea_ambiguedad():
    history = [("Hotel Amsterdam", "Viajes")] * 3 + [("Sanitas", "Seguros")] * 3
    sin_ignorar = categorize.train(history, {"Viajes", "Seguros"})
    ignorando = categorize.train(history, {"Viajes", "Seguros"}, ignored=["Amsterdam"])
    assert categorize.predict(sin_ignorar, "SANITAS AMSTERDAM") is None          # ambigua por la ciudad
    assert categorize.predict(ignorando, "SANITAS AMSTERDAM") == "Seguros"
    assert categorize.predict(ignorando, "TIENDA AMSTERDAM") is None               # la ciudad sola ya no decide


def test_las_palabras_ignoradas_no_distinguen_mayusculas_ni_acentos():
    model = categorize.train([("Bar León", "Comida")] * 3, {"Comida"}, ignored=["LEON"])
    assert "LEON" not in model
    assert categorize.predict(model, "BAR") == "Comida"


def test_ignora_categorias_que_no_estan_permitidas():
    model = categorize.train([("B100", "B100")] * 3, {"Comida"})
    assert categorize.predict(model, "B100") is None
