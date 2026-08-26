#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import re

ROOT = Path(__file__).resolve().parents[2]
UPDATE_WORKFLOW = ROOT / ".github" / "workflows" / "update.yml"
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
ORBIT_VERIFIER = (
    "iml885203/orbit/.github/actions/verify-orbit-release"
    "@d7f2c503228479f27162e558720a6e679627f809"
)


def load_yaml(path):
    result = subprocess.run(
        [
            "ruby",
            "-ryaml",
            "-rjson",
            "-e",
            "puts JSON.generate(YAML.load_file(ARGV.fetch(0)))",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def external_action_pin_errors(workflow_text):
    errors = []
    pin = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
    version_comment = re.compile(r"#\s+v\S+")
    for number, line in enumerate(workflow_text.splitlines(), 1):
        match = re.search(r"\buses:\s+([^\s#]+)", line)
        if not match or match.group(1).startswith("./"):
            continue
        if not pin.fullmatch(match.group(1)):
            errors.append(f"line {number} action is not commit-pinned")
        if not version_comment.search(line):
            errors.append(f"line {number} action pin has no human-readable version")
    return errors


def update_workflow_errors(workflow):
    errors = []
    if workflow.get("permissions") != {"contents": "read"}:
        errors.append("workflow token must be read-only")
    jobs = workflow.get("jobs", {})
    for name, job in jobs.items():
        permissions = job.get("permissions", workflow.get("permissions", {}))
        if name != "update" and "write" in permissions.values():
            errors.append(f"unexpected write-capable job: {name}")
    if jobs.get("update", {}).get("permissions") != {
        "actions": "write",
        "contents": "write",
        "pull-requests": "write",
    }:
        errors.append("update job must own the exact write permissions")
    resolve_steps = jobs.get("resolve", {}).get("steps", [])
    release_steps = [step for step in resolve_steps if step.get("id") == "release"]
    if len(release_steps) != 1:
        errors.append("resolve job must contain one release resolver")
        return errors
    resolver = release_steps[0]
    if resolver.get("env", {}).get("REQUESTED_PROJECT") != "${{ inputs.project }}":
        errors.append("resolver project must come from the workflow input")
    script = resolver.get("run", "")
    for contract in (
        'project="${REQUESTED_PROJECT:-tunlease}"',
        'repos/iml885203/$project/releases/latest',
        '"bucket/$project.json"',
    ):
        if contract not in script:
            errors.append(f"resolver does not use resolved project: {contract}")
    return errors


def promotion_allowed(resolve_result, current, project, verify_result):
    return (
        resolve_result == "success"
        and current != "true"
        and (project != "orbit" or verify_result == "success")
    )


class UpdateWorkflowContractTest(unittest.TestCase):
    def setUp(self):
        self.workflow = load_yaml(UPDATE_WORKFLOW)
        self.jobs = self.workflow["jobs"]

    def test_workflow_has_no_write_capable_bypass(self):
        self.assertEqual(update_workflow_errors(self.workflow), [])
        mutated = json.loads(json.dumps(self.workflow))
        mutated["jobs"]["bypass"] = {
            "runs-on": "ubuntu-latest",
            "permissions": {"contents": "write"},
            "steps": [{"run": "echo bypass"}],
        }
        self.assertIn("unexpected write-capable job: bypass", update_workflow_errors(mutated))

    def test_external_actions_in_complete_promotion_closure_are_commit_pinned(self):
        for path in (UPDATE_WORKFLOW, TEST_WORKFLOW):
            self.assertEqual(external_action_pin_errors(path.read_text()), [], str(path))
            workflow = load_yaml(path)
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    action = step.get("uses")
                    if action and not action.startswith("./"):
                        self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", f"{path}: {action}")

    def test_remote_verifier_pin_requires_a_version_comment(self):
        workflow = UPDATE_WORKFLOW.read_text()
        for comment in (
            " # v0.16.0 release-security boundary",
            " # issue-125",
        ):
            with self.subTest(comment=comment):
                mutated = workflow.replace(
                    " # v0.16.0 release-security boundary",
                    "" if comment.startswith(" # v") else comment,
                    1,
                )
                self.assertTrue(external_action_pin_errors(mutated))

    def test_read_only_verification_is_a_required_predecessor_of_write_job(self):
        verify = self.jobs["verify-orbit"]
        update = self.jobs["update"]
        self.assertEqual(verify["needs"], "resolve")
        self.assertEqual(verify["permissions"], {"contents": "read"})
        self.assertIn("project == 'orbit'", verify["if"])
        self.assertEqual(len(verify["steps"]), 1)
        verify_step = verify["steps"][0]
        self.assertEqual(verify_step["uses"], ORBIT_VERIFIER)
        self.assertEqual(verify_step["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertEqual(
            verify_step["with"],
            {"mode": "published", "tag": "${{ needs.resolve.outputs.tag }}"},
        )
        self.assertNotIn("continue-on-error", verify_step)
        self.assertEqual(set(update["needs"]), {"resolve", "verify-orbit"})
        self.assertEqual(
            update["permissions"],
            {"actions": "write", "contents": "write", "pull-requests": "write"},
        )
        condition = update["if"]
        self.assertEqual(
            " ".join(condition.split()),
            "always() && needs.resolve.result == 'success' && "
            "needs.resolve.outputs.current != 'true' && "
            "(needs.resolve.outputs.project != 'orbit' || "
            "needs.verify-orbit.result == 'success')",
        )
        for required in (
            "always()",
            "needs.resolve.result == 'success'",
            "needs.resolve.outputs.current != 'true'",
            "needs.resolve.outputs.project != 'orbit'",
            "needs.verify-orbit.result == 'success'",
        ):
            self.assertIn(required, condition)

    def test_permission_gate_truth_table_preserves_default_and_tunlease(self):
        cases = [
            (("success", "false", "tunlease", "skipped"), True),
            (("success", "false", "orbit", "success"), True),
            (("success", "false", "orbit", "failure"), False),
            (("success", "false", "orbit", "skipped"), False),
            (("success", "true", "tunlease", "skipped"), False),
            (("failure", "false", "tunlease", "skipped"), False),
        ]
        for inputs, expected in cases:
            self.assertEqual(promotion_allowed(*inputs), expected, inputs)
        resolve_script = next(
            step["run"] for step in self.jobs["resolve"]["steps"] if step.get("id") == "release"
        )
        self.assertIn('project="${REQUESTED_PROJECT:-tunlease}"', resolve_script)

    def test_resolver_contract_rejects_unresolved_project_paths(self):
        for original, replacement in (
            ('repos/iml885203/$project/releases/latest', 'repos/iml885203/orbit/releases/latest'),
            ('"bucket/$project.json"', '"bucket/orbit.json"'),
        ):
            mutated = json.loads(json.dumps(self.workflow))
            resolver = next(
                step for step in mutated["jobs"]["resolve"]["steps"] if step.get("id") == "release"
            )
            resolver["run"] = resolver["run"].replace(original, replacement)
            self.assertTrue(update_workflow_errors(mutated), replacement)

    def test_resolver_executes_latest_lookup_for_the_resolved_project(self):
        resolver = next(
            step["run"] for step in self.jobs["resolve"]["steps"] if step.get("id") == "release"
        )
        for project in ("tunlease", "orbit"):
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory)
                fake_gh = fixture / "gh"
                fake_gh.write_text(
                    """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$GH_CALLS"
printf '%s\n' v99.0.0
"""
                )
                fake_gh.chmod(0o755)
                fake_jq = fixture / "jq"
                fake_jq.write_text(
                    f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$JQ_CALLS"
exec {shutil.which('jq')} "$@"
"""
                )
                fake_jq.chmod(0o755)
                output = fixture / "outputs"
                env = os.environ | {
                    "PATH": f"{fixture}:{os.environ['PATH']}",
                    "GH_CALLS": str(fixture / "calls"),
                    "JQ_CALLS": str(fixture / "jq-calls"),
                    "GITHUB_OUTPUT": str(output),
                    "REQUESTED_PROJECT": project,
                    "REQUESTED_VERSION": "",
                }
                result = subprocess.run(
                    ["bash", "-c", resolver], cwd=ROOT, env=env, text=True, capture_output=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    f"api repos/iml885203/{project}/releases/latest --jq .tag_name",
                    (fixture / "calls").read_text(),
                )
                self.assertIn(
                    f"-r .version bucket/{project}.json",
                    (fixture / "jq-calls").read_text(),
                )
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values["project"], project)
                self.assertEqual(values["tag"], "v99.0.0")

    def test_dependabot_maintains_pinned_github_actions(self):
        config = load_yaml(DEPENDABOT)
        entries = [
            update
            for update in config["updates"]
            if update["package-ecosystem"] == "github-actions" and update["directory"] == "/"
        ]
        self.assertEqual(len(entries), 1)
        self.assertIn("interval", entries[0]["schedule"])
if __name__ == "__main__":
    unittest.main()
