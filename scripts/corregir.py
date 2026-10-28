"""Corrige a mano la categoría de uno o varios gastos (category_source = 'manual').

Usa SUPABASE_DB_URL y SUPABASE_CA_CERT, con el rol app_fetcher (el único, junto a postgres, que puede escribir).
Desde la raíz del repo:

  python scripts/corregir.py Ocio 123 124        pone Ocio a los gastos con esos id
  python scripts/corregir.py Ocio 123 --dry-run  solo muestra qué cambiaría

Los id salen de scripts/revisar_ia.py. Lo marcado como manual no lo vuelven a tocar las reglas, los viajes ni la
IA, y el historial y la IA aprenden de ello. Es todo o nada: si un id no existe o la categoría no vale, no cambia nada.
"""
import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class NoSePuedeCorregir(ValueError):
    """La categoría o algún id no valen; el mensaje dice cuál."""


def elegir_categoria(texto, allowed):
    """La categoría permitida que corresponde a `texto` (sin distinguir mayúsculas), o NoSePuedeCorregir."""
    for categoria in allowed:
        if categoria.casefold() == texto.casefold():
            return categoria
    cercana = difflib.get_close_matches(texto, allowed, n=1, cutoff=0.5)
    ayuda = f" ¿Quisiste decir {cercana[0]!r}?" if cercana else ""
    raise NoSePuedeCorregir(f"La categoría {texto!r} no está en categorias_permitidas.{ayuda}")


def corregir(cur, categoria, ids, allowed, dry_run=False):
    """Pone `categoria` como manual a los gastos `ids`. Devuelve [(id, cuenta, fecha, importe, descripción, antes)].

    Si la categoría no es Viajes, también limpia la columna viaje: un gasto sacado de un viaje deja de contar en él.
    """
    categoria = elegir_categoria(categoria, allowed)
    cur.execute(
        "select id, account, booking_date, amount, description, category from transactions "
        "where id = any(%s::bigint[]) order by id",
        (list(ids),),
    )
    filas = cur.fetchall()
    faltan = sorted(set(ids) - {f[0] for f in filas})
    if faltan:
        raise NoSePuedeCorregir(f"No existe ningún gasto con id {', '.join(map(str, faltan))}.")
    if categoria == "Ingresos":
        gastos = [f[0] for f in filas if f[3] < 0]
        if gastos:
            raise NoSePuedeCorregir(f"Ingresos es solo para importes positivos (id {', '.join(map(str, gastos))}).")
    if not dry_run:
        cur.execute(
            "update transactions set category = %s, category_source = 'manual', "
            "viaje = case when %s = 'Viajes' then viaje else null end where id = any(%s::bigint[])",
            (categoria, categoria, list(ids)),
        )
    return filas


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("categoria", help="categoría nueva (una de categorias_permitidas)")
    parser.add_argument("ids", type=int, nargs="+", help="id de los gastos (los da scripts/revisar_ia.py)")
    parser.add_argument("--dry-run", action="store_true", help="solo muestra qué cambiaría")
    args = parser.parse_args(argv)

    from db import connect
    from rules import load_rules

    allowed = load_rules().allowed
    try:
        categoria = elegir_categoria(args.categoria, allowed)
        with connect() as conn, conn.cursor() as cur:
            filas = corregir(cur, categoria, args.ids, allowed, args.dry_run)
    except NoSePuedeCorregir as e:
        sys.exit(str(e))
    for id_, account, d, amount, desc, antes in filas:
        print(f"{id_:>6} {d} {account:<12} {amount:>9} € {antes or '-'} -> {categoria}  {(desc or '')[:50]}")
    print(f"{'Se corregirían' if args.dry_run else 'Corregidos'} {len(filas)} gastos como manual.")


if __name__ == "__main__":
    main()
