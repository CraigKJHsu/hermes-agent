import hashlib
import json
import os

from tools import file_tools


def test_file_sha256_hashes_regular_file_and_rejects_directory(tmp_path):
    source = tmp_path / "evidence.yaml"
    source.write_bytes(b"model: gpt-6-luna\n")

    result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert result["size"] == source.stat().st_size
    assert result["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": str(tmp_path)}))

    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": str(fifo)}))
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": "~not-a-real-user/file"}))
    assert "error" in json.loads(file_tools._handle_file_sha256({"path": "bad\0path"}))


def test_file_sha256_unsupported_platform_returns_error(tmp_path, monkeypatch):
    source = tmp_path / "evidence.txt"
    source.write_text("safe")
    with monkeypatch.context() as patch:
        patch.setattr(file_tools.sys, "platform", "win32")
        patch.delattr(file_tools.os, "O_NONBLOCK", raising=False)
        result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert "descriptor path verification unavailable" in result["error"]


def test_file_sha256_detects_path_replacement_during_hash(tmp_path, monkeypatch):
    folder = tmp_path / "sources"
    folder.mkdir()
    source = folder / "evidence.txt"
    source.write_text("original")
    real_sha256 = hashlib.sha256

    class MovingDigest:
        def __init__(self):
            self.digest = real_sha256()

        def update(self, chunk):
            folder.rename(tmp_path / "moved")
            folder.mkdir()
            source.write_text("replacement")
            self.digest.update(chunk)

        def hexdigest(self):
            return self.digest.hexdigest()

    with monkeypatch.context() as patch:
        patch.setattr(file_tools.hashlib, "sha256", MovingDigest)
        result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert "file path changed during read" in result["error"]


def test_hash_rejects_early_eof(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = tmp_path / "evidence.txt"
    source.write_text("safe")
    real_fstat = os.fstat

    def advertised_size(fd):
        value = real_fstat(fd)
        fields = {name: getattr(value, name) for name in (
            "st_mode", "st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns",
        )}
        return SimpleNamespace(**fields, st_size=value.st_size + 1)

    with monkeypatch.context() as patch:
        patch.setattr(file_tools.os, "fstat", advertised_size)
        result = json.loads(file_tools._handle_file_sha256({"path": str(source)}))
    assert "file changed during read" in result["error"]
