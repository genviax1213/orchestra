import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from orchestra.cli import load_task_prompt, tail_text, task_snapshot


PROJECT_ROOT = Path("/mnt/rll/projects/orchestra")


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "orchestra", *args],
        cwd=str(cwd or PROJECT_ROOT),
        text=True,
        capture_output=True,
        env={"PYTHONPATH": str(PROJECT_ROOT / "src")},
        check=False,
    )


class OrchestraCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True, text=True)
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_init_creates_state_file(self) -> None:
        result = run_cli("init", str(self.repo), "--base-branch", "main")
        self.assertEqual(result.returncode, 0, result.stderr)

        state_path = self.repo / ".orchestra" / "state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["base_branch"], "main")

    def test_task_add_and_list(self) -> None:
        self.assertEqual(run_cli("init", str(self.repo)).returncode, 0)
        add_result = run_cli(
            "task",
            "add",
            "auth-ui",
            "--repo",
            str(self.repo),
            "--branch",
            "feat/auth-ui",
            "--agent",
            "codex",
            "--prompt",
            "Implement auth ui",
        )
        self.assertEqual(add_result.returncode, 0, add_result.stderr)

        list_result = run_cli("task", "list", "--repo", str(self.repo))
        self.assertEqual(list_result.returncode, 0, list_result.stderr)
        self.assertIn("auth-ui", list_result.stdout)
        self.assertIn("feat/auth-ui", list_result.stdout)

    def test_launch_dry_run_outputs_command(self) -> None:
        self.assertEqual(run_cli("init", str(self.repo)).returncode, 0)
        self.assertEqual(
            run_cli(
                "task",
                "add",
                "billing-api",
                "--repo",
                str(self.repo),
                "--branch",
                "feat/billing-api",
                "--agent",
                "claude",
                "--prompt",
                "Implement billing api",
            ).returncode,
            0,
        )
        result = run_cli("launch", "billing-api", "--repo", str(self.repo), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude", result.stdout)
        self.assertIn("Implement billing api", result.stdout)

    def test_launch_all_dry_run_outputs_all_matching_tasks(self) -> None:
        self.assertEqual(run_cli("init", str(self.repo)).returncode, 0)
        self.assertEqual(
            run_cli(
                "task",
                "add",
                "auth-ui",
                "--repo",
                str(self.repo),
                "--branch",
                "feat/auth-ui",
                "--agent",
                "codex",
                "--prompt",
                "Implement auth ui",
            ).returncode,
            0,
        )
        self.assertEqual(
            run_cli(
                "task",
                "add",
                "billing-api",
                "--repo",
                str(self.repo),
                "--branch",
                "feat/billing-api",
                "--agent",
                "claude",
                "--prompt",
                "Implement billing api",
            ).returncode,
            0,
        )

        result = run_cli("launch-all", "--repo", str(self.repo), "--dry-run", "--only-status", "planned")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("auth-ui\tDRY-RUN", result.stdout)
        self.assertIn("billing-api\tDRY-RUN", result.stdout)

    def test_help_lists_tui_command(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tui", result.stdout)

    def test_task_snapshot_reports_static_fields(self) -> None:
        task = {
            "name": "auth-ui",
            "status": "planned",
            "branch": "feat/auth-ui",
            "agent": "codex",
            "session_name": "orchestra-auth-ui",
            "worktree_path": str(self.repo / "missing-worktree"),
        }
        snapshot = task_snapshot(task)
        self.assertEqual(snapshot["name"], "auth-ui")
        self.assertEqual(snapshot["status"], "planned")
        self.assertEqual(snapshot["branch"], "feat/auth-ui")
        self.assertEqual(snapshot["agent"], "codex")
        self.assertEqual(snapshot["worktree"], "missing")

    def test_prompt_file_is_loaded(self) -> None:
        prompt_path = self.repo / "prompt.md"
        prompt_path.write_text("Run from file\n", encoding="utf-8")
        prompt = load_task_prompt({"name": "file-task", "prompt_file": str(prompt_path), "prompt": ""})
        self.assertEqual(prompt, "Run from file")

    def test_tail_text_reads_last_lines(self) -> None:
        log_path = self.repo / "task.log"
        log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.assertEqual(tail_text(log_path, 2), "two\nthree")

    def test_shell_launch_writes_log(self) -> None:
        session_prefix = f"test-{os.getpid()}-{int(time.time() * 1000)}"
        self.assertEqual(
            run_cli("init", str(self.repo), "--session-prefix", session_prefix).returncode,
            0,
        )
        self.assertEqual(
            run_cli(
                "task",
                "add",
                "smoke",
                "--repo",
                str(self.repo),
                "--branch",
                "chore/smoke",
                "--agent",
                "shell",
                "--prompt",
                "printf 'smoke ok\\n'",
            ).returncode,
            0,
        )
        launch_result = run_cli("launch", "smoke", "--repo", str(self.repo))
        self.assertEqual(launch_result.returncode, 0, launch_result.stderr)

        for _ in range(20):
            logs_result = run_cli("logs", "smoke", "--repo", str(self.repo))
            if "smoke ok" in logs_result.stdout:
                break
            time.sleep(0.1)
        else:
            self.fail("expected smoke log output")

        log_path_result = run_cli("logs", "smoke", "--repo", str(self.repo), "--path")
        self.assertEqual(log_path_result.returncode, 0, log_path_result.stderr)
        self.assertIn(".orchestra/logs/smoke.log", log_path_result.stdout)


if __name__ == "__main__":
    unittest.main()
