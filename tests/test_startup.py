import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash"), "WSL launcher requires Bash")
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        shutil.copyfile(ROOT / "start-linux.sh", self.project / "start-linux.sh")
        (self.project / "start-windows.ps1").touch()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.log"
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STARTUP_LOG": str(self.log),
            "PREPARE_EXIT": "0",
            "DOCKER_FAIL": "",
            "WRITE_IP": "yes",
        }
        self.command("wslpath", 'printf "%s\\n" "$2"\n')
        self.command("powershell.exe", """
printf 'powershell:%s\\n' "$*" >> "$STARTUP_LOG"
[[ "$PREPARE_EXIT" == 0 ]] || exit "$PREPARE_EXIT"
if [[ "$WRITE_IP" == yes ]]; then
    mkdir -p .certs
    printf '192.168.1.42\\r\\n' > .certs/singer-ip.txt
fi
""")
        self.command("docker", """
printf 'docker:%s:%s:%s\\n' "$PWD" "$*" "${KARAOKE_LAN_IP-}" >> "$STARTUP_LOG"
[[ "$*" != "$DOCKER_FAIL" ]]
""")

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        path.chmod(0o755)

    def run_start(self, **environment):
        return subprocess.run(
            ["bash", str(self.project / "start-linux.sh")],
            cwd=self.root,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def commands(self):
        return self.log.read_text().splitlines()

    def test_starts_from_project_directory_and_exports_windows_ip(self):
        result = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertIn(
            f"powershell:-NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f"-File {self.project}/start-windows.ps1 -PrepareOnly",
            commands,
        )
        self.assertEqual(commands[-2:], [
            f"docker:{self.project}:compose up -d --build:192.168.1.42",
            f"docker:{self.project}:compose watch:192.168.1.42",
        ])

    def test_docker_unavailable_stops_before_certificate_preparation(self):
        result = self.run_start(DOCKER_FAIL="info")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Inicie o Docker Desktop", result.stderr)
        self.assertFalse(any(line.startswith("powershell:") for line in self.commands()))

    def test_compose_unavailable_stops_before_certificate_preparation(self):
        result = self.run_start(DOCKER_FAIL="compose version")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker Compose indisponivel", result.stderr)
        self.assertEqual(len(self.commands()), 1)

    def test_preparation_failure_does_not_start_containers(self):
        result = self.run_start(PREPARE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Falha ao preparar", result.stderr)
        self.assertFalse(any("compose up" in line for line in self.commands()))

    def test_missing_ip_does_not_start_containers(self):
        result = self.run_start(WRITE_IP="no")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nao gerou .certs/singer-ip.txt", result.stderr)
        self.assertFalse(any("compose up" in line for line in self.commands()))

    def test_build_failure_does_not_start_watcher(self):
        result = self.run_start(DOCKER_FAIL="compose up -d --build")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Falha ao iniciar os containers", result.stderr)
        self.assertFalse(any("compose watch" in line for line in self.commands()))

    def test_watch_failure_is_reported(self):
        result = self.run_start(DOCKER_FAIL="compose watch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Falha ao monitorar", result.stderr)


if __name__ == "__main__":
    unittest.main()
