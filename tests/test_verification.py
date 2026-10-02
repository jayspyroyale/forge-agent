import json
import sys

from forge.security.permissions import ApprovalChoice, PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.verification.checks import VerificationCheck
from forge.verification.detect import detect_checks
from forge.verification.runner import Verifier
from forge.workspace import Workspace

PYTHON = f'"{sys.executable}"'

# --- Detection --------------------------------------------------------------------


def kinds_and_names(checks):
    return [(check.kind, check.name) for check in checks]


def test_empty_project_has_no_checks(tmp_path):
    assert detect_checks(tmp_path) == []


def test_python_tests_directory(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x(): pass\n")
    checks = detect_checks(tmp_path)
    assert kinds_and_names(checks) == [("test", "pytest")]
    assert checks[0].command.endswith("-m pytest -q")
    assert "tests" in checks[0].reason


def test_pytest_from_pyproject_with_ruff_and_mypy(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n[tool.mypy]\n")
    assert kinds_and_names(detect_checks(tmp_path)) == [("test", "pytest"), ("typecheck", "mypy"), ("lint", "ruff")]


def test_python_files_without_tests_have_no_checks(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_checks(tmp_path) == []


def test_project_virtualenv_is_preferred(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    scripts = tmp_path / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if sys.platform == "win32" else "python")).write_text("")
    assert ".venv" in detect_checks(tmp_path)[0].command


def test_node_scripts_and_package_manager(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "lint": "eslint .", "build": "tsc"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    checks = detect_checks(tmp_path)
    assert [(c.kind, c.command) for c in checks] == [
        ("test", "pnpm test"),
        ("lint", "pnpm run lint"),
        ("build", "pnpm run build"),
    ]


def test_npm_placeholder_test_script_is_ignored(tmp_path):
    placeholder = 'echo "Error: no test specified" && exit 1'
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": placeholder}}))
    assert detect_checks(tmp_path) == []


def test_rust_and_go(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    (tmp_path / "go.mod").write_text("module x\n")
    assert [(c.kind, c.command) for c in detect_checks(tmp_path)] == [
        ("test", "cargo test"),
        ("build", "cargo build"),
        ("test", "go test ./..."),
        ("lint", "go vet ./..."),
    ]


def test_broken_config_files_are_ignored(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is not toml [")
    (tmp_path / "package.json").write_text("{not json")
    assert detect_checks(tmp_path) == []


# --- Running checks -----------------------------------------------------------------


def check(code, kind="test", name="check"):
    return VerificationCheck(name=name, kind=kind, command=f'{PYTHON} -c "{code}"', reason="test")


def permissive(tmp_path, *checks):
    return Verifier(Workspace(tmp_path), checks, PermissionEngine(PermissionPolicy.permissive()))


def test_passing_check(tmp_path):
    result = permissive(tmp_path).run_check(check("print('all good')"))
    assert result.status == "passed"
    assert result.passed and not result.failing
    assert result.exit_code == 0
    assert "all good" in result.summary


def test_failing_check_keeps_the_end_of_the_output(tmp_path):
    code = "import sys; print('\\n'.join(f'line {n}' for n in range(100))); sys.exit(1)"
    result = permissive(tmp_path).run_check(check(code))
    assert result.status == "failed"
    assert result.failing
    assert result.exit_code == 1
    assert "line 99" in result.summary
    assert "line 0\n" not in result.summary


def test_check_needs_permission(tmp_path):
    verifier = Verifier(Workspace(tmp_path), [], PermissionEngine())  # default policy, no approver
    result = verifier.run_check(check("print('x')"))
    assert result.status == "skipped"
    assert not result.failing
    assert "Not run" in result.summary


def test_check_session_approval(tmp_path):
    answers = [ApprovalChoice.ALLOW_SESSION]
    engine = PermissionEngine(approver=lambda request: answers.pop(0))
    verifier = Verifier(Workspace(tmp_path), [], engine)
    first = verifier.run_check(check("print(1)"))
    second = verifier.run_check(check("print(1)"))
    assert (first.status, second.status) == ("passed", "passed")
    assert engine.history[0].request.tool_name == "verification"


def test_missing_tool_is_a_failure_not_a_crash(tmp_path):
    missing = VerificationCheck(name="nope", kind="lint", command="definitely_missing_linter_xyz .", reason="test")
    result = permissive(tmp_path).run_check(missing)
    assert result.failing
