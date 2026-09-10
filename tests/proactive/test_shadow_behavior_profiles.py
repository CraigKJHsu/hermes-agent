"""Exercise actual CPython entry points in fresh processes; never contact a remote host."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("operation", ["udp", "tcp", "fork", "subprocess"])
def test_shadow_blocks_socket_and_process_entry_points(operation):
    code = r'''
import os, socket, subprocess, sys
from scripts.shadow_behavior_profiles import deny_external_effects
# Create sockets before the hook so the test exercises sendto/connect too.
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sys.addaudithook(deny_external_effects)
operation = sys.argv[1]
try:
    if operation == "udp":
        udp.sendto(b"local guard probe", ("127.0.0.1", 9))
    elif operation == "tcp":
        tcp.connect(("127.0.0.1", 9))
    elif operation == "fork":
        pid = os.fork()
        if pid == 0:
            os._exit(5)
        os.waitpid(pid, 0)
    else:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
except RuntimeError as exc:
    assert "Shadow replay prohibits" in str(exc), str(exc)
else:
    raise AssertionError("entry point was not blocked: " + operation)
finally:
    udp.close()
    tcp.close()
'''
    result = subprocess.run([sys.executable, "-c", code, operation],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
