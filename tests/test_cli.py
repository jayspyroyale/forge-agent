from typer.testing import CliRunner

from forge import __version__
from forge.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output
    assert "--version" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_succeeds():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Forge Doctor" in result.output
    assert "Everything looks good." in result.output
