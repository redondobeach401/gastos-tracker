"""Tests de integración: ejecutan el SQL real de fetch.py y backup.py contra un Postgres de usar y tirar.

Necesitan TEST_DATABASE_URL: superusuario de una base vacía y desechable (NUNCA la de producción: cada test
recrea el esquema `public`). Sin ella se saltan. En CI la pone el servicio postgres de ci.yml.
Los tests conectan con los mismos roles que producción (app_fetcher, app_backup) para probar también RLS y permisos.
"""
import csv
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import backup
import fetch
from scripts import corregir, revisar_ia

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="falta TEST_DATABASE_URL")

SCHEMA = (Path(__file__).resolve().parent.parent / "schema.sql").read_text(encoding="utf-8")
ROLES = ("looker", "app_fetcher", "app_backup")
FAR_FUTURE = "2099-01-01T00:00:00+00:00"


def role_url(role):
    return make_conninfo(**{**conninfo_to_dict(ADMIN_URL), "user": role, "password": "test"})


@pytest.fixture
def db():
    assert "supabase" not in ADMIN_URL, "TEST_DATABASE_URL apunta a Supabase: usa una base desechable"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute("drop schema public cascade")
        admin.execute("create schema public")
        admin.execute("grant usage on schema public to public")
        for role in ROLES:
            admin.execute(
                f"do $$ begin if not exists (select from pg_roles where rolname = '{role}') then "
                f"create role {role} login password 'test'; end if; end $$"
            )
        admin.execute(SCHEMA)
        yield admin


def tx(ref, amount, party, kind="DBIT", day="2026-10-02"):
    return {
        "transaction_amount": {"amount": str(amount)},
        "credit_debit_indicator": kind,
        "booking_date": day,
        "creditor" if kind == "DBIT" else "debtor": {"name": party},
        "entry_reference": ref,
        "balance_after_transaction": {"amount": "999"},
    }


def run_fetch(monkeypatch, txs_by_uid, valid_until=FAR_FUTURE, otros_bancos=None, saldos=None):
    """Ejecuta fetch.main() con la API del banco simulada y la base de datos de pruebas como app_fetcher.
    `otros_bancos`: {etiqueta: {uid: movimientos}} para simular más de un banco (cuentas con ese nombre).
    `saldos`: {uid: saldo del banco}; sin él, el banco no da saldo y no se comprueba."""
    bancos = {"banco": txs_by_uid, **(otros_bancos or {})}
    sessions = {label: {"valid_until": valid_until, "accounts": [{"uid": uid} for uid in txs]}
                for label, txs in bancos.items()}
    todos = {uid: txs for por_banco in bancos.values() for uid, txs in por_banco.items()}
    monkeypatch.setattr(fetch, "SESSIONS", sessions)
    monkeypatch.setattr(fetch, "fetch_transactions", lambda uid, date_from: todos[uid])
    monkeypatch.setattr(fetch, "fetch_balance", lambda uid: (saldos or {}).get(uid))
    monkeypatch.setattr(fetch, "connect", lambda: psycopg.connect(role_url("app_fetcher")))
    monkeypatch.setattr(fetch, "ALERTS", [])
    monkeypatch.setattr(fetch, "RESUMEN", [])
    fetch.main()


def test_schema_se_aplica_dos_veces_sin_error(db):
    db.execute(SCHEMA)


def test_inserta_movimientos_con_categoria_y_sin_saldo(db, monkeypatch):
    run_fetch(monkeypatch, {"u1": [
        tx("r1", "12.50", "ALDI"),
        tx("r2", "1500", "EMPRESA", kind="CRDT"),
        tx("r3", "3", "COMERCIO RARO"),
    ]})
    rows = db.execute(
        "select external_id, amount, category, is_transfer, source, raw ? 'balance_after_transaction' "
        "from transactions order by external_id"
    ).fetchall()
    assert rows == [
        ("r1", Decimal("-12.50"), "Comida", False, "enablebanking", False),
        ("r2", Decimal(1500), "Ingresos", False, "enablebanking", False),
        ("r3", Decimal(-3), None, False, "enablebanking", False),
    ]


def test_relanzar_no_duplica_movimientos(db, monkeypatch):
    txs = {"u1": [tx("r1", "12.50", "ALDI"), tx("r2", "4", "ALDI")]}
    run_fetch(monkeypatch, txs)
    run_fetch(monkeypatch, txs)
    assert db.execute("select count(*) from transactions").fetchone()[0] == 2


