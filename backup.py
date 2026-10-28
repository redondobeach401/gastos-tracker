"""Exporta las tablas transactions y saldos_iniciales a CSV (todas las columnas y filas, con cabecera).

Uso:  python backup.py transactions.csv saldos_iniciales.csv

saldos_iniciales se rellena a mano y no se puede regenerar desde el banco, así que va en la copia.

Restaurar: aplicar schema.sql (el esquema no va en los CSV) y cargarlos con
  \\copy transactions from 'transactions.csv' with (format csv, header)
  \\copy saldos_iniciales from 'saldos_iniciales.csv' with (format csv, header)
"""
import sys

from db import connect

TABLAS = ("transactions", "saldos_iniciales")


def main(*paths):
    with connect() as conn, conn.cursor() as cur:
        for tabla, path in zip(TABLAS, paths, strict=True):
            with open(path, "wb") as f, cur.copy(f"copy {tabla} to stdout with (format csv, header)") as copy:
                f.writelines(copy)
            cur.execute(f"select count(*) from {tabla}")
            print(f"{cur.fetchone()[0]} filas de {tabla} exportadas a {path}")


if __name__ == "__main__":
    if len(sys.argv) != len(TABLAS) + 1:
        sys.exit(__doc__)
    main(*sys.argv[1:])
