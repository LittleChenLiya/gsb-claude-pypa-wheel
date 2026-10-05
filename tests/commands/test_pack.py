from __future__ import annotations

import email.policy
import os
import re
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
from wheel._commands.pack import pack
from wheel.wheelfile import WheelError

from .util import run_command

THISDIR = os.path.dirname(__file__)
TESTWHEEL_NAME = "test-1.0-py2.py3-none-any.whl"
TESTWHEEL_PATH = os.path.join(THISDIR, "..", "testdata", TESTWHEEL_NAME)


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


# The tests below use minimal local fixtures (a tiny unpacked wheel tree
# created in a temporary directory) to verify that `wheel pack` checks the
# wheel identity explicitly -- the .dist-info directory name, the METADATA
# Name/Version fields and the WHEEL tags jointly determine the wheel file
# name -- and never modifies the input tree, not even on validation failure.
#
# Run them with:
#
#     python -m pytest tests/commands/test_pack.py -v


def _make_minimal_unpacked_wheel(
    root: str,
    *,
    metadata_name: str = "foo",
    tags: tuple[str, ...] = ("py3-none-any",),
    build: str | None = None,
    extra_dist_info: bool = False,
) -> None:
    """Create a minimal unpacked wheel tree for foo-1.0 in *root*."""
    dist_info = os.path.join(root, "foo-1.0.dist-info")
    os.makedirs(dist_info)
    with open(os.path.join(dist_info, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: 1.0\n")

    wheel_lines = ["Wheel-Version: 1.0", "Root-Is-Purelib: true"]
    wheel_lines += [f"Tag: {tag}" for tag in tags]
    if build is not None:
        wheel_lines.append(f"Build: {build}")

    with open(os.path.join(dist_info, "WHEEL"), "w") as f:
        f.write("\n".join(wheel_lines) + "\n")

    with open(os.path.join(dist_info, "RECORD"), "w") as f:
        f.write("")

    with open(os.path.join(root, "foo.py"), "w") as f:
        f.write("print('hello foo')\n")

    if extra_dist_info:
        other = os.path.join(root, "bar-2.0.dist-info")
        os.makedirs(other)
        with open(os.path.join(other, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: bar\nVersion: 2.0\n")


def _snapshot_tree(root: str) -> dict[str, bytes]:
    """Return a {relative path: content} mapping of every file under *root*."""
    snapshot = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            with open(path, "rb") as f:
                snapshot[os.path.relpath(path, root)] = f.read()

    return snapshot


def test_pack_minimal_fixture_consistent(tmp_path: Path) -> None:
    """A consistent foo-1.0 tree produces a correct wheel, input unchanged."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir))
    before = _snapshot_tree(str(unpack_dir))

    pack(str(unpack_dir), str(dest_dir), None)

    wheel_path = dest_dir.joinpath("foo-1.0-py3-none-any.whl")
    assert wheel_path.is_file()
    with ZipFile(wheel_path) as zf:
        names = zf.namelist()
        assert "foo.py" in names
        assert "foo-1.0.dist-info/METADATA" in names
        assert "foo-1.0.dist-info/WHEEL" in names
        assert "foo-1.0.dist-info/RECORD" in names

        parser = BytesParser(policy=email.policy.compat32)
        metadata = parser.parsebytes(zf.read("foo-1.0.dist-info/METADATA"))
        assert metadata["Name"] == "foo"
        assert metadata["Version"] == "1.0"
        wheel_msg = parser.parsebytes(zf.read("foo-1.0.dist-info/WHEEL"))
        assert wheel_msg.get_all("Tag") == ["py3-none-any"]

    assert _snapshot_tree(str(unpack_dir)) == before


def test_pack_minimal_fixture_display_name_is_normalized(tmp_path: Path) -> None:
    """The METADATA display name may differ in case; the file name uses the
    normalized name from the .dist-info directory."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir), metadata_name="Foo")

    pack(str(unpack_dir), str(dest_dir), None)

    assert dest_dir.joinpath("foo-1.0-py3-none-any.whl").is_file()


def test_pack_minimal_fixture_rejects_mismatched_name(tmp_path: Path) -> None:
    """METADATA Name 'bar' in a foo-1.0.dist-info directory is rejected."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir), metadata_name="bar")
    before = _snapshot_tree(str(unpack_dir))

    with pytest.raises(WheelError, match="'bar'.*does not match"):
        pack(str(unpack_dir), str(dest_dir), None)

    assert list(dest_dir.glob("*.whl")) == []
    assert _snapshot_tree(str(unpack_dir)) == before


def test_pack_minimal_fixture_rejects_multiple_dist_info(tmp_path: Path) -> None:
    """Two .dist-info directories are rejected."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir), extra_dist_info=True)
    before = _snapshot_tree(str(unpack_dir))

    with pytest.raises(WheelError, match="Multiple .dist-info directories"):
        pack(str(unpack_dir), str(dest_dir), None)

    assert list(dest_dir.glob("*.whl")) == []
    assert _snapshot_tree(str(unpack_dir)) == before


def test_pack_minimal_fixture_rejects_conflicting_tags(tmp_path: Path) -> None:
    """A tag that cannot be represented in a wheel file name is rejected."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir), tags=("py3-none-any", "py3.none-any"))
    before = _snapshot_tree(str(unpack_dir))

    with pytest.raises(WheelError, match="Invalid or conflicting tag"):
        pack(str(unpack_dir), str(dest_dir), None)

    assert list(dest_dir.glob("*.whl")) == []
    assert _snapshot_tree(str(unpack_dir)) == before


def test_pack_minimal_fixture_existing_build_tag(tmp_path: Path) -> None:
    """An existing Build tag in WHEEL is used in the file name (original
    rules), and the input tree is left untouched."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir), build="2")
    before = _snapshot_tree(str(unpack_dir))

    pack(str(unpack_dir), str(dest_dir), None)

    assert dest_dir.joinpath("foo-1.0-2-py3-none-any.whl").is_file()
    assert _snapshot_tree(str(unpack_dir)) == before


@pytest.mark.parametrize(
    "build_tag, error",
    [
        pytest.param("b1", "build tag must begin with a digit", id="digitstart"),
        pytest.param("1-b", "invalid character ('-') in build tag", id="hyphen"),
    ],
)
def test_pack_minimal_fixture_invalid_build_tag(
    tmp_path: Path, build_tag: str, error: str
) -> None:
    """Build tags are interpreted by the original rules also when pack() is
    called directly; invalid ones are rejected before anything is written."""
    unpack_dir = tmp_path.joinpath("unpacked")
    unpack_dir.mkdir()
    dest_dir = tmp_path.joinpath("dest")
    dest_dir.mkdir()
    _make_minimal_unpacked_wheel(str(unpack_dir))
    before = _snapshot_tree(str(unpack_dir))

    with pytest.raises(WheelError, match=re.escape(error)):
        pack(str(unpack_dir), str(dest_dir), build_tag)

    assert list(dest_dir.glob("*.whl")) == []
    assert _snapshot_tree(str(unpack_dir)) == before
