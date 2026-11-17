"""Sandbox to run `run_pipeline.sh` without touching anything real.

Used by `tests/e2e/test_run_pipeline_e2e.py`. What it builds, all inside a temporary directory:

- `<tmp>/project/run_pipeline.sh`: a COPY of the script from the working tree. The real script never
  runs in its place. The copy has no code (`*.py`): even if a shim failed, there is nothing
  from Odoo or scraping to run.
- `<tmp>/shims/`: fake `python`, `timeout`, `git`, `conda`, `aws`, `sudo` and `shutdown`
  commands. Each one records its call in `<tmp>/calls.tsv` and does nothing else. `sudo` and
  `shutdown` are mandatory: the script's `trap EXIT` runs `sudo shutdown -h +1`.
  `timeout` drops the duration and executes the rest (which reaches the `python` shim).
- `<tmp>/system/`: links only to the utilities the script uses (`date`, `tee`, `mkdir`,
  `cat`, `rm`, `wc`, `xargs`, `echo`, `dirname`). The `PATH` is `shims:system` and nothing else, so
  any other command (`curl`, `ssh`, ...) gives "command not found" and the test detects it.
  If `run_pipeline.sh` starts using another utility without external effects, add it to
  `UTILITIES`; if it has external effects, give it a shim.
- `<tmp>/conda/etc/profile.d/conda.sh`: empty; `conda info --base` (shim) points there.

The subprocess environment is built from scratch (no `BASH_ENV`, exported functions, AWS
credentials or `PYTHONPATH`), with proxies set to `127.0.0.1:9` and the AWS IMDS turned off.

Before each run, `check_shims()` checks, with the same bash and the same environment, that each
protected command resolves to its shim (type `file`, path inside `<tmp>/shims`). If not, it raises
`ShimNotProtecting` and the run is NOT done.

Deliberate failures: `run(fail={"scraper/main.py": 124})` makes the `python` shim exit with
124 (what `timeout` returns when it expires) when the first argument is that script.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tests.e2e import _helpers as h

SCRIPT = "run_pipeline.sh"
BASH = "/bin/bash"
SHIMS = ("python", "timeout", "git", "conda", "aws", "sudo", "shutdown")
UTILITIES = ("date", "tee", "mkdir", "cat", "rm", "wc", "xargs", "echo", "dirname")
SYSTEM_PATH = "/usr/bin:/bin"
RUN_TIMEOUT_S = 60

_RECORD = 'printf \'%s\\t%s\\t%s\\n\' "${0##*/}" "$PWD" "$*" >> "$PI_SHIM_LOG"\n'
_SHIM_BODIES = {
    "python": (
        'script="$1"\n'
        "code=0\n"
        "# PI_SHIM_FAIL='scraper/main.py=124 ml_model/x.py=1'\n"
        "for rule in $PI_SHIM_FAIL; do\n"
        '  case "$rule" in "$script="*) code="${rule#*=}";; esac\n'
        "done\n"
        'printf \'shim python: %s -> exit %s\\n\' "$script" "$code"\n'
        'exit "$code"\n'
    ),
    "timeout": 'shift\nexec "$@"\n',
    "conda": (
        'if [ "$1" = info ] && [ "$2" = --base ]; then printf \'%s\\n\' "$PI_SHIM_CONDA_BASE"; fi\n'
        "exit 0\n"
    ),
}
_DEFAULT_BODY = "exit 0\n"  # git, aws, sudo, shutdown: they only record


class ShimNotProtecting(RuntimeError):
    """A protected command does not resolve to its shim: the run refuses to start."""


@dataclass(frozen=True)
class Call:
    command: str
    cwd: str
    args: str

    @property
    def script(self) -> str:
        return self.args.split()[0] if self.args else ""


@dataclass
class SandboxRun:
    returncode: int
    stdout: str
    stderr: str
    calls: list[Call]
    project: Path

    def of(self, command: str) -> list[Call]:
        return [c for c in self.calls if c.command == command]

    @property
    def python_scripts(self) -> list[str]:
        return [c.script for c in self.of("python")]

    @property
    def pipeline_log(self) -> str:
        p = self.project / "logs" / "pipeline_execution.log"
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    def trace(self) -> str:
        lines = [f"run_pipeline.sh (copy in {self.project}) exited with {self.returncode}. Calls:"]
        lines += [f"  {c.command} {c.args}" for c in self.calls]
        lines += ["--- stdout ---", self.stdout.rstrip() or "(empty)",
                  "--- stderr ---", self.stderr.rstrip() or "(empty)"]
        return "\n".join(lines)


@dataclass
class Sandbox:
    root: Path
    project: Path = field(init=False)
    shims: Path = field(init=False)
    system: Path = field(init=False)
    conda_base: Path = field(init=False)
    log: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        h._check_outside_repo(self.root)
        self.project = self.root / "project"
        self.shims = self.root / "shims"
        self.system = self.root / "system"
        self.conda_base = self.root / "conda"
        self.log = self.root / "calls.tsv"
        for d in (self.project / "ml_model", self.project / "scraper", self.project / "logs",
                  self.shims, self.system, self.conda_base / "etc" / "profile.d",
                  self.root / "home", self.root / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h.REPO_ROOT / SCRIPT, self.project / SCRIPT)
        (self.conda_base / "etc" / "profile.d" / "conda.sh").write_text(
            "# Fake conda.sh for the test: 'conda' resolves to the shim on the PATH.\n")
        for name in SHIMS:
            p = self.shims / name
            p.write_text("#!/bin/sh\n# run_pipeline.sh test shim: records and does nothing real.\n"
                         + _RECORD + _SHIM_BODIES.get(name, _DEFAULT_BODY))
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        for name in UTILITIES:
            real = shutil.which(name, path=SYSTEM_PATH)
            if real is None:
                raise RuntimeError(f"Utility '{name}' not found in {SYSTEM_PATH}.")
            (self.system / name).symlink_to(real)

    # --- initial state of the copy ------------------------------------------------------------
    def write(self, rel: str, text: str) -> Path:
        p = self.project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    # --- environment and lock -----------------------------------------------------------------
    def env(self, fail: dict[str, int] | None = None, path: str | None = None) -> dict[str, str]:
        proxy = "http://127.0.0.1:9"
        return {
            "PATH": path or f"{self.shims}:{self.system}",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "PI_SHIM_LOG": str(self.log),
            "PI_SHIM_CONDA_BASE": str(self.conda_base),
            "PI_SHIM_FAIL": " ".join(f"{k}={v}" for k, v in (fail or {}).items()),
            "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy,
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        }

    def check_shims(self, env: dict[str, str]) -> None:
        """Requires each protected command to resolve to its shim; otherwise refuses to run."""
        probe = 'for c in "$@"; do printf \'%s\\t%s\\t%s\\n\' "$c" "$(type -t "$c")" "$(type -P "$c")"; done'
        r = subprocess.run([BASH, "--noprofile", "--norc", "-c", probe, "probe", *SHIMS],
                           env=env, capture_output=True, text=True, timeout=10)
        problems = []
        for line in r.stdout.splitlines():
            name, kind, where = (line.split("\t") + ["", ""])[:3]
            if kind != "file" or Path(where).parent != self.shims:
                problems.append(f"{name}: type={kind or '?'} path={where or '?'}")
        if r.returncode != 0 or len(r.stdout.splitlines()) != len(SHIMS) or problems:
            raise ShimNotProtecting(
                "Refusing to run run_pipeline.sh: these commands do not resolve to their shim "
                f"in {self.shims} (sudo/shutdown are mandatory because of the trap EXIT): "
                + ("; ".join(problems) or r.stderr.strip() or "unexpected output"))
        if any(self.project.rglob("*.py")):
            raise ShimNotProtecting("The project copy contains .py code; not running.")

    # --- run ----------------------------------------------------------------------------------
    def run(self, stage: int = 1, fail: dict[str, int] | None = None) -> SandboxRun:
        env = self.env(fail)
        self.check_shims(env)
        self.log.unlink(missing_ok=True)
        args = [BASH, "--noprofile", "--norc", str(self.project / SCRIPT)]
        if stage == 2:
            args += ["--stage", "2"]
        p = subprocess.run(args, cwd=self.project, env=env, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S)
        calls = []
        if self.log.is_file():
            for line in self.log.read_text(encoding="utf-8").splitlines():
                command, cwd, args_ = (line.split("\t", 2) + ["", ""])[:3]
                calls.append(Call(command, cwd, args_))
        return SandboxRun(p.returncode, p.stdout, p.stderr, calls, self.project)
