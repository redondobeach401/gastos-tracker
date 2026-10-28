"""Carga y valida categorias.yml (reglas de categorización y traspasos)."""
import difflib
import itertools
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import yaml

DEFAULT_PATH = Path(__file__).with_name("categorias.yml")


class ReglasInvalidas(ValueError):
    """categorias.yml está mal escrito; el mensaje dice dónde."""


class Rules(NamedTuple):
    transfer_re: re.Pattern          # descripción de un traspaso entre cuentas propias
    transfer_exact: tuple            # (descripción exacta, importe absoluto) que también son traspasos
    categories: tuple                # (patrón compilado, categoría), por orden: la primera que coincida gana
    examples: tuple                  # (descripción de ejemplo, categoría esperada), para los tests
    allowed: tuple                   # categorías permitidas (categorias_permitidas)
    ignored: tuple = ()              # palabras que el historial no usa para decidir (palabras_ignoradas)
    descriptions: tuple = ()         # (categoría, qué significa) para el prompt de la IA (descripciones)
    trips: tuple = ()                # (nombre, desde, hasta) de cada viaje (viajes.fechas)
    trip_keep: tuple = ()            # categorías que no pasan a Viajes durante un viaje (viajes.no_cambian)


def _compile(pattern, where):
    if not isinstance(pattern, str) or not pattern.strip():
        raise ReglasInvalidas(f"{where}: el patrón debe ser un texto no vacío, no {pattern!r}")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ReglasInvalidas(f"{where}: expresión regular no válida {pattern!r}: {e}") from e


def _mapping(value, where, required, optional=()):
    """Comprueba que `value` es un diccionario con las claves esperadas (así un error de tecleo no pasa en silencio)."""
    if not isinstance(value, dict):
        raise ReglasInvalidas(f"{where}: debe ser un diccionario, no {type(value).__name__}")
    missing = [k for k in required if k not in value]
    unknown = [k for k in value if k not in (*required, *optional)]
    if missing:
        raise ReglasInvalidas(f"{where}: falta {', '.join(missing)}")
    if unknown:
        permitidas = ", ".join((*required, *optional))
        raise ReglasInvalidas(f"{where}: clave desconocida {', '.join(map(str, unknown))} (permitidas: {permitidas})")
    return value


def _list(value, where):
    if not isinstance(value, list) or not value:
        raise ReglasInvalidas(f"{where}: debe ser una lista con al menos un elemento")
    return value


def load_rules(path=DEFAULT_PATH):
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ReglasInvalidas(f"categorias.yml no es un YAML válido: {e}") from e
    data = _mapping(data, "categorias.yml", required=("traspasos", "categorias", "categorias_permitidas"),
                    optional=("palabras_ignoradas", "descripciones", "viajes"))

    ignoradas = data.get("palabras_ignoradas") or []
    if not isinstance(ignoradas, list) or not all(isinstance(w, str) and w.strip() for w in ignoradas):
        raise ReglasInvalidas("palabras_ignoradas: debe ser una lista de textos no vacíos")
    repetidas_ign = sorted({w for w in ignoradas if [x.casefold() for x in ignoradas].count(w.casefold()) > 1})
    if repetidas_ign:
        raise ReglasInvalidas(f"palabras_ignoradas: repetidas {', '.join(repetidas_ign)}")

    permitidas = _list(data["categorias_permitidas"], "categorias_permitidas")
    if not all(isinstance(c, str) and c.strip() for c in permitidas):
        raise ReglasInvalidas("categorias_permitidas: todos los elementos deben ser textos no vacíos")
    repetidas = sorted({c for c in permitidas if permitidas.count(c) > 1})
    if repetidas:
        raise ReglasInvalidas(f"categorias_permitidas: repetidas {', '.join(repetidas)}")

    descripciones = data.get("descripciones") or {}
    if not isinstance(descripciones, dict):
        raise ReglasInvalidas(f"descripciones: debe ser un diccionario categoría: texto, no {type(descripciones).__name__}")
    for cat, texto in descripciones.items():
        if cat not in permitidas:
            cercana = difflib.get_close_matches(str(cat), permitidas, n=1)
            ayuda = f"¿Quisiste decir {cercana[0]!r}?" if cercana else "Añádela a categorias_permitidas."
            raise ReglasInvalidas(f"descripciones: la categoría {cat!r} no está en categorias_permitidas. {ayuda}")
        if not isinstance(texto, str) or not texto.strip():
            raise ReglasInvalidas(f"descripciones.{cat}: debe ser un texto no vacío")

    trips, trip_keep = _viajes(data.get("viajes"), permitidas)

    traspasos = _mapping(data["traspasos"], "traspasos", required=("patrones",), optional=("exactos",))
    patrones = _list(traspasos["patrones"], "traspasos.patrones")
    for i, p in enumerate(patrones, 1):
        _compile(p, f"traspasos.patrones[{i}]")
    transfer_re = re.compile("|".join(f"(?:{p})" for p in patrones), re.IGNORECASE)

    exact = []
    for i, e in enumerate(traspasos.get("exactos") or [], 1):
        where = f"traspasos.exactos[{i}]"
        e = _mapping(e, where, required=("descripcion", "importe"))
        if not isinstance(e["descripcion"], str):
            raise ReglasInvalidas(f"{where}: descripcion debe ser un texto")
        if isinstance(e["importe"], bool) or not isinstance(e["importe"], int | float):
            raise ReglasInvalidas(f"{where}: importe debe ser un número, no {e['importe']!r}")
        exact.append((e["descripcion"], abs(Decimal(str(e["importe"])))))

    categories, examples = [], []
    for i, r in enumerate(_list(data["categorias"], "categorias"), 1):
        where = f"categorias[{i}]"
        r = _mapping(r, where, required=("categoria", "patron"), optional=("ejemplos",))
        if not isinstance(r["categoria"], str) or not r["categoria"].strip():
            raise ReglasInvalidas(f"{where}: categoria debe ser un texto no vacío")
        if r["categoria"] not in permitidas:
            cercana = difflib.get_close_matches(r["categoria"], permitidas, n=1)
            ayuda = f"¿Quisiste decir {cercana[0]!r}?" if cercana else "Si es nueva, añádela a categorias_permitidas."
            raise ReglasInvalidas(f"{where}: la categoría {r['categoria']!r} no está en categorias_permitidas. {ayuda}")
        categories.append((_compile(r["patron"], f"{where} ({r['categoria']})"), r["categoria"]))
        ejemplos = r.get("ejemplos") or []
        if not isinstance(ejemplos, list) or not all(isinstance(x, str) for x in ejemplos):
            raise ReglasInvalidas(f"{where} ({r['categoria']}): ejemplos debe ser una lista de textos")
        examples += [(x, r["categoria"]) for x in ejemplos]

    return Rules(transfer_re, tuple(exact), tuple(categories), tuple(examples), tuple(permitidas), tuple(ignoradas),
                 tuple((c, " ".join(t.split())) for c, t in descripciones.items()), trips, trip_keep)


