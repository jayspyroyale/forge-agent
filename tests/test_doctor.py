from typer.testing import CliRunner

from forge import doctor
from forge.cli import app


def test_all_checks_pass_in_normal_environment():
    results = doctor.run_all_checks()
    assert results
    assert all(result.passed for result in results), results


def test_git_check_fails_when_git_is_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor.check_git()
    assert result.passed is False


def test_workspace_check_fails_for_missing_directory(tmp_path):
    result = doctor.check_workspace(tmp_path / "does-not-exist")
    assert result.passed is False


def test_doctor_command_exits_with_error_when_a_check_fails(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Some checks failed" in result.output
