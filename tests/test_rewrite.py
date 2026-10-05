from __future__ import annotations

import base64
import datetime
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from wheel.wheelfile import WheelError, WheelFile
from wheel._commands.rewrite import rewrite

NL = chr(10)


def _make_wheel(path, entries, record_entries=None):
    dist_info = "test-1.0.dist-info"
    record_path = dist_info + "/RECORD"
    with ZipFile(path, "w") as zf:
        for arcname, data in entries.items():
            if arcname == record_path:
                continue
            zf.writestr(arcname, data)

        if record_entries is None:
            record_entries = {}
            for arcname, data in entries.items():
                if arcname == record_path:
                    continue
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                record_entries[arcname] = ("sha256", digest, len(data))

        lines = []
        for arcname, (algo, digest, size) in record_entries.items():
            lines.append("%s,%s=%s,%d" % (arcname, algo, digest, size))
        lines.append(record_path + ",,")
        zf.writestr(record_path, NL.join(lines) + NL)


def _read_wheel(path):
    result = {}
    with ZipFile(path, "r") as zf:
        for info in zf.infolist():
            result[info.filename] = zf.read(info.filename)
    return result


def _record_hashes(path):
    import base64
    import csv
    from io import StringIO

    with ZipFile(path, "r") as zf:
        record_path = None
        for name in zf.namelist():
            if name.endswith("/RECORD"):
                record_path = name
                break
        assert record_path is not None
        data = zf.read(record_path).decode("utf-8")

    result = {}
    for row in csv.reader(StringIO(data)):
        if not row or not row[1]:
            continue
        arcname, hash_str, size = row
        algo, digest = hash_str.split("=")
        result[arcname] = (algo, digest, int(size))
    return result


@pytest.fixture
def wheel_path(tmp_path):
    return tmp_path / "test-1.0-py2.py3-none-any.whl"


@pytest.fixture
def sample_entries():
    return {
        "hello/__init__.py": b"print('hello')" + NL.encode(),
        "hello/hello.py": b"def hello():" + NL.encode() + b"    return 'world'" + NL.encode(),
        "test-1.0.dist-info/METADATA": b"Metadata-Version: 2.1" + NL.encode() + b"Name: test" + NL.encode() + b"Version: 1.0" + NL.encode(),
        "test-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0" + NL.encode(),
    }


@pytest.fixture
def sample_wheel(wheel_path, sample_entries):
    _make_wheel(wheel_path, sample_entries)
    return wheel_path


class TestRewriteBasic:
    def test_replace_one_text(self, sample_wheel):
        new_content = b"def hello():" + NL.encode() + b"    return 'new world'" + NL.encode()
        rewrite(str(sample_wheel), {"hello/hello.py": new_content})

        entries = _read_wheel(sample_wheel)
        assert entries["hello/hello.py"] == new_content

        record = _record_hashes(sample_wheel)
        for arcname, (algo, digest, size) in record.items():
            actual = entries[arcname]
            assert size == len(actual)
            import base64
            expected = base64.urlsafe_b64encode(
                hashlib.new(algo, actual).digest()
            ).rstrip(b"=").decode()
            assert digest == expected

    def test_other_files_byte_identical(self, sample_wheel, sample_entries):
        original = _read_wheel(sample_wheel)
        new_content = b"def hello():" + NL.encode() + b"    return 'new world'" + NL.encode()
        rewrite(str(sample_wheel), {"hello/hello.py": new_content})

        entries = _read_wheel(sample_wheel)
        for arcname, data in original.items():
            if arcname == "hello/hello.py":
                continue
            if arcname.endswith("/RECORD"):
                # RECORD is regenerated with new hashes, so it is expected to
                # differ. Its correctness is verified by other tests.
                continue
            assert entries[arcname] == data

    def test_replace_metadata(self, sample_wheel, sample_entries):
        new_metadata = b"Metadata-Version: 2.1" + NL.encode() + b"Name: test" + NL.encode() + b"Version: 2.0" + NL.encode()
        rewrite(str(sample_wheel), {"test-1.0.dist-info/METADATA": new_metadata})

        entries = _read_wheel(sample_wheel)
        assert entries["test-1.0.dist-info/METADATA"] == new_metadata
        assert entries["hello/__init__.py"] == sample_entries["hello/__init__.py"]
        assert entries["hello/hello.py"] == sample_entries["hello/hello.py"]

    def test_replace_multiple(self, sample_wheel, sample_entries):
        new_hello = b"print('new hello')" + NL.encode()
        new_metadata = b"Metadata-Version: 2.1" + NL.encode() + b"Name: test" + NL.encode() + b"Version: 3.0" + NL.encode()
        rewrite(
            str(sample_wheel),
            {
                "hello/__init__.py": new_hello,
                "test-1.0.dist-info/METADATA": new_metadata,
            },
        )

        entries = _read_wheel(sample_wheel)
        assert entries["hello/__init__.py"] == new_hello
        assert entries["test-1.0.dist-info/METADATA"] == new_metadata
        assert entries["hello/hello.py"] == sample_entries["hello/hello.py"]