def _viajes(viajes, permitidas):
    """Valida la sección viajes: devuelve ((nombre, desde, hasta), ...) por fecha y las categorías de no_cambian."""
    if viajes is None:
        return (), ()
    viajes = _mapping(viajes, "viajes", required=("fechas",), optional=("no_cambian",))
    if "Viajes" not in permitidas:
        raise ReglasInvalidas("viajes: la categoría 'Viajes' debe estar en categorias_permitidas")
    no_cambian = viajes.get("no_cambian") or []
    if not isinstance(no_cambian, list):
        raise ReglasInvalidas("viajes.no_cambian: debe ser una lista de categorías")
    for cat in no_cambian:
        if cat not in permitidas or cat == "Viajes":
            cercana = difflib.get_close_matches(str(cat), permitidas, n=1)
            ayuda = f" ¿Quisiste decir {cercana[0]!r}?" if cercana and cat != "Viajes" else ""
            raise ReglasInvalidas(f"viajes.no_cambian: {cat!r} no es una categoría permitida distinta de Viajes.{ayuda}")
    trips = []
    for i, v in enumerate(_list(viajes["fechas"], "viajes.fechas"), 1):
        where = f"viajes.fechas[{i}]"
        v = _mapping(v, where, required=("nombre", "desde", "hasta"))
        if not isinstance(v["nombre"], str) or not v["nombre"].strip():
            raise ReglasInvalidas(f"{where}: nombre debe ser un texto no vacío")
        where = f"{where} ({v['nombre']})"
        for k in ("desde", "hasta"):  # YAML convierte 2026-10-09 (sin comillas) en fecha
            if not isinstance(v[k], date) or isinstance(v[k], datetime):
                raise ReglasInvalidas(f"{where}: {k} debe ser una fecha AAAA-MM-DD sin comillas, no {v[k]!r}")
        if v["desde"] > v["hasta"]:
            raise ReglasInvalidas(f"{where}: desde ({v['desde']}) es posterior a hasta ({v['hasta']})")
        trips.append((v["nombre"].strip(), v["desde"], v["hasta"]))
    nombres = [n for n, _, _ in trips]
    repetidos = sorted({n for n in nombres if nombres.count(n) > 1})
    if repetidos:
        raise ReglasInvalidas(f"viajes.fechas: nombres repetidos {', '.join(repetidos)}")
    trips.sort(key=lambda t: t[1])
    for (n1, _, h1), (n2, d2, _) in itertools.pairwise(trips):
        if d2 <= h1:
            raise ReglasInvalidas(f"viajes.fechas: {n1} y {n2} se solapan")
    return tuple(trips), tuple(no_cambian)
