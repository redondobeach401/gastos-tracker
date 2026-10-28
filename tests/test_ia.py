"""Tests de ia.py con la API de Anthropic simulada y sin base de datos."""
import pytest
import requests

import ia

PENDING = [(1, "abanca-1", "2026-10-20", -12.5, "CABIFY MADRID"), (2, "abanca-1", "2026-10-21", -3, "XYZ 123")]
ALLOWED = ["Comida", "Ocio", "Ingresos"]


class FakeResponse:
    def __init__(self, resultados):
        self._resultados = resultados

    def raise_for_status(self):
        pass

    def json(self):
        return {"content": [{"type": "tool_use", "input": {"resultados": self._resultados}}]}


class FakeCursor:
    def __init__(self):
        self.updates = []
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchall(self):
        return PENDING if "source = 'enablebanking'" in self.queries[-1] else [("Ocio", "UBER TRIP")]

    def executemany(self, sql, rows):
        self.updates = list(rows)


def respond(monkeypatch, resultados):
    monkeypatch.setattr(ia.SESSION, "post", lambda *a, **kw: FakeResponse(resultados))


def test_devuelve_solo_categorias_validas_de_ids_conocidos(monkeypatch):
    respond(monkeypatch, [
        {"id": 1, "categoria": "Ocio"},
        {"id": 2, "categoria": ia.NO_SE},
        {"id": 99, "categoria": "Comida"},       # id que no se pidió
    ])
    assert ia.suggest(PENDING, ALLOWED, [], "k")[0] == {1: "Ocio"}


def test_el_prompt_incluye_que_significa_cada_categoria():
    prompt = ia.build_prompt(PENDING, [("Ocio", "UBER TRIP")], [("Tarjeta", "comisiones de la tarjeta")])
    assert "- Tarjeta: comisiones de la tarjeta" in prompt
    assert prompt.index("- Tarjeta:") < prompt.index("Clasifica estos gastos")


def test_el_prompt_no_tiene_seccion_de_significados_sin_descripciones():
    assert "Qué significa" not in ia.build_prompt(PENDING, [("Ocio", "UBER TRIP")])


def test_las_descripciones_llegan_al_prompt_enviado_a_la_api(monkeypatch):
    enviados = []

    def post(*a, **kw):
        enviados.append(kw["json"]["messages"][0]["content"])
        return FakeResponse([])

    monkeypatch.setattr(ia.SESSION, "post", post)
    ia.suggest(PENDING, ALLOWED, [], "k", descripciones=[("Ocio", "ratos libres")])
    assert "- Ocio: ratos libres" in enviados[0]


def test_ingresos_no_es_una_categoria_valida_para_gastos(monkeypatch):
    respond(monkeypatch, [{"id": 1, "categoria": "Ingresos"}])
    assert ia.suggest(PENDING, ALLOWED, [], "k")[0] == {}


def test_escribe_con_origen_ia_y_solo_si_sigue_sin_categoria(monkeypatch):
    respond(monkeypatch, [{"id": 1, "categoria": "Ocio"}])
    cur = FakeCursor()
    done, _ = ia.categorize_pending_ia(cur, ALLOWED, api_key="k")
    assert done == [(1, "abanca-1", "2026-10-20", -12.5, "CABIFY MADRID", "Ocio")]
    assert cur.updates == [("Ocio", 1)]


def test_dry_run_no_escribe(monkeypatch):
    respond(monkeypatch, [{"id": 1, "categoria": "Ocio"}])
    cur = FakeCursor()
    assert len(ia.categorize_pending_ia(cur, ALLOWED, dry_run=True, api_key="k")[0]) == 1
    assert cur.updates == []


