"""Ayudantes compartidos para todas las pruebas de Price Intelligence.

Qué deja disponible:
- ``REPO_ROOT`` y ``DATOS_F1``: rutas de la raíz del repo y de los datos congelados de F1.
- ``cargar_modulo("ml_model/2_train_models.py")``: importa un script por su ruta, aunque el
  nombre empiece con número. No ejecuta ``main()`` (los scripts lo protegen con
  ``if __name__ == "__main__"``). El módulo queda en caché: cargarlo dos veces devuelve el mismo.
- ``en_directorio(ruta)``: context manager que cambia el cwd y lo restaura (los scripts de
  ``ml_model/`` usan rutas relativas como ``'ml_model/...'``; correrlos siempre en un tmp).
- Fixtures ``repo_root`` y ``datos_f1`` (esta última salta la prueba con una razón clara si
  ``data/f1_antes_2026-11-15/`` no existe, como pasa en CI).
- Perfiles de Hypothesis: ``pi`` (por omisión: 200 ejemplos por prueba, determinista) y
  ``exploracion`` (2000 ejemplos, aleatorio). Se elige con la variable ``HYPOTHESIS_PROFILE``.
  Las pruebas no fijan ``max_examples`` propio (lo pisaría el perfil); si una necesita menos
  ejemplos por costo, se justifica en la propia prueba.

Candados que aplican a todas las pruebas:
- Cada prueba debe llevar uno de los marcadores ``unit``, ``property``, ``e2e`` o
  ``differential``; si falta, pytest se detiene con ``UsageError`` (una prueba sin marcador
  nunca correría en ``scripts/verify.sh``, que selecciona por marcador).
- Sin red: dentro del proceso de pytest, ``socket.connect`` hacia una dirección que no sea
  local (loopback o socket Unix) lanza ``RuntimeError``. Los subprocesos (E2E) no quedan
  cubiertos por este candado; ahí aplica el de ``scripts/verify.sh``.

Reglas: ninguna prueba llama a AWS, Odoo, Apify ni Bedrock, y ninguna escribe en el repo
(los scripts que escriben CSV se corren en ``tmp_path``).

Importar desde una prueba: ``from tests.conftest import cargar_modulo, REPO_ROOT``.
"""
import contextlib
import importlib.util
import ipaddress
import logging
import os
import socket
import sys
import types
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DATOS_F1 = REPO_ROOT / "data" / "f1_antes_2026-11-15"

# --- Hypothesis -------------------------------------------------------------------------------
# deadline=None: el primer llamado a pandas tarda más que el límite de 200 ms por importar y
# preparar estructuras internas; eso no es un defecto del código y volvía las pruebas inestables.
# pi: derandomize=True, cada corrida usa los mismos ejemplos, así una falla siempre se reproduce
# y verify.sh no cambia de color entre corridas (tampoco se escribe la base .hypothesis/).
# El costo: "pi" nunca prueba entradas nuevas; para eso está "exploracion".
settings.register_profile(
    "pi",
    max_examples=200,
    deadline=None,
    derandomize=True,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
# Para buscar bugs nuevos (corrida aparte, no en el gate; tarda varios minutos):
#   HYPOTHESIS_PROFILE=exploracion python -m pytest -m property -q
settings.register_profile(
    "exploracion",
    max_examples=2000,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "pi"))


