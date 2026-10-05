from __future__ import annotations

import email.policy
import os.path
import re
from email.generator import BytesGenerator
from email.parser import BytesParser
from io import BytesIO
from itertools import product

from packaging.tags import parse_tag
from packaging.utils import canonicalize_name, canonicalize_version
from packaging.version import InvalidVersion, Version

from ..wheelfile import WheelError, WheelFile

DIST_INFO_RE = re.compile(r"^(?P<namever>(?P<name>.+?)-(?P<ver>\d.*?))\.dist-info$")


def _parse_dist_info_name(dist_info_dir: str) -> tuple[str, str]:
    match = DIST_INFO_RE.match(dist_info_dir)
    if not match:
        raise WheelError(f"Invalid .dist-info directory name: {dist_info_dir!r}")
    return match.group("name"), match.group("ver")


def _read_metadata(directory: str, dist_info_dir: str) -> tuple[str, str]:
    metadata_path = os.path.join(directory, dist_info_dir, "METADATA")
    try:
        with open(metadata_path, "rb") as f:
            metadata = BytesParser(policy=email.policy.compat32).parse(f)
    except FileNotFoundError:
        raise WheelError(f"Missing {dist_info_dir}/METADATA file") from None

    name = metadata["Name"]
    version = metadata["Version"]
    if not name or not version:
        raise WheelError(f"{dist_info_dir}/METADATA must contain both Name and Version")
    return name, version


def _read_wheel_tags(
    directory: str, dist_info_dir: str
) -> tuple[list[str], str | None]:
    wheel_path = os.path.join(directory, dist_info_dir, "WHEEL")
    try:
        with open(wheel_path, "rb") as f:
            info = BytesParser(policy=email.policy.compat32).parse(f)
    except FileNotFoundError:
        raise WheelError(f"Missing {dist_info_dir}/WHEEL file") from None

    tags: list[str] = info.get_all("Tag", [])
    if not tags:
        raise WheelError(
            f"No tags present in {dist_info_dir}/WHEEL; cannot determine target "
            f"wheel filename"
        )

    return tags, info.get("Build")


def _validate_tags(tags: list[str]) -> None:
    tag_set: set[str] = set()
    for tag in tags:
        tag_set.update(str(t) for t in parse_tag(tag))

    impls = sorted({tag.split("-")[0] for tag in tag_set})
    abivers = sorted({tag.split("-")[1] for tag in tag_set})
    platforms = sorted({tag.split("-")[2] for tag in tag_set})

    expected = {f"{i}-{a}-{p}" for i, a, p in product(impls, abivers, platforms)}

    if expected != tag_set:
        tagline = "-".join([".".join(impls), ".".join(abivers), ".".join(platforms)])
        extra = sorted(expected - tag_set)
        raise WheelError(
            f"Conflicting tags in WHEEL: {tags!r} would produce tagline "
            f"{tagline!r} which expands to {len(expected)} tags, but only "
            f"{len(tag_set)} are present; extra tags: {extra}"
        )


def _validate_build_tag(build_tag: str | None) -> None:
    if build_tag is None:
        return
    if build_tag and not build_tag[0].isdigit():
        raise WheelError("Build tag must begin with a digit")
    if "-" in build_tag:
        raise WheelError("Invalid character hyphen in build tag")


def _render_metadata(directory: str, dist_info_dir: str, new_version: str) -> bytes:
    metadata_path = os.path.join(directory, dist_info_dir, "METADATA")
    with open(metadata_path, "rb") as f:
        metadata = BytesParser(policy=email.policy.compat32).parse(f)

    del metadata["Version"]
    metadata["Version"] = new_version

    buf = BytesIO()
    BytesGenerator(buf, maxheaderlen=0).flatten(metadata)
    return buf.getvalue()


def _render_wheel(
    directory: str, dist_info_dir: str, build_number: str | None
) -> bytes:
    wheel_path = os.path.join(directory, dist_info_dir, "WHEEL")
    with open(wheel_path, "rb") as f:
        info = BytesParser(policy=email.policy.compat32).parse(f)

    del info["Build"]
    if build_number:
        info["Build"] = build_number

    buf = BytesIO()
    BytesGenerator(buf, maxheaderlen=0).flatten(info)
    return buf.getvalue()