def test_corrige_la_fecha_de_un_movimiento_ya_guardado_sin_tocar_su_categoria(db, monkeypatch, capsys):
    db.execute(
        "insert into transactions (account, booking_date, amount, description, source, external_id, category, "
        "category_source) values ('banco', '2026-10-19', -12.50, 'ALDI', 'enablebanking', 'r1', 'Manual', 'manual')"
    )
    run_fetch(monkeypatch, {"u1": [{**tx("r1", "12.50", "ALDI", day="2026-10-19"), "value_date": "2026-10-17"}]})
    assert db.execute("select booking_date::text, category from transactions").fetchall() == [("2026-10-17", "Manual")]
    log = capsys.readouterr().out
    assert "0 nuevos, 1 con fecha corregida, 0 recategorizados" in log


def test_recategoriza_solo_los_que_no_tienen_categoria(db, monkeypatch):
    for ref, category in (("r1", None), ("r2", "Manual")):
        db.execute(
            "insert into transactions (account, booking_date, amount, description, source, external_id, category) "
            "values ('banco', '2026-10-02', -12.50, 'ALDI', 'enablebanking', %s, %s)",
            (ref, category),
        )
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI"), tx("r2", "12.50", "ALDI")]})
    cats = dict(db.execute("select external_id, category from transactions").fetchall())
    assert cats == {"r1": "Comida", "r2": "Manual"}


def test_marca_traspaso_por_pareja_en_otra_cuenta(db, monkeypatch):
    run_fetch(monkeypatch, {
        "u1": [tx("a1", "50", "MOVIMIENTO RARO")],
        "u2": [tx("b1", "50", "MOVIMIENTO RARO", kind="CRDT")],
        "u3": [tx("c1", "7", "ALDI")],
    })
    flags = dict(db.execute("select external_id, is_transfer from transactions").fetchall())
    assert flags == {"a1": True, "b1": True, "c1": False}


@pytest.mark.parametrize("cuenta, dia_otra, esperado", [
    ("revolut", "2026-10-04", True),            # Revolut: hasta 2 días de diferencia
    ("trade-republic", "2026-10-04", True),
    ("revolut", "2026-10-05", False),           # 3 días: ya no
    ("otro", "2026-10-03", False),              # cuentas sin retraso: solo el mismo día
])
def test_traspaso_por_pareja_con_retraso_solo_en_revolut_y_trade_republic(db, monkeypatch, cuenta, dia_otra, esperado):
    run_fetch(monkeypatch, {"u1": [tx("a1", "50", "MOVIMIENTO RARO", day="2026-10-02")]},
              otros_bancos={cuenta: {"u2": [tx("b1", "50", "MOVIMIENTO RARO", kind="CRDT", day=dia_otra)]}})
    flags = dict(db.execute("select external_id, is_transfer from transactions").fetchall())
    assert flags == {"a1": esperado, "b1": esperado}


def test_no_avisa_de_gasto_sin_categoria_en_un_traspaso_por_pareja(db, monkeypatch, capsys):
    run_fetch(monkeypatch, {
        "u1": [tx("a1", "50", "MOVIMIENTO RARO"), tx("a2", "9", "GASTO SUELTO")],
        "u2": [tx("b1", "50", "MOVIMIENTO RARO", kind="CRDT")],
    })
    avisos = [linea for linea in capsys.readouterr().out.splitlines() if "Gasto sin categoría" in linea]
    assert len(avisos) == 1
    assert "GASTO SUELTO" in avisos[0]
    assert "MOVIMIENTO RARO" not in avisos[0]


def historial(db, desc, category, n=2, source=None):
    for _ in range(n):
        db.execute(
            "insert into transactions (booking_date, amount, description, category, category_source) "
            "values ('2026-09-01', -5, %s, %s, %s)",
            (desc, category, source),
        )


def fila(db, ref):
    return db.execute(
        "select category, category_source from transactions where external_id = %s", (ref,)
    ).fetchone()


def test_categoriza_por_historial_lo_que_no_cubren_las_reglas(db, monkeypatch, capsys):
    historial(db, "Fruteria Pepe", "Comida")
    run_fetch(monkeypatch, {"u1": [tx("h1", "8", "FRUTERIA PEPE MADRID")]})
    assert fila(db, "h1") == ("Comida", "historial")
    out = capsys.readouterr().out
    assert "Categorizado por historial como 'Comida'" in out
    assert "Gasto sin categoría" not in out


