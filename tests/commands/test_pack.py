from __future__ import annotations

import email.policy
import hashlib
import os
import shutil
import sys
from email.generator import BytesGenerator
from email.message import Message
from email.parser import BytesParser
from io import StringIO
from subprocess import CalledProcessError
from zipfile import Path, ZipFile

import pytest
from pytest import TempPathFactory

from wheel._commands import main

from .util import run_command

THISDIR = os.path.dirname(__file__)
TESTWHEEL_NAME = "test-1.0-py2.py3-none-any.whl"
TESTWHEEL_PATH = os.path.join(THISDIR, "..", "testdata", TESTWHEEL_NAME)


def _make_minimal_wheel_dir(
    base_dir: Path,
    name: str = "foo",
    version: str = "1.0",
    tags: list[str] | None = None,
    build: str | None = None,
) -> Path:
    """Create a minimal unpacked wheel directory for testing.

    The directory contains a .dist-info directory with METADATA and WHEEL,
    plus a single Python module.  This is the smallest fixture that can be
    packed into a valid wheel.
    """
    if tags is None:
        tags = ["py3-none-any"]

    dist_info_dir = base_dir / f"{name}-{version}.dist-info"
    dist_info_dir.mkdir(parents=True)

    metadata = Message()
    metadata["Metadata-Version"] = "2.1"
    metadata["Name"] = name
    metadata["Version"] = version
    (dist_info_dir / "METADATA").write_bytes(metadata.as_bytes())

    wheel_info = Message()
    wheel_info["Wheel-Version"] = "1.0"
    wheel_info["Generator"] = "test"
    wheel_info["Root-Is-Purelib"] = "true"
    for tag in tags:
        wheel_info["Tag"] = tag
    if build is not None:
        wheel_info["Build"] = build
    (dist_info_dir / "WHEEL").write_bytes(wheel_info.as_bytes())

    pkg_dir = base_dir / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_bytes(b"")

    return base_dir


@pytest.mark.filterwarnings("error:Duplicate name")
@pytest.mark.parametrize(
    "build_tag_arg, existing_build_tag, filename",
    [
        pytest.param(None, None, "test-1.0-py2.py3-none-any.whl", id="nobuildnum"),
        pytest.param("2b", None, "test-1.0-2b-py2.py3-none-any.whl", id="newbuildarg"),
        pytest.param(None, "3", "test-1.0-3-py2.py3-none-any.whl", id="oldbuildnum"),
        pytest.param("", "3", "test-1.0-py2.py3-none-any.whl", id="erasebuildnum"),
    ],
)
def test_pack(
    tmp_path_factory: TempPathFactory,
    tmp_path: Path,
    build_tag_arg: str | None,
    existing_build_tag: str | None,
    filename: str,
) -> None:
    unpack_dir = tmp_path_factory.mktemp("wheeldir")
    with ZipFile(TESTWHEEL_PATH) as zf:
        old_record = zf.read("test-1.0.dist-info/RECORD")
        old_record_lines = sorted(
            line.rstrip()
            for line in old_record.split(b"\n")
            if line and not line.startswith(b"test-1.0.dist-info/WHEEL,")
        )
        zf.extractall(unpack_dir)

    if existing_build_tag:
        # Add the build number to WHEEL
        wheel_file_path = unpack_dir.joinpath("test-1.0.dist-info").joinpath("WHEEL")
        wheel_file_content = wheel_file_path.read_bytes()
        assert b"Build" not in wheel_file_content
        wheel_file_content += b"Build: 3\r\n"
        wheel_file_path.write_bytes(wheel_file_content)

    args = ["--dest", tmp_path, unpack_dir]
    if build_tag_arg is not None:
        (args.insert(3, "--build"),)
        args.insert(4, build_tag_arg)

    run_command("pack", *args)
    new_wheel_path = tmp_path.joinpath(filename)
    assert new_wheel_path.is_file()

    with ZipFile(new_wheel_path) as zf:
        new_record = zf.read("test-1.0.dist-info/RECORD")
        new_record_lines = sorted(
            line.rstrip()
            for line in new_record.split(b"\n")
            if line and not line.startswith(b"test-1.0.dist-info/WHEEL,")
        )

        parser = BytesParser(policy=email.policy.compat32)
        new_wheel_file_content = parser.parsebytes(zf.read("test-1.0.dist-info/WHEEL"))

    assert new_record_lines == old_record_lines

    # Line endings and trailing blank line will depend on whether WHEEL
    # was modified.  Circumvent this by comparing parsed key/value pairs.
    expected_wheel_content = Message()
    expected_wheel_content["Wheel-Version"] = "1.0"
    expected_wheel_content["Generator"] = "bdist_wheel (0.30.0)"
    expected_wheel_content["Root-Is-Purelib"] = "false"
    expected_wheel_content["Tag"] = "py2-none-any"
    expected_wheel_content["Tag"] = "py3-none-any"
    expected_build_num = (
        build_tag_arg if build_tag_arg is not None else existing_build_tag
    )
    if expected_build_num:
        expected_wheel_content["Build"] = expected_build_num

    assert sorted(new_wheel_file_content.items()) == sorted(
        expected_wheel_content.items()
    )


