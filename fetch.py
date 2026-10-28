"""Descarga movimientos nuevos de Enable Banking y los guarda en Supabase."""
import hashlib
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jwt
import psycopg
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import logs
from categorize import categorize_pending
from db import connect
from ia import categorize_pending_ia, resumen_nuevas
from rules import load_rules
from viajes import aplicar_viajes

API = "https://api.enablebanking.com"
APP_ID = os.environ["EB_APP_ID"]
KEY = os.environ["EB_PRIVATE_KEY"]            # contenido del .pem
SESSIONS = json.loads(os.environ["EB_SESSIONS"])
CUTOFF = date.fromisoformat(os.environ["CUTOFF_DATE"])  # 1er día en que manda la API
STALE_DAYS = int(os.environ.get("STALE_DAYS", "30"))     # días sin movimientos nuevos para avisar
ALERTS_FILE = os.environ.get("ALERTS_FILE")              # si existe, el workflow lo convierte en un issue
ALERTS = []
SIN_CATEGORIA_AVISO = int(os.environ.get("SIN_CATEGORIA_AVISO", "10"))  # gastos sin categoría para el aviso resumen
ERRORES_FILE = os.environ.get("ERRORES_FILE")          # si existe, el workflow lo añade al issue de fallo (qué cuentas fallaron)
RESUMEN_FILE = os.environ.get("RESUMEN_FILE")            # si existe, el workflow lo convierte en otro issue (Telegram solo los lunes)
RESUMEN = []
MAX_PAGINAS = 100     # páginas de movimientos por cuenta: freno por si la API repite la continuation_key
MAX_ERROR_TEXT = 300   # el cuerpo de un error del banco puede ser un HTML largo que taparía el log
PROCESADAS = []        # cuentas ya guardadas (con commit) en esta ejecución, para el mensaje si una posterior falla
CUENTAS_CON_RETRASO = ("revolut", "trade-republic")  # sus traspasos pueden apuntarse con días de diferencia
VENTANA_DIAS = int(os.environ.get("VENTANA_DIAS", "30"))   # se relee desde tantos días antes del último movimiento guardado
SALDO_TOLERANCIA = Decimal(os.environ.get("SALDO_TOLERANCIA", "1"))  # € de diferencia con el saldo del banco para avisar
DIAS_RETRASO = 2       # ventana para emparejar un traspaso con una de esas cuentas (el resto, el mismo día)


class ErrorBanco(Exception):
    """El banco (o su consentimiento) ha fallado al leer una cuenta; las demás cuentas siguen."""


def alert(msg):
    """Aviso que debe ver una persona: sale en el log y, en Actions, también como issue."""
    logs.warning(msg)
    ALERTS.append(msg)


def make_session():
    """Sesión con reintentos y espera creciente ante errores temporales de la API (429 y 5xx)."""
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


SESSION = make_session()


TOKEN_TTL = 3600
_token = {"value": None, "exp": 0}  # JWT en uso: firmar RS256 en cada página pedida es trabajo repetido


def headers():
    now = int(time.time())
    if now >= _token["exp"] - 300:  # se renueva 5 min antes de que caduque
        _token["value"] = jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + TOKEN_TTL},
            KEY, algorithm="RS256", headers={"kid": APP_ID},
        )
        _token["exp"] = now + TOKEN_TTL
    return {"Authorization": f"Bearer {_token['value']}"}


def request_ids(r):
    """Cabeceras de la respuesta que identifican la petición (para citarlas a soporte)."""
    return ", ".join(f"{k}={v}" for k, v in getattr(r, "headers", {}).items() if "request" in k.lower() or "trace" in k.lower())


def fetch_transactions(uid, date_from):
    params = {"date_from": date_from.isoformat(), "transaction_status": "BOOK"}
    out = []
    for _ in range(MAX_PAGINAS):
        r = SESSION.get(f"{API}/accounts/{uid}/transactions", params=params, headers=headers(), timeout=60)
        if not r.ok:
            raise ErrorBanco(f"Error {r.status_code} en GET /accounts/{{uid}}/transactions de la cuenta {uid}: "
                     f"{r.text[:MAX_ERROR_TEXT]}\n"
                     f"Petición: {request_ids(r) or 'sin cabeceras de identificación'}.\n"
                     f"Cuentas ya procesadas: {', '.join(PROCESADAS) or 'ninguna'}.\n"
                     "Si es 401/403/422 puede haber caducado el consentimiento: repite auth.py. "
                     "Si es ASPSP_ERROR o 5xx falla el banco: reintenta más tarde.")
        data = r.json()
        out += data.get("transactions", [])
        if not data.get("continuation_key"):
            return out
        params["continuation_key"] = data["continuation_key"]
    raise ErrorBanco(f"La cuenta {uid} sigue devolviendo páginas tras {MAX_PAGINAS}: se corta por si la API repite la "
                     "continuation_key.")


