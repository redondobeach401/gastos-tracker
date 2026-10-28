"""Tests de las funciones puras de fetch.py. Ejecutar:  uv run --with pytest --with-requirements requirements.txt pytest"""
from decimal import Decimal
from types import MappingProxyType

import pytest

import fetch


@pytest.mark.parametrize("desc, amount, expected", [
    ("Ahorro para Hucha principal", Decimal(-50), True),
    ("Move to save", Decimal(-10), True),
    ("PARA INTERESES TRF.PERIODICA", Decimal(-5), True),
    ("NOMBRE APELLIDO - REVOLUT Payment from Nombre Apellido", Decimal(200), True),
    ("Payment from Pepe Garcia", Decimal(20), False),
    (".", Decimal(-400), True),
    (".", Decimal(400), True),
    (".", Decimal(399), False),
    ("ALDI SUPERMERCADOS", Decimal(-20), False),
])
def test_is_transfer(desc, amount, expected):
    assert fetch.is_transfer(desc, amount) is expected


@pytest.mark.parametrize("desc, amount, expected", [
    ("Ahorro para Hucha principal", Decimal(-50), None),        # traspaso: sin categoría
    ("NOMINA EMPRESA", Decimal(1500), "Ingresos"),               # importe positivo
    ("PAGO BIZUM A JUAN", Decimal(-10), "Bizum"),
    ("calistenia PAGO BIZUM", Decimal(-10), "Entrenamiento"),    # la primera regla gana
    ("METRO DE MADRID", Decimal("-1.5"), "Transporte"),
    ("OVPAY AMSTERDAM", Decimal(-3), "Viajes"),
    ("ENERO", Decimal(-800), "Alquiler"),
    ("BARCELONA TIENDA", Decimal(-12), None),                    # \bBAR\b no casa con BARCELONA
    ("COMERCIO DESCONOCIDO", Decimal(-12), None),                # sin regla
])
def test_categorize(desc, amount, expected):
    assert fetch.categorize(desc, amount) == expected


def test_sesion_reintenta_429_y_5xx_con_espera():
    retry = fetch.SESSION.get_adapter(fetch.API).max_retries
    assert retry.total == 3
    assert {429, 500, 503} <= set(retry.status_forcelist)
    assert retry.backoff_factor > 0


def test_alert_se_guarda_y_write_alerts_crea_el_markdown(tmp_path, monkeypatch, capsys):
    out = tmp_path / "alerts.md"
    monkeypatch.setattr(fetch, "ALERTS", [])
    monkeypatch.setattr(fetch, "ALERTS_FILE", str(out))
    fetch.alert("consentimiento caduca")
    fetch.write_alerts()
    assert "::warning::consentimiento caduca" in capsys.readouterr().out
    assert out.read_text(encoding="utf-8") == "## Avisos de enable-banking\n\n- consentimiento caduca\n"


def test_write_alerts_no_crea_fichero_si_no_hay_avisos(tmp_path, monkeypatch):
    out = tmp_path / "alerts.md"
    monkeypatch.setattr(fetch, "ALERTS", [])
    monkeypatch.setattr(fetch, "ALERTS_FILE", str(out))
    fetch.write_alerts()
    assert not out.exists()


def test_write_alerts_crea_el_resumen_en_su_propio_fichero(tmp_path, monkeypatch):
    out = tmp_path / "resumen.md"
    monkeypatch.setattr(fetch, "ALERTS", [])
    monkeypatch.setattr(fetch, "ALERTS_FILE", str(tmp_path / "alerts.md"))
    monkeypatch.setattr(fetch, "RESUMEN", ["12 gastos sin categoría"])
    monkeypatch.setattr(fetch, "RESUMEN_FILE", str(out))
    fetch.write_alerts()
    assert out.read_text(encoding="utf-8") == "## Gastos sin categoría\n\n12 gastos sin categoría\n"
    assert not (tmp_path / "alerts.md").exists()


