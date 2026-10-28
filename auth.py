"""Autoriza un banco con Enable Banking y guarda la sesión (ejecutar EN LOCAL).

Uso:  python auth.py abanca abanca
(1er argumento: etiqueta que verás en la base de datos; 2º: texto para buscar el banco)
"""
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import requests

API = "https://api.enablebanking.com"
APP_ID = os.environ["EB_APP_ID"]
with open(os.environ["EB_KEY_PATH"], "rb") as f:
    KEY = f.read()
REDIRECT = os.environ.get("EB_REDIRECT_URL", "https://localhost/auth_redirect")
VALID_DAYS = int(os.environ.get("EB_VALID_DAYS", "90"))
COUNTRY = os.environ.get("EB_COUNTRY", "ES")


def headers():
    now = int(time.time())
    token = jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
        KEY, algorithm="RS256", headers={"kid": APP_ID},
    )
    return {"Authorization": f"Bearer {token}"}


def check(r):
    if not r.ok:
        sys.exit(f"Error {r.status_code}: {r.text}")
    return r.json()


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    label, search = sys.argv[1], sys.argv[2].lower()

    data = check(requests.get(f"{API}/aspsps", params={"country": COUNTRY}, headers=headers(), timeout=60))
    matches = [a for a in data["aspsps"] if search in a["name"].lower()]
    if len(matches) != 1:
        print("Bancos que coinciden:", [a["name"] for a in matches] or "ninguno")
        sys.exit("Afina el texto de búsqueda hasta que salga solo uno.")
    bank = matches[0]
    print("Banco:", bank["name"])

    valid_until = (datetime.now(UTC) + timedelta(days=VALID_DAYS)).isoformat()
    state = str(uuid.uuid4())
    auth = check(requests.post(f"{API}/auth", headers=headers(), timeout=60, json={
        "access": {"valid_until": valid_until},
        "aspsp": {"name": bank["name"], "country": bank["country"]},
        "state": state,
        "redirect_url": REDIRECT,
        "psu_type": "personal",
    }))
    print("\n1) Abre este enlace en el navegador y autoriza en tu banco:\n")
    print(auth["url"])
    print("\n2) Al acabar, el navegador mostrará un error de conexión: es normal.")
    print("   Copia la URL COMPLETA de la barra de direcciones y pégala aquí.\n")
    redirected = input("URL: ").strip()
    query = parse_qs(urlparse(redirected).query)
    code = query.get("code", [None])[0]
    if not code:
        sys.exit("No encuentro 'code' en esa URL.")
    # Comprueba que la URL pegada es la respuesta a ESTA autorización y no un código ajeno
    if query.get("state", [None])[0] != state:
        sys.exit("El 'state' de esa URL no coincide con el de esta autorización: repite auth.py desde el principio.")

    session = check(requests.post(f"{API}/sessions", json={"code": code}, headers=headers(), timeout=60))
    accounts = [{"uid": a["uid"], "name": a.get("name") or a.get("details") or ""} for a in session["accounts"]]

    path = "sessions.json"
    sessions = {}
    if os.path.exists(path):
        with open(path) as f:
            sessions = json.load(f)
    sessions[label] = {"session_id": session["session_id"], "accounts": accounts, "valid_until": valid_until}
    with open(path, "w") as f:
        json.dump(sessions, f, indent=2)
    print(f"\nGuardado en {path}. Cuentas encontradas en {label}: {len(accounts)}")
    for a in accounts:
        print("  -", a["name"] or a["uid"])
    print("\nCuando hayas autorizado todos los bancos, copia el contenido de sessions.json")
    print("al secret EB_SESSIONS de GitHub.")


if __name__ == "__main__":
    main()