class TestRewriteFailures:
    def test_second_replacement_fails_old_wheel_readable(self, sample_wheel):
        original = _read_wheel(sample_wheel)
        original_bytes = sample_wheel.read_bytes()

        with pytest.raises(WheelError, match="Entry not found"):
            rewrite(
                str(sample_wheel),
                {
                    "hello/hello.py": b"new content" + NL.encode(),
                    "nonexistent/file.py": b"new" + NL.encode(),
                },
            )

        assert sample_wheel.read_bytes() == original_bytes
        entries = _read_wheel(sample_wheel)
        for arcname, data in original.items():
            assert entries[arcname] == data

    def test_missing_entry(self, sample_wheel):
        with pytest.raises(WheelError, match="Entry not found"):
            rewrite(str(sample_wheel), {"nonexistent.py": b"new" + NL.encode()})

    def test_unsafe_path_traversal(self, sample_wheel):
        with pytest.raises(WheelError, match="Unsafe entry path"):
            rewrite(str(sample_wheel), {"../evil.py": b"new" + NL.encode()})

    def test_unsafe_absolute_path(self, sample_wheel):
        with pytest.raises(WheelError, match="Unsafe entry path"):
            rewrite(str(sample_wheel), {"/absolute/path.py": b"new" + NL.encode()})

    def test_unsafe_backslash_path(self, sample_wheel):
        with pytest.raises(WheelError, match="Unsafe entry path"):
            rewrite(str(sample_wheel), {r"sub\file.py": b"new" + NL.encode()})

    def test_no_replacements(self, sample_wheel):
        with pytest.raises(WheelError, match="No replacements specified"):
            rewrite(str(sample_wheel), {})

    def test_dest_not_overwritten_on_failure(self, sample_wheel, tmp_path):
        dest = tmp_path / "output.whl"
        dest.write_bytes(b"original destination content")

        with pytest.raises(WheelError):
            rewrite(
                str(sample_wheel),
                {"nonexistent.py": b"new" + NL.encode()},
                dest=str(dest),
            )

        assert dest.read_bytes() == b"original destination content"


class TestRewriteSignatures:
    def _make_signed_wheel(self, path, entries):
        dist_info = "test-1.0.dist-info"
        record_path = dist_info + "/RECORD"
        jws_path = record_path + ".jws"
        with ZipFile(path, "w") as zf:
            for arcname, data in entries.items():
                zf.writestr(arcname, data)
            lines = []
            for arcname, data in entries.items():
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                lines.append("%s,sha256=%s,%d" % (arcname, digest, len(data)))
            lines.append(record_path + ",,")
            zf.writestr(record_path, NL.join(lines) + NL)
            zf.writestr(jws_path, b"fake-jws-signature")

    def test_signature_rejected_by_default(self, tmp_path):
        wheel = tmp_path / "test-1.0-py2.py3-none-any.whl"
        self._make_signed_wheel(
            wheel,
            {
                "hello/__init__.py": b"print('hello')" + NL.encode(),
                "test-1.0.dist-info/METADATA": b"Metadata-Version: 2.1" + NL.encode(),
            },
        )

        with pytest.raises(WheelError, match="embedded signature"):
            rewrite(str(wheel), {"hello/__init__.py": b"print('new')" + NL.encode()})

    def test_signature_removed_with_flag(self, tmp_path):
        wheel = tmp_path / "test-1.0-py2.py3-none-any.whl"
        self._make_signed_wheel(
            wheel,
            {
                "hello/__init__.py": b"print('hello')" + NL.encode(),
                "test-1.0.dist-info/METADATA": b"Metadata-Version: 2.1" + NL.encode(),
            },
        )

        with pytest.warns(UserWarning, match="signature"):
            rewrite(
                str(wheel),
                {"hello/__init__.py": b"print('new')" + NL.encode()},
                remove_signatures=True,
            )

        entries = _read_wheel(wheel)
        assert "test-1.0.dist-info/RECORD.jws" not in entries
        assert entries["hello/__init__.py"] == b"print('new')" + NL.encode()

    def test_p7s_signature_removed(self, tmp_path):
        wheel = tmp_path / "test-1.0-py2.py3-none-any.whl"
        dist_info = "test-1.0.dist-info"
        record_path = dist_info + "/RECORD"
        self._make_signed_wheel(
            wheel,
            {
                "hello/__init__.py": b"print('hello')" + NL.encode(),
                "test-1.0.dist-info/METADATA": b"Metadata-Version: 2.1" + NL.encode(),
            },
        )
        # Replace the .jws with a .p7s signature
        with ZipFile(wheel, "r") as zf:
            entries = {info.filename: zf.read(info.filename) for info in zf.infolist()}
        entries.pop(record_path + ".jws")
        entries[record_path + ".p7s"] = b"fake-p7s"
        with ZipFile(wheel, "w") as zf:
            for arcname, data in entries.items():
                zf.writestr(arcname, data)

        with pytest.warns(UserWarning, match="signature"):
            rewrite(
                str(wheel),
                {"hello/__init__.py": b"print('new')" + NL.encode()},
                remove_signatures=True,
            )

        entries = _read_wheel(wheel)
        assert record_path + ".p7s" not in entries
        assert record_path + ".jws" not in entries


