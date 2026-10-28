"""Valida categorias.yml (el fichero real) y comprueba que rules.py rechaza los errores habituales al editarlo."""
from datetime import date
from decimal import Decimal

import pytest

import fetch
import rules

PERMITIDAS = "categorias_permitidas: [A, Comida, Rota]\n"
VALIDO = PERMITIDAS + """
traspasos:
  patrones: ['Move to save']
categorias:
  - categoria: Comida
    patron: 'ALDI'
"""


def cargar(tmp_path, texto):
    f = tmp_path / "categorias.yml"
    f.write_text(texto, encoding="utf-8")
    return rules.load_rules(f)


# --- el fichero real -----------------------------------------------------------------------------

def test_el_fichero_real_es_valido_y_tiene_reglas():
    r = rules.load_rules()
    assert r.categories
    assert r.transfer_re.pattern


def test_cada_regla_tiene_al_menos_un_ejemplo():
    """Sin ejemplos no se puede comprobar que la regla funciona ni que otra anterior no la tapa."""
    r = rules.load_rules()
    con_ejemplo = {categoria for _, categoria in r.examples}
    sin_ejemplo = [rx.pattern for rx, categoria in r.categories if categoria not in con_ejemplo]
    assert not sin_ejemplo, f"reglas sin ejemplos: {sin_ejemplo}"


def test_los_ejemplos_acaban_en_la_categoria_de_su_regla():
    """Si falla, otra regla anterior (el orden importa) se queda con el ejemplo."""
    errores = [
        f"{ejemplo!r}: esperada {esperada!r}, sale {fetch.categorize(ejemplo, Decimal(-1))!r}"
        for ejemplo, esperada in rules.load_rules().examples
        if fetch.categorize(ejemplo, Decimal(-1)) != esperada
    ]
    assert not errores, "\n".join(errores)


def test_no_hay_reglas_ni_patrones_de_traspaso_duplicados():
    r = rules.load_rules()
    reglas = [(rx.pattern, categoria) for rx, categoria in r.categories]
    assert len(reglas) == len(set(reglas)), "hay una regla repetida"


def test_el_importe_exacto_de_los_traspasos_se_lee_como_decimal_absoluto():
    assert (".", Decimal(400)) in rules.load_rules().transfer_exact


# --- validación de ficheros mal escritos -----------------------------------------------------------

def test_un_fichero_minimo_valido_carga(tmp_path):
    r = cargar(tmp_path, VALIDO)
    assert [c for _, c in r.categories] == ["Comida"]
    assert r.transfer_exact == ()
    assert r.transfer_re.search("MOVE TO SAVE")


@pytest.mark.parametrize("texto, mensaje", [
    ("categorias: [{categoria: A, patron: x}]", "falta traspasos"),
    ("traspasos: {patrones: [x]}", "falta categorias"),
    ("traspasos: {patrones: [x]}\ncategorias: []", "al menos un elemento"),
    ("traspasos: {patrones: []}\ncategorias: [{categoria: A, patron: x}]", "al menos un elemento"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: A, patrón: x}]", "falta patron"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: A, patron: x, ejemplo: y}]", "clave desconocida ejemplo"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: A, patron: '('}]", "expresión regular no válida"),
    ("traspasos: {patrones: ['(']}\ncategorias: [{categoria: A, patron: x}]", "expresión regular no válida"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: A, patron: ''}]", "no vacío"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: '', patron: x}]", "categoria debe ser un texto"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: A, patron: x, ejemplos: hola}]", "lista de textos"),
    (("traspasos: {patrones: [x], exactos: [{descripcion: '.', importe: cuatrocientos}]}\n"
      "categorias: [{categoria: A, patron: x}]"), "importe debe ser un número"),
    ("traspasos: {patrones: [x], exactos: [{descripcion: '.'}]}\ncategorias: [{categoria: A, patron: x}]",
     "falta importe"),
])
def test_rechaza_ficheros_mal_escritos(tmp_path, texto, mensaje):
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, PERMITIDAS + texto)


