"""Caja de arena para correr `run_pipeline.sh` sin tocar nada real.

Lo usa `tests/e2e/test_run_pipeline_e2e.py`. Qué arma, todo dentro de un directorio temporal:

- `<tmp>/project/run_pipeline.sh`: COPIA del script del árbol de trabajo. El script real nunca
  se ejecuta en su lugar. En la copia no hay código (`*.py`): aunque un shim fallara, no hay nada
  de Odoo ni de scraping que correr.
- `<tmp>/shims/`: comandos falsos `python`, `timeout`, `git`, `conda`, `aws`, `sudo` y
  `shutdown`. Cada uno anota su llamada en `<tmp>/llamadas.tsv` y no hace nada más. `sudo` y
  `shutdown` son obligatorios: el `trap EXIT` del script corre `sudo shutdown -h +1`.
  `timeout` descarta la duración y ejecuta el resto (que llega al shim de `python`).
- `<tmp>/sistema/`: enlaces solo a las utilidades que el script usa (`date`, `tee`, `mkdir`,
  `cat`, `rm`, `wc`, `xargs`, `echo`, `dirname`). El `PATH` es `shims:sistema` y nada más, así que
  cualquier otro comando (`curl`, `ssh`, ...) da "command not found" y la prueba lo detecta.
  Si `run_pipeline.sh` empieza a usar otra utilidad sin efectos externos, se agrega a
  `UTILIDADES`; si tiene efectos externos, se le hace un shim.
- `<tmp>/conda/etc/profile.d/conda.sh`: vacío; `conda info --base` (shim) apunta ahí.

El entorno del subproceso se arma desde cero (sin `BASH_ENV`, funciones exportadas, credenciales
de AWS ni `PYTHONPATH`), con proxies a `127.0.0.1:9` y el IMDS de AWS apagado.

Antes de cada corrida, `check_shims()` comprueba con el mismo bash y el mismo entorno que cada
comando protegido resuelve a su shim (tipo `file`, ruta dentro de `<tmp>/shims`). Si no, lanza
`ShimNoProtege` y la corrida NO se hace.

Fallas a propósito: `run(fallar={"scraper/main.py": 124})` hace que el shim de `python` salga con
124 (lo que devuelve `timeout` al vencerse) cuando el primer argumento es ese script.
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
UTILIDADES = ("date", "tee", "mkdir", "cat", "rm", "wc", "xargs", "echo", "dirname")
SISTEMA_PATH = "/usr/bin:/bin"
RUN_TIMEOUT_S = 60

_REGISTRO = 'printf \'%s\\t%s\\t%s\\n\' "${0##*/}" "$PWD" "$*" >> "$PI_SHIM_LOG"\n'
_SHIM_BODIES = {
    "python": (
        'script="$1"\n'
        "code=0\n"
        "# PI_SHIM_FALLAR='scraper/main.py=124 ml_model/x.py=1'\n"
        "for regla in $PI_SHIM_FALLAR; do\n"
        '  case "$regla" in "$script="*) code="${regla#*=}";; esac\n'
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
_DEFAULT_BODY = "exit 0\n"  # git, aws, sudo, shutdown: solo registran


class ShimNoProtege(RuntimeError):
    """Un comando protegido no resuelve a su shim: la corrida se niega a empezar."""


@dataclass(frozen=True)
class Llamada:
    comando: str
    cwd: str
    args: str

    @property
    def script(self) -> str:
        return self.args.split()[0] if self.args else ""


@dataclass
class Corrida:
    returncode: int
    stdout: str
    stderr: str
    llamadas: list[Llamada]
    project: Path

    def de(self, comando: str) -> list[Llamada]:
        return [c for c in self.llamadas if c.comando == comando]

    @property
    def scripts_python(self) -> list[str]:
        return [c.script for c in self.de("python")]

    @property
    def pipeline_log(self) -> str:
        p = self.project / "logs" / "pipeline_execution.log"
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    def traza(self) -> str:
        lines = [f"run_pipeline.sh (copia en {self.project}) salió con {self.returncode}. Llamadas:"]
        lines += [f"  {c.comando} {c.args}" for c in self.llamadas]
        lines += ["--- stdout ---", self.stdout.rstrip() or "(vacío)",
                  "--- stderr ---", self.stderr.rstrip() or "(vacío)"]
        return "\n".join(lines)


@dataclass
class Sandbox:
    root: Path
    project: Path = field(init=False)
    shims: Path = field(init=False)
    sistema: Path = field(init=False)
    conda_base: Path = field(init=False)
    log: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        h._check_outside_repo(self.root)
        self.project = self.root / "project"
        self.shims = self.root / "shims"
        self.sistema = self.root / "sistema"
        self.conda_base = self.root / "conda"
        self.log = self.root / "llamadas.tsv"
        for d in (self.project / "ml_model", self.project / "scraper", self.project / "logs",
                  self.shims, self.sistema, self.conda_base / "etc" / "profile.d",
                  self.root / "home", self.root / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h.REPO_ROOT / SCRIPT, self.project / SCRIPT)
        (self.conda_base / "etc" / "profile.d" / "conda.sh").write_text(
            "# conda.sh falso de la prueba: 'conda' resuelve al shim del PATH.\n")
        for name in SHIMS:
            p = self.shims / name
            p.write_text("#!/bin/sh\n# Shim de prueba de run_pipeline.sh: registra y no hace nada real.\n"
                         + _REGISTRO + _SHIM_BODIES.get(name, _DEFAULT_BODY))
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        for name in UTILIDADES:
            real = shutil.which(name, path=SISTEMA_PATH)
            if real is None:
                raise RuntimeError(f"No se encontró la utilidad '{name}' en {SISTEMA_PATH}.")
            (self.sistema / name).symlink_to(real)

    # --- estado inicial de la copia -----------------------------------------------------------
    def write(self, rel: str, text: str) -> Path:
        p = self.project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    # --- entorno y candado --------------------------------------------------------------------
    def env(self, fallar: dict[str, int] | None = None, path: str | None = None) -> dict[str, str]:
        proxy = "http://127.0.0.1:9"
        return {
            "PATH": path or f"{self.shims}:{self.sistema}",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "PI_SHIM_LOG": str(self.log),
            "PI_SHIM_CONDA_BASE": str(self.conda_base),
            "PI_SHIM_FALLAR": " ".join(f"{k}={v}" for k, v in (fallar or {}).items()),
            "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy,
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        }

    def check_shims(self, env: dict[str, str]) -> None:
        """Exige que cada comando protegido resuelva a su shim; si no, se niega a correr."""
        probe = 'for c in "$@"; do printf \'%s\\t%s\\t%s\\n\' "$c" "$(type -t "$c")" "$(type -P "$c")"; done'
        r = subprocess.run([BASH, "--noprofile", "--norc", "-c", probe, "probe", *SHIMS],
                           env=env, capture_output=True, text=True, timeout=10)
        problems = []
        for line in r.stdout.splitlines():
            name, kind, where = (line.split("\t") + ["", ""])[:3]
            if kind != "file" or Path(where).parent != self.shims:
                problems.append(f"{name}: tipo={kind or '?'} ruta={where or '?'}")
        if r.returncode != 0 or len(r.stdout.splitlines()) != len(SHIMS) or problems:
            raise ShimNoProtege(
                "Se niega a correr run_pipeline.sh: estos comandos no resuelven a su shim "
                f"en {self.shims} (sudo/shutdown son obligatorios por el trap EXIT): "
                + ("; ".join(problems) or r.stderr.strip() or "salida inesperada"))
        if any(self.project.rglob("*.py")):
            raise ShimNoProtege("La copia del proyecto trae código .py; no se corre.")

    # --- corrida ------------------------------------------------------------------------------
    def run(self, stage: int = 1, fallar: dict[str, int] | None = None) -> Corrida:
        env = self.env(fallar)
        self.check_shims(env)
        self.log.unlink(missing_ok=True)
        args = [BASH, "--noprofile", "--norc", str(self.project / SCRIPT)]
        if stage == 2:
            args += ["--stage", "2"]
        p = subprocess.run(args, cwd=self.project, env=env, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S)
        llamadas = []
        if self.log.is_file():
            for line in self.log.read_text(encoding="utf-8").splitlines():
                comando, cwd, args_ = (line.split("\t", 2) + ["", ""])[:3]
                llamadas.append(Llamada(comando, cwd, args_))
        return Corrida(p.returncode, p.stdout, p.stderr, llamadas, self.project)