# --- Marcadores obligatorios ------------------------------------------------------------------
MARCADORES_DE_SUITE = {"unit", "property", "e2e", "differential"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Detiene la corrida si alguna prueba recolectada no tiene marcador de suite.

    Corre antes de que ``-m`` deseleccione nada, así revisa todas las pruebas recolectadas.
    """
    sin_marcador = [
        item.nodeid
        for item in items
        if not MARCADORES_DE_SUITE & {marca.name for marca in item.iter_markers()}
    ]
    if sin_marcador:
        raise pytest.UsageError(
            "Estas pruebas no tienen ninguno de los marcadores "
            f"{sorted(MARCADORES_DE_SUITE)} y nunca correrían en scripts/verify.sh: "
            + ", ".join(sin_marcador)
            + ". Agrega pytestmark o @pytest.mark.<marcador>."
        )


# --- Candado de red ---------------------------------------------------------------------------
def _es_direccion_local(familia, direccion):
    if familia == getattr(socket, "AF_UNIX", object()):
        return True
    host = direccion[0] if isinstance(direccion, tuple) else direccion
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False  # un nombre de host que no es localhost: se resolvería por red


def _bloquear(original):
    def conectar(self, direccion, *args, **kwargs):
        if not _es_direccion_local(self.family, direccion):
            raise RuntimeError(
                f"Red bloqueada en pruebas: intento de conexión a {direccion!r}. Ninguna prueba "
                "puede llamar a AWS, Odoo, Apify, Bedrock ni a otro servicio externo."
            )
        return original(self, direccion, *args, **kwargs)

    return conectar


@pytest.fixture(scope="session", autouse=True)
def sin_red():
    """Bloquea conexiones a direcciones no locales durante toda la sesión de pytest.

    Es de alcance de sesión (no usa ``monkeypatch``) para que Hypothesis no lo trate como fixture
    por función. Cubre todo lo que corre dentro de las pruebas y fixtures; no cubre la
    recolección (los imports de módulo de los archivos de prueba, que no abren conexiones).
    """
    originales = {nombre: getattr(socket.socket, nombre) for nombre in ("connect", "connect_ex")}
    for nombre, original in originales.items():
        setattr(socket.socket, nombre, _bloquear(original))
    try:
        yield
    finally:
        for nombre, original in originales.items():
            setattr(socket.socket, nombre, original)


# --- Logger de producción ---------------------------------------------------------------------
def _instalar_log_config_de_prueba():
    """Sustituye ``log_config`` por una versión que no escribe archivos.

    El ``log_config.py`` real crea ``../logs/wayakit_app.log`` fuera del repo al importarse.
    Las pruebas no deben escribir ahí (ese log puede mezclarse con el de producción), así que
    los scripts cargados con ``cargar_modulo`` reciben un logger sin handlers de archivo.
    """
    if "log_config" in sys.modules:
        return
    modulo = types.ModuleType("log_config")
    logger = logging.getLogger("wayakit_price_prediction.pruebas")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    modulo.logger = logger
    modulo.get_logger = lambda: logger
    modulo.setup_logging = lambda *args, **kwargs: logger
    sys.modules["log_config"] = modulo


_CACHE_MODULOS = {}


def cargar_modulo(ruta_relativa):
    """Importa un archivo .py del repo por su ruta relativa a la raíz y lo devuelve."""
    if ruta_relativa in _CACHE_MODULOS:
        return _CACHE_MODULOS[ruta_relativa]
    _instalar_log_config_de_prueba()
    ruta = REPO_ROOT / ruta_relativa
    nombre = "pi_prueba_" + ruta_relativa.replace("/", "_").replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    _CACHE_MODULOS[ruta_relativa] = modulo
    return modulo


@contextlib.contextmanager
def en_directorio(ruta):
    """Cambia el directorio de trabajo y lo restaura al salir (Python 3.10 no trae
    ``contextlib.chdir``). Sirve dentro de pruebas de Hypothesis, donde ``monkeypatch`` (de
    alcance por función) no se puede usar."""
    anterior = os.getcwd()
    os.chdir(ruta)
    try:
        yield Path(ruta)
    finally:
        os.chdir(anterior)


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT


@pytest.fixture(scope="session")
def datos_f1():
    """Carpeta con los datos congelados de F1; salta la prueba si no existe."""
    if not DATOS_F1.is_dir():
        pytest.skip(
            f"No existe {DATOS_F1.relative_to(REPO_ROOT)} (datos reales congelados de F1, fuera "
            "de git). Las pruebas e2e y diferenciales los necesitan; en CI se usarán fixtures "
            "sintéticos (F4)."
        )
    return DATOS_F1