def test_una_ciudad_de_palabras_ignoradas_no_tuerce_el_historial(db, monkeypatch):
    historial(db, "Fruteria Pepe", "Comida")
    historial(db, "Hotel Amsterdam", "Viajes", n=3)
    run_fetch(monkeypatch, {"u1": [tx("h1", "8", "FRUTERIA PEPE AMSTERDAM"), tx("h2", "8", "TIENDA AMSTERDAM")]})
    assert fila(db, "h1") == ("Comida", "historial")  # sin ignorar AMSTERDAM, sería ambiguo
    assert fila(db, "h2") == (None, None)              # y la ciudad sola no decide "Viajes"


def test_las_reglas_marcan_su_origen(db, monkeypatch):
    run_fetch(monkeypatch, {"u1": [tx("r1", "8", "ALDI")]})
    assert fila(db, "r1") == ("Comida", "regla")


def test_no_aprende_de_lo_que_puso_el_propio_historial(db, monkeypatch):
    historial(db, "Zapateria Lola", "Compras", source="historial")
    run_fetch(monkeypatch, {"u1": [tx("h1", "30", "ZAPATERIA LOLA")]})
    assert fila(db, "h1") == (None, None)


def test_si_el_historial_es_ambiguo_lo_deja_sin_categoria_y_avisa(db, monkeypatch, capsys):
    historial(db, "Mercado", "Comida")
    historial(db, "Mercado", "Compras")
    run_fetch(monkeypatch, {"u1": [tx("h1", "12", "MERCADO CENTRAL")]})
    assert fila(db, "h1") == (None, None)
    assert "Gasto sin categoría" in capsys.readouterr().out


def test_una_regla_corrige_lo_que_puso_el_historial_pero_no_lo_manual(db, monkeypatch):
    for ref, source in (("r1", "historial"), ("r2", "manual")):
        db.execute(
            "insert into transactions (account, booking_date, amount, description, source, external_id, "
            "category, category_source) values ('banco', '2026-10-02', -12.50, 'ALDI', 'enablebanking', %s, "
            "'Compras', %s)",
            (ref, source),
        )
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI"), tx("r2", "12.50", "ALDI")]})
    assert fila(db, "r1") == ("Comida", "regla")
    assert fila(db, "r2") == ("Compras", "manual")


AMSTERDAM = (("Ámsterdam", date(2026, 10, 9), date(2026, 10, 12)),)


def con_viajes(monkeypatch, trips=AMSTERDAM, keep=("Servicios",)):
    monkeypatch.setattr(fetch, "RULES", fetch.RULES._replace(trips=trips, trip_keep=keep))


def guardado(db, ref, day, desc, category=None, source=None):
    db.execute(
        "insert into transactions (account, booking_date, amount, description, source, external_id, category, "
        "category_source) values ('banco', %s, -10, %s, 'enablebanking', %s, %s, %s)",
        (day, desc, ref, category, source),
    )


def test_los_gastos_durante_un_viaje_pasan_a_viajes(db, monkeypatch, capsys):
    con_viajes(monkeypatch)
    guardado(db, "v1", "2026-10-10", "COMERCIO RARO")                           # sin categoría
    guardado(db, "v2", "2026-10-11", "UBER EATS", "Uber Eats", "regla")         # una regla no lo salva
    guardado(db, "v3", "2026-10-12", "TIENDA", "Compras", "ia")
    guardado(db, "k1", "2026-10-10", "APPLE.COM/BILL", "Servicios", "regla")    # no_cambian
    guardado(db, "k2", "2026-10-10", "CENA", "Ocio", "manual")                  # a mano: no se toca
    guardado(db, "k3", "2026-10-13", "ALDI", "Comida", "regla")                 # fuera de fechas
    run_fetch(monkeypatch, {"u1": []})
    filas = dict(db.execute("select external_id, (category, category_source, viaje) from transactions").fetchall())
    assert filas == {
        "v1": ("Viajes", "viaje", "Ámsterdam"),
        "v2": ("Viajes", "viaje", "Ámsterdam"),
        "v3": ("Viajes", "viaje", "Ámsterdam"),
        "k1": ("Servicios", "regla", None),
        "k2": ("Ocio", "manual", None),
        "k3": ("Comida", "regla", None),
    }
    assert capsys.readouterr().out.count("En Viajes por el viaje 'Ámsterdam'") == 3