def fetch_balance(uid):
    """Saldo contabilizado (CLBD) de la cuenta en euros según el banco, o None si no se puede saber.

    Es solo una comprobación: nunca rompe el fetch, así que cualquier fallo del banco se queda en un aviso del log.
    """
    try:
        r = SESSION.get(f"{API}/accounts/{uid}/balances", headers=headers(), timeout=60)
        if not r.ok:
            logs.warning(f"No se pudo leer el saldo de la cuenta {uid}: HTTP {r.status_code}")
            return None
        for b in r.json().get("balances", []):
            amount = b.get("balance_amount") or {}
            if b.get("balance_type") == "CLBD" and amount.get("currency", "EUR") == "EUR":
                return Decimal(amount["amount"])
    except (requests.RequestException, ValueError, KeyError, ArithmeticError) as e:
        logs.warning(f"No se pudo leer el saldo de la cuenta {uid}: {e!r}")
    return None


def check_saldo(cur, account, banco):
    """Compara el saldo del banco con el que sale de la base (saldo inicial + movimientos) y avisa si no cuadran."""
    cur.execute("select s.saldo + coalesce(sum(t.amount), 0) from saldos_iniciales s "
                "left join transactions t on t.account = s.account and t.currency = 'EUR' where s.account = %s "
                "group by s.saldo", (account,))
    fila = cur.fetchone()
    if banco is None or fila is None:  # sin saldo del banco, o la cuenta no tiene saldo inicial: nada que comparar
        return
    diferencia = banco - fila[0]
    print(f"{account}: saldo del banco {banco} €, en la base {fila[0]} € (diferencia {diferencia} €)")
    if abs(diferencia) > SALDO_TOLERANCIA:
        alert(f"{account}: el saldo no cuadra. El banco dice {banco} € y la base de datos {fila[0]} € "
              f"(diferencia {diferencia} €). Puede faltar o sobrar algún movimiento, o haber importes retenidos.")


def currency_of(tx):
    return (tx.get("transaction_amount") or {}).get("currency") or "EUR"


RULES = load_rules()  # categorias.yml: traspasos y categorías (se validan al cargar)


def is_transfer(desc, amount):
    """Traspaso entre cuentas propias (se excluye de gastos/ingresos en los informes)."""
    return bool(RULES.transfer_re.search(desc)) or any(
        desc == d and abs(amount) == importe for d, importe in RULES.transfer_exact
    )


def categorize(desc, amount):
    """Categoría según la descripción: "Ingresos" si entra dinero (como en el histórico),
    la de la primera regla de categorias.yml si es un gasto; None si es un traspaso o no hay regla."""
    if is_transfer(desc, amount):
        return None
    if amount > 0:
        return "Ingresos"
    return next((cat for rx, cat in RULES.categories if rx.search(desc)), None)


def normalize(tx, seen):
    amount = Decimal(tx["transaction_amount"]["amount"])
    debit = tx.get("credit_debit_indicator") == "DBIT"
    if debit:
        amount = -amount
    # value_date primero: Abanca pone en booking_date una fecha de anotación posterior (a veces futura)
    d = tx.get("value_date") or tx.get("booking_date") or tx.get("transaction_date")
    party = ((tx.get("creditor") if debit else tx.get("debtor")) or {}).get("name")
    rem = " ".join(tx.get("remittance_information") or [])
    desc = " - ".join(x for x in (party, rem) if x) or tx.get("note") or ""
    ext = tx.get("entry_reference") or tx.get("transaction_id")
    if not ext:  # banco sin id: hash + contador para movimientos idénticos
        base = hashlib.sha1(f"{d}|{amount}|{desc}".encode()).hexdigest()
        seen[base] = seen.get(base, 0) + 1
        ext = f"{base}#{seen[base]}"
    return d, amount, desc, ext, tx


RAW_DROP = ("balance_after_transaction",)  # saldo tras cada movimiento: dato sensible que no usamos


def raw_for_storage(tx):
    """Copia del movimiento del banco sin los campos que no queremos guardar en `raw`."""
    return {k: v for k, v in tx.items() if k not in RAW_DROP}


RECATEGORIZABLE = (  # fila existente a la que una regla puede poner categoría
    "((transactions.category is null or transactions.category_source in ('historial', 'ia')) "
    "and excluded.category is not null and excluded.category is distinct from transactions.category)"
)


