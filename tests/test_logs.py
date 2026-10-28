"""Tests de logs.py: anotaciones, grupos plegables y tarjeta de resumen de Actions."""
import logs


def test_notice_y_warning_usan_anotaciones_de_actions(capsys):
    logs.notice("hola")
    logs.warning("ojo")
    assert capsys.readouterr().out == "::notice::hola\n::warning::ojo\n"


def test_group_solo_agrupa_en_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with logs.group("Título"):
        print("dentro")
    assert capsys.readouterr().out == "dentro\n"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with logs.group("Título"):
        print("dentro")
    assert capsys.readouterr().out == "::group::Título\ndentro\n::endgroup::\n"


def test_step_summary_escribe_solo_si_hay_fichero(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    logs.step_summary("nada")  # no falla sin la variable
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    logs.step_summary("uno")
    logs.step_summary("dos")
    assert out.read_text(encoding="utf-8") == "uno\ndos\n"