def test_un_movimiento_nuevo_del_viaje_entra_en_el_mismo_fetch(db, monkeypatch):
    con_viajes(monkeypatch)
    run_fetch(monkeypatch, {"u1": [tx("n1", "8", "ALBERT HEIJN", day="2026-10-11"), tx("n2", "8", "ALDI", day="2026-10-13")]})
    assert db.execute("select viaje from transactions where external_id = 'n1'").fetchone()[0] == "Ámsterdam"
    assert fila(db, "n1") == ("Viajes", "viaje")
    assert fila(db, "n2") == ("Comida", "regla")


def test_relanzar_no_vuelve_a_marcar_lo_ya_marcado(db, monkeypatch, capsys):
    con_viajes(monkeypatch)
    guardado(db, "v1", "2026-10-10", "COMERCIO RARO")
    run_fetch(monkeypatch, {"u1": []})
    capsys.readouterr()
    run_fetch(monkeypatch, {"u1": []})
    assert "En Viajes por el viaje" not in capsys.readouterr().out


def test_si_el_viaje_cambia_de_fechas_lo_que_queda_fuera_vuelve_a_estar_sin_categoria(db, monkeypatch, capsys):
    con_viajes(monkeypatch)
    guardado(db, "v1", "2026-10-09", "COMERCIO RARO")
    guardado(db, "v2", "2026-10-11", "OTRO COMERCIO")
    run_fetch(monkeypatch, {"u1": []})
    con_viajes(monkeypatch, trips=(("Ámsterdam", date(2026, 10, 10), date(2026, 10, 12)),))
    run_fetch(monkeypatch, {"u1": []})
    filas = dict(db.execute("select external_id, (category, category_source, viaje) from transactions").fetchall())
    assert filas == {"v1": (None, None, None), "v2": ("Viajes", "viaje", "Ámsterdam")}
    assert "Ya no está en ningún viaje" in capsys.readouterr().out


def test_el_historial_no_aprende_de_los_viajes(db, monkeypatch):
    historial(db, "Albert Heijn", "Viajes", source="viaje")
    run_fetch(monkeypatch, {"u1": [tx("h1", "8", "ALBERT HEIJN", day="2026-11-02")]})
    assert fila(db, "h1") == (None, None)


def test_vista_de_gasto_por_viaje(db, monkeypatch):
    con_viajes(monkeypatch)
    guardado(db, "v1", "2026-10-09", "COMERCIO RARO")
    guardado(db, "v2", "2026-10-11", "OTRO COMERCIO")
    run_fetch(monkeypatch, {"u1": []})
    with psycopg.connect(role_url("looker")) as conn:
        filas = conn.execute("select viaje, desde::text, hasta::text, gasto, movimientos from v_gasto_por_viaje").fetchall()
    assert filas == [("Ámsterdam", "2026-10-09", "2026-10-11", Decimal(20), 2)]


def test_categorize_en_modo_prueba_no_escribe(db, monkeypatch, capsys):
    import categorize
    import db as db_module

    historial(db, "Fruteria Pepe", "Comida")
    db.execute(
        "insert into transactions (account, booking_date, amount, description, source, external_id) "
        "values ('banco', '2026-10-02', -8, 'FRUTERIA PEPE', 'enablebanking', 'h1')"
    )
    monkeypatch.setattr(db_module, "connect", lambda: psycopg.connect(role_url("app_fetcher")))
    categorize.main(["--dry-run"])
    assert "Se categorizarían 1 gastos" in capsys.readouterr().out
    assert fila(db, "h1") == (None, None)
    categorize.main([])
    assert fila(db, "h1") == ("Comida", "historial")


def test_avisa_si_el_consentimiento_caduca_pronto(db, monkeypatch):
    soon = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    run_fetch(monkeypatch, {"u1": [tx("r1", "1", "ALDI")]}, valid_until=soon)
    assert any("caduca pronto" in a for a in fetch.ALERTS)


def test_avisa_si_una_cuenta_no_tiene_movimientos_nuevos(db, monkeypatch):
    monkeypatch.setattr(fetch, "STALE_DAYS", -1)  # cualquier fecha ya es "antigua"
    run_fetch(monkeypatch, {"u1": [tx("r1", "1", "ALDI")]})
    assert any("sin movimientos nuevos" in a for a in fetch.ALERTS)


