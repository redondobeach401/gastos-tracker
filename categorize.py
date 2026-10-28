"""Categoriza por historial los gastos del banco que las reglas de categorias.yml no cubren.

Aprende qué palabras de la descripción van casi siempre con la misma categoría en los gastos ya categorizados
(ALDI → Comida) y las busca en los gastos sin categoría. Si no está claro, no decide y el gasto se queda vacío.
Sin IA ni servicios externos: todo sale de la propia base de datos. Lo que pone se marca con
category_source = 'historial', y no aprende de sí mismo (solo de lo categorizado por reglas o a mano).

fetch.py lo llama al final de cada ejecución. A mano (con SUPABASE_DB_URL y SUPABASE_CA_CERT):
  python categorize.py             categoriza todos los gastos del banco sin categoría
  python categorize.py --dry-run   solo muestra qué haría, sin escribir nada (incluye el detalle de cada gasto)
  python categorize.py --verbose   además del resumen, una línea por gasto categorizado
"""
import collections
import re
import sys
import unicodedata
from typing import NamedTuple

MIN_SUPPORT = 2      # veces que la palabra tiene que haber aparecido en gastos con categoría
MIN_PURITY = 0.9     # fracción mínima de esas veces con la misma categoría
# Palabras que no dicen nada del comercio. Las genéricas que se reparten entre categorías (MADRID, ES...)
# ya las descarta MIN_PURITY; estas son las que podrían colarse por repetirse mucho en un mismo tipo de gasto.
STOP = {"WWW", "COM", "COMPRA", "COMPRAS", "TARJETA", "TARJ", "PAGO", "CON", "LOS", "LAS", "DEL", "POR", "PARA"}

HISTORY_SQL = (
    "select coalesce(description, ''), category from transactions "
    "where category is not null and not is_transfer and amount < 0 "
    "and category_source is distinct from 'historial' and category_source is distinct from 'ia' "
    "and category_source is distinct from 'viaje'"
)
PENDING_SQL = (
    "select id, account, booking_date, amount, coalesce(description, '') from transactions "
    "where source = 'enablebanking' and category is null and not is_transfer and amount < 0 "
    "order by booking_date"
)


def words(desc):
    """Palabras útiles de una descripción: sin acentos, en mayúsculas, sin números ni palabras de 1-2 letras."""
    s = unicodedata.normalize("NFKD", desc).encode("ascii", "ignore").decode().upper()
    return {w for w in re.sub(r"[^A-Z ]", " ", s).split() if len(w) >= 3 and w not in STOP}


def train(history, allowed, ignored=()):
    """Cuenta, para cada palabra, en qué categorías ha aparecido. history: [(descripción, categoría)].

    Las palabras de `ignored` (palabras_ignoradas de categorias.yml) no entran en el modelo, así que nunca deciden.
    """
    skip = set().union(*(words(w) for w in ignored))
    model = collections.defaultdict(collections.Counter)
    for desc, category in history:
        if category in allowed:
            for w in words(desc) - skip:
                model[w][category] += 1
    return model


class Evidence(NamedTuple):
    """Lo que el historial sabe de una palabra de la descripción."""
    word: str
    counts: tuple        # ((categoría, veces), ...), de más a menos veces y, si empatan, por nombre
    decisive: bool       # cumple soporte y pureza: por sí sola indicaría su categoría

    @property
    def category(self):
        return self.counts[0][0]

    def __str__(self):
        total = sum(n for _, n in self.counts)
        detalle = ", ".join(f"{c} {n}" for c, n in self.counts)
        return f"{self.word} ({detalle}; {self.counts[0][1]}/{total})"


class Decision(NamedTuple):
    category: str | None  # None: no decide
    reason: str           # explicación legible
    evidence: tuple       # Evidence de cada palabra conocida


def explain(model, desc, min_support=MIN_SUPPORT, min_purity=MIN_PURITY):
    """Decide la categoría de `desc` y explica por qué (qué palabras, con qué recuentos)."""
    evidence = []
    for w in sorted(words(desc)):
        counts = model.get(w)
        if not counts:
            continue
        ordered = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        total = sum(counts.values())
        evidence.append(Evidence(w, ordered, total >= min_support and ordered[0][1] / total >= min_purity))
    decisive = [e for e in evidence if e.decisive]
    categories = sorted({e.category for e in decisive})
    if not evidence:
        return Decision(None, "ninguna palabra aparece en el historial", ())
    if not decisive:
        return Decision(None, f"ninguna palabra es concluyente: {', '.join(map(str, evidence))}", tuple(evidence))
    if len(categories) > 1:
        return Decision(None, f"las palabras indican categorías distintas: {', '.join(map(str, decisive))}",
                        tuple(evidence))
    return Decision(categories[0], f"por {', '.join(map(str, decisive))}", tuple(evidence))


def predict(model, desc, min_support=MIN_SUPPORT, min_purity=MIN_PURITY):
    """Categoría si alguna palabra la indica con claridad; None si ninguna lo hace o si indican categorías distintas."""
    return explain(model, desc, min_support, min_purity).category


def categorize_pending(cur, allowed, ignored=(), dry_run=False):
    """Categoriza los gastos del banco sin categoría.

    Devuelve [(id, cuenta, fecha, importe, descripción, categoría, motivo)] de los que ha categorizado.
    """
    cur.execute(PENDING_SQL)
    pending = cur.fetchall()
    if not pending:  # lo habitual: sin gastos por categorizar no hace falta leer ni entrenar con el historial
        return []
    cur.execute(HISTORY_SQL)
    model = train(cur.fetchall(), set(allowed), ignored)
    done = []
    for row in pending:
        decision = explain(model, row[4])
        if decision.category:
            done.append((*row, decision.category, decision.reason))
    if not dry_run and done:
        cur.executemany(
            "update transactions set category = %s, category_source = 'historial' "
            "where id = %s and category is null",
            [(category, row_id) for row_id, *_, category, _reason in done],
        )
    return done


def main(argv):
    from db import connect
    from rules import load_rules

    dry_run = "--dry-run" in argv
    verbose = "--verbose" in argv or dry_run
    with connect() as conn, conn.cursor() as cur:
        rules = load_rules()
        done = categorize_pending(cur, rules.allowed, rules.ignored, dry_run=dry_run)
        for _, account, d, amount, desc, category, reason in done if verbose else []:
            print(f"{account} {d} {amount} € {desc!r} -> {category} ({reason})")
        verbo = "Se categorizarían" if dry_run else "Categorizados"
        print(f"{verbo} {len(done)} gastos por historial.")


if __name__ == "__main__":
    main(sys.argv[1:])
