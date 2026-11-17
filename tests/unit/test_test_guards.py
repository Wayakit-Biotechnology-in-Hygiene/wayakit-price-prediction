"""Self-tests of the locks in ``tests/conftest.py``: blocked network and mandatory marker.

If either lock breaks, a test could call an external service or end up never running
in ``scripts/verify.sh`` without anyone noticing.
"""
import socket
import subprocess
import sys
import textwrap

import pytest

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "family, address",
    [
        (socket.AF_INET, ("203.0.113.10", 443)),  # TEST-NET-3: never answers, but must not be attempted
        (socket.AF_INET, ("example.invalid", 80)),  # a host name would be resolved over the network
        (socket.AF_INET6, ("2001:db8::1", 443)),  # IPv6 documentation range
    ],
)
def test_connection_to_external_address_is_blocked(family, address):
    with socket.socket(family, socket.SOCK_STREAM) as s:
        with pytest.raises(RuntimeError, match="Network blocked"):
            s.connect(address)
        with pytest.raises(RuntimeError, match="Network blocked"):
            s.connect_ex(address)


def test_local_connection_is_still_allowed():
    """Loopback is not blocked: a local test server must be usable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))


def test_test_without_marker_stops_collection(tmp_path):
    """A test without unit/property/e2e/differential makes pytest fail with UsageError (code 4)."""
    test_file = tmp_path / "test_without_marker.py"
    test_file.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.unit
            def test_with_marker():
                pass

            def test_forgotten():
                pass
            """
        )
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "-c", str(REPO_ROOT / "pytest.ini"), "--rootdir", str(REPO_ROOT),
            "-p", "tests.conftest", "-m", "unit", str(test_file),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    error_line = next(l for l in output.splitlines() if "have none of the markers" in l)
    assert "test_forgotten" in error_line
    assert "test_with_marker" not in error_line