def test_sin_avisos_si_todo_esta_al_dia(db, monkeypatch):
    monkeypatch.setattr(fetch, "STALE_DAYS", 100_000)
    run_fetch(monkeypatch, {"u1": [tx("r1", "1", "ALDI")]})
    assert fetch.ALERTS == []


def test_resumen_de_gastos_sin_categoria_cubre_todo_el_historial(db, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "SIN_CATEGORIA_AVISO", 3)
    for i in range(2):  # fuera de la ventana de lectura de hoy: los avisos sueltos no los ven
        db.execute(
            "insert into transactions (account, booking_date, amount, description, source, external_id) "
            "values ('otra', '2026-10-02', -5, 'COSA RARA', 'enablebanking', %s)",
            (f"v{i}",),
        )
    run_fetch(monkeypatch, {"u1": [tx("r1", "3", "COMERCIO RARO")]})
    assert len(fetch.RESUMEN) == 1
    assert "3 gastos sin categoría (el más antiguo, 2026-10-02)" in fetch.RESUMEN[0]
    assert "3 gastos sin categoría" in capsys.readouterr().out


def test_sin_resumen_si_no_se_llega_al_umbral(db, monkeypatch):
    monkeypatch.setattr(fetch, "SIN_CATEGORIA_AVISO", 3)
    run_fetch(monkeypatch, {"u1": [tx("r1", "3", "COMERCIO RARO")]})
    assert fetch.RESUMEN == []


def test_permisos_de_cada_rol(db):
    db.execute("insert into transactions (booking_date, amount) values ('2026-10-01', 1)")
    with psycopg.connect(role_url("app_fetcher")) as conn:
        assert conn.execute("select count(*) from transactions").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from transactions")
    with psycopg.connect(role_url("app_backup")) as conn:
        assert conn.execute("select count(*) from transactions").fetchone()[0] == 1
        conn.execute("select count(*) from saldos_iniciales")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from saldos_iniciales")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into transactions (booking_date, amount) values ('2026-10-01', 1)")


def test_vistas_de_looker(db):
    filas = [  # cuenta, fecha, importe, categoría, traspaso
        ("a1", "2026-09-10", 2000, "Ingresos", False),
        ("a1", "2026-09-15", -30, "Comida", False),
        ("a1", "2026-09-20", -10, "Comida", False),
        ("a1", "2026-09-21", -500, None, True),     # traspaso: ni gasto ni ingreso
        ("a2", "2026-09-21", 500, None, True),
        ("a1", "2026-10-02", -40, None, False),
    ]
    for account, day, amount, category, transfer in filas:
        db.execute(
            "insert into transactions (account, booking_date, amount, category, is_transfer) values (%s, %s, %s, %s, %s)",
            (account, day, amount, category, transfer),
        )
    db.execute("insert into saldos_iniciales values ('a1', 100), ('a2', 10)")  # 'historico' u otras: sin saldo
    db.execute("insert into transactions (account, booking_date, amount, is_transfer) values ('sin_saldo', '2026-09-01', 7, true)")
    with psycopg.connect(role_url("looker")) as conn:
        gasto = conn.execute(
            "select mes::text, account, categoria, gasto, movimientos from v_gasto_mensual_categoria order by 1, 3"
        ).fetchall()
        ahorro = conn.execute("select mes::text, ingresos, gastos, ahorro from v_ahorro_mensual order by 1").fetchall()
        saldo = conn.execute("select account, mes::text, saldo from v_saldo_mensual order by 1, 2").fetchall()
        actual = conn.execute("select account, saldo, ultimo_movimiento::text from v_saldo_actual order by 1").fetchall()
    assert gasto == [
        ("2026-09-01", "a1", "Comida", Decimal(40), 2),
        ("2026-10-01", "a1", "Sin categoría", Decimal(40), 1),
    ]
    assert ahorro == [
        ("2026-09-01", Decimal(2000), Decimal(40), Decimal(1960)),
        ("2026-10-01", Decimal(0), Decimal(40), Decimal(-40)),
    ]
    assert saldo == [
        ("a1", "2026-09-01", Decimal(1560)),
        ("a1", "2026-10-01", Decimal(1520)),
        ("a2", "2026-09-01", Decimal(510)),
    ]
    assert actual == [("a1", Decimal(1520), "2026-10-02"), ("a2", Decimal(510), "2026-09-21")]


