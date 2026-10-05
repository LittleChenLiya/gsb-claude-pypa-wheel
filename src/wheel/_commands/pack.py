from __future__ import annotations

import email.policy
import os
import os.path
import re
import stat
from email.generator import BytesGenerator
from email.message import Message
from email.parser import BytesParser
from io import BytesIO
from zipfile import ZipInfo

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ..wheelfile import WHEEL_INFO_RE, WheelError, WheelFile, get_zipinfo_datetime

DIST_INFO_RE = re.compile(r"^(?P<namever>(?P<name>.+?)-(?P<ver>\d.*?))\.dist-info$")


def pack(
    directory: str,
    dest_dir: str,
    build_number: str | None,
    local_version: str | None = None,
) -> None:
    """Repack a previously unpacked wheel directory into a new wheel file.

    The identity of the wheel is verified explicitly before anything is
    published: the .dist-info directory name, the Name/Version fields of
    .dist-info/METADATA and the tags of .dist-info/WHEEL must all agree, as
    they jointly determine the target wheel file name. The directory name is
    never blindly trusted. All validation happens before the wheel is written,
    and the unpacked source tree is never modified.

    :param directory: The unpacked wheel directory
    :param dest_dir: Destination directory (defaults to the current directory)
    :param build_number: Build tag to use in the wheel name
    :param local_version: Local version identifier to add or replace
    """
    # Find the .dist-info directory (there must be exactly one)
    dist_info_dirs = [
        fn
        for fn in os.listdir(directory)
        if os.path.isdir(os.path.join(directory, fn)) and DIST_INFO_RE.match(fn)
    ]
    if len(dist_info_dirs) > 1:
        raise WheelError(f"Multiple .dist-info directories found in {directory}")
    elif not dist_info_dirs:
        raise WheelError(f"No .dist-info directories found in {directory}")

    # Verify the identity declared by .dist-info/METADATA against the
    # .dist-info directory name. The normalized (file) name and the display
    # name are separate things: METADATA's Name is the display name, while
    # the directory and wheel file names use the normalized form.
    dist_info_dir = dist_info_dirs[0]
    match = DIST_INFO_RE.match(dist_info_dir)
    assert match is not None
    dir_name = match.group("name")
    dir_version = match.group("ver")

    metadata_path = os.path.join(directory, dist_info_dir, "METADATA")
    try:
        with open(metadata_path, "rb") as f:
            metadata = BytesParser(policy=email.policy.compat32).parse(f)
    except FileNotFoundError:
        raise WheelError(f"Missing {dist_info_dir}/METADATA file") from None

    display_name = metadata.get("Name")
    if not display_name:
        raise WheelError(f"Missing Name field in {dist_info_dir}/METADATA")

    if canonicalize_name(display_name) != canonicalize_name(dir_name):
        raise WheelError(
            f"Name {display_name!r} in {dist_info_dir}/METADATA does not match "
            f"the .dist-info directory name {dir_name!r}"
        )

    metadata_version = metadata.get("Version")
    if not metadata_version:
        raise WheelError(f"Missing Version field in {dist_info_dir}/METADATA")

    try:
        parsed_dir_version = Version(dir_version)
    except InvalidVersion as e:
        raise WheelError(
            f"Invalid version {dir_version!r} in .dist-info directory name: {e}"
        ) from None

    try:
        parsed_metadata_version = Version(metadata_version)
    except InvalidVersion as e:
        raise WheelError(
            f"Invalid version {metadata_version!r} in {dist_info_dir}/METADATA: {e}"
        ) from None

    if parsed_dir_version != parsed_metadata_version:
        raise WheelError(
            f"Version {metadata_version!r} in {dist_info_dir}/METADATA does not "
            f"match the .dist-info directory version {dir_version!r}"
        )

    if "-" in dir_version:
        raise WheelError(
            f"Invalid version {dir_version!r} in .dist-info directory name: "
            "wheel filenames cannot contain '-' in the version"
        )

    # Read the tags and the existing build number from .dist-info/WHEEL
    wheel_file_path = os.path.join(directory, dist_info_dir, "WHEEL")
    try:
        with open(wheel_file_path, "rb") as f:
            info = BytesParser(policy=email.policy.compat32).parse(f)
    except FileNotFoundError:
        raise WheelError(f"Missing {dist_info_dir}/WHEEL file") from None

    tags: list[str] = info.get_all("Tag", [])
    existing_build_number = info.get("Build")

    if not tags:
        raise WheelError(
            f"No tags present in {dist_info_dir}/WHEEL; cannot determine target "
            f"wheel filename"
        )

    # Each tag must be a single pyver-abi-plat triple; anything else (missing
    # components, or components containing the compressed-tagline separators
    # '-' or '.') cannot be represented unambiguously in a wheel file name and
    # is rejected as conflicting.
    for tag in tags:
        parts = tag.split("-")
        if len(parts) != 3 or not all(parts) or any("." in part for part in parts):
            raise WheelError(
                f"Invalid or conflicting tag {tag!r} in {dist_info_dir}/WHEEL"
            )

    # Determine the effective build number, interpreting it by the same rules
    # as the command line parser: it must begin with a digit and must not
    # contain '-'.
    build_number = build_number if build_number is not None else existing_build_number
    if build_number:
        if not build_number[0].isdigit():
            raise WheelError("build tag must begin with a digit")
        elif "-" in build_number:
            raise WheelError("invalid character ('-') in build tag")

    # Determine the new version, updating the local version identifier if
    # requested
    new_version = dir_version
    if local_version is not None:
        base_version = dir_version.split("+", 1)[0]
        new_version = (
            base_version if local_version == "" else f"{base_version}+{local_version}"
        )
        try:
            Version(new_version)
        except InvalidVersion as e:
            raise WheelError(f"Invalid version {new_version!r}: {e}") from None

        if "-" in new_version:
            raise WheelError(
                f"Invalid local version identifier {local_version!r}: "
                "wheel filenames cannot contain '-' in the version; "
                "use '_' instead of '-'"
            )

    # Compute the target wheel file name from the verified identity. The name
    # component is the normalized name from the .dist-info directory (never
    # the display name from METADATA).
    name_version = f"{dir_name}-{new_version}"
    file_namever = name_version
    if build_number:
        file_namever += "-" + build_number

    tagline = compute_tagline(tags)
    filename = f"{file_namever}-{tagline}.whl"
    if not WHEEL_INFO_RE.match(filename):
        raise WheelError(
            f"The verified identity of {dist_info_dir} does not produce a valid "
            f"wheel filename ({filename!r})"
        )

    # All validation is done at this point; nothing below may fail because of
    # inconsistent input, and the source tree is never modified. Content that
    # needs to change (a renamed .dist-info directory, an updated Version in
    # METADATA or an updated Build in WHEEL) is transformed while writing the
    # archive.
    new_dist_info_dir = f"{name_version}.dist-info"
    overrides: dict[str, bytes] = {}

    if new_version != metadata_version:
        del metadata["Version"]
        metadata["Version"] = new_version
        overrides[f"{new_dist_info_dir}/METADATA"] = _flatten(metadata)

    if build_number is not None and build_number != existing_build_number:
        del info["Build"]
        if build_number:
            info["Build"] = build_number

        overrides[f"{new_dist_info_dir}/WHEEL"] = _flatten(info)

    # Repack the wheel
    wheel_path = os.path.join(dest_dir, filename)
    with WheelFile(wheel_path, "w") as wf:
        print(f"Repacking wheel as {wheel_path}...", end="", flush=True)
        _write_files(wf, directory, dist_info_dir, new_dist_info_dir, overrides)

    print("OK")


