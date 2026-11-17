"""Autopruebas de los candados de ``tests/conftest.py``: red bloqueada y marcador obligatorio.

Si alguno de estos candados se rompe, una prueba podría llamar a un servicio externo o quedarse
sin correr en ``scripts/verify.sh`` sin que nadie lo note.
"""
import socket
import subprocess
import sys
import textwrap

import pytest

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "familia, direccion",
    [
        (socket.AF_INET, ("203.0.113.10", 443)),  # TEST-NET-3: nunca responde, pero no debe intentarse
        (socket.AF_INET, ("example.invalid", 80)),  # un nombre de host se resolvería por red
        (socket.AF_INET6, ("2001:db8::1", 443)),  # rango de documentación IPv6
    ],
)
def test_conexion_a_direccion_externa_se_bloquea(familia, direccion):
    with socket.socket(familia, socket.SOCK_STREAM) as s:
        with pytest.raises(RuntimeError, match="Red bloqueada"):
            s.connect(direccion)
        with pytest.raises(RuntimeError, match="Red bloqueada"):
            s.connect_ex(direccion)


def test_conexion_local_sigue_permitida():
    """Loopback no se bloquea: un servidor local de prueba debe poder usarse."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as servidor:
        servidor.bind(("127.0.0.1", 0))
        servidor.listen(1)
        puerto = servidor.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as cliente:
            cliente.connect(("127.0.0.1", puerto))


def test_prueba_sin_marcador_detiene_la_recoleccion(tmp_path):
    """Una prueba sin unit/property/e2e/differential hace fallar pytest con UsageError (código 4)."""
    archivo = tmp_path / "test_sin_marcador.py"
    archivo.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.unit
            def test_con_marcador():
                pass

            def test_olvidada():
                pass
            """
        )
    )
    resultado = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "-c", str(REPO_ROOT / "pytest.ini"), "--rootdir", str(REPO_ROOT),
            "-p", "tests.conftest", "-m", "unit", str(archivo),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    salida = resultado.stdout + resultado.stderr
    assert resultado.returncode == pytest.ExitCode.USAGE_ERROR, salida
    linea_error = next(l for l in salida.splitlines() if "no tienen ninguno de los marcadores" in l)
    assert "test_olvidada" in linea_error
    assert "test_con_marcador" not in linea_error