def test_un_banco_caido_no_pierde_lo_guardado_de_los_demas_y_hace_fallar_al_final(db, monkeypatch, capsys, tmp_path):
    def api(uid, date_from):
        if uid == "u2":
            raise fetch.ErrorBanco("Error 401 en GET /accounts/{uid}/transactions")
        return todos[uid]

    todos = {"u1": [tx("r1", "12.50", "ALDI")], "u3": [tx("r3", "4", "ALDI")]}
    monkeypatch.setattr(fetch, "SESSIONS", {
        label: {"valid_until": FAR_FUTURE, "accounts": [{"uid": uid}]}
        for label, uid in (("banco1", "u1"), ("banco2", "u2"), ("banco3", "u3"))
    })
    monkeypatch.setattr(fetch, "fetch_transactions", api)
    monkeypatch.setattr(fetch, "fetch_balance", lambda uid: None)
    monkeypatch.setattr(fetch, "connect", lambda: psycopg.connect(role_url("app_fetcher")))
    monkeypatch.setattr(fetch, "ALERTS", [])
    monkeypatch.setattr(fetch, "RESUMEN", [])
    monkeypatch.setattr(fetch, "PROCESADAS", [])
    monkeypatch.setattr(fetch, "ERRORES_FILE", str(tmp_path / "errores.md"))
    with pytest.raises(SystemExit) as e:
        fetch.main()
    assert "- `banco2`: Error 401" in (tmp_path / "errores.md").read_text(encoding="utf-8")
    assert "1 cuenta(s) sin leer: banco2" in str(e.value) and "banco1, banco3" in str(e.value)
    assert db.execute("select account from transactions order by account").fetchall() == [("banco1",), ("banco3",)]
    assert "::error::banco2: Error 401" in capsys.readouterr().out


def test_guarda_la_moneda_real_y_avisa_si_no_es_euro(db, monkeypatch):
    en_dolares = tx("r2", "20", "TIENDA USA")
    en_dolares["transaction_amount"]["currency"] = "USD"
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI"), en_dolares]})
    assert db.execute("select external_id, currency from transactions order by 1").fetchall() == [
        ("r1", "EUR"), ("r2", "USD")]
    assert any("movimientos en USD" in a for a in fetch.ALERTS)


def test_las_vistas_de_looker_solo_suman_euros(db):
    for account, amount, currency in (("a1", -10, "EUR"), ("a1", -99, "USD")):
        db.execute("insert into transactions (account, booking_date, amount, currency, category) "
                   "values (%s, '2026-10-02', %s, %s, 'Comida')", (account, amount, currency))
    db.execute("insert into saldos_iniciales values ('a1', 100)")
    with psycopg.connect(role_url("looker")) as conn:
        assert conn.execute("select gasto from v_gasto_mensual_categoria").fetchall() == [(Decimal(10),)]
        assert conn.execute("select gastos from v_ahorro_mensual").fetchall() == [(Decimal(10),)]
        assert conn.execute("select saldo from v_saldo_actual").fetchall() == [(Decimal(90),)]
        assert conn.execute("select saldo from v_saldo_mensual").fetchall() == [(Decimal(90),)]


def test_relee_los_ultimos_30_dias_para_ver_movimientos_que_llegan_tarde(db, monkeypatch):
    db.execute("insert into transactions (account, booking_date, amount, source, external_id) "
               "values ('banco', '2026-10-25', -1, 'enablebanking', 'viejo')")
    desde = []
    monkeypatch.setattr(fetch, "CUTOFF", date(2026, 9, 1))
    run_fetch(monkeypatch, {"u1": []})
    monkeypatch.setattr(fetch, "fetch_transactions", lambda uid, date_from: desde.append(date_from) or [])
    fetch.main()
    assert desde == [date(2026, 9, 25)]


def test_avisa_si_el_saldo_del_banco_no_cuadra_con_la_base(db, monkeypatch, capsys):
    db.execute("insert into saldos_iniciales values ('banco', 100)")
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]}, saldos={"u1": Decimal(80)})
    assert any("banco: el saldo no cuadra" in a and "diferencia -7.50" in a for a in fetch.ALERTS)
    assert "saldo del banco 80 €, en la base 87.50 €" in capsys.readouterr().out


def test_no_avisa_si_el_saldo_cuadra_o_no_hay_dato(db, monkeypatch):
    db.execute("insert into saldos_iniciales values ('banco', 100), ('otro', 5)")
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]}, saldos={"u1": Decimal("87.50")},
              otros_bancos={"otro": {"u2": []}})  # u2: el banco no da saldo
    assert fetch.ALERTS == []
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]}, saldos={"u1": Decimal(50)})
    assert fetch.ALERTS  # prueba de que sí avisaría con otra cifra


