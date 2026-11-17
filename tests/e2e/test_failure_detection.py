"""Self-tests of the failure detection in `tests/e2e/_helpers.py` (no data, no network).

The `ml_model/` scripts swallow exceptions and exit with 0, so `run_ml_pipeline` considers a
stage good only if it exits with 0, writes no ` - ERROR - ` lines and leaves its outputs. These
tests show that detection works: FAKE scripts with the names of the real stages are placed in
`tmp_path` and `run.ok is False` is required for each form of silent failure.
There is a positive control (all good → `run.ok is True`) so they cannot pass thanks to a detector
that always says "fail".
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.e2e import _helpers as h

pytestmark = pytest.mark.unit

_FAKE = textwrap.dedent('''\
    import sys
    from pathlib import Path
    outputs = {outputs!r}
    for rel in outputs[:{write}]:
        Path(rel).parent.mkdir(parents=True, exist_ok=True)
        Path(rel).write_text("x\\n1\\n")
    {extra}
    sys.exit({code})
''')


def _fake_repo(tmp_path: Path, bad_stage: str | None = None, *, write_all: bool = True,
               extra: str = "", code: int = 0) -> Path:
    """Fake repo: each stage writes its outputs and exits with 0, except `bad_stage`."""
    repo = tmp_path / "repo"
    for stage in h.STAGES:
        script = repo / stage.args[0]
        script.parent.mkdir(parents=True, exist_ok=True)
        bad = stage.name == bad_stage
        outputs = list(stage.outputs)
        script.write_text(_FAKE.format(
            outputs=outputs,
            write=len(outputs) if (not bad or write_all) else 0,
            extra=extra if bad else "",
            code=code if bad else 0,
        ))
    return repo


def test_positive_control_compliant_scripts_give_ok(tmp_path):
    run = h.run_ml_pipeline(_fake_repo(tmp_path), raise_on_error=False)
    assert run.ok, run.trace()
    assert [s.stage.name for s in run.steps] == [s.name for s in h.STAGES]


@pytest.mark.parametrize("stage", [s.name for s in h.STAGES])
def test_exit_0_without_writing_its_output_is_a_failure(tmp_path, stage):
    run = h.run_ml_pipeline(_fake_repo(tmp_path, stage, write_all=False), raise_on_error=False)
    assert run.ok is False, run.trace()
    failed = run.steps[-1]
    assert failed.stage.name == stage and failed.returncode == 0
    assert any(p.startswith("did not generate:") for p in failed.problems), failed.problems
    # It stops at the stage that failed: the following ones do not run.
    assert len(run.steps) == [s.name for s in h.STAGES].index(stage) + 1


@pytest.mark.parametrize("where", ["stdout", "stderr"])
def test_error_line_in_the_log_is_a_failure_even_with_exit_0(tmp_path, where):
    msg = "2026-11-17 21:50:00,000 - wayakit - ERROR - Error while preprocessing: KeyError('price_sar')"
    extra = f"print({msg!r}, file=sys.{where})"
    run = h.run_ml_pipeline(_fake_repo(tmp_path, "1a_preprocess", extra=extra), raise_on_error=False)
    assert run.ok is False, run.trace()
    failed = run.steps[-1]
    assert failed.returncode == 0 and failed.stage.name == "1a_preprocess"
    assert any("ERROR level" in p for p in failed.problems), failed.problems
    assert "[FAIL] 1a_preprocess" in run.trace() and "[DID NOT RUN] 1b_preprocess" in run.trace()


def test_nonzero_exit_code_is_a_failure(tmp_path):
    run = h.run_ml_pipeline(_fake_repo(tmp_path, "2_train", code=3), raise_on_error=False)
    assert run.ok is False
    assert "exited with code 3" in run.steps[-1].problems


def test_raise_on_error_raises_pipeline_error_with_the_trace(tmp_path):
    with pytest.raises(h.PipelineError, match=r"\[FAIL\] 3_predict"):
        h.run_ml_pipeline(_fake_repo(tmp_path, "3_predict", write_all=False))


def test_stale_output_deleted_by_copy_inputs_does_not_count_as_generated(tmp_path, monkeypatch):
    """An output that came with the code (from a previous run) is deleted before running."""
    data = tmp_path / "data"
    for rel in h.INPUT_FILES:
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_text("a\n1\n")
    monkeypatch.setattr(h, "DATA_DIR", data)
    repo = _fake_repo(tmp_path, "3_predict", write_all=False)
    (repo / h.REPORT).write_text("stale\n")
    h.copy_inputs(repo)
    assert not (repo / h.REPORT).exists()
    run = h.run_ml_pipeline(repo, raise_on_error=False)
    assert run.ok is False and "did not generate: " + h.REPORT in run.steps[-1].problems


def test_does_not_run_inside_the_real_repo():
    with pytest.raises(ValueError, match="inside the repo"):
        h.run_ml_pipeline(h.REPO_ROOT / "ml_model", raise_on_error=False)
