"""Categoriza con Claude Haiku los gastos del banco que ni las reglas ni el historial han cubierto.

Es la tercera capa (reglas → historial → IA). Recibe la descripción y el importe de cada gasto pendiente, las
categorías permitidas y unos ejemplos reales de tu historial (ahí está tu criterio: taxi → Ocio...). Si no está
seguro, responde NO_SE y el gasto se queda vacío; entonces propone además el nombre de una categoría nueva que
encajaría, y el log las resume para que decidas si crearlas en categorias.yml. Lo que pone se marca con category_source = 'ia' para poder
revisarlo en Looker, y no aprende de sí mismo (solo de lo categorizado por reglas o a mano).

Nunca rompe el fetch: sin ANTHROPIC_API_KEY, o si la API falla, no hace nada y avisa.

fetch.py lo llama al final de cada ejecución. A mano (con SUPABASE_DB_URL, SUPABASE_CA_CERT y ANTHROPIC_API_KEY):
  python ia.py             categoriza los gastos pendientes
  python ia.py --dry-run   solo muestra qué haría, sin escribir nada (incluye el detalle de cada gasto)
  python ia.py --verbose   además del resumen, una línea por gasto categorizado
"""
import collections
import os
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import logs
from categorize import PENDING_SQL

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-5-5"
NO_SE = "NO_SE"
MAX_PENDING = 50       # gastos por ejecución: acota el coste si se acumulan muchos
EXAMPLES_PER_CATEGORY = 6
MIN_PARA_ALERTA = 4    # con menos pendientes, que la IA no clasifique la mayoría no dice nada
UMBRAL_SIN_RESULTADO = 0.5

EXAMPLES_SQL = (
    "select category, description from ("
    "  select category, description, row_number() over (partition by category order by max(booking_date) desc) n "
    "  from transactions "
    "  where category is not null and not is_transfer and amount < 0 and coalesce(description, '') <> '' "
    "  and category_source is distinct from 'historial' and category_source is distinct from 'ia' "
    "  and category_source is distinct from 'viaje' "
    "  group by category, description"
    ") t where n <= %s order by category"
)

SYSTEM = (
    "Clasificas gastos bancarios de una persona en España en sus categorías personales. "
    "Usa los ejemplos para imitar su criterio. Si no puedes decidir con razonable seguridad, responde "
    f"{NO_SE}: es mejor no clasificar que equivocarse. En ese caso, indica en categoria_nueva el nombre corto de "
    "una categoría que no existe y encajaría (una sola palabra o dos, como Salud); déjalo vacío si ninguna."
)
def make_session():
    """Sesión con reintentos y espera creciente ante errores temporales de la API (429, 5xx y 529: sobrecargada).

    raise_on_status=False: si se agotan, la última respuesta llega tal cual y raise_for_status da el mensaje de la API.
    """
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504, 529), allowed_methods=("POST",),
                  raise_on_status=False)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


SESSION = make_session()
MAX_NUEVA = 30        # longitud máxima del nombre propuesto: es texto del modelo y acaba en el log


def build_tool(allowed):
    return {
        "name": "clasificar",
        "description": "Devuelve la categoría de cada gasto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resultados": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "categoria": {"enum": [*allowed, NO_SE]},
                            "categoria_nueva": {"type": "string", "description": "Solo con NO_SE; vacío si ninguna."},
                        },
                        "required": ["id", "categoria"],
                    },
                }
            },
            "required": ["resultados"],
        },
    }


def build_prompt(pending, examples, descripciones=()):
    por_categoria = {}
    for category, desc in examples:
        por_categoria.setdefault(category, []).append(desc)
    ejemplos = "\n".join(f"- {c}: " + " | ".join(ds) for c, ds in por_categoria.items())
    gastos = "\n".join(f"{row_id}: {desc!r} ({amount} €)" for row_id, _acc, _d, amount, desc in pending)
    # Una categoría nueva no tiene ejemplos y, sin esto, la IA solo ve su nombre y responde NO_SE.
    significados = "".join(f"- {c}: {texto}\n" for c, texto in descripciones)
    significados = f"Qué significa cada categoría (úsalo sobre todo si aún no tiene ejemplos):\n{significados}\n" if significados else ""
    return f"Ejemplos de gastos ya clasificados por categoría:\n{ejemplos}\n\n{significados}Clasifica estos gastos:\n{gastos}"


