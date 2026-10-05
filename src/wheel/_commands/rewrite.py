"""Explicitly replace existing members of a wheel archive.

Only the members named in the replacement mapping are changed; every other
member is copied with its original contents unchanged. The ``RECORD`` file is
always regenerated so that it matches the new contents, and signature
attachments (``RECORD.jws`` / ``RECORD.p7s``) are handled explicitly because
modifying any member invalidates them.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import os
import os.path
import shutil
import tempfile
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Literal
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from ..wheelfile import (
    WHEEL_INFO_RE,
    WheelError,
    WheelFile,
    get_zipinfo_datetime,
)
from .tags import _strip_zip64_extra

__all__ = ["rewrite"]

CompressionStrategy = Literal["preserve", "stored", "deflated"]
TimestampStrategy = Literal["preserve", "current"]

# Suffixes used by the PEP 427 wheel signature mechanisms.
SIGNATURE_SUFFIXES = (".jws", ".p7s")


def _validate_arcname(arcname: str) -> None:
    """Validate that *arcname* is a safe, PEP 427-style archive member name."""
    if not isinstance(arcname, str) or not arcname:
        raise WheelError("Archive member names must be non-empty strings")
    if "\\" in arcname:
        raise WheelError(
            f"Invalid member name {arcname!r}: wheel paths must use forward "
            "slashes ('/'), not backslashes"
        )
    if arcname.startswith("/"):
        raise WheelError(
            f"Invalid member name {arcname!r}: wheel paths must be relative "
            "(no leading '/')"
        )
    # A single trailing slash is the normal spelling of a ZIP directory
    # entry; strip just that one slash for component validation so that a
    # directory name can be resolved and rejected explicitly as a
    # non-replaceable member (but e.g. "foo//bar.py" is still invalid).
    candidate = arcname[:-1] if arcname.endswith("/") else arcname
    parts = candidate.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise WheelError(
            f"Invalid member name {arcname!r}: wheel paths must not contain "
            "empty, '.' or '..' components"
        )
    if ":" in parts[0]:
        raise WheelError(
            f"Invalid member name {arcname!r}: wheel paths must not contain a "
            "drive letter"
        )
    try:
        arcname.encode("utf-8")
    except UnicodeEncodeError as e:
        raise WheelError(f"Invalid member name {arcname!r}: {e}") from e


def _is_signature(arcname: str) -> bool:
    return arcname.endswith(SIGNATURE_SUFFIXES)


def _resolved_zinfo(
    zinfo: ZipInfo,
    compress_type: int,
    timestamp_strategy: TimestampStrategy,
) -> ZipInfo:
    """Return a copy of *zinfo* with the chosen compression/timestamp policy."""
    zinfo = copy.copy(zinfo)
    zinfo.extra = _strip_zip64_extra(zinfo.extra)
    zinfo.compress_type = compress_type
    if timestamp_strategy == "current":
        zinfo.date_time = get_zipinfo_datetime()
    return zinfo


def rewrite(
    wheel: str | os.PathLike[str],
    replacements: Mapping[str, bytes],
    output: str | os.PathLike[str] | None = None,
    *,
    drop_signatures: bool = False,
    compression: CompressionStrategy = "preserve",
    timestamps: TimestampStrategy = "preserve",
) -> str:
    """Rewrite *wheel*, replacing only the members listed in *replacements*.

    Each key of *replacements* must be the exact name of an existing regular
    file member of the wheel; selecting a member that does not exist, a
    directory entry, the ``RECORD`` file or a signature attachment is an
    error, as are duplicate (including case-differing) member names.

    All checks and reads happen before the new archive is written. The new
    archive is assembled in a temporary file and verified before it replaces
    the destination, so any failure leaves the original wheel untouched and
    readable.

    Members not present in *replacements* are copied with their original
    contents unchanged. Their compression method and timestamp are also kept
    by default (``compression="preserve"``, ``timestamps="preserve"``).

    Because replacing any member invalidates embedded signatures, wheels that
    contain ``.jws``/``.p7s`` signature attachments must be rewritten with
    *drop_signatures* set; the attachments are then omitted instead of being
    kept while no longer valid.

    :param wheel: Path to the wheel to rewrite.
    :param replacements: Mapping of archive member name to new bytes content.
    :param output: Destination path (defaults to overwriting *wheel* in place).
    :param drop_signatures: Omit ``RECORD.jws``/``RECORD.p7s`` attachments.
    :param compression: ``"preserve"`` (default), ``"stored"`` or
        ``"deflated"`` compression for every member.
    :param timestamps: ``"preserve"`` (default) keeps each member's original
        ZIP timestamp; ``"current"`` stamps every member with the current time
        (or ``SOURCE_DATE_EPOCH`` when set).
    :return: The path of the rewritten wheel.
    """
    if compression not in ("preserve", "stored", "deflated"):
        raise WheelError(
            f"Invalid compression strategy {compression!r}: expected one of "
            "'preserve', 'stored' or 'deflated'"
        )
    if timestamps not in ("preserve", "current"):
        raise WheelError(
            f"Invalid timestamp strategy {timestamps!r}: expected 'preserve' "
            "or 'current'"
        )
    if not replacements:
        raise WheelError("No replacements given; nothing to rewrite")

    # ---- Validate the requested member names up front ------------------
    seen_lower: dict[str, str] = {}
    for arcname, data in replacements.items():
        _validate_arcname(arcname)
        if not isinstance(data, bytes):
            raise WheelError(
                f"Replacement for {arcname!r} must be bytes, got "
                f"{type(data).__name__}"
            )
        lowered = arcname.lower()
        if lowered in seen_lower:
            raise WheelError(
                "Duplicate replacement specified for members "
                f"{seen_lower[lowered]!r} and {arcname!r}"
            )
        seen_lower[lowered] = arcname

    output_path = (
        Path(output).resolve() if output is not None else Path(wheel).resolve()
    )
    if output_path.suffix != ".whl":
        raise WheelError(f"Output filename must end in '.whl': {output_path}")
    output_match = WHEEL_INFO_RE.match(output_path.name)
    if output_match is None:
        raise WheelError(f"Bad wheel filename {output_path.name!r}")

    tmp_dir: str | None = None
    tmp_path: Path | None = None
    try:
        # ---- Read and verify everything before touching the destination --
        with WheelFile(wheel, "r") as src:
            record_path = src.record_path
            dist_info_path = src.dist_info_path

            infolist = src.infolist()
            member_names = {info.filename for info in infolist}
            member_names_lower = {
                info.filename.lower(): info.filename for info in infolist
            }

            # The output must keep the same distribution name and version,
            # because its RECORD lives under the input's .dist-info directory.
            output_namever = output_match.group("namever")
            if output_namever != src.parsed_filename.group("namever"):
                raise WheelError(
                    "Cannot write a rewritten wheel for "
                    f"{src.parsed_filename.group('namever')!r} to "
                    f"{output_path.name!r}: the output must keep the same "
                    "name and version"
                )

            # Resolve every replacement to an existing ZipInfo member and
            # reject ambiguous or illegal selections.
            for arcname in replacements:
                if arcname.lower() == record_path.lower():
                    raise WheelError(
                        f"Cannot replace {arcname!r}: RECORD is regenerated "
                        "from the wheel contents and must not be replaced"
                    )
                if _is_signature(arcname):
                    raise WheelError(
                        f"Cannot replace signature attachment {arcname!r}; "
                        "use drop_signatures to remove invalid signatures"
                    )
                if arcname not in member_names:
                    hint = member_names_lower.get(arcname.lower())
                    if hint is not None and hint != arcname:
                        raise WheelError(
                            f"No member named {arcname!r} in wheel (did you "
                            f"mean {hint!r}?)"
                        )
                    raise WheelError(
                        f"No member named {arcname!r} in wheel; only existing "
                        "members can be replaced"
                    )
                info = src.getinfo(arcname)
                if info.is_dir():
                    raise WheelError(f"Cannot replace directory entry {arcname!r}")

            signature_names = sorted(
                info.filename
                for info in infolist
                if info.filename.rsplit("/", 1)[0] == dist_info_path
                and _is_signature(info.filename)
            )
            if signature_names and not drop_signatures:
                raise WheelError(
                    "Wheel contains signature attachment(s) "
                    f"{', '.join(signature_names)} which would be invalidated "
                    "by the rewrite; re-run with drop_signatures=True to omit "
                    "them instead of leaving a stale signature in place"
                )

            def dropped(name: str) -> bool:
                return (
                    drop_signatures
                    and name.rsplit("/", 1)[0] == dist_info_path
                    and _is_signature(name)
                )

            # Read every member now. Reading through WheelFile verifies each
            # member against RECORD, so a corrupt input is rejected before any
            # new archive is created.
            original_data: dict[str, bytes] = {}
            for info in infolist:
                if info.is_dir() or info.filename == record_path:
                    continue
                if dropped(info.filename):
                    continue
                original_data[info.filename] = src.read(info)

            archive_comment = src.comment

        force_compress: int | None = {
            "stored": ZIP_STORED,
            "deflated": ZIP_DEFLATED,
            "preserve": None,
        }[compression]

        # ---- Build the replacement archive in a temporary file ----------
        # Create the destination directory first (but only after all input
        # checks above have passed) so the temp file can be created next to
        # the destination and moved into place atomically.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(
            prefix=".wheel-rewrite-", dir=str(output_path.parent)
        )
        tmp_path = Path(tmp_dir) / output_path.name

        with WheelFile(tmp_path, "w") as dst:
            # Reuse the input's .dist-info layout (important for wheels whose
            # filename case differs from their dist-info directory name).
            dst.dist_info_path = dist_info_path
            dst.record_path = record_path
            dst.comment = archive_comment

            for info in infolist:
                if info.is_dir() or info.filename == record_path:
                    continue
                if dropped(info.filename):
                    continue

                compress_type = (
                    force_compress
                    if force_compress is not None
                    else info.compress_type
                )
                out_info = _resolved_zinfo(info, compress_type, timestamps)
                dst.writestr(
                    out_info,
                    replacements.get(info.filename, original_data[info.filename]),
                )

            # Regenerate RECORD from the members just written.
            record_buffer = StringIO()
            writer = csv.writer(
                record_buffer, delimiter=",", quotechar='"', lineterminator="\n"
            )
            for fname, (algorithm, digest) in dst._file_hashes.items():
                writer.writerow(
                    (fname, f"{algorithm}={digest}", dst._file_sizes[fname])
                )
            writer.writerow((record_path, "", ""))

            old_record_info = next(
                info for info in infolist if info.filename == record_path
            )
            record_compress = (
                force_compress
                if force_compress is not None
                else old_record_info.compress_type
            )
            record_info = _resolved_zinfo(
                old_record_info, record_compress, timestamps
            )
            # Write the new RECORD through the base ZipFile so that
            # WheelFile.writestr does not register a hash for it. Clear the
            # collected tables first so that WheelFile.close() does not emit a
            # second, partial RECORD (it only writes one when the table is
            # non-empty); the file written here is already complete.
            dst._file_hashes = {}
            dst._file_sizes = {}
            ZipFile.writestr(
                dst, record_info, record_buffer.getvalue().encode("utf-8")
            )

        # ---- Verify the new archive before replacing the destination ----
        with WheelFile(tmp_path, "r") as verify:
            assert verify.record_path == record_path
            for fname, expected in replacements.items():
                assert verify.read(fname) == expected
            bad = verify.testzip()
            if bad is not None:
                raise WheelError(
                    f"Internal error verifying rewritten wheel at {bad!r}"
                )
            if drop_signatures and any(
                _is_signature(fname) for fname in verify.namelist()
            ):
                raise WheelError(
                    "Internal error: a signature attachment was not dropped"
                )

        # ---- All replacements succeeded: swap the files atomically ------
        os.replace(tmp_path, output_path)
        tmp_path = None
        return str(output_path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        if tmp_dir is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp_dir)