@pytest.mark.parametrize("texto, mensaje", [
    ("- esto no es un diccionario", "debe ser un diccionario"),
    ("traspasos: [sin cerrar", "no es un YAML válido"),
    ("traspasos: {patrones: [x]}\ncategorias: [{categoria: A, patron: x}]", "falta categorias_permitidas"),
])
def test_rechaza_la_estructura_general_mal_escrita(tmp_path, texto, mensaje):
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, texto)


# --- categorias_permitidas -------------------------------------------------------------------------

def test_una_categoria_mal_escrita_se_rechaza_y_sugiere_la_correcta(tmp_path):
    texto = VALIDO.replace("categoria: Comida", "categoria: Comdia")
    with pytest.raises(rules.ReglasInvalidas, match=r"'Comdia' no está en categorias_permitidas.*'Comida'"):
        cargar(tmp_path, texto)


def test_las_mayusculas_cuentan_como_categoria_distinta(tmp_path):
    with pytest.raises(rules.ReglasInvalidas, match="'comida' no está.*'Comida'"):
        cargar(tmp_path, VALIDO.replace("categoria: Comida", "categoria: comida"))


def test_una_categoria_nueva_sin_declarar_pide_anadirla_a_la_lista(tmp_path):
    with pytest.raises(rules.ReglasInvalidas, match="Si es nueva, añádela a categorias_permitidas"):
        cargar(tmp_path, VALIDO.replace("categoria: Comida", "categoria: Zzzzzz"))


@pytest.mark.parametrize("permitidas, mensaje", [
    ("[]", "al menos un elemento"),
    ("[Comida, Comida]", "repetidas Comida"),
    ("[Comida, '']", "textos no vacíos"),
    ("[Comida, 5]", "textos no vacíos"),
])
def test_rechaza_listas_de_categorias_permitidas_mal_escritas(tmp_path, permitidas, mensaje):
    texto = VALIDO.replace("categorias_permitidas: [A, Comida, Rota]", f"categorias_permitidas: {permitidas}")
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, texto)


# --- palabras_ignoradas -----------------------------------------------------------------------------

def test_palabras_ignoradas_es_opcional(tmp_path):
    assert cargar(tmp_path, VALIDO).ignored == ()


def test_el_fichero_real_ignora_las_ciudades_de_las_descripciones_del_banco():
    ignored = {w.upper() for w in rules.load_rules().ignored}
    assert {"MADRID", "AMSTERDAM", "LUXEMBOURG", "HELP"} <= ignored


@pytest.mark.parametrize("valor, mensaje", [
    ("Madrid", "lista de textos"),
    ("[Madrid, '']", "lista de textos"),
    ("[Madrid, 3]", "lista de textos"),
    ("[Madrid, MADRID]", "repetidas"),
])
def test_rechaza_palabras_ignoradas_mal_escritas(tmp_path, valor, mensaje):
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, f"palabras_ignoradas: {valor}\n" + VALIDO)


# --- descripciones ----------------------------------------------------------------------------------

def test_descripciones_es_opcional(tmp_path):
    assert cargar(tmp_path, VALIDO).descriptions == ()


def test_lee_las_descripciones_y_normaliza_los_espacios(tmp_path):
    r = cargar(tmp_path, "descripciones:\n  Rota: comisiones   de la\n    tarjeta\n" + VALIDO)
    assert r.descriptions == (("Rota", "comisiones de la tarjeta"),)


def test_el_fichero_real_describe_todas_las_categorias_de_gastos_para_la_ia():
    """Una categoría nueva sin descripción ni historial hace que la IA responda NO_SE siempre."""
    r = rules.load_rules()
    sin_describir = set(r.allowed) - {"Ingresos"} - dict(r.descriptions).keys()
    assert not sin_describir, f"categorías sin descripción en categorias.yml: {sorted(sin_describir)}"


@pytest.mark.parametrize("valor, mensaje", [
    ("{Rotta: algo}", "'Rotta' no está en categorias_permitidas.*'Rota'"),
    ("{Rota: ''}", "descripciones.Rota: debe ser un texto no vacío"),
    ("{Rota: 5}", "descripciones.Rota: debe ser un texto no vacío"),
    ("[Rota]", "debe ser un diccionario"),
])
def test_rechaza_descripciones_mal_escritas(tmp_path, valor, mensaje):
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, f"descripciones: {valor}\n" + VALIDO)


