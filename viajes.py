"""Pone en Viajes los gastos hechos durante un viaje (sección viajes de categorias.yml).

fetch.py lo llama en cada ejecución, después de las reglas y antes del historial y la IA, sobre todo el histórico
del banco: así un viaje añadido tarde, o con las fechas cambiadas, se corrige solo en el siguiente fetch.

- No toca lo categorizado a mano (category_source = 'manual') ni lo que esté en una categoría de no_cambian
  (cargos que se pagan igual en casa: alquiler, suscripciones...).
- Lo que marca queda con category_source = 'viaje' y el nombre del viaje en la columna viaje. El historial y la
  IA no aprenden de ello: si no, "ALBERT HEIJN" sería Viajes en cualquier fecha.
- Si un viaje desaparece del fichero o cambia de fechas, lo que quede fuera vuelve a estar sin categoría y lo
  categorizan el historial y la IA (la categoría que tenía antes del viaje no se guarda).
"""
CATEGORIA = "Viajes"

MARCAR_SQL = (
    "update transactions set category = %s, category_source = 'viaje', viaje = %s "
    "where source = 'enablebanking' and amount < 0 and not is_transfer and booking_date between %s and %s "
    "and category_source is distinct from 'manual' and coalesce(category, '') <> all(%s::text[]) "
    "and (category_source is distinct from 'viaje' or viaje is distinct from %s) "
    "returning account, booking_date, amount, description"
)


def aplicar_viajes(cur, trips, keep):
    """Devuelve ([(viaje, cuenta, fecha, importe, descripción)] marcados ahora,
    [(cuenta, fecha, importe, descripción)] que han dejado de estar en un viaje).
    trips: ((nombre, desde, hasta), ...); keep: categorías que no cambian (viajes.no_cambian)."""
    cur.execute("select id, viaje, booking_date from transactions where category_source = 'viaje'")
    fuera = [row_id for row_id, viaje, d in cur.fetchall()
             if not any(nombre == viaje and desde <= d <= hasta for nombre, desde, hasta in trips)]
    liberados = []
    if fuera:
        cur.execute(
            "update transactions set category = null, category_source = null, viaje = null where id = any(%s::bigint[]) "
            "returning account, booking_date, amount, description",
            (fuera,),
        )
        liberados = cur.fetchall()
    marcados = []
    for nombre, desde, hasta in trips:
        cur.execute(MARCAR_SQL, (CATEGORIA, nombre, desde, hasta, list(keep), nombre))
        marcados += [(nombre, *row) for row in cur.fetchall()]
    return marcados, liberados