def _write_files(
    wf: WheelFile,
    directory: str,
    old_dist_info_dir: str,
    new_dist_info_dir: str,
    metadata_content: bytes | None,
    wheel_content: bytes | None,
) -> None:
    for root, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue

            arcname = os.path.relpath(path, directory).replace(os.path.sep, "/")

            if arcname.startswith(old_dist_info_dir + "/"):
                arcname = new_dist_info_dir + arcname[len(old_dist_info_dir) :]

            if arcname == wf.record_path:
                continue

            if (
                arcname == new_dist_info_dir + "/METADATA"
                and metadata_content is not None
            ):
                wf.writestr(arcname, metadata_content)
            elif arcname == new_dist_info_dir + "/WHEEL" and wheel_content is not None:
                wf.writestr(arcname, wheel_content)
            else:
                wf.write(path, arcname)


def pack(
    directory: str,
    dest_dir: str,
    build_number: str | None,
    local_version: str | None = None,
) -> None:
    """Repack a previously unpacked wheel directory into a new wheel file.

    The .dist-info/WHEEL file must contain one or more tags so that the target
    wheel file name can be determined.  The wheel filename is derived from the
    .dist-info directory name, the METADATA Name/Version, and the WHEEL tags;
    these must all be consistent.  The input directory is never modified.

    :param directory: The unpacked wheel directory
    :param dest_dir: Destination directory (defaults to the current directory)
    :param build_number: Build tag to use in the wheel name
    :param local_version: Local version identifier to add or replace
    """
    # Find the .dist-info directory
    dist_info_dirs = [
        fn
        for fn in os.listdir(directory)
        if os.path.isdir(os.path.join(directory, fn)) and DIST_INFO_RE.match(fn)
    ]
    if len(dist_info_dirs) > 1:
        raise WheelError(f"Multiple .dist-info directories found in {directory}")
    elif not dist_info_dirs:
        raise WheelError(f"No .dist-info directories found in {directory}")

    dist_info_dir = dist_info_dirs[0]
    dir_name, dir_version = _parse_dist_info_name(dist_info_dir)

    # Read and cross-check METADATA
    metadata_name, metadata_version = _read_metadata(directory, dist_info_dir)

    if canonicalize_name(dir_name) != canonicalize_name(metadata_name):
        raise WheelError(
            f".dist-info directory name {dir_name!r} does not match METADATA "
            f"Name {metadata_name!r}"
        )

    if canonicalize_version(dir_version) != canonicalize_version(metadata_version):
        raise WheelError(
            f".dist-info directory version {dir_version!r} does not match "
            f"METADATA Version {metadata_version!r}"
        )

    # Read and validate WHEEL tags
    tags, existing_build = _read_wheel_tags(directory, dist_info_dir)
    _validate_tags(tags)

    # Determine the build tag (CLI argument takes precedence)
    build_number = build_number if build_number is not None else existing_build
    _validate_build_tag(build_number)

    # Determine the namever for the .dist-info directory (no build tag)
    namever = f"{dir_name}-{dir_version}"
    new_dist_info_dir = dist_info_dir
    metadata_content: bytes | None = None
    wheel_content: bytes | None = None

    # Apply local version identifier (without modifying the input tree)
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
                "wheel filenames cannot contain hyphen in the version; "
                "use underscore instead of hyphen"
            )

        namever = f"{dir_name}-{new_version}"
        new_dist_info_dir = f"{namever}.dist-info"
        metadata_content = _render_metadata(directory, dist_info_dir, new_version)

    # Render WHEEL if the build tag changed
    if build_number is not None and build_number != existing_build:
        wheel_content = _render_wheel(directory, dist_info_dir, build_number)

    # Compute the wheel filename (namever + optional build tag + tagline)
    filename_namever = namever
    if build_number:
        filename_namever += "-" + build_number

    tagline = compute_tagline(tags)
    wheel_path = os.path.join(dest_dir, f"{filename_namever}-{tagline}.whl")

    # Repack the wheel (input tree is not modified)
    with WheelFile(wheel_path, "w") as wf:
        print(f"Repacking wheel as {wheel_path}...", end="", flush=True)
        _write_files(
            wf,
            directory,
            dist_info_dir,
            new_dist_info_dir,
            metadata_content,
            wheel_content,
        )

    print("OK")


def compute_tagline(tags: list[str]) -> str:
    """Compute a tagline from a list of tags.

    :param tags: A list of tags
    :return: A tagline
    """
    tag_set: set[str] = set()
    for tag in tags:
        tag_set.update(str(t) for t in parse_tag(tag))

    impls = sorted({tag.split("-")[0] for tag in tag_set})
    abivers = sorted({tag.split("-")[1] for tag in tag_set})
    platforms = sorted({tag.split("-")[2] for tag in tag_set})
    return "-".join([".".join(impls), ".".join(abivers), ".".join(platforms)])