def guardar_movimientos(cur, account, txs):
    """Inserta los movimientos de una cuenta, o corrige los que ya estaban.

    Devuelve (leídos, nuevos, con fecha corregida, recategorizados, monedas que no son EUR).
    """
    seen, rows = {}, []
    for tx in txs:
        d, amount, desc, ext, raw = normalize(tx, seen)
        category = categorize(desc, amount)
        rows.append((account, d, amount, desc, ext, json.dumps(raw_for_storage(raw)),
                     is_transfer(desc, amount), category, "regla" if category else None,
                     currency_of(raw)))
    cur.execute("select external_id, booking_date, category from transactions "
                "where account=%s and external_id = any(%s)", (account, [r[4] for r in rows]))
    antes = {ext: (fecha, cat) for ext, fecha, cat in cur.fetchall()}
    new = recategorized = fecha_corregida = 0
    # Un solo viaje a la base (modo pipeline) en vez de uno por movimiento; returning=True deja un
    # resultado por fila, en el mismo orden.
    cur.executemany(
        "insert into transactions (account, booking_date, amount, description, source, external_id, "
        "raw, is_transfer, category, category_source, currency) "
        "values (%s, %s, %s, %s, 'enablebanking', %s, %s::jsonb, %s, %s, %s, %s) "
        # La fecha siempre se corrige. Una regla rellena la categoría vacía y corrige la que puso
        # el historial; nunca toca lo demás.
        "on conflict (account, external_id) do update "
        "set booking_date = excluded.booking_date, "
        "category = case when " + RECATEGORIZABLE + " then excluded.category else transactions.category end, "
        "category_source = case when " + RECATEGORIZABLE + " then excluded.category_source "
        "else transactions.category_source end "
        "where transactions.booking_date is distinct from excluded.booking_date or " + RECATEGORIZABLE + " "
        "returning (xmax = 0), booking_date, category",
        rows,
        returning=True,
    )
    for row in rows:
        res = cur.fetchone()  # None: ya existía y no cambia
        cur.nextset()
        if not res:
            continue
        if res[0]:
            new += 1
            continue
        fecha_antes, categoria_antes = antes[row[4]]
        fecha_corregida += fecha_antes != res[1]
        recategorized += categoria_antes != res[2]
    return len(rows), new, fecha_corregida, recategorized, sorted({r[9] for r in rows} - {"EUR"})


def procesar_cuenta(conn, cur, account, uid):
    """Lee una cuenta del banco, la guarda (con commit) y la comprueba: moneda, saldo y actividad.

    Devuelve ((cuenta, leídos, nuevos, fecha corregida, recategorizados), desde qué fecha se ha leído).
    Lanza ErrorBanco, sin haber tocado la base, si el banco no deja leerla.
    """
    cur.execute("select max(booking_date) from transactions where account=%s and source='enablebanking'", (account,))
    last = cur.fetchone()[0]
    date_from = max(last - timedelta(days=VENTANA_DIAS), CUTOFF) if last else CUTOFF
    txs = fetch_transactions(uid, date_from)
    leidos, new, fecha_corregida, recategorized, monedas = guardar_movimientos(cur, account, txs)
    print(f"{account}: {leidos} leídos desde {date_from}, {new} nuevos, "
          f"{fecha_corregida} con fecha corregida, {recategorized} recategorizados")
    PROCESADAS.append(account)
    if monedas:  # las vistas de Looker solo cuentan euros: no se mezclan monedas sin convertir
        alert(f"{account}: hay movimientos en {', '.join(monedas)}. Las vistas de Looker solo suman EUR, "
              "así que no cuentan en gastos ni saldos.")
    try:
        # Savepoint: si falla la consulta (p. ej. falta un permiso), solo se deshace la comprobación, no la cuenta.
        with conn.transaction():
            check_saldo(cur, account, fetch_balance(uid))
    except psycopg.Error as e:
        logs.warning(f"{account}: no se ha podido comprobar el saldo: {e}")
    cur.execute("select max(booking_date) from transactions where account=%s and source='enablebanking'", (account,))
    newest = cur.fetchone()[0] or CUTOFF
    if (date.today() - newest).days > STALE_DAYS:
        alert(f"{account}: sin movimientos nuevos desde {newest} (más de {STALE_DAYS} días). "
              "Comprueba que el consentimiento sigue vigente o que la cuenta tiene actividad.")
    conn.commit()  # cada cuenta queda guardada aunque falle una posterior
    return (account, leidos, new, fecha_corregida, recategorized), date_from


