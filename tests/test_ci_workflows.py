"""@brief The CI runs the suite in two processes, with timeouts (HANDOFF point 113)."""
import subprocess
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_halves():
    """Import tools/run_test_halves.py by path, without touching sys.path for the session."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_test_halves",
                                                  ROOT / "tools" / "run_test_halves.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


halves = _load_halves()

WORKFLOWS = ROOT / ".github" / "workflows"


def test_external_actions_are_pinned_to_immutable_commits():
    """Every external action must use a full commit, including publish steps."""
    yaml = pytest.importorskip("yaml")
    for path in WORKFLOWS.glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action and not action.startswith("./"):
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), (path.name, action)


def _touch(directory, *names):
    for name in names:
        (directory / name).write_text("", encoding="utf-8")


def test_the_split_is_a_partition_at_the_documented_file(tmp_path):
    _touch(tmp_path, "test_a.py", "test_photo_detection_generality.py",
           "test_photo_detector_ui.py", "test_zeta.py", "helper.py", "conftest.py")
    first, second = halves.split_test_files(tmp_path)
    assert [Path(p).name for p in first] == ["test_a.py", "test_photo_detection_generality.py"]
    assert [Path(p).name for p in second] == ["test_photo_detector_ui.py", "test_zeta.py"]


def test_a_missing_split_file_or_an_empty_half_is_refused(tmp_path):
    """@brief Negative: an empty half would pass without testing anything."""
    _touch(tmp_path, "test_a.py", "test_b.py")
    with pytest.raises(halves.SplitError):
        halves.split_test_files(tmp_path)
    _touch(tmp_path, "test_photo_detector_ui.py")
    with pytest.raises(halves.SplitError):
        halves.split_test_files(tmp_path, split_at="test_a.py")


def test_the_real_suite_is_covered_exactly_once():
    first, second = halves.split_test_files(ROOT / "tests")
    every = sorted(f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py"))
    assert sorted(first + second) == every and not set(first) & set(second)
    listed = subprocess.run([sys.executable, str(ROOT / "tools" / "run_test_halves.py"), "both", "--list"],
                            capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert listed.returncode == 0 and listed.stdout.split() == first + second


@pytest.mark.parametrize("name, job", [("release.yml", "build"), ("tests.yml", "test")])
def test_the_workflows_run_two_halves_with_timeouts(name, job):
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    steps = workflow["jobs"][job]["steps"]
    assert workflow["jobs"][job]["timeout-minutes"] > 0
    runs = {str(s.get("run", "")) for s in steps}
    assert any("run_test_halves.py first" in r for r in runs)
    assert any("run_test_halves.py second" in r for r in runs)
    # Negative: no step runs the whole suite in one pytest process any more.
    assert not any("-m pytest" in r for r in runs)
    for step in steps:
        if "run_test_halves.py" in str(step.get("run", "")):
            assert step["timeout-minutes"] > 0 and step["env"]["QT_QPA_PLATFORM"] == "offscreen"


def test_the_push_workflow_is_read_only_and_uses_the_release_dependencies():
    yaml = pytest.importorskip("yaml")
    text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    triggers = workflow.get("on", workflow.get(True))
    assert "push" in triggers and "pull_request" in triggers
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["test"]["permissions"] == {"contents": "read"}
    assert "secrets." not in text.replace(" no secrets.", "")  # expressions, not the comment
    assert "${{ secrets" not in text
    assert "-c requirements/release-constraints.txt" in text
    checkout = [s for s in workflow["jobs"]["test"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout and checkout[0]["with"]["persist-credentials"] is False


@pytest.mark.parametrize("name, job", [("release.yml", "build"), ("tests.yml", "test")])
def test_a_failed_run_uploads_the_watchdog_report(name, job):
    """A hang or native crash must leave the stacks the watchdog wrote (audit 138, D6).

    The watchdog writes to %TEMP%; the test steps pin TEMP to the runner's temp
    folder and a step that runs only on failure uploads the report from there.
    """
    yaml = pytest.importorskip("yaml")
    from conftest import HANG_REPORT
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    steps = workflow["jobs"][job]["steps"]
    for step in steps:
        if "run_test_halves.py" in str(step.get("run", "")):
            assert step["env"]["TEMP"] == step["env"]["TMP"] == "${{ runner.temp }}"
    uploads = [step for step in steps
               if str(step.get("uses", "")).startswith("actions/upload-artifact")
               and "failure()" in str(step.get("if", ""))]
    assert len(uploads) == 1
    pattern = uploads[0]["with"]["path"]
    assert pattern.startswith("${{ runner.temp }}/")
    # The uploaded pattern is the file name the watchdog really uses.
    import fnmatch
    assert fnmatch.fnmatch(HANG_REPORT.name, pattern.rsplit("/", 1)[1])


# -- review 2026-10-29: triggers, early weights fetch, pip cache ---------------


def _workflow(name):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow):
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    return workflow.get("on", workflow.get(True))


def _push_runs(push, ref):
    """GitHub's push filter for the refs used here: `branches` alone matches no tag at all.

    Only the patterns '**' and 'v*' / 'v*.*.*' occur in these workflows; a
    pattern of another shape fails the test instead of being guessed.
    """
    import fnmatch
    kind, _, name = ref.removeprefix("refs/").partition("/")
    included = push.get("branches" if kind == "heads" else "tags")
    ignored = push.get("branches-ignore" if kind == "heads" else "tags-ignore")
    if push and included is None and ignored is None:
        return False  # filters only for the other ref kind: this kind never runs
    for pattern in (included or []) + (ignored or []):
        assert pattern in ("**", "v*", "v*.*.*"), pattern
    matches = lambda patterns: any(p == "**" or fnmatch.fnmatchcase(name, p) for p in patterns)
    if included is not None:
        return matches(included)
    return not matches(ignored)


def test_the_tests_workflow_runs_on_branch_pushes_but_not_on_release_tags():
    """@brief K2: a tag push no longer starts a third copy of the suite; every branch push still does."""
    push = _triggers(_workflow("tests.yml"))["push"]
    for branch in ("refs/heads/main", "refs/heads/ADVANCED", "refs/heads/feature/x"):
        assert _push_runs(push, branch), branch
    assert not _push_runs(push, "refs/tags/v1.0.3")
    # The release workflow still runs on that tag (it tests the tag itself),
    # and never on a branch push.
    release = _triggers(_workflow("release.yml"))["push"]
    assert _push_runs(release, "refs/tags/v1.0.3") and not _push_runs(release, "refs/heads/main")


def test_the_release_gate_still_finds_a_run_of_the_commit_on_any_branch():
    """@brief K2 negative: publish-release asks GitHub for runs of the commit, not of a ref, so branch runs count."""
    script = (ROOT / "scripts" / "publish-release.ps1").read_text(encoding="utf-8")
    assert "gh run list --commit $sha --workflow Tests" in script
    assert "--branch" not in script and "--event" not in script


def test_the_release_fetches_the_weights_before_the_tests_with_a_bounded_retry():
    """@brief K4: the fetch precedes the test halves, retries a network failure (exit 1) 3 times 30 s apart."""
    steps = _workflow("release.yml")["jobs"]["build"]["steps"]
    names = [step.get("name", "") for step in steps]
    fetch = names.index("Fetch the model weights")
    place = names.index("Place the model weights")
    first_half = next(i for i, step in enumerate(steps) if "run_test_halves.py first" in str(step.get("run", "")))
    second_half = next(i for i, step in enumerate(steps) if "run_test_halves.py second" in str(step.get("run", "")))
    build = next(i for i, step in enumerate(steps) if "build-windows.ps1" in str(step.get("run", "")))
    assert fetch < first_half < second_half < place < build
    script = steps[fetch]["run"]
    assert "$attempts = 3" in script and "Start-Sleep -Seconds 30" in script
    assert "$status -ne 1" in script, "a permanent failure (exit 2) is not retried"
    assert steps[fetch]["timeout-minutes"] > 0 and steps[place]["timeout-minutes"] > 0
    # Negative: the tests run WITHOUT the weights, as in tests.yml: they wait
    # outside the checkout until the tests are over.
    staging = steps[fetch]["env"]["WEIGHTS_STAGING"]
    assert staging.startswith("${{ runner.temp }}") and "--target $env:WEIGHTS_STAGING" in script
    assert steps[place]["env"]["WEIGHTS_STAGING"] == staging
    assert "-IncludeYoloModel" in steps[build]["run"]


@pytest.mark.parametrize("name, job, required", [
    ("tests.yml", "test", {"requirements.txt", "requirements/yolo.txt", "requirements/release-constraints.txt"}),
    ("release.yml", "build", {"requirements.txt", "requirements/yolo.txt", "requirements/build.txt",
                              "requirements/release-constraints.txt"}),
])
def test_setup_python_caches_pip_keyed_on_the_installed_requirements(name, job, required):
    """@brief K5: pip's cache is on and its key covers every requirement file a pip install of the job reads."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    steps = _workflow(name)["jobs"][job]["steps"]
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1 and setup[0]["with"]["cache"] == "pip"
    keyed = set(setup[0]["with"]["cache-dependency-path"].split())
    assert keyed == required
    for path in keyed:
        assert (ROOT / path).is_file(), path
    # Negative: a requirement file the install reads but the key misses would
    # serve a stale cache silently.
    for read in re.findall(r"-[rc] (requirements[\w./-]*\.txt)", text):
        assert read in keyed, read