def tx(**kwargs):
    base = {
        "transaction_amount": {"amount": "12.50"},
        "credit_debit_indicator": "DBIT",
        "booking_date": "2026-10-02",
        "creditor": {"name": "ALDI"},
        "debtor": {"name": "EMPRESA"},
        "remittance_information": ["COMPRA"],
        "entry_reference": "ref-1",
    }
    return {**base, **kwargs}


def test_normalize_gasto_usa_acreedor_y_signo_negativo():
    d, amount, desc, ext, _ = fetch.normalize(tx(), {})
    assert (d, amount, desc, ext) == ("2026-10-02", Decimal("-12.50"), "ALDI - COMPRA", "ref-1")


def test_normalize_ingreso_usa_deudor_y_signo_positivo():
    _, amount, desc, _, _ = fetch.normalize(tx(credit_debit_indicator="CRDT"), {})
    assert amount == Decimal("12.50")
    assert desc == "EMPRESA - COMPRA"


def test_normalize_prefiere_value_date_a_booking_date():
    # Abanca: compra del 17 anotada con booking_date del 19
    d = fetch.normalize(tx(booking_date="2026-10-19", value_date="2026-10-17"), {})[0]
    assert d == "2026-10-17"


def test_normalize_sin_value_date_usa_booking_date():
    assert fetch.normalize(tx(value_date=None), {})[0] == "2026-10-02"


def test_normalize_sin_id_genera_hash_con_contador_para_duplicados():
    seen = {}
    sin_id = tx(entry_reference=None)
    ext1 = fetch.normalize(sin_id, seen)[3]
    ext2 = fetch.normalize(sin_id, seen)[3]
    assert ext1.endswith("#1")
    assert ext2.endswith("#2")
    assert ext1.split("#")[0] == ext2.split("#")[0]


def test_raw_for_storage_quita_el_saldo_sin_mutar_el_original():
    original = {"transaction_amount": {"amount": "1"}, "balance_after_transaction": {"amount": "999"}}
    stored = fetch.raw_for_storage(original)
    assert "balance_after_transaction" not in stored
    assert stored["transaction_amount"] == {"amount": "1"}
    assert "balance_after_transaction" in original


