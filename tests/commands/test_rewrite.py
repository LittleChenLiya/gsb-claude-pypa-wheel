"""Tests for the explicit wheel rewriter."""

from __future__ import annotations

import csv
import hashlib
import shutil
import sys
from io import StringIO, TextIOWrapper
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from wheel._commands import main
from wheel._commands.rewrite import rewrite
from wheel.wheelfile import WheelError, WheelFile, urlsafe_b64encode

from .util import run_command

TESTWHEEL_NAME = "test-1.0-py2.py3-none-any.whl"
TESTWHEEL_PATH = Path(__file__).parent.parent / "testdata" / TESTWHEEL_NAME
DIST_INFO = "test-1.0.dist-info"
RECORD_NAME = f"{DIST_INFO}/RECORD"
TARGET_MEMBER = "hello/hello.py"
NEW_TEXT = b'print("goodbye, world!")\n'


@pytest.fixture
def wheelpath(tmp_path: Path) -> Path:
    fn = tmp_path / TESTWHEEL_NAME
    shutil.copy(TESTWHEEL_PATH, fn)
    return fn


def _record_rows(path: Path) -> dict[str, list[str]]:
    with WheelFile(path) as wf:
        with wf.open(wf.record_path) as record:
            rows = csv.reader(TextIOWrapper(record, newline="", encoding="utf-8"))
            return {row[0]: row for row in rows}


def _add_signature(path: Path, suffix: str = ".p7s") -> str:
    """Add a detached signature attachment to a wheel (as sign_wheel does)."""
    name = f"{DIST_INFO}/RECORD{suffix}"
    with ZipFile(path, "a") as zf:
        zf.writestr(name, b"fake-signature-bytes")
    return name


def test_rewrite_replaces_text_and_new_record_verifies(wheelpath: Path) -> None:
    result = rewrite(str(wheelpath), {TARGET_MEMBER: NEW_TEXT})
    assert result == str(wheelpath)

    # The rewritten wheel opens and fully verifies against its new RECORD.
    with WheelFile(wheelpath) as wf:
        assert wf.read(TARGET_MEMBER) == NEW_TEXT
        assert wf.testzip() is None

    # The regenerated RECORD has a correct hash/size row for the new content.
    rows = _record_rows(wheelpath)
    expected_hash = (
        "sha256="
        + urlsafe_b64encode(hashlib.sha256(NEW_TEXT).digest()).decode("ascii")
    )
    assert rows[TARGET_MEMBER] == [
        TARGET_MEMBER,
        expected_hash,
        str(len(NEW_TEXT)),
    ]
    assert rows[RECORD_NAME][1:] == ["", ""]


def test_rewrite_keeps_other_members_verbatim(wheelpath: Path) -> None:
    with ZipFile(TESTWHEEL_PATH) as before:
        # Directory entries are not regular members; only file members carry
        # content that has to survive the rewrite unchanged.
        file_infos = [
            info
            for info in before.infolist()
            if not info.is_dir()
            and info.filename not in (RECORD_NAME, TARGET_MEMBER)
        ]
        expected = {info.filename: before.read(info.filename) for info in file_infos}

    rewrite(str(wheelpath), {TARGET_MEMBER: NEW_TEXT})

    with ZipFile(wheelpath) as after:
        # Contents of every other file member are byte for byte identical.
        for name, data in expected.items():
            assert after.read(name) == data, name

        # Default strategy preserves compression method and timestamps.
        for info_before in file_infos:
            info_after = after.getinfo(info_before.filename)
            assert (
                info_after.compress_type == info_before.compress_type
            ), info_before.filename
            assert (
                info_after.date_time == info_before.date_time
            ), info_before.filename