# --- viajes ------------------------------------------------------------------------------------------

VIAJES = PERMITIDAS.replace("Rota]", "Rota, Viajes]") + VALIDO.removeprefix(PERMITIDAS)


def test_viajes_es_opcional(tmp_path):
    r = cargar(tmp_path, VALIDO)
    assert (r.trips, r.trip_keep) == ((), ())


def test_lee_los_viajes_ordenados_por_fecha(tmp_path):
    texto = VIAJES + """
viajes:
  no_cambian: [A]
  fechas:
    - {nombre: Roma, desde: 2026-12-01, hasta: 2026-12-03}
    - {nombre: Ámsterdam, desde: 2026-10-09, hasta: 2026-10-12}
"""
    r = cargar(tmp_path, texto)
    assert r.trips == (("Ámsterdam", date(2026, 10, 9), date(2026, 10, 12)), ("Roma", date(2026, 12, 1), date(2026, 12, 3)))
    assert r.trip_keep == ("A",)


def test_el_fichero_real_tiene_un_viaje_de_ejemplo():
    r = rules.load_rules()
    assert ("Escapada de ejemplo", date(2026, 1, 9), date(2026, 1, 12)) in r.trips
    assert "Alquiler" in r.trip_keep


@pytest.mark.parametrize("fechas, mensaje", [
    ("[{nombre: X, desde: '2026-10-09', hasta: 2026-10-12}]", "desde debe ser una fecha"),
    ("[{nombre: X, desde: 2026-10-12, hasta: 2026-10-09}]", "posterior"),
    ("[{nombre: X, desde: 2026-10-09}]", "falta hasta"),
    ("[{nombre: '', desde: 2026-10-09, hasta: 2026-10-09}]", "nombre debe ser un texto"),
    ("[{nombre: X, desde: 2026-10-01, hasta: 2026-10-05}, {nombre: X, desde: 2026-11-01, hasta: 2026-11-02}]",
     "nombres repetidos X"),
    ("[{nombre: X, desde: 2026-10-01, hasta: 2026-10-05}, {nombre: Y, desde: 2026-10-05, hasta: 2026-10-07}]",
     "X y Y se solapan"),
    ("[]", "al menos un elemento"),
])
def test_rechaza_fechas_de_viajes_mal_escritas(tmp_path, fechas, mensaje):
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, VIAJES + f"viajes:\n  fechas: {fechas}\n")


@pytest.mark.parametrize("no_cambian, mensaje", [
    ("[Comdia]", "'Comdia' no es una categoría.*'Comida'"),
    ("[Viajes]", "'Viajes' no es una categoría permitida distinta de Viajes"),
    ("Comida", "debe ser una lista"),
])
def test_rechaza_no_cambian_mal_escrito(tmp_path, no_cambian, mensaje):
    texto = VIAJES + f"viajes:\n  no_cambian: {no_cambian}\n  fechas: [{{nombre: X, desde: 2026-10-01, hasta: 2026-10-02}}]\n"
    with pytest.raises(rules.ReglasInvalidas, match=mensaje):
        cargar(tmp_path, texto)


def test_viajes_exige_que_exista_la_categoria_viajes(tmp_path):
    with pytest.raises(rules.ReglasInvalidas, match="'Viajes' debe estar en categorias_permitidas"):
        cargar(tmp_path, VALIDO + "viajes:\n  fechas: [{nombre: X, desde: 2026-10-01, hasta: 2026-10-02}]\n")


def test_el_fichero_real_permite_ingresos_porque_el_codigo_lo_asigna():
    assert "Ingresos" in rules.load_rules().allowed


def test_todas_las_reglas_del_fichero_real_usan_categorias_permitidas():
    r = rules.load_rules()
    assert {categoria for _, categoria in r.categories} <= set(r.allowed)


def test_el_mensaje_de_error_indica_que_regla_falla(tmp_path):
    texto = VALIDO + "  - categoria: Rota\n    patron: '(sin cerrar'\n"
    with pytest.raises(rules.ReglasInvalidas, match=r"categorias\[2\] \(Rota\)"):
        cargar(tmp_path, texto)