def test_si_falla_la_consulta_del_saldo_la_cuenta_se_guarda_igual_y_solo_avisa(db, monkeypatch, capsys):
    db.execute("insert into saldos_iniciales values ('banco', 100)")
    db.execute("revoke select on public.saldos_iniciales from app_fetcher")  # como antes de aplicar el SQL nuevo
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]}, saldos={"u1": Decimal(80)})
    assert db.execute("select count(*) from transactions").fetchone()[0] == 1
    assert fetch.ALERTS == []
    assert "::warning::banco: no se ha podido comprobar el saldo" in capsys.readouterr().out


def test_no_compara_el_saldo_de_una_cuenta_sin_saldo_inicial(db, monkeypatch):
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]}, saldos={"u1": Decimal(1)})
    assert fetch.ALERTS == []


def test_app_fetcher_solo_puede_actualizar_las_columnas_que_usa(db):
    db.execute("insert into transactions (booking_date, amount) values ('2026-10-01', 1)")
    with psycopg.connect(role_url("app_fetcher")) as conn:
        conn.execute("update transactions set category = 'Comida', category_source = 'ia', is_transfer = false, "
                     "viaje = null, booking_date = '2026-10-02'")
        conn.commit()
        for columna in ("amount", "account", "external_id", "raw", "description"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"update transactions set {columna} = {columna}")
            conn.rollback()


def test_looker_no_puede_leer_raw_pero_si_el_resto(db):
    db.execute("insert into transactions (booking_date, amount, raw) values ('2026-10-01', 1, '{\"iban\": \"ES00\"}')")
    with psycopg.connect(role_url("looker")) as conn:
        assert conn.execute("select amount from transactions").fetchall() == [(Decimal(1),)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select raw from transactions")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from transactions")


ALLOWED = ("Comida", "Ocio", "Viajes", "Ingresos")


def gasto(db, category=None, source=None, amount=-10, viaje=None, desc="BAR PEPE"):
    return db.execute(
        "insert into transactions (account, booking_date, amount, description, category, category_source, viaje) "
        "values ('banco', '2026-10-02', %s, %s, %s, %s, %s) returning id", (amount, desc, category, source, viaje)
    ).fetchone()[0]


def corregir_como_fetcher(*args, **kwargs):
    with psycopg.connect(role_url("app_fetcher")) as conn, conn.cursor() as cur:
        return corregir.corregir(cur, *args, **kwargs)


def test_corregir_pone_la_categoria_como_manual_y_la_deja_al_dia(db):
    a, b = gasto(db, "Comida", "ia"), gasto(db, None, None)
    filas = corregir_como_fetcher("ocio", [a, b], ALLOWED)  # sin distinguir mayúsculas
    assert [(f[0], f[5]) for f in filas] == [(a, "Comida"), (b, None)]
    assert db.execute("select category, category_source from transactions order by id").fetchall() == [
        ("Ocio", "manual"), ("Ocio", "manual")]


def test_corregir_fuera_de_un_viaje_limpia_la_columna_viaje(db):
    a = gasto(db, "Viajes", "viaje", viaje="Ámsterdam")
    corregir_como_fetcher("Ocio", [a], ALLOWED)
    assert db.execute("select viaje from transactions").fetchone()[0] is None
    assert db.execute("select count(*) from v_gasto_por_viaje").fetchone()[0] == 0


def test_corregir_a_viajes_conserva_el_nombre_del_viaje(db):
    a = gasto(db, "Viajes", "viaje", viaje="Ámsterdam")
    corregir_como_fetcher("Viajes", [a], ALLOWED)
    assert db.execute("select viaje, category_source from transactions").fetchone() == ("Ámsterdam", "manual")


@pytest.mark.parametrize("categoria, ids, mensaje", [
    ("Ocioo", [1], "¿Quisiste decir 'Ocio'?"),
    ("Ocio", [1, 999], "No existe ningún gasto con id 999"),
    ("Ingresos", [1], "Ingresos es solo para importes positivos (id 1)"),
])
def test_corregir_es_todo_o_nada(db, categoria, ids, mensaje):
    gasto(db, "Comida", "ia")
    with pytest.raises(corregir.NoSePuedeCorregir, match=re.escape(mensaje)):
        corregir_como_fetcher(categoria, ids, ALLOWED)
    assert db.execute("select category, category_source from transactions").fetchall() == [("Comida", "ia")]