def test_print_resumen_suma_las_cuentas_y_alimenta_la_tarjeta(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(fetch, "ALERTS", ["caduca pronto"])
    fetch.print_resumen([("abanca-1", 10, 2, 1, 0), ("abanca-2", 5, 1, 0, 3)], 1, 2, 3, 4, 8.44)
    assert capsys.readouterr().out == (
        "Resumen: 2 cuentas, 15 leídos, 3 nuevos, 1 con fecha corregida, 3 recategorizados, "
        "1 traspasos por pareja, 2 por historial, 3 por IA, 4 sin categoría · 8.4 s\n"
    )
    text = summary.read_text(encoding="utf-8")
    assert "| abanca-2 | 5 | 1 | 0 | 3 |" in text and "⚠️ caduca pronto" in text
    assert "Uso de la IA" not in text
    fetch.print_resumen([], 0, 0, 0, 0, 1, {"tokens_in": 100, "tokens_out": 20, "segundos": 1.5})
    assert "Uso de la IA: 100 tokens de entrada, 20 de salida, 1.5 s" in summary.read_text(encoding="utf-8")


def test_error_del_banco_recorta_el_cuerpo_y_lista_las_cuentas_ya_procesadas(monkeypatch):
    class Resp:
        ok = False
        status_code = 500
        text = "x" * 5000

    monkeypatch.setattr(fetch.SESSION, "get", lambda *a, **kw: Resp())
    monkeypatch.setattr(fetch, "headers", dict)
    monkeypatch.setattr(fetch, "PROCESADAS", ["abanca-1"])
    with pytest.raises(fetch.ErrorBanco) as e:
        fetch.fetch_transactions("u1", fetch.CUTOFF)
    msg = str(e.value)
    assert len(msg) < 600 and "Cuentas ya procesadas: abanca-1." in msg


def test_error_del_banco_incluye_estado_endpoint_e_id_de_la_peticion(monkeypatch):
    class Resp:
        ok = False
        status_code = 502
        text = '{"error": "ASPSP_ERROR"}'
        headers = MappingProxyType({"X-Request-Id": "abc-123", "Content-Type": "application/json"})

    monkeypatch.setattr(fetch.SESSION, "get", lambda *a, **kw: Resp())
    monkeypatch.setattr(fetch, "headers", dict)
    with pytest.raises(fetch.ErrorBanco) as e:
        fetch.fetch_transactions("u1", fetch.CUTOFF)
    msg = str(e.value)
    assert "Error 502 en GET /accounts/{uid}/transactions" in msg and "ASPSP_ERROR" in msg
    assert "X-Request-Id=abc-123" in msg and "Content-Type" not in msg


def respuesta_saldos(monkeypatch, balances, ok=True, status=200):
    class Resp:
        def __init__(self):
            self.ok, self.status_code = ok, status

        def json(self):
            return {"balances": balances}

    monkeypatch.setattr(fetch.SESSION, "get", lambda *a, **kw: Resp())
    monkeypatch.setattr(fetch, "headers", dict)


def test_fetch_balance_usa_el_saldo_contabilizado_en_euros(monkeypatch):
    respuesta_saldos(monkeypatch, [
        {"balance_type": "ITAV", "balance_amount": {"amount": "999", "currency": "EUR"}},
        {"balance_type": "CLBD", "balance_amount": {"amount": "123.45", "currency": "EUR"}},
    ])
    assert fetch.fetch_balance("u1") == Decimal("123.45")


@pytest.mark.parametrize("balances", [
    [],
    [{"balance_type": "ITAV", "balance_amount": {"amount": "1", "currency": "EUR"}}],
    [{"balance_type": "CLBD", "balance_amount": {"amount": "1", "currency": "USD"}}],
    [{"balance_type": "CLBD", "balance_amount": {"amount": "no-es-un-numero", "currency": "EUR"}}],
])
def test_fetch_balance_devuelve_none_si_no_hay_saldo_utilizable(monkeypatch, balances):
    respuesta_saldos(monkeypatch, balances)
    assert fetch.fetch_balance("u1") is None


def test_fetch_balance_no_rompe_el_fetch_si_el_banco_falla(monkeypatch):
    respuesta_saldos(monkeypatch, [], ok=False, status=500)
    assert fetch.fetch_balance("u1") is None

    def falla(*a, **kw):
        raise fetch.requests.ConnectionError("sin red")

    monkeypatch.setattr(fetch.SESSION, "get", falla)
    assert fetch.fetch_balance("u1") is None


def test_corta_si_la_api_repite_la_continuation_key(monkeypatch):
    llamadas = []

    class Resp:
        ok = True

        def json(self):
            return {"transactions": [], "continuation_key": "siempre-la-misma"}

    monkeypatch.setattr(fetch.SESSION, "get", lambda *a, **kw: llamadas.append(1) or Resp())
    monkeypatch.setattr(fetch, "headers", dict)
    monkeypatch.setattr(fetch, "MAX_PAGINAS", 3)
    with pytest.raises(fetch.ErrorBanco, match="tras 3"):
        fetch.fetch_transactions("u1", fetch.CUTOFF)
    assert len(llamadas) == 3


def test_write_errores_deja_cada_cuenta_con_la_primera_linea_del_error(monkeypatch, tmp_path):
    destino = tmp_path / "errores.md"
    monkeypatch.setattr(fetch, "ERRORES_FILE", str(destino))
    fetch.write_errores([("abanca-1", "Error 401 en GET /accounts/{uid}/transactions: caducado\nPetición: x"),
                         ("revolut", "Error 502 en GET: ASPSP_ERROR")])
    texto = destino.read_text(encoding="utf-8")
    assert "- `abanca-1`: Error 401 en GET /accounts/{uid}/transactions: caducado\n" in texto
    assert "- `revolut`: Error 502 en GET: ASPSP_ERROR\n" in texto
    assert "Petición" not in texto


def test_write_errores_sin_fichero_configurado_no_hace_nada(monkeypatch):
    monkeypatch.setattr(fetch, "ERRORES_FILE", None)
    fetch.write_errores([("abanca-1", "Error 401")])
