"""Salida de log común: anotaciones de GitHub Actions (::notice::, ::warning:: y ::error::), grupos plegables y resumen."""
import contextlib
import os


def notice(msg):
    print(f"::notice::{msg}")


def warning(msg):
    print(f"::warning::{msg}")


def error(msg):
    print(f"::error::{msg}")


def step_summary(markdown):
    """Añade markdown a la tarjeta de resumen de la ejecución en Actions; en local no hace nada."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


@contextlib.contextmanager
def group(title):
    """Agrupa líneas bajo un título plegable en el log de Actions; en local no añade nada."""
    en_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if en_actions:
        print(f"::group::{title}")
    try:
        yield
    finally:
        if en_actions:
            print("::endgroup::")
