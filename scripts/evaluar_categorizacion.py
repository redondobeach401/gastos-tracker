"""Banco de pruebas de la categorización por historial (categorize.py) con los datos reales. Solo lee.

Usa SUPABASE_DB_URL y SUPABASE_CA_CERT; mejor con el rol app_backup (solo lectura). Desde la raíz del repo:

  python scripts/evaluar_categorizacion.py                       # validación cruzada: cobertura, aciertos, errores
  python scripts/evaluar_categorizacion.py --ruido 5             # además, ensucia descripciones y mira si cambia
  python scripts/evaluar_categorizacion.py --comparar            # tabla con distintos umbrales
  python scripts/evaluar_categorizacion.py --soporte 3 --pureza 0.8
  python scripts/evaluar_categorizacion.py --explicar "FRUTERIA PEPE MADRID" --explicar "MERCADO"
"""
import argparse
import collections
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import categorize

HISTORY_SQL = (
    "select source, coalesce(description, ''), category from transactions "
    "where category is not null and not is_transfer and amount < 0 "
    "and category_source is distinct from 'historial' and category_source is distinct from 'ia'"
)

# Ruido "neutro": no debería cambiar nunca el resultado (si lo cambia, es un fallo).
# Ruido "realista": añade palabras de verdad (ciudades) que el historial puede conocer; puede cambiarlo.
CIUDADES = ["MADRID", "LEON", "BARCELONA", "AMSTERDAM", "VALENCIA", "LUXEMBOURG"]
ACENTOS = str.maketrans("AEIOUaeiou", "ÁÉÍÓÚáéíóú")


def ruido_neutro(desc, rng):
    cambios = [
        lambda s: f"{s} {rng.randint(1000, 99999999)}",
        lambda s: f"{rng.randint(1000, 9999)} {s}",
        lambda s: f"COMPRA {s}",
        lambda s: f"WWW.{s}.COM",
        lambda s: f"{s} TARJ. {rng.randint(1000, 9999)}",
        lambda s: f"{s} ES",
        lambda s: s.lower(),
        lambda s: s.title(),
        lambda s: s.translate(ACENTOS),
        lambda s: s.replace(" ", "   "),
    ]
    for cambio in rng.sample(cambios, k=rng.randint(1, 3)):
        desc = cambio(desc)
    return desc


def ruido_realista(desc, rng):
    return f"{desc} {rng.choice(CIUDADES)}"


def validacion_cruzada(rows, pliegues, semilla, soporte, pureza, allowed, ignored=()):
    """Cada gasto se predice con un modelo entrenado sin su pliegue. Devuelve [(source, desc, real, Decision)]."""
    indices = list(range(len(rows)))
    random.Random(semilla).shuffle(indices)
    resultados = []
    for p in range(pliegues):
        prueba = set(indices[p::pliegues])
        modelo = categorize.train([(d, c) for i, (_, d, c) in enumerate(rows) if i not in prueba], allowed, ignored)
        for i in sorted(prueba):
            source, desc, real = rows[i]
            resultados.append((source, desc, real, categorize.explain(modelo, desc, soporte, pureza)))
    return resultados


def resumen(resultados):
    total = len(resultados)
    decididos = [r for r in resultados if r[3].category]
    aciertos = sum(r[2] == r[3].category for r in decididos)
    return total, len(decididos), aciertos


def pct(a, b):
    return f"{a / b:.0%}" if b else "-"


def informe(resultados, max_errores):
    total, decididos, aciertos = resumen(resultados)
    print(f"\nTOTAL: {total} gastos | decide en {decididos} ({pct(decididos, total)}) | "
          f"acierta {aciertos} de {decididos} ({pct(aciertos, decididos)})")

    print("\nPor origen:")
    for source in sorted({r[0] for r in resultados}):
        t, d, a = resumen([r for r in resultados if r[0] == source])
        print(f"  {source:14} {t:4} gastos | decide {pct(d, t):>4} | acierta {pct(a, d):>4}")

    print("\nPor categoría real:")
    for cat in sorted({r[2] for r in resultados}):
        t, d, a = resumen([r for r in resultados if r[2] == cat])
        print(f"  {cat:22} {t:4} gastos | decide {pct(d, t):>4} | acierta {pct(a, d):>4}")

    confusiones = collections.Counter((r[2], r[3].category) for r in resultados if r[3].category not in (None, r[2]))
    if confusiones:
        print("\nConfusiones (real -> predicha):")
        for (real, pred), n in confusiones.most_common(10):
            print(f"  {n:3}x  {real} -> {pred}")

    errores = [r for r in resultados if r[3].category not in (None, r[2])]
    if errores:
        print(f"\nErrores (máximo {max_errores}):")
        for _, desc, real, decision in errores[:max_errores]:
            print(f"  {desc[:50]!r:52} real={real!r:22} predicha={decision.category!r}  {decision.reason}")


