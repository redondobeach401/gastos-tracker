"""Conexión a Postgres (Supabase) compartida por fetch.py y backup.py."""
import os
import tempfile

import psycopg

STATEMENT_TIMEOUT_MS = 120_000   # 2 min: de sobra para este volumen de datos


def connect():
    """Conexión verificando el certificado del servidor (verify-full)."""
    # strip(): al pegar un secret en GitHub suele colarse un salto de línea final (acaba en el nombre de la base)
    url = os.environ["SUPABASE_DB_URL"].strip()
    with tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
        f.write(os.environ["SUPABASE_CA_CERT"].strip() + "\n")  # contenido del .crt de la CA de Supabase
    try:
        conn = psycopg.connect(url, sslmode="verify-full", sslrootcert=f.name, connect_timeout=10)
    finally:
        os.unlink(f.name)  # libpq ya leyó el certificado al conectar
    # Corta una consulta atascada en vez de dejar colgada la ejecución. Con `set` y commit (no por el parámetro
    # `options` de la conexión, que los poolers como el de Supabase pueden rechazar) y antes de usar la conexión.
    conn.execute(f"set statement_timeout = {STATEMENT_TIMEOUT_MS}")
    conn.commit()
    return conn
