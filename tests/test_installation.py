import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import shlex
from hermes_router.specification import WorkSpec


def test_launchd_generator_is_valid_and_does_not_install(tmp_path):
    output = tmp_path / "router.plist"
    data = tmp_path / "runtime/data"
    result = subprocess.run([sys.executable, "scripts/generate-launchd.py", "--output", str(output),
        "--data-dir", str(data), "--repository", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    config = plistlib.loads(output.read_bytes())
    assert config["ProgramArguments"][0] == str(Path.cwd() / ".venv/bin/python")
    assert config["ThrottleInterval"] == 30
    assert config["KeepAlive"] == {"SuccessfulExit": False}
    assert config["EnvironmentVariables"]["HERMES_ROUTER_ALLOW_REAL"] == "0"
    assert json.loads(config["EnvironmentVariables"]["HERMES_ROUTER_ALLOWED_REPOS"]) == [str(tmp_path)]
    assert subprocess.run(["/usr/bin/plutil", "-lint", str(output)], capture_output=True).returncode == 0


def test_backup_refuses_running_owner_and_restores_sqlite(tmp_path):
    from hermes_router.development import DevelopmentService
    data = tmp_path / "data"
    service = DevelopmentService(data)
    job = service.store.create("persist", "/repo")
    command = [sys.executable, "scripts/backup.py", "--data-dir", str(data), "--output", str(tmp_path / "backup")]
    assert subprocess.run(command, capture_output=True).returncode != 0
    service.close()
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(tmp_path / "backup/development.sqlite3")) as conn:
        assert json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])["prompt"] == "persist"
    assert subprocess.run(command, capture_output=True).returncode != 0


def test_installer_preserves_custom_skill(tmp_path):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    shutil.copy("scripts/install.sh", root / "scripts/install.sh")
    (root / "skills/dev-router").mkdir(parents=True)
    (root / "skills/dev-router/SKILL.md").write_text("new skill")
    (root / ".venv/bin").mkdir(parents=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    for path in (tools / "uv", root / ".venv/bin/python", root / ".venv/bin/hermes-router"):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    (root / ".venv/bin/python").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    home = tmp_path / "home"
    skill = home / ".hermes/skills/dev-router/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("user customization")
    env = dict(os.environ, HOME=str(home), PATH=str(tools)+":/usr/bin:/bin")
    result = subprocess.run(["/bin/bash", str(root / "scripts/install.sh"), "--install-skill"], env=env, capture_output=True)
    assert result.returncode != 0
    assert skill.read_text() == "user customization"
    skill.unlink()
    result = subprocess.run(["/bin/bash", str(root / "scripts/install.sh"), "--install-skill"], env=env, capture_output=True)
    assert result.returncode == 0
    assert skill.read_text() == "new skill"
    assert (home / ".local/bin/dev-router").exists()


def test_example_specification_and_shell_syntax():
    spec = WorkSpec.model_validate_json(Path("examples/function-spec.json").read_text())
    assert spec.allowed_files == ("app.py",)
    for path in Path("scripts").glob("*.sh"):
        assert subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True).returncode == 0