@pytest.mark.parametrize(
    "local_version_arg, existing_local_version, filename, expected_version",
    [
        pytest.param(
            "l0.cal",
            None,
            "test-1.0+l0.cal-py2.py3-none-any.whl",
            "1.0+l0.cal",
            id="addlocal",
        ),
        pytest.param(
            "new",
            "old",
            "test-1.0+new-py2.py3-none-any.whl",
            "1.0+new",
            id="replacelocal",
        ),
        pytest.param(
            "",
            "old",
            "test-1.0-py2.py3-none-any.whl",
            "1.0",
            id="removelocal",
        ),
    ],
)
def test_pack_local_version(
    tmp_path_factory: TempPathFactory,
    tmp_path: Path,
    local_version_arg: str,
    existing_local_version: str | None,
    filename: str,
    expected_version: str,
) -> None:
    unpack_dir = tmp_path_factory.mktemp("wheeldir")
    with ZipFile(TESTWHEEL_PATH) as zf:
        zf.extractall(unpack_dir)

    dist_info_dir = unpack_dir.joinpath("test-1.0.dist-info")
    if existing_local_version:
        existing_version = f"1.0+{existing_local_version}"
        new_dist_info_dir = unpack_dir.joinpath(f"test-{existing_version}.dist-info")
        dist_info_dir.rename(new_dist_info_dir)
        dist_info_dir = new_dist_info_dir

        wheel_file_path = dist_info_dir.joinpath("WHEEL")
        wheel_file_content = wheel_file_path.read_bytes()
        assert b"Build" not in wheel_file_content

        metadata_path = dist_info_dir.joinpath("METADATA")
        metadata = BytesParser(policy=email.policy.compat32).parsebytes(
            metadata_path.read_bytes()
        )
        del metadata["Version"]
        metadata["Version"] = existing_version
        with open(metadata_path, "wb") as f:
            BytesGenerator(f, maxheaderlen=0).flatten(metadata)

    args = ["--dest", tmp_path, unpack_dir, "--local-version", local_version_arg]
    run_command("pack", *args)
    new_wheel_path = tmp_path.joinpath(filename)
    assert new_wheel_path.is_file()

    with ZipFile(new_wheel_path) as zf:
        dist_info_prefix = f"test-{expected_version}.dist-info"
        parser = BytesParser(policy=email.policy.compat32)
        metadata = parser.parsebytes(zf.read(f"{dist_info_prefix}/METADATA"))
        assert metadata["Version"] == expected_version

        wheel_file_content = parser.parsebytes(zf.read(f"{dist_info_prefix}/WHEEL"))
        assert wheel_file_content["Wheel-Version"] == "1.0"


