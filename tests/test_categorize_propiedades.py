"""Tests de propiedades de categorize.py: Hypothesis genera cientos de historiales y descripciones aleatorios
y comprueba que estas reglas se cumplen SIEMPRE. Si encuentra un caso que las rompe, lo reduce al ejemplo
mínimo y lo muestra en el fallo.
"""
import collections
import string

from hypothesis import assume, given, settings
from hypothesis import strategies as st

import categorize

PERMITIDAS = ("Comida", "Compras", "Ocio")
NO_PERMITIDA = "B100"
ACENTOS = str.maketrans("AEIOU", "ÁÉÍÓÚ")
MUCHOS = settings(max_examples=300, deadline=None)

palabra = st.text(alphabet=string.ascii_uppercase, min_size=3, max_size=8).filter(lambda w: w not in categorize.STOP)


@st.composite
def escenario(draw):
    """Historial y descripción construidos con un vocabulario pequeño, para que se repitan palabras."""
    vocabulario = draw(st.lists(palabra, min_size=1, max_size=8, unique=True))
    descripcion = st.lists(st.sampled_from(vocabulario), min_size=1, max_size=3).map(" ".join)
    categoria = st.sampled_from((*PERMITIDAS, NO_PERMITIDA))
    historial = draw(st.lists(st.tuples(descripcion, categoria), max_size=30))
    consulta = " ".join([draw(descripcion), *draw(st.lists(palabra, max_size=2))])
    return historial, consulta


def decidir(historial, consulta, soporte=categorize.MIN_SUPPORT, pureza=categorize.MIN_PURITY):
    return categorize.explain(categorize.train(historial, set(PERMITIDAS)), consulta, soporte, pureza)


def calculo_independiente(historial, consulta, soporte, pureza):
    """La misma regla, contada a mano sin usar train/explain: lo que el código debería decidir."""
    concluyentes = set()
    for w in categorize.words(consulta):
        veces = collections.Counter(c for d, c in historial if c in PERMITIDAS and w in categorize.words(d))
        total = sum(veces.values())
        if total >= soporte and max(veces.values(), default=0) / max(total, 1) >= pureza:
            concluyentes |= {c for c, n in veces.items() if n == max(veces.values())}
    return concluyentes.pop() if len(concluyentes) == 1 else None


@MUCHOS
@given(escenario())
def test_solo_devuelve_una_categoria_permitida_o_nada(caso):
    assert decidir(*caso).category in (*PERMITIDAS, None)


@MUCHOS
@given(escenario(), st.integers(1, 4), st.sampled_from((0.6, 0.8, 0.9, 1.0)))
def test_decide_lo_mismo_que_un_calculo_independiente(caso, soporte, pureza):
    historial, consulta = caso
    assert decidir(historial, consulta, soporte, pureza).category == calculo_independiente(
        historial, consulta, soporte, pureza
    )


@MUCHOS
@given(escenario())
def test_si_decide_la_explicacion_lo_justifica(caso):
    decision = decidir(*caso)
    concluyentes = [e for e in decision.evidence if e.decisive]
    if decision.category:
        assert concluyentes
        assert {e.category for e in concluyentes} == {decision.category}
        assert decision.reason.startswith("por ")
    else:
        assert len({e.category for e in concluyentes}) != 1


@MUCHOS
@given(
    escenario(),
    st.lists(st.sampled_from(["minusculas", "acentos", "numeros", "espacios", "genericas"]), min_size=1),
    st.integers(1000, 10**9),
)
def test_mayusculas_acentos_numeros_espacios_y_palabras_genericas_no_cambian_nada(caso, cambios, numero):
    historial, consulta = caso
    sucia = consulta
    for cambio in cambios:
        sucia = {
            "minusculas": lambda s: s.lower(),
            "acentos": lambda s: s.translate(ACENTOS),
            "numeros": lambda s: f"{numero} {s} {numero}",
            "espacios": lambda s: s.replace(" ", "   "),
            "genericas": lambda s: f"COMPRA WWW {s} TARJ",
        }[cambio](sucia)
    assert decidir(historial, sucia) == decidir(historial, consulta)


@MUCHOS
@given(escenario(), st.randoms(use_true_random=False))
def test_el_orden_del_historial_no_influye(caso, rnd):
    historial, consulta = caso
    barajado = list(historial)
    rnd.shuffle(barajado)
    assert decidir(barajado, consulta) == decidir(historial, consulta)


@MUCHOS
@given(escenario(), palabra)
def test_una_palabra_que_el_historial_no_conoce_no_cambia_nada(caso, nueva):
    historial, consulta = caso
    assume(all(nueva not in categorize.words(d) for d, _ in historial))
    assert decidir(historial, f"{consulta} {nueva}").category == decidir(historial, consulta).category


@MUCHOS
@given(escenario(), st.data())
def test_anadir_una_palabra_ignorada_nunca_cambia_la_decision(caso, data):
    historial, consulta = caso
    vocabulario = sorted(set().union(*(categorize.words(d) for d, _ in historial), categorize.words(consulta)))
    assume(vocabulario)
    ignoradas = data.draw(st.lists(st.sampled_from(vocabulario), min_size=1, unique=True))
    modelo = categorize.train(historial, set(PERMITIDAS), ignoradas)
    for w in ignoradas:
        assert w not in modelo
        assert categorize.explain(modelo, f"{consulta} {w}") == categorize.explain(modelo, consulta)


@MUCHOS
@given(st.lists(palabra, unique=True, max_size=20), st.sampled_from(PERMITIDAS), st.text(max_size=40))
def test_nunca_decide_con_palabras_vistas_una_sola_vez(vocabulario, categoria, consulta):
    historial = [(w, categoria) for w in vocabulario]  # cada palabra, una sola vez
    assert decidir(historial, consulta + " " + " ".join(vocabulario)).category is None


@MUCHOS
@given(st.text(max_size=60))
def test_con_el_historial_vacio_nunca_decide(consulta):
    assert decidir([], consulta).category is None


@MUCHOS
@given(st.text(max_size=80))
def test_cualquier_texto_raro_se_procesa_sin_errores(texto):
    """Emojis, saltos de línea, caracteres de otros alfabetos...: nunca debe lanzar una excepción."""
    assert categorize.words(texto) <= {w for w in categorize.words(texto) if w.isascii() and w.isupper()}
    decidir([("ALDI", "Comida")] * 2, texto)
