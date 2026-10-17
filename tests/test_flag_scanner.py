"""Tests for feature-flag-staleness-scanner.

Builds real fixture git repos in tmp (with backdated commits) and runs
the actual scanner against them. No mocks.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import flag_scanner  # noqa: E402


def git(repo, *args, date=None):
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True,
        env=env, check=True)


def make_git_repo():
    repo = tempfile.mkdtemp(prefix="flags-fixture-")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    return repo


APP_V1 = '''from launchdarkly import client

def render(user):
    if client.variation("legacy_banner", user, False):
        return "old banner"
    return "new banner"
'''

APP_V2 = '''from launchdarkly import client
import os

def render(user):
    if client.variation("legacy_banner", user, False):
        return "old banner"
    if client.variation("new_checkout", user, False):
        return "checkout v2"
    if unleash.isEnabled("mystery_flag"):
        return "mystery"
    if os.getenv("FEATURE_NEW_UI"):
        return "new ui"
    return "default"
'''

YAML_V1 = "FEATURE_API_V2: true\n"
YAML_V2 = "FEATURE_API_V2: false\n# FEATURE_API_V2 rollout paused 2026-10-10\n"

STATES = {
    "new_checkout": 100,
    "legacy_banner": 20,
    "FEATURE_NEW_UI": 50,
    "FEATURE_API_V2": 50,
}


def build_mixed_fixture():
    """Fixture with exactly: 1 STALE, 1 ZOMBIE, 1 ORPHAN, 2 healthy flags."""
    repo = make_git_repo()
    app = os.path.join(repo, "app.py")
    cfg = os.path.join(repo, "flags.yaml")
    states = os.path.join(repo, "states.json")

    with open(app, "w") as fh:
        fh.write(APP_V1)
    with open(cfg, "w") as fh:
        fh.write(YAML_V1)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial", date="2025-01-01T00:00:00")

    with open(app, "w") as fh:
        fh.write(APP_V2)
    with open(cfg, "w") as fh:
        fh.write(YAML_V2)
    with open(states, "w") as fh:
        json.dump(STATES, fh)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add flags", date="2026-10-10T00:00:00")
    return repo, states


def build_clean_fixture():
    repo = make_git_repo()
    with open(os.path.join(repo, "app.py"), "w") as fh:
        fh.write('import os\nx = os.getenv("FEATURE_CLEAN")\n'
                 'y = client.variation("active_flag", user, False)\n')
    states = os.path.join(repo, "states.json")
    with open(states, "w") as fh:
        json.dump({"FEATURE_CLEAN": 80, "active_flag": 50}, fh)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "all healthy",
        date="2026-10-15T00:00:00")
    return repo, states


def run_scanner(root, states_path, extra=()):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = flag_scanner.main([root, "--states", states_path, *extra])
    return code, buf.getvalue()


class TestFlagScanner(unittest.TestCase):
    def test_stale_detected_exactly(self):
        repo, states = build_mixed_fixture()
        code, out = run_scanner(repo, states)
        self.assertEqual(code, 1, "exit code must be 1 when STALE flags exist")
        self.assertIn("STALE (1)", out)
        self.assertIn("new_checkout", out)

    def test_zombie_detected_exactly(self):
        repo, states = build_mixed_fixture()
        _, out = run_scanner(repo, states)
        self.assertIn("ZOMBIE (1)", out)
        self.assertIn("legacy_banner", out)

    def test_orphan_detected_exactly(self):
        repo, states = build_mixed_fixture()
        _, out = run_scanner(repo, states)
        self.assertIn("ORPHAN (1)", out)
        self.assertIn("mystery_flag", out)

    def test_healthy_flags_not_flagged(self):
        repo, states = build_mixed_fixture()
        _, out = run_scanner(repo, states)
        stale_section = out.split("STALE")[1].split("ZOMBIE")[0]
        zombie_section = out.split("ZOMBIE")[1].split("ORPHAN")[0]
        self.assertNotIn("FEATURE_NEW_UI", stale_section + zombie_section)
        self.assertNotIn("FEATURE_API_V2", stale_section + zombie_section)

    def test_clean_fixture_clean_report(self):
        repo, states = build_clean_fixture()
        code, out = run_scanner(repo, states)
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_non_git_repo_skips_zombie_gracefully(self):
        plain = tempfile.mkdtemp(prefix="flags-plain-")
        with open(os.path.join(plain, "app.py"), "w") as fh:
            fh.write('x = client.variation("some_flag", user, False)\n')
        states = os.path.join(plain, "states.json")
        with open(states, "w") as fh:
            json.dump({"some_flag": 10}, fh)
        code, out = run_scanner(plain, states)
        self.assertEqual(code, 0)
        self.assertIn("not a git repo", out)

    def test_missing_states_file_errors(self):
        repo, _ = build_clean_fixture()
        with contextlib.redirect_stderr(io.StringIO()):
            code = flag_scanner.main([repo, "--states", "/nope/missing.json"])
        self.assertEqual(code, 2)

    def test_json_format(self):
        repo, states = build_mixed_fixture()
        _, out = run_scanner(repo, states, extra=("--format", "json"))
        data = json.loads(out)
        self.assertEqual(len(data["stale"]), 1)
        self.assertEqual(data["stale"][0]["flag"], "new_checkout")
        self.assertEqual(len(data["zombie"]), 1)
        self.assertEqual(len(data["orphan"]), 1)


if __name__ == "__main__":
    unittest.main()