def test_pack_local_version_rejects_hyphen(
    tmp_path_factory: TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unpack_dir = tmp_path_factory.mktemp("wheeldir")
    with ZipFile(TESTWHEEL_PATH) as zf:
        zf.extractall(unpack_dir)

    argv = [
        "wheel",
        "pack",
        "--dest",
        str(tmp_path),
        str(unpack_dir),
        "--local-version",
        "bad-local",
    ]
    stdout = StringIO()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", argv)
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "bad-local" in stderr.getvalue()


def test_pack_local_version_rejects_invalid(
    tmp_path_factory: TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unpack_dir = tmp_path_factory.mktemp("wheeldir")
    with ZipFile(TESTWHEEL_PATH) as zf:
        zf.extractall(unpack_dir)

    argv = [
        "wheel",
        "pack",
        "--dest",
        str(tmp_path),
        str(unpack_dir),
        "--local-version",
        "!invalid",
    ]
    stdout = StringIO()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", argv)
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "!invalid" in stderr.getvalue()


@pytest.mark.parametrize(
    "build_tag, error",
    [
        pytest.param("foo", "build tag must begin with a digit", id="digitstart"),
        pytest.param("1-f", "invalid character ('-') in build tag", id="hyphen"),
    ],
)
def test_pack_invalid_build_tag(
    tmp_path_factory: TempPathFactory, tmp_path: Path, build_tag: str, error: str
) -> None:
    unpack_dir = tmp_path_factory.mktemp("wheeldir")
    with ZipFile(TESTWHEEL_PATH) as zf:
        zf.extractall(unpack_dir)

    with pytest.raises(CalledProcessError) as exc_info:
        run_command(
            "pack",
            "--dest",
            tmp_path,
            unpack_dir,
            "--build-number",
            build_tag,
            catch_systemexit=False,
        )

    exc = exc_info.value
    assert exc.returncode == 2
    assert f"error: argument --build-number: {error}" in exc.stderr


# ---------------------------------------------------------------------------
# Identity checks: dist-info directory, METADATA Name/Version, and WHEEL tags
# must all be consistent.  The wheel filename is derived from all three, not
# guessed from the directory name alone.
# ---------------------------------------------------------------------------


def test_pack_consistent_foo_1_0_generates_wheel(
    tmp_path_factory: TempPathFactory, tmp_path: Path
) -> None:
    """A consistent foo-1.0 directory produces the correct wheel."""
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    _make_minimal_wheel_dir(wheel_dir, name="foo", version="1.0")

    run_command("pack", "--dest", tmp_path, wheel_dir)

    expected = tmp_path / "foo-1.0-py3-none-any.whl"
    assert expected.is_file()

    with ZipFile(expected) as zf:
        assert "foo-1.0.dist-info/METADATA" in zf.namelist()
        assert "foo-1.0.dist-info/WHEEL" in zf.namelist()
        assert "foo-1.0.dist-info/RECORD" in zf.namelist()
        metadata = BytesParser(policy=email.policy.compat32).parsebytes(
            zf.read("foo-1.0.dist-info/METADATA")
        )
        assert metadata["Name"] == "foo"
        assert metadata["Version"] == "1.0"


def test_pack_metadata_name_mismatch_rejects(
    tmp_path_factory: TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """METADATA Name differing from the dist-info directory name is rejected."""
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    _make_minimal_wheel_dir(wheel_dir, name="foo", version="1.0")

    # Change METADATA Name to "bar" without renaming the dist-info directory
    metadata_path = wheel_dir / "foo-1.0.dist-info" / "METADATA"
    metadata = BytesParser(policy=email.policy.compat32).parsebytes(
        metadata_path.read_bytes()
    )
    del metadata["Name"]
    metadata["Name"] = "bar"
    metadata_path.write_bytes(metadata.as_bytes())

    argv = ["wheel", "pack", "--dest", str(tmp_path), str(wheel_dir)]
    stdout = StringIO()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", argv)
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "does not match METADATA Name" in stderr.getvalue()
    assert not list(tmp_path.glob("*.whl"))


def test_pack_metadata_version_mismatch_rejects(
    tmp_path_factory: TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """METADATA Version differing from the dist-info directory version is rejected."""
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    _make_minimal_wheel_dir(wheel_dir, name="foo", version="1.0")

    metadata_path = wheel_dir / "foo-1.0.dist-info" / "METADATA"
    metadata = BytesParser(policy=email.policy.compat32).parsebytes(
        metadata_path.read_bytes()
    )
    del metadata["Version"]
    metadata["Version"] = "2.0"
    metadata_path.write_bytes(metadata.as_bytes())

    argv = ["wheel", "pack", "--dest", str(tmp_path), str(wheel_dir)]
    stdout = StringIO()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", argv)
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "does not match METADATA Version" in stderr.getvalue()
    assert not list(tmp_path.glob("*.whl"))


def test_pack_multiple_dist_info_rejects(
    tmp_path_factory: TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple .dist-info directories are rejected."""
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    _make_minimal_wheel_dir(wheel_dir, name="foo", version="1.0")

    # Add a second .dist-info directory
    second = wheel_dir / "bar-1.0.dist-info"
    second.mkdir()
    (second / "METADATA").write_bytes(
        b"Metadata-Version: 2.1\nName: bar\nVersion: 1.0\n"
    )
    (second / "WHEEL").write_bytes(b"Wheel-Version: 1.0\nTag: py3-none-any\n")

    argv = ["wheel", "pack", "--dest", str(tmp_path), str(wheel_dir)]
    stdout = StringIO()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", argv)
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "Multiple .dist-info directories" in stderr.getvalue()
    assert not list(tmp_path.glob("*.whl"))


def test_pack_conflicting_tags_rejects(
    tmp_path_factory: TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflicting WHEEL tags (non-round-tripping tagline) are rejected."""
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    # py2-none-any + py3-abi3-any would produce tagline py2.py3-none.abi3-any
    # which expands to 4 tags instead of the original 2
    _make_minimal_wheel_dir(
        wheel_dir,
        name="foo",
        version="1.0",
        tags=["py2-none-any", "py3-abi3-any"],
    )

    argv = ["wheel", "pack", "--dest", str(tmp_path), str(wheel_dir)]
    stdout = StringIO()
    stderr = StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", argv)
        m.setattr(sys, "stdout", stdout)
        m.setattr(sys, "stderr", stderr)
        returncode = main()

    assert returncode == 1
    assert "Conflicting tags" in stderr.getvalue()
    assert not list(tmp_path.glob("*.whl"))


def test_pack_input_unchanged(
    tmp_path_factory: TempPathFactory, tmp_path: Path
) -> None:
    """The input directory is not modified after packing.

    Uses three directories: the input wheel directory, the destination
    directory, and a backup copy for byte-for-byte comparison.
    """
    # Directory 1: the unpacked wheel directory (input)
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    _make_minimal_wheel_dir(wheel_dir, name="foo", version="1.0")

    # Directory 2: destination for the output wheel
    dest_dir = tmp_path_factory.mktemp("destdir")

    # Directory 3: backup of the input tree for comparison
    backup_dir = tmp_path_factory.mktemp("backupdir")
    shutil.copytree(wheel_dir, backup_dir, dirs_exist_ok=True)

    def _tree_hashes(root: Path) -> dict[str, str]:
        hashes = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(root))
                hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    before = _tree_hashes(wheel_dir)

    run_command(
        "pack",
        "--dest",
        dest_dir,
        wheel_dir,
        "--local-version",
        "mod",
        "--build-number",
        "1",
    )

    after = _tree_hashes(wheel_dir)
    assert before == after, "Input directory was modified by pack"

    # The backup should also match
    assert _tree_hashes(backup_dir) == before

    # The output wheel should exist
    assert (dest_dir / "foo-1.0+mod-1-py3-none-any.whl").is_file()


def test_pack_build_tag_interpretation(
    tmp_path_factory: TempPathFactory, tmp_path: Path
) -> None:
    """The build tag is interpreted by the original rules:

    CLI --build-number takes precedence over WHEEL Build; when neither is
    given, no build tag is used; when WHEEL has a Build and no CLI argument
    is given, the WHEEL Build is used.
    """
    # Case 1: WHEEL has Build, no CLI arg -> WHEEL Build is used
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    _make_minimal_wheel_dir(wheel_dir, name="foo", version="1.0", build="3")

    run_command("pack", "--dest", tmp_path, wheel_dir)
    assert (tmp_path / "foo-1.0-3-py3-none-any.whl").is_file()

    # Case 2: CLI arg overrides WHEEL Build
    wheel_dir2 = tmp_path_factory.mktemp("wheeldir2")
    _make_minimal_wheel_dir(wheel_dir2, name="bar", version="2.0", build="3")

    run_command("pack", "--dest", tmp_path, wheel_dir2, "--build-number", "5")
    assert (tmp_path / "bar-2.0-5-py3-none-any.whl").is_file()

    # Case 3: No build anywhere -> no build tag
    wheel_dir3 = tmp_path_factory.mktemp("wheeldir3")
    _make_minimal_wheel_dir(wheel_dir3, name="baz", version="3.0")

    run_command("pack", "--dest", tmp_path, wheel_dir3)
    assert (tmp_path / "baz-3.0-py3-none-any.whl").is_file()

    # Case 4: Empty CLI arg erases WHEEL Build
    wheel_dir4 = tmp_path_factory.mktemp("wheeldir4")
    _make_minimal_wheel_dir(wheel_dir4, name="qux", version="4.0", build="3")

    run_command("pack", "--dest", tmp_path, wheel_dir4, "--build-number", "")
    assert (tmp_path / "qux-4.0-py3-none-any.whl").is_file()


def test_pack_canonical_name_in_filename(
    tmp_path_factory: TempPathFactory, tmp_path: Path
) -> None:
    """The canonical (normalized) name is used for comparison, while the
    display name from METADATA is preserved.  A dist-info directory named
    with underscores matches METADATA Name with hyphens.
    """
    wheel_dir = tmp_path_factory.mktemp("wheeldir")
    # Directory name uses underscore, METADATA uses hyphen (display name)
    _make_minimal_wheel_dir(wheel_dir, name="foo_bar", version="1.0")
    # Override METADATA Name to use hyphen (display name differs from dir name)
    metadata_path = wheel_dir / "foo_bar-1.0.dist-info" / "METADATA"
    metadata = BytesParser(policy=email.policy.compat32).parsebytes(
        metadata_path.read_bytes()
    )
    del metadata["Name"]
    metadata["Name"] = "foo-bar"
    metadata_path.write_bytes(metadata.as_bytes())

    run_command("pack", "--dest", tmp_path, wheel_dir)

    # The wheel filename uses the directory name as-is (foo_bar)
    expected = tmp_path / "foo_bar-1.0-py3-none-any.whl"
    assert expected.is_file()

    with ZipFile(expected) as zf:
        metadata = BytesParser(policy=email.policy.compat32).parsebytes(
            zf.read("foo_bar-1.0.dist-info/METADATA")
        )
        # Display name is preserved in METADATA
        assert metadata["Name"] == "foo-bar"