def describe_error(e):
    """Causa legible de un fallo: para errores HTTP, el estado y el mensaje de la API (nunca cabeceras ni clave)."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        try:
            detalle = e.response.json()["error"]
            return f"HTTP {e.response.status_code} {detalle.get('type', '')}: {detalle.get('message', '')}".strip()
        except (ValueError, KeyError, TypeError, AttributeError):
            return f"HTTP {e.response.status_code}"
    return repr(e)


def suggest(pending, allowed, examples, api_key, stats=None, descripciones=()):
    """Pide a Claude la categoría de cada gasto.

    Devuelve ({id: categoría}, {id: categoría nueva propuesta}). Los NO_SE y lo no válido no salen en el primero;
    el segundo solo trae los NO_SE con una propuesta que no sea ya una categoría existente.
    Si se pasa stats (un dict), lo rellena con tokens_in, tokens_out, no_se, invalidos y sin_respuesta.
    """
    stats = stats if stats is not None else {}
    usable = [c for c in allowed if c != "Ingresos"]  # Ingresos es solo para importes positivos
    resp = SESSION.post(
        API_URL,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        json={
            "model": MODEL,
            "max_tokens": 2048,
            "system": SYSTEM,
            "tools": [build_tool(usable)],
            "tool_choice": {"type": "tool", "name": "clasificar"},
            "messages": [{"role": "user", "content": build_prompt(pending, examples, descripciones)}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    ids = {row[0] for row in pending}
    body = resp.json()
    usage = body.get("usage") or {}
    stats.update(tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0),
                 no_se=0, invalidos=0)
    result, nuevas, respondidos = {}, {}, set()
    for block in body.get("content", []):
        if block.get("type") == "tool_use":
            for r in block["input"].get("resultados", []):
                if r.get("id") not in ids:
                    continue
                respondidos.add(r["id"])
                if r.get("categoria") in usable:  # descarta NO_SE e inventos
                    result[r["id"]] = r["categoria"]
                elif r.get("categoria") == NO_SE:
                    stats["no_se"] += 1
                    nueva = " ".join(str(r.get("categoria_nueva") or "").split())[:MAX_NUEVA]
                    if nueva and nueva.casefold() not in {c.casefold() for c in allowed}:
                        nuevas[r["id"]] = nueva
                else:
                    stats["invalidos"] += 1
    stats["sin_respuesta"] = len(ids - respondidos)
    return result, nuevas


def resumen_nuevas(sin_categoria):
    """Líneas de log con las categorías que la IA recomienda crear. sin_categoria: [(descripción, propuesta)]."""
    por_categoria = collections.defaultdict(list)
    for desc, nueva in sin_categoria:
        por_categoria[nueva].append(desc)
    return [
        f"La IA no ha podido categorizar {len(descs)} gasto(s); recomienda crear la categoría {nueva!r} "
        f"(categorias_permitidas en categorias.yml): {', '.join(repr(d) for d in descs)}"
        for nueva, descs in sorted(por_categoria.items(), key=lambda kv: -len(kv[1]))
    ]


def categorize_pending_ia(cur, allowed, dry_run=False, api_key=None, alerta=None, uso=None, descripciones=()):
    """Categoriza con IA los gastos del banco sin categoría.

    Devuelve ([(id, cuenta, fecha, importe, descripción, categoría)] de los que ha categorizado,
    [(descripción, categoría nueva recomendada)] de los que no ha podido). Si falta la clave o la API falla,
    devuelve ([], []): el fetch no debe romperse por esto.
    alerta: función a la que se manda el aviso de calidad (por defecto, solo el log); fetch pasa la suya para
    que además salga como issue. uso: dict que se rellena con los tokens y segundos de la llamada.
    descripciones: [(categoría, qué significa)] de categorias.yml, para las categorías sin ejemplos en el historial.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logs.notice("IA omitida: falta ANTHROPIC_API_KEY (el secret está vacío o no existe)")
        return [], []
    cur.execute(PENDING_SQL)
    todos = cur.fetchall()
    pending = todos[:MAX_PENDING]
    if not pending:
        print("IA: sin gastos pendientes de categorizar, no se llama a la API")
        return [], []
    cur.execute(EXAMPLES_SQL, (EXAMPLES_PER_CATEGORY,))
    stats = {}
    print(f"{'[dry-run] ' if dry_run else ''}IA: llamando a {MODEL} con {len(pending)} gasto(s) pendientes: "
          + ", ".join(f"{row[4]!r}" for row in pending[:5]) + (", ..." if len(pending) > 5 else ""))
    inicio = time.monotonic()
    try:
        suggestions, nuevas = suggest(pending, allowed, cur.fetchall(), api_key, stats, descripciones)
    except (requests.RequestException, KeyError, ValueError) as e:
        logs.warning(f"IA: la llamada a la API ha fallado y {len(pending)} gasto(s) siguen sin categoría: "
                     f"{describe_error(e)}")
        return [], []
    segundos = time.monotonic() - inicio
    if uso is not None:
        uso.update(tokens_in=stats["tokens_in"], tokens_out=stats["tokens_out"], segundos=segundos)
    done = [(*row[:5], suggestions[row[0]]) for row in pending if row[0] in suggestions]
    recorte = f" (de {len(todos)}; límite {MAX_PENDING})" if len(todos) > len(pending) else ""
    print(
        f"{'[dry-run] ' if dry_run else ''}IA: {len(pending)} pendientes{recorte} → {len(done)} categorizados, {stats['no_se']} NO_SE, "
        f"{stats['invalidos']} descartados por categoría inválida, {stats['sin_respuesta']} sin respuesta · "
        f"{stats['tokens_in']} tok in / {stats['tokens_out']} out · {segundos:.1f} s"
    )
    for row in pending:
        if row[0] not in suggestions:
            logs.notice(f"IA: sin categoría para {row[4]!r} ({row[3]} €): NO_SE, categoría inválida o sin respuesta")
    sin_resultado = len(pending) - len(done)
    if len(pending) >= MIN_PARA_ALERTA and sin_resultado > len(pending) * UMBRAL_SIN_RESULTADO:
        (alerta or logs.warning)(
            f"La IA solo ha categorizado {len(done)} de {len(pending)} gastos: revisa el modelo ({MODEL}) o el prompt."
        )
    if not dry_run and done:
        cur.executemany(
            "update transactions set category = %s, category_source = 'ia' where id = %s and category is null",
            [(row[-1], row[0]) for row in done],
        )
    sin_categoria = [(row[4], nuevas[row[0]]) for row in pending if row[0] in nuevas]
    return done, sin_categoria


def main(argv):
    from db import connect
    from rules import load_rules

    dry_run = "--dry-run" in argv
    verbose = "--verbose" in argv or dry_run
    with connect() as conn, conn.cursor() as cur:
        rules = load_rules()
        done, sin_categoria = categorize_pending_ia(cur, rules.allowed, dry_run=dry_run, descripciones=rules.descriptions)
        for _, account, d, amount, desc, category in done if verbose else []:
            print(f"{account} {d} {amount} € {desc!r} -> {category}")
        for line in resumen_nuevas(sin_categoria):
            print(line)
        verbo = "Se categorizarían" if dry_run else "Categorizados"
        print(f"{verbo} {len(done)} gastos con IA.")


if __name__ == "__main__":
    main(sys.argv[1:])
