"""Tests de db.connect sin base de datos: comprueban qué recibe psycopg."""
import db


class FalsaConexion:
    def __init__(self):
        self.pasos = []

    def execute(self, sql):
        self.pasos.append(sql)

    def commit(self):
        self.pasos.append("commit")


def test_connect_ignora_saltos_de_linea_y_espacios_al_pegar_los_secrets(monkeypatch):
    llamadas = []

    def falso_connect(url, **kwargs):
        with open(kwargs["sslrootcert"], encoding="utf-8") as f:
            llamadas.append((url, kwargs, f.read()))
        return FalsaConexion()

    monkeypatch.setattr(db.psycopg, "connect", falso_connect)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u@host:5432/postgres\n")
    monkeypatch.setenv("SUPABASE_CA_CERT", "\n-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n\n")

    conn = db.connect()
    url, kwargs, cert = llamadas[0]
    assert url == "postgresql://u@host:5432/postgres"
    assert kwargs["sslmode"] == "verify-full"
    assert cert == "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
    assert conn.pasos == ["set statement_timeout = 120000", "commit"]  # el timeout queda confirmado antes de usarla