def test_rewrite_failure_leaves_old_wheel_readable(wheelpath: Path) -> None:
    original_bytes = wheelpath.read_bytes()

    # The second replacement names a member that does not exist, so the whole
    # rewrite must fail.
    with pytest.raises(WheelError, match="No member named 'does/not/exist.py'"):
        rewrite(
            str(wheelpath),
            {TARGET_MEMBER: NEW_TEXT, "does/not/exist.py": b"data"},
        )

    # The destination file is untouched and still fully readable/verifiable.
    assert wheelpath.read_bytes() == original_bytes
    with WheelFile(wheelpath) as wf:
        assert wf.testzip() is None
        assert wf.read(TARGET_MEMBER) != NEW_TEXT
    with ZipFile(TESTWHEEL_PATH) as original, ZipFile(wheelpath) as current:
        for name in original.namelist():
            assert current.read(name) == original.read(name), name


def test_rewrite_missing_member_alone(wheelpath: Path) -> None:
    original_bytes = wheelpath.read_bytes()
    with pytest.raises(WheelError, match="No member named 'nope.py'"):
        rewrite(str(wheelpath), {"nope.py": NEW_TEXT})
    assert wheelpath.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape.py",
        "/absolute.py",
        "foo//bar.py",
        "foo/./bar.py",
        "foo\\bar.py",
        "C:/drive.py",
    ],
)
def test_rewrite_rejects_unsafe_names(wheelpath: Path, bad_name: str) -> None:
    with pytest.raises(WheelError, match="Invalid member name"):
        rewrite(str(wheelpath), {bad_name: NEW_TEXT})


def test_rewrite_rejects_record_and_directory(wheelpath: Path) -> None:
    with pytest.raises(WheelError, match="RECORD is regenerated"):
        rewrite(str(wheelpath), {RECORD_NAME: b"junk"})
    with pytest.raises(WheelError, match="directory entry"):
        rewrite(str(wheelpath), {"hello/": b""})


@pytest.mark.parametrize("suffix", [".p7s", ".jws"])
def test_rewrite_signed_wheel_requires_explicit_handling(
    wheelpath: Path, suffix: str
) -> None:
    sig_name = _add_signature(wheelpath, suffix)
    original_bytes = wheelpath.read_bytes()

    # Without an explicit decision the rewrite is refused, so a stale,
    # invalid signature can never be left behind as if it were still valid.
    with pytest.raises(WheelError, match="signature attachment"):
        rewrite(str(wheelpath), {TARGET_MEMBER: NEW_TEXT})
    assert wheelpath.read_bytes() == original_bytes

    # With drop_signatures the attachment is omitted and RECORD verifies.
    rewrite(str(wheelpath), {TARGET_MEMBER: NEW_TEXT}, drop_signatures=True)
    with WheelFile(wheelpath) as wf:
        assert sig_name not in wf.namelist()
        assert wf.testzip() is None
        assert wf.read(TARGET_MEMBER) == NEW_TEXT


@pytest.mark.parametrize("suffix", [".p7s", ".jws"])
def test_rewrite_signature_member_cannot_be_replaced(
    wheelpath: Path, suffix: str
) -> None:
    _add_signature(wheelpath, suffix)
    with pytest.raises(WheelError, match="Cannot replace signature attachment"):
        rewrite(
            str(wheelpath),
            {f"{DIST_INFO}/RECORD{suffix}": b"new-signature"},
            drop_signatures=True,
        )


def test_rewrite_compression_and_timestamp_strategies(wheelpath: Path) -> None:
    target_before = ZipFile(wheelpath).getinfo(TARGET_MEMBER)
    assert target_before.compress_type == ZIP_DEFLATED

    rewrite(
        str(wheelpath),
        {TARGET_MEMBER: NEW_TEXT},
        compression="stored",
        timestamps="current",
    )
    with ZipFile(wheelpath) as after:
        info = after.getinfo(TARGET_MEMBER)
        assert info.compress_type == ZIP_STORED
        assert info.date_time != target_before.date_time

    # Invalid strategy values are rejected.
    with pytest.raises(WheelError, match="Invalid compression strategy"):
        rewrite(
            str(wheelpath),
            {TARGET_MEMBER: NEW_TEXT},
            compression="zip",  # type: ignore[arg-type]
        )
    with pytest.raises(WheelError, match="Invalid timestamp strategy"):
        rewrite(
            str(wheelpath),
            {TARGET_MEMBER: NEW_TEXT},
            timestamps="now",  # type: ignore[arg-type]
        )


