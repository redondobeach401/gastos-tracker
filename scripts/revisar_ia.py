"""Lista los gastos que ha categorizado la IA (o el historial, las reglas...) para revisarlos. Solo lee.

Usa SUPABASE_DB_URL y SUPABASE_CA_CERT; vale el rol app_backup (solo lectura). Desde la raíz del repo:

  python scripts/revisar_ia.py                          los 30 últimos de la IA
  python scripts/revisar_ia.py --limite 100             más
  python scripts/revisar_ia.py --categoria Ocio         solo los que la IA ha puesto en Ocio
  python scripts/revisar_ia.py --fuente historial       los del historial (regla, viaje o manual también valen)

Para corregir uno: python scripts/corregir.py <categoría> <id> [<id>...]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FUENTES = ("ia", "historial", "regla", "viaje", "manual")

RESUMEN_SQL = (
    "select category, count(*) from transactions where category_source = %s "
    "group by category order by count(*) desc, category"
)
GASTOS_SQL = (
    "select id, booking_date, account, amount, category, description from transactions "
    "where category_source = %s and (%s::text is null or category = %s) "
    "order by booking_date desc, id desc limit %s"
)


def revisar(cur, fuente="ia", categoria=None, limite=30):
    """Devuelve ([(categoría, cuántos)] de esa fuente, [(id, fecha, cuenta, importe, categoría, descripción)])."""
    cur.execute(RESUMEN_SQL, (fuente,))
    resumen = cur.fetchall()
    cur.execute(GASTOS_SQL, (fuente, categoria, categoria, limite))
    return resumen, cur.fetchall()


def formatear(resumen, gastos, fuente):
    if not resumen:
        return [f"Ningún gasto con category_source = {fuente!r}."]
    lineas = [f"Categorizados por {fuente!r}: " + ", ".join(f"{c} {n}" for c, n in resumen), ""]
    lineas += [f"{id_:>6} {d} {account:<12} {amount:>9} € {category or '-':<20} {(desc or '')[:60]}"
               for id_, d, account, amount, category, desc in gastos]
    lineas += ["", "Corrige con: python scripts/corregir.py <categoría> <id> [<id>...]"]
    return lineas


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fuente", choices=FUENTES, default="ia", help="quién puso la categoría (ia)")
    parser.add_argument("--categoria", help="solo los de esta categoría")
    parser.add_argument("--limite", type=int, default=30, help="cuántos mostrar (30)")
    args = parser.parse_args(argv)

    from db import connect

    with connect() as conn:
        conn.read_only = True
        resumen, gastos = revisar(conn.cursor(), args.fuente, args.categoria, args.limite)
    print("\n".join(formatear(resumen, gastos, args.fuente)))


if __name__ == "__main__":
    main()