def test_corregir_en_modo_prueba_no_escribe(db):
    a = gasto(db, "Comida", "ia")
    assert corregir_como_fetcher("Ocio", [a], ALLOWED, dry_run=True)[0][5] == "Comida"
    assert db.execute("select category, category_source from transactions").fetchall() == [("Comida", "ia")]


def test_lo_corregido_a_mano_no_lo_vuelve_a_cambiar_el_fetch(db, monkeypatch):
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]})  # la regla lo pone en Comida
    id_ = db.execute("select id from transactions").fetchone()[0]
    corregir_como_fetcher("Ocio", [id_], ALLOWED)
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI")]})
    assert db.execute("select category, category_source from transactions").fetchall() == [("Ocio", "manual")]


def test_corregir_desde_la_linea_de_comandos(db, monkeypatch, capsys):
    import db as modulo_db

    monkeypatch.setattr(modulo_db, "connect", lambda: psycopg.connect(role_url("app_fetcher")))
    id_ = gasto(db, "Comida", "ia", desc="BAR PEPE")
    corregir.main(["Ocio", str(id_)])
    salida = capsys.readouterr().out
    assert "Comida -> Ocio" in salida and "Corregidos 1 gastos como manual." in salida
    assert db.execute("select category, category_source from transactions").fetchone() == ("Ocio", "manual")
    for argv, error in ((["Ocioo", str(id_)], "Quisiste decir"), (["Ocio", "999999"], "No existe ningún gasto")):
        with pytest.raises(SystemExit, match=error):
            corregir.main(argv)


def test_revisar_lista_solo_lo_de_esa_fuente_y_filtra_por_categoria(db):
    gasto(db, "Comida", "ia", desc="MARKET UNO")
    gasto(db, "Ocio", "ia", desc="BAR DOS")
    gasto(db, "Ocio", "manual", desc="BAR TRES")
    with psycopg.connect(role_url("app_backup")) as conn, conn.cursor() as cur:
        resumen, gastos = revisar_ia.revisar(cur, "ia")
        assert dict(resumen) == {"Comida": 1, "Ocio": 1}
        assert sorted(g[5] for g in gastos) == ["BAR DOS", "MARKET UNO"]
        assert [g[5] for g in revisar_ia.revisar(cur, "ia", categoria="Ocio")[1]] == ["BAR DOS"]
        assert len(revisar_ia.revisar(cur, "ia", limite=1)[1]) == 1
        salida = "\n".join(revisar_ia.formatear(*revisar_ia.revisar(cur, "manual"), "manual"))
    assert "BAR TRES" in salida and "scripts/corregir.py" in salida


def test_revisar_sin_resultados_lo_dice(db):
    with psycopg.connect(role_url("app_backup")) as conn, conn.cursor() as cur:
        assert revisar_ia.formatear(*revisar_ia.revisar(cur, "ia"), "ia") == ["Ningún gasto con category_source = 'ia'."]


def test_backup_exporta_saldos_iniciales_con_app_backup(db, monkeypatch, tmp_path):
    db.execute("insert into saldos_iniciales values ('a1', 100.50), ('a2', -10)")
    monkeypatch.setattr(backup, "connect", lambda: psycopg.connect(role_url("app_backup")))
    backup.main(str(tmp_path / "transactions.csv"), str(tmp_path / "saldos_iniciales.csv"))
    rows = list(csv.DictReader((tmp_path / "saldos_iniciales.csv").open(encoding="utf-8", newline="")))
    assert rows == [{"account": "a1", "saldo": "100.50"}, {"account": "a2", "saldo": "-10"}]


def test_backup_exporta_todas_las_filas_con_cabecera(db, monkeypatch, tmp_path):
    run_fetch(monkeypatch, {"u1": [tx("r1", "12.50", "ALDI"), tx("r2", "4", "ALDI")]})
    monkeypatch.setattr(backup, "connect", lambda: psycopg.connect(role_url("app_backup")))
    out = tmp_path / "transactions.csv"
    backup.main(str(out), str(tmp_path / "saldos_iniciales.csv"))
    rows = list(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert sorted(r["external_id"] for r in rows) == ["r1", "r2"]
    assert list(rows[0]) == ["id", "account", "booking_date", "amount", "currency", "category", "description",
                             "source", "external_id", "raw", "is_transfer", "category_source", "viaje"]
