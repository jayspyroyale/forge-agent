import json

from typer.testing import CliRunner

from forge.cli import app

runner = CliRunner()


def test_tools_list():
    result = runner.invoke(app, ["tools", "list"])
    assert result.exit_code == 0
    for name in ["read_file", "list_files", "search_text", "file_exists", "current_directory"]:
        assert name in result.output


def test_tools_describe_shows_schema():
    result = runner.invoke(app, ["tools", "describe", "read_file"])
    assert result.exit_code == 0
    assert '"path"' in result.output
    assert '"start_line"' in result.output


def test_tools_describe_unknown():
    result = runner.invoke(app, ["tools", "describe", "nope"])
    assert result.exit_code == 1
    assert "No tool named 'nope'" in result.output


def test_tools_run_with_key_value_arguments(workspace_root):
    result = runner.invoke(app, ["tools", "run", "read_file", "path=allowed.txt", "-w", str(workspace_root)])
    assert result.exit_code == 0
    assert "hello from inside" in result.output


def test_tools_run_with_json_arguments(workspace_root):
    arguments = json.dumps({"path": "allowed.txt", "max_lines": 1})
    result = runner.invoke(app, ["tools", "run", "read_file", "--json", arguments, "-w", str(workspace_root)])
    assert result.exit_code == 0
    assert "hello from inside" in result.output


def test_tools_run_outside_workspace_fails(workspace_root):
    result = runner.invoke(app, ["tools", "run", "read_file", "path=../outside.txt", "-w", str(workspace_root)])
    assert result.exit_code == 1
    assert "outside the workspace" in result.output
    assert "secret" not in result.output


def test_tools_run_bad_arguments(workspace_root):
    result = runner.invoke(app, ["tools", "run", "read_file", "not-a-pair", "-w", str(workspace_root)])
    assert result.exit_code == 2
    assert "Expected key=value" in result.output


def test_tools_run_invalid_json(workspace_root):
    result = runner.invoke(app, ["tools", "run", "read_file", "--json", "{oops", "-w", str(workspace_root)])
    assert result.exit_code == 2


def test_tools_run_write_asks_and_respects_no(workspace_root):
    result = runner.invoke(
        app, ["tools", "run", "write_file", "path=new.txt", "content=hi", "-w", str(workspace_root)], input="n\n"
    )
    assert result.exit_code == 1
    assert "Permission needed" in result.output
    assert "denied by the user" in result.output
    assert not (workspace_root / "new.txt").exists()


def test_tools_run_write_with_yes(workspace_root):
    result = runner.invoke(
        app, ["tools", "run", "write_file", "path=new.txt", "content=hi", "-w", str(workspace_root), "--yes"]
    )
    assert result.exit_code == 0
    assert (workspace_root / "new.txt").read_text() == "hi"


def test_tools_run_dangerous_command_denied_even_with_yes(workspace_root):
    result = runner.invoke(
        app, ["tools", "run", "run_command", "command=rm -rf .", "-w", str(workspace_root), "--yes"]
    )
    assert result.exit_code == 1
    assert "dangerous actions are denied by policy" in result.output
    assert (workspace_root / "allowed.txt").exists()