def test_rewrite_to_separate_output(tmp_path: Path, wheelpath: Path) -> None:
    out = tmp_path / "rewritten" / TESTWHEEL_NAME
    result = rewrite(
        str(wheelpath), {TARGET_MEMBER: NEW_TEXT}, output=str(out)
    )
    assert result == str(out.resolve())
    assert out.exists()
    with WheelFile(out) as wf:
        assert wf.read(TARGET_MEMBER) == NEW_TEXT
        assert wf.testzip() is None
    # The original wheel was left in place and unchanged.
    with ZipFile(wheelpath) as wf:
        assert wf.read(TARGET_MEMBER) != NEW_TEXT


def test_rewrite_rejects_changed_output_namever(
    tmp_path: Path, wheelpath: Path
) -> None:
    bad_out = tmp_path / "test-2.0-py2.py3-none-any.whl"
    with pytest.raises(WheelError, match="same name and version"):
        rewrite(
            str(wheelpath), {TARGET_MEMBER: NEW_TEXT}, output=str(bad_out)
        )
    assert not bad_out.exists()


def test_rewrite_rejects_empty_replacements(wheelpath: Path) -> None:
    with pytest.raises(WheelError, match="No replacements given"):
        rewrite(str(wheelpath), {})


def test_cli_rewrite_and_failure_atomicity(
    tmp_path: Path, wheelpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = tmp_path / "hello.py"
    replacement.write_bytes(NEW_TEXT)
    original_bytes = wheelpath.read_bytes()

    output = run_command(
        "rewrite",
        str(wheelpath),
        "--replace",
        TARGET_MEMBER,
        str(replacement),
    ).strip()
    assert output == str(wheelpath.resolve())
    with WheelFile(wheelpath) as wf:
        assert wf.read(TARGET_MEMBER) == NEW_TEXT
        assert wf.testzip() is None
    rewritten_bytes = wheelpath.read_bytes()
    assert rewritten_bytes != original_bytes

    # A replacement whose source file does not exist fails before the wheel is
    # touched; the command exits non-zero and the wheel stays readable.
    missing = tmp_path / "missing.py"
    stdout, stderr = StringIO(), StringIO()
    with monkeypatch.context() as m:
        m.setattr(
            sys,
            "argv",
            [
                "wheel",
                "rewrite",
                str(wheelpath),
                "--replace",
                "hello/__init__.py",
                str(missing),
            ],
        )
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "Cannot read replacement" in stderr.getvalue()
    assert wheelpath.read_bytes() == rewritten_bytes  # previous rewrite stands
    with WheelFile(wheelpath) as wf:
        assert wf.testzip() is None


def test_cli_rewrite_signed_wheel(
    tmp_path: Path, wheelpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sig_name = _add_signature(wheelpath)
    replacement = tmp_path / "hello.py"
    replacement.write_bytes(NEW_TEXT)

    # Without an explicit signature decision the CLI exits non-zero and the
    # wheel is not modified.
    before = wheelpath.read_bytes()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(
            sys,
            "argv",
            [
                "wheel",
                "rewrite",
                str(wheelpath),
                "--replace",
                TARGET_MEMBER,
                str(replacement),
            ],
        )
        m.setattr(sys, "stdout", StringIO())
        m.setattr(sys, "stderr", stderr)
        assert main() == 1
    assert "signature attachment" in stderr.getvalue()
    assert wheelpath.read_bytes() == before

    output = run_command(
        "rewrite",
        str(wheelpath),
        "--replace",
        TARGET_MEMBER,
        str(replacement),
        "--drop-signatures",
        "--compression",
        "preserve",
        "--timestamps",
        "preserve",
    ).strip()
    assert output == str(wheelpath.resolve())
    with WheelFile(wheelpath) as wf:
        assert sig_name not in wf.namelist()
        assert wf.testzip() is None