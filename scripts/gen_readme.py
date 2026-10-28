"""Regenera las tablas del README que salen de otros ficheros del repo.

- <!-- hooks:start/end -->   hooks de .pre-commit-config.yaml
- <!-- secrets:start/end --> secrets usados en el workflow

Solo toca lo que hay entre los marcadores. Si algo cambia, reescribe el README y
pre-commit cancela el commit para que hagas `git add README.md` y repitas.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PRECOMMIT = ROOT / ".pre-commit-config.yaml"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# Descripción de cada hook / secret. Si falta alguno, el script falla para que lo documentes.
HOOKS = {
    "trailing-whitespace": "Quita espacios sobrantes al final de las líneas",
    "end-of-file-fixer": "Asegura el salto de línea final",
    "check-yaml": "Valida que los `.yml` (incluido el workflow) sean correctos",
    "check-merge-conflict": "Detecta marcas de conflicto sin resolver",
    "check-added-large-files": "Evita subir ficheros grandes por accidente",
    "detect-private-key": "Bloquea claves privadas (`.pem`)",
    "gitleaks": "Bloquea tokens y claves de formatos conocidos (AWS, GitHub…)",
    "no-db-url-password": (
        "Bloquea URLs de base de datos (postgresql, mysql, mongodb) que incluyan usuario y contraseña. "
        "Hook propio, porque `gitleaks` no las detecta"
    ),
    "ruff": "Revisa el código Python y corrige lo que puede (`ruff.toml`)",
    "genera-readme": "Regenera las tablas de hooks y secrets de este README",
}
SECRETS = {
    "EB_APP_ID": "ID de la aplicación en Enable Banking",
    "EB_PRIVATE_KEY": "Contenido del `.pem`",
    "EB_SESSIONS": "Contenido de `sessions.json`",
    "SUPABASE_DB_URL": "Cadena de conexión Postgres del rol `app_fetcher` (permisos mínimos sobre `transactions`)",
    "SUPABASE_CA_CERT": "Certificado de la CA de Supabase (contenido del `.crt`), para conectar con `verify-full`",
    "SUPABASE_BACKUP_DB_URL": "Cadena de conexión Postgres del rol `app_backup` (solo lectura), para las copias de seguridad",
    "BACKUP_PASSPHRASE": "Contraseña con la que se cifra la copia de seguridad semanal (guárdala también fuera de GitHub)",
    "ANTHROPIC_API_KEY": "Clave de la API de Anthropic para categorizar con IA (`ia.py`). Opcional: sin ella no hay categorización con IA",
    "TELEGRAM_BOT_TOKEN": "Token del bot de Telegram (@BotFather) para los avisos. Opcional: sin él solo se abre el issue",
    "TELEGRAM_CHAT_ID": "ID del chat al que el bot envía los avisos. Opcional, va junto al token",
}


def table(headers, rows):
    lines = [f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}"]
    lines += [f"| {' | '.join(row)} |" for row in rows]
    return "\n".join(lines)


def describe(names, descriptions, kind):
    missing = [n for n in names if n not in descriptions]
    if missing:
        sys.exit(f"gen_readme.py: falta la descripción de {kind}: {', '.join(missing)} (añádela en el script)")
    return [descriptions[n] for n in names]


def hooks_table():
    ids = list(dict.fromkeys(re.findall(r"^\s*-\s*id:\s*(\S+)", PRECOMMIT.read_text(encoding="utf-8"), re.MULTILINE)))
    return table(["Hook", "Qué hace"], [(f"`{i}`", d) for i, d in zip(ids, describe(ids, HOOKS, "hooks"))])


def secrets_table():
    text = "\n".join(w.read_text(encoding="utf-8") for w in WORKFLOWS)
    names = list(dict.fromkeys(re.findall(r"secrets\.(\w+)", text)))
    return table(["Secret", "Contenido"], [(f"`{n}`", d) for n, d in zip(names, describe(names, SECRETS, "secrets"))])


def replace_block(text, name, content):
    pattern = re.compile(rf"(<!-- {name}:start -->).*?(<!-- {name}:end -->)", re.DOTALL)
    if not pattern.search(text):
        sys.exit(f"gen_readme.py: faltan los marcadores <!-- {name}:start --> / <!-- {name}:end --> en README.md")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(2)}", text)


def main():
    old = README.read_text(encoding="utf-8")
    new = replace_block(old, "hooks", hooks_table())
    new = replace_block(new, "secrets", secrets_table())
    if new != old:
        README.write_text(new, encoding="utf-8", newline="\n")
        print("README.md regenerado")


if __name__ == "__main__":
    main()