def marcar_traspasos_por_pareja(cur, ventanas):
    """Marca como traspaso lo que no casa con los patrones de categorias.yml: mismo día e importe opuesto en otra
    cuenta propia (hasta DIAS_RETRASO días de diferencia si una de las dos es de CUENTAS_CON_RETRASO).

    Solo desde la fecha más antigua leída hoy: la pareja de lo ya guardado se marcó en su día.
    Devuelve [(cuenta, fecha, importe, descripción)] de lo marcado.
    """
    desde = min((d for _, d in ventanas), default=date.max)
    cur.execute(
        "update transactions t set is_transfer = true "
        "where not t.is_transfer and t.source='enablebanking' and t.amount <> 0 and t.booking_date >= %s "
        "and exists (select 1 from transactions o where o.source='enablebanking' and o.account <> t.account "
        "and o.amount = -t.amount "
        "and abs(o.booking_date - t.booking_date) <= "
        "case when t.account = any(%s) or o.account = any(%s) then %s else 0 end) "
        "returning t.account, t.booking_date, t.amount, t.description",
        (desde - timedelta(days=DIAS_RETRASO) if desde != date.max else desde,
         list(CUENTAS_CON_RETRASO), list(CUENTAS_CON_RETRASO), DIAS_RETRASO),
    )
    return cur.fetchall()


def avisar_gastos_sin_categoria(cur, ventanas):
    """Avisa de los gastos sin categoría: cada uno de lo leído hoy y, si hay muchos, un resumen de todo el histórico.

    Va al final, para no avisar de lo que acaba de resolverse. Devuelve cuántos gastos de lo leído hoy siguen sin categoría.
    """
    sin_categoria = 0
    for account, date_from in ventanas:
        cur.execute(
            "select booking_date, amount, description from transactions "
            "where account=%s and source='enablebanking' and booking_date>=%s "
            "and category is null and not is_transfer and amount<0 order by booking_date",
            (account, date_from),
        )
        for d, amount, desc in cur.fetchall():
            sin_categoria += 1
            logs.warning(f"Gasto sin categoría en {account}: {d} {amount} € {desc!r}. Añade una regla en categorias.yml.")
    # Resumen de todo el histórico: los avisos de arriba solo cubren la ventana leída hoy.
    cur.execute(
        "select count(*), min(booking_date) from transactions "
        "where source='enablebanking' and category is null and not is_transfer and amount<0"
    )
    total, mas_antiguo = cur.fetchone()
    if total >= SIN_CATEGORIA_AVISO:
        msg = f"{total} gastos sin categoría (el más antiguo, {mas_antiguo}). Añade reglas en categorias.yml."
        logs.warning(msg)
        RESUMEN.append(msg)
    return sin_categoria