def _flatten(message: Message) -> bytes:
    """Serialize an email message to bytes."""
    buffer = BytesIO()
    BytesGenerator(buffer, maxheaderlen=0).flatten(message)
    return buffer.getvalue()


def _write_files(
    wf: WheelFile,
    base_dir: str,
    old_dist_info_dir: str,
    new_dist_info_dir: str,
    overrides: dict[str, bytes],
) -> None:
    """Write the contents of a directory to a wheel file without modifying it.

    Files under the old .dist-info directory are archived under the new
    .dist-info directory name, and files present in ``overrides`` are written
    with the given content instead of what is on disk.
    """
    deferred: list[tuple[str, str]] = []
    old_prefix = old_dist_info_dir + "/"
    for root, dirnames, filenames in os.walk(base_dir):
        # Sort the directory names so that `os.walk` will walk them in a
        # defined order on the next iteration.
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.normpath(os.path.join(root, name))
            if not os.path.isfile(path):
                continue

            arcname = os.path.relpath(path, base_dir).replace(os.path.sep, "/")
            if arcname.startswith(old_prefix):
                arcname = new_dist_info_dir + "/" + arcname[len(old_prefix) :]

            if arcname == wf.record_path:
                pass
            elif root.endswith(".dist-info"):
                deferred.append((path, arcname))
            else:
                _write_file(wf, path, arcname, overrides)

    deferred.sort()
    for path, arcname in deferred:
        _write_file(wf, path, arcname, overrides)


def _write_file(
    wf: WheelFile, path: str, arcname: str, overrides: dict[str, bytes]
) -> None:
    data = overrides.get(arcname)
    if data is None:
        wf.write(path, arcname)
        return

    # Write the override content, preserving the source file's metadata
    st = os.stat(path)
    zinfo = ZipInfo(arcname, date_time=get_zipinfo_datetime(st.st_mtime))
    zinfo.external_attr = (stat.S_IMODE(st.st_mode) | stat.S_IFMT(st.st_mode)) << 16
    zinfo.compress_type = wf.compression
    wf.writestr(zinfo, data)


def compute_tagline(tags: list[str]) -> str:
    """Compute a tagline from a list of tags.

    :param tags: A list of tags
    :return: A tagline
    """
    impls = sorted({tag.split("-")[0] for tag in tags})
    abivers = sorted({tag.split("-")[1] for tag in tags})
    platforms = sorted({tag.split("-")[2] for tag in tags})
    return "-".join([".".join(impls), ".".join(abivers), ".".join(platforms)])