def estabilidad(rows, modelo, variantes, semilla, soporte, pureza):
    """Compara la predicción de cada descripción con la de sus versiones ensuciadas."""
    rng = random.Random(semilla)
    salida = {}
    for nombre, ensuciar in (("neutro", ruido_neutro), ("realista", ruido_realista)):
        pruebas, cambios = 0, []
        for _, desc, _ in rows:
            original = categorize.predict(modelo, desc, soporte, pureza)
            for _ in range(variantes):
                sucia = ensuciar(desc, rng)
                nueva = categorize.predict(modelo, sucia, soporte, pureza)
                pruebas += 1
                if nueva != original:
                    cambios.append((desc, sucia, original, nueva))
        salida[nombre] = (pruebas, cambios)
    return salida


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--soporte", type=int, default=categorize.MIN_SUPPORT)
    parser.add_argument("--pureza", type=float, default=categorize.MIN_PURITY)
    parser.add_argument("--pliegues", type=int, default=5, help="partes de la validación cruzada (5)")
    parser.add_argument("--semilla", type=int, default=0, help="para repetir exactamente el mismo reparto aleatorio")
    parser.add_argument("--ruido", type=int, default=0, metavar="N", help="versiones ensuciadas por gasto (0: no)")
    parser.add_argument("--comparar", action="store_true", help="tabla con distintos umbrales")
    parser.add_argument("--explicar", action="append", default=[], metavar="TEXTO", help="explica un caso concreto")
    parser.add_argument("--errores", type=int, default=15, help="errores a mostrar (15)")
    parser.add_argument("--sin-ignoradas", action="store_true", help="no aplica palabras_ignoradas (para comparar)")
    args = parser.parse_args(argv)

    from db import connect
    from rules import load_rules

    rules = load_rules()
    allowed = set(rules.allowed)
    ignored = () if args.sin_ignoradas else rules.ignored
    with connect() as conn:
        conn.read_only = True
        rows = conn.execute(HISTORY_SQL).fetchall()
    modelo = categorize.train([(d, c) for _, d, c in rows], allowed, ignored)
    print(f"Historial: {len(rows)} gastos categorizados | umbrales: soporte {args.soporte}, pureza {args.pureza} | "
          f"palabras ignoradas: {len(ignored)}")

    if args.explicar:
        for texto in args.explicar:
            decision = categorize.explain(modelo, texto, args.soporte, args.pureza)
            print(f"\n{texto!r} -> {decision.category or 'sin categoría'}\n  {decision.reason}")
        return

    if args.comparar:
        print(f"\n{'soporte':>7} {'pureza':>6} | {'decide':>6} {'acierta':>7}")
        for soporte in (1, 2, 3, 4):
            for pureza in (0.6, 0.7, 0.8, 0.9, 1.0):
                res = validacion_cruzada(rows, args.pliegues, args.semilla, soporte, pureza, allowed, ignored)
                total, decididos, aciertos = resumen(res)
                print(f"{soporte:>7} {pureza:>6} | {pct(decididos, total):>6} {pct(aciertos, decididos):>7}")
        return

    informe(validacion_cruzada(rows, args.pliegues, args.semilla, args.soporte, args.pureza, allowed, ignored),
            args.errores)

    if args.ruido:
        for nombre, (pruebas, cambios) in estabilidad(
            rows, modelo, args.ruido, args.semilla, args.soporte, args.pureza
        ).items():
            print(f"\nRuido {nombre}: {pruebas} variantes, cambia el resultado en {len(cambios)} ({pct(len(cambios), pruebas)})")
            for desc, sucia, antes, despues in cambios[: args.errores]:
                print(f"  {desc[:30]!r:32} -> {sucia[:40]!r:42} {antes!r} -> {despues!r}")


if __name__ == "__main__":
    main()