def test_sin_clave_no_hace_nada_ni_consulta(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cur = FakeCursor()
    assert ia.categorize_pending_ia(cur, ALLOWED) == ([], [])
    assert cur.queries == []


@pytest.mark.parametrize("error", [requests.ConnectionError("caído"), ValueError("no es json")])
def test_si_la_api_falla_no_rompe_el_fetch(monkeypatch, capsys, error):
    def boom(*a, **kw):
        raise error
    monkeypatch.setattr(ia.SESSION, "post", boom)
    assert ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k") == ([], [])
    out = capsys.readouterr().out
    assert "la llamada a la API ha fallado y 2 gasto(s) siguen sin categoría" in out


def test_el_log_dice_que_llama_a_la_api_y_con_que_gastos(monkeypatch, capsys):
    respond(monkeypatch, [{"id": 1, "categoria": "Ocio"}, {"id": 2, "categoria": "Comida"}])
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    out = capsys.readouterr().out
    assert f"IA: llamando a {ia.MODEL} con 2 gasto(s) pendientes: 'CABIFY MADRID', 'XYZ 123'" in out


def test_el_log_dice_que_no_hay_pendientes_y_no_llama_a_la_api(monkeypatch, capsys):
    class SinPendientes(FakeCursor):
        def fetchall(self):
            return []

    def no_debe_llamar(*a, **kw):
        raise AssertionError("no debería llamar a la API")
    monkeypatch.setattr(ia.SESSION, "post", no_debe_llamar)
    assert ia.categorize_pending_ia(SinPendientes(), ALLOWED, api_key="k") == ([], [])
    assert "IA: sin gastos pendientes de categorizar, no se llama a la API" in capsys.readouterr().out


def test_el_log_lista_los_gastos_que_la_ia_no_ha_categorizado(monkeypatch, capsys):
    respond(monkeypatch, [{"id": 1, "categoria": "Ocio"}, {"id": 2, "categoria": ia.NO_SE}])
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    out = capsys.readouterr().out
    assert "IA: sin categoría para 'XYZ 123' (-3 €): NO_SE, categoría inválida o sin respuesta" in out
    assert "sin categoría para 'CABIFY MADRID'" not in out


def test_recomienda_categoria_nueva_cuando_no_sabe_y_no_existe(monkeypatch):
    respond(monkeypatch, [
        {"id": 1, "categoria": ia.NO_SE, "categoria_nueva": "  Salud  "},
        {"id": 2, "categoria": ia.NO_SE, "categoria_nueva": "ocio"},      # ya existe (sin mirar mayúsculas)
    ])
    _, nuevas = ia.suggest(PENDING, ALLOWED, [], "k")
    assert nuevas == {1: "Salud"}


def test_no_recomienda_nada_para_lo_que_si_categoriza(monkeypatch):
    respond(monkeypatch, [{"id": 1, "categoria": "Ocio", "categoria_nueva": "Salud"}])
    assert ia.suggest(PENDING, ALLOWED, [], "k")[1] == {}


def test_recortar_el_nombre_propuesto(monkeypatch):
    respond(monkeypatch, [{"id": 1, "categoria": ia.NO_SE, "categoria_nueva": "X" * 200}])
    assert len(ia.suggest(PENDING, ALLOWED, [], "k")[1][1]) == ia.MAX_NUEVA


def test_categorize_devuelve_los_sin_categoria_con_su_propuesta(monkeypatch):
    respond(monkeypatch, [{"id": 1, "categoria": "Ocio"}, {"id": 2, "categoria": ia.NO_SE, "categoria_nueva": "Salud"}])
    _, sin_categoria = ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    assert sin_categoria == [("XYZ 123", "Salud")]


def test_resumen_agrupa_por_categoria_y_ordena_por_frecuencia():
    lines = ia.resumen_nuevas([("FCIA A", "Salud"), ("GYM", "Deporte"), ("FCIA B", "Salud")])
    assert len(lines) == 2
    assert lines[0].startswith("La IA no ha podido categorizar 2 gasto(s); recomienda crear la categoría 'Salud'")
    assert "'FCIA A', 'FCIA B'" in lines[0]
    assert ia.resumen_nuevas([]) == []


def test_resumen_cuenta_cada_tipo_de_descarte(monkeypatch, capsys):
    respond(monkeypatch, [{"id": 1, "categoria": "Inventada"}])
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    out = capsys.readouterr().out
    assert "0 categorizados, 0 NO_SE, 1 descartados por categoría inválida, 1 sin respuesta" in out


def test_error_http_muestra_estado_y_mensaje_de_la_api(monkeypatch, capsys):
    class Resp:
        status_code = 429

        def json(self):
            return {"error": {"type": "rate_limit_error", "message": "slow down"}}

    def boom(*a, **kw):
        raise requests.HTTPError("429", response=Resp())
    monkeypatch.setattr(ia.SESSION, "post", boom)
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    assert "HTTP 429 rate_limit_error: slow down" in capsys.readouterr().out


def test_sin_clave_avisa(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ia.categorize_pending_ia(FakeCursor(), ALLOWED)
    assert "falta ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_avisa_si_la_ia_no_clasifica_la_mayoria(monkeypatch, capsys):
    monkeypatch.setattr(ia, "MIN_PARA_ALERTA", 2)
    respond(monkeypatch, [{"id": 1, "categoria": ia.NO_SE}, {"id": 2, "categoria": ia.NO_SE}])
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    assert "::warning::La IA solo ha categorizado 0 de 2" in capsys.readouterr().out


def test_no_avisa_con_pocos_pendientes(monkeypatch, capsys):
    respond(monkeypatch, [{"id": 1, "categoria": ia.NO_SE}])
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k")
    assert "La IA solo ha categorizado" not in capsys.readouterr().out


def test_el_aviso_de_calidad_va_a_la_funcion_alerta_y_el_uso_se_devuelve(monkeypatch):
    monkeypatch.setattr(ia, "MIN_PARA_ALERTA", 2)
    respond(monkeypatch, [{"id": 1, "categoria": ia.NO_SE}, {"id": 2, "categoria": ia.NO_SE}])
    avisos, uso = [], {}
    ia.categorize_pending_ia(FakeCursor(), ALLOWED, api_key="k", alerta=avisos.append, uso=uso)
    assert len(avisos) == 1 and "0 de 2" in avisos[0]
    assert uso.keys() == {"tokens_in", "tokens_out", "segundos"}


def test_la_sesion_reintenta_los_errores_temporales_de_la_api():
    retry = ia.SESSION.get_adapter("https://api.anthropic.com").max_retries
    assert retry.total == 3 and "POST" in retry.allowed_methods
    assert {429, 500, 503, 529} <= set(retry.status_forcelist)
    assert retry.raise_on_status is False