def main():
    ventanas = []  # (cuenta, desde qué fecha se ha leído), para avisar de gastos sin categoría al final
    errores = []   # (cuenta, mensaje) de las cuentas que el banco no ha dejado leer: se falla al final
    cuentas = []   # (cuenta, leídos, nuevos, fecha corregida, recategorizados), para el resumen final
    inicio = time.monotonic()
    with connect() as conn, conn.cursor() as cur:
        for label, s in SESSIONS.items():
            if datetime.fromisoformat(s["valid_until"]) < datetime.now(UTC) + timedelta(days=14):
                alert(f"El consentimiento de {label} caduca pronto ({s['valid_until']}). Repite auth.py y actualiza EB_SESSIONS.")
            for i, acc in enumerate(s["accounts"]):
                account = label if len(s["accounts"]) == 1 else f"{label}-{i + 1}"
                try:
                    resumen, date_from = procesar_cuenta(conn, cur, account, acc["uid"])
                except ErrorBanco as e:
                    # Sin tocar la base: lo ya guardado de las demás cuentas se conserva y se sigue con ellas.
                    logs.error(f"{account}: {e}")
                    errores.append((account, str(e)))
                    continue
                cuentas.append(resumen)
                ventanas.append((account, date_from))
        with logs.group("Traspasos por pareja"):
            por_pareja = marcar_traspasos_por_pareja(cur, ventanas)
            for account, d, amount, desc in por_pareja:
                logs.notice(f"Marcado como traspaso por pareja en otra cuenta: {account} {d} {amount} € {desc!r}")
        # Gastos en las fechas de un viaje de categorias.yml → Viajes (viajes.py), antes del historial y la IA.
        with logs.group("Viajes"):
            por_viaje, fuera_de_viaje = aplicar_viajes(cur, RULES.trips, RULES.trip_keep)
            for viaje, account, d, amount, desc in por_viaje:
                logs.notice(f"En Viajes por el viaje {viaje!r}: {account} {d} {amount} € {desc!r}")
            for account, d, amount, desc in fuera_de_viaje:
                logs.notice(f"Ya no está en ningún viaje, queda sin categoría: {account} {d} {amount} € {desc!r}")
        # Lo que las reglas no cubren, por historial (categorize.py), ya con los traspasos por pareja marcados.
        with logs.group("Categorización por historial"):
            por_historial = categorize_pending(cur, RULES.allowed, RULES.ignored)
            for _, account, d, amount, desc, category, reason in por_historial:
                logs.notice(f"Categorizado por historial como {category!r}: {account} {d} {amount} € {desc!r} ({reason})")
        # Lo que sigue sin categoría, con IA (ia.py): solo si hay ANTHROPIC_API_KEY y sin romper el fetch si falla.
        with logs.group("Categorización con IA"):
            uso_ia = {}
            done_ia, sin_categoria_ia = categorize_pending_ia(cur, RULES.allowed, alerta=alert, uso=uso_ia,
                                                                descripciones=RULES.descriptions)
            for _, account, d, amount, desc, category in done_ia:
                logs.notice(f"Categorizado con IA como {category!r}: {account} {d} {amount} € {desc!r}")
            for line in resumen_nuevas(sin_categoria_ia):
                logs.notice(line)
        sin_categoria = avisar_gastos_sin_categoria(cur, ventanas)
    print_resumen(cuentas, len(por_pareja), len(por_historial), len(done_ia), sin_categoria, time.monotonic() - inicio,
                  uso_ia)
    if errores:  # tras guardar todo lo que sí se ha podido leer, para que el workflow avise del fallo
        write_errores(errores)
        sys.exit(f"{len(errores)} cuenta(s) sin leer: {', '.join(a for a, _ in errores)}. "
                 f"Cuentas guardadas: {', '.join(PROCESADAS) or 'ninguna'}.")


def print_resumen(cuentas, por_pareja, por_historial, por_ia, sin_categoria, segundos, uso_ia=None):
    """Una línea final con lo que ha hecho la ejecución y, en Actions, la misma información como tarjeta de resumen."""
    leidos, nuevos, corregidas, recategorizados = (sum(c[i] for c in cuentas) for i in (1, 2, 3, 4))
    print(
        f"Resumen: {len(cuentas)} cuentas, {leidos} leídos, {nuevos} nuevos, {corregidas} con fecha corregida, "
        f"{recategorizados} recategorizados, {por_pareja} traspasos por pareja, {por_historial} por historial, "
        f"{por_ia} por IA, {sin_categoria} sin categoría · {segundos:.1f} s"
    )
    filas = "\n".join(f"| {a} | {le} | {n} | {f} | {r} |" for a, le, n, f, r in cuentas)
    logs.step_summary(
        "## Resumen del fetch\n\n| Cuenta | Leídos | Nuevos | Fecha corregida | Recategorizados |\n|---|---|---|---|---|\n"
        f"{filas}\n\nTraspasos por pareja: {por_pareja} · categorizados por historial: {por_historial} · "
        f"por IA: {por_ia} · gastos sin categoría: {sin_categoria} · {segundos:.1f} s"
        + (f"\n\nUso de la IA: {uso_ia['tokens_in']} tokens de entrada, {uso_ia['tokens_out']} de salida, "
           f"{uso_ia['segundos']:.1f} s" if uso_ia else "")
        + "".join(f"\n\n⚠️ {m}" for m in ALERTS)
    )


def write_errores(errores):
    """Deja en ERRORES_FILE qué cuentas no se han podido leer y por qué (primera línea del error), para el issue de fallo."""
    if ERRORES_FILE:
        lines = "\n".join(f"- `{account}`: {msg.splitlines()[0]}" for account, msg in errores)
        Path(ERRORES_FILE).write_text(f"\nCuentas que el banco no ha dejado leer:\n\n{lines}\n", encoding="utf-8")


def write_alerts():
    """Deja los avisos en ALERTS_FILE y el resumen en RESUMEN_FILE (markdown) para que el workflow los publique como issues."""
    if ALERTS_FILE and ALERTS:
        body = "\n".join(f"- {m}" for m in ALERTS)
        Path(ALERTS_FILE).write_text(f"## Avisos de enable-banking\n\n{body}\n", encoding="utf-8")
    if RESUMEN_FILE and RESUMEN:
        Path(RESUMEN_FILE).write_text(f"## Gastos sin categoría\n\n{RESUMEN[0]}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
    write_alerts()