class TestRewriteCompression:
    def test_preserve_compression(self, sample_wheel):
        with ZipFile(sample_wheel, "r") as zf:
            original_compression = {
                info.filename: info.compress_type for info in zf.infolist()
            }

        rewrite(str(sample_wheel), {"hello/hello.py": b"new content" + NL.encode()})

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                if info.filename == "hello/hello.py":
                    continue
                assert info.compress_type == original_compression[info.filename]

    def test_override_compression_stored(self, sample_wheel):
        rewrite(
            str(sample_wheel),
            {"hello/hello.py": b"new content" + NL.encode()},
            compression=ZIP_STORED,
        )

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                assert info.compress_type == ZIP_STORED

    def test_override_compression_deflated(self, sample_wheel):
        rewrite(
            str(sample_wheel),
            {"hello/hello.py": b"new content" + NL.encode()},
            compression=ZIP_DEFLATED,
        )

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                assert info.compress_type == ZIP_DEFLATED


class TestRewriteTimestamp:
    def test_preserve_timestamp(self, sample_wheel):
        with ZipFile(sample_wheel, "r") as zf:
            original_timestamps = {
                info.filename: info.date_time for info in zf.infolist()
            }

        rewrite(str(sample_wheel), {"hello/hello.py": b"new content" + NL.encode()})

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                if info.filename == "hello/hello.py":
                    continue
                assert info.date_time == original_timestamps[info.filename]

    def test_override_timestamp(self, sample_wheel):
        ts = datetime.datetime(2020, 6, 15, 12, 30, 0)
        rewrite(
            str(sample_wheel),
            {"hello/hello.py": b"new content" + NL.encode()},
            timestamp=ts,
        )

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                assert info.date_time == (2020, 6, 15, 12, 30, 0)

    def test_override_timestamp_iso_string(self, sample_wheel):
        rewrite(
            str(sample_wheel),
            {"hello/hello.py": b"new content" + NL.encode()},
            timestamp="2021-01-01T00:00:00",
        )

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                assert info.date_time == (2021, 1, 1, 0, 0, 0)


class TestRewriteDest:
    def test_write_to_dest(self, sample_wheel, tmp_path):
        original_bytes = sample_wheel.read_bytes()
        dest_dir = tmp_path / "output"
        dest_dir.mkdir()
        dest = dest_dir / "test-1.0-py2.py3-none-any.whl"

        rewrite(
            str(sample_wheel),
            {"hello/hello.py": b"new content" + NL.encode()},
            dest=str(dest),
        )

        assert sample_wheel.read_bytes() == original_bytes
        entries = _read_wheel(dest)
        assert entries["hello/hello.py"] == b"new content" + NL.encode()

    def test_overwrite_in_place(self, sample_wheel):
        rewrite(str(sample_wheel), {"hello/hello.py": b"new content" + NL.encode()})

        entries = _read_wheel(sample_wheel)
        assert entries["hello/hello.py"] == b"new content" + NL.encode()


class TestRewriteWithWheelFile:
    def test_rewritten_wheel_passes_wheelfile(self, sample_wheel):
        rewrite(str(sample_wheel), {"hello/hello.py": b"new content" + NL.encode()})

        with WheelFile(sample_wheel) as wf:
            wf.testzip()

    def test_rewritten_wheel_record_hashes_valid(self, sample_wheel):
        rewrite(str(sample_wheel), {"hello/hello.py": b"new content" + NL.encode()})

        with WheelFile(sample_wheel) as wf:
            for name in wf.namelist():
                if name == wf.record_path:
                    continue
                wf.read(name)


class TestRewriteExternalAttr:
    def test_preserve_external_attr(self, sample_wheel):
        with ZipFile(sample_wheel, "r") as zf:
            original_attrs = {
                info.filename: info.external_attr for info in zf.infolist()
            }

        rewrite(str(sample_wheel), {"hello/hello.py": b"new content" + NL.encode()})

        with ZipFile(sample_wheel, "r") as zf:
            for info in zf.infolist():
                if info.filename == "hello/hello.py":
                    continue
                if info.filename.endswith("/RECORD"):
                    # RECORD is regenerated with default attributes.
                    continue
                assert info.external_attr == original_attrs[info.filename]
