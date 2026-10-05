from __future__ import annotations

import datetime
import os
import shutil
import tempfile
import warnings
from typing import Mapping

from zipfile import ZipFile, ZipInfo

from ..wheelfile import WheelError, WheelFile

__all__ = ["rewrite"]

_SIGNATURE_SUFFIXES = (".jws", ".p7s")


def _is_safe_entry_path(path):
    if not path:
        return False
    if path.startswith("/"):
        return False
    if "\\" in path:
        return False
    if len(path) >= 2 and path[1] == ":":
        return False
    parts = path.split("/")
    if ".." in parts:
        return False
    return True


def _parse_timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError as e:
        raise WheelError("Invalid timestamp %r: %s" % (value, e)) from e


def _build_record(wf):
    """Build the RECORD content from the hashes computed by WheelFile.

    The RECORD is a CSV file listing every member with its hash and size.
    The RECORD itself is listed with empty hash and size.
    """
    import csv
    from io import StringIO

    data = StringIO()
    writer = csv.writer(data, delimiter=",", quotechar='"', lineterminator="\n")
    for fname, (algorithm, hash_) in wf._file_hashes.items():
        writer.writerow((fname, algorithm + "=" + hash_, wf._file_sizes[fname]))
    writer.writerow((wf.record_path, "", ""))
    return data.getvalue()


def rewrite(wheel_path, replacements, *, dest=None, compression=None,
            timestamp=None, remove_signatures=False):
    if not replacements:
        raise WheelError("No replacements specified")

    if isinstance(timestamp, str):
        timestamp = _parse_timestamp(timestamp)

    with WheelFile(wheel_path, "r") as wf:
        namelist = wf.namelist()

        # 1. Entry paths must be safe relative paths before anything else.
        for path in replacements:
            if not _is_safe_entry_path(path):
                raise WheelError("Unsafe entry path: %r" % path)

        # 2. Every replacement target must exist and be uniquely identified.
        #    Exact matching avoids selection ambiguity: a path that does not
        #    match exactly (or matches more than once) is rejected.
        for path in replacements:
            if path not in namelist:
                raise WheelError("Entry not found in wheel: %r" % path)
            if namelist.count(path) > 1:
                raise WheelError(
                    "Ambiguous entry path (matches multiple entries): %r" % path
                )

        signature_paths = [
            name for name in namelist if name.endswith(_SIGNATURE_SUFFIXES)
        ]
        if signature_paths and not remove_signatures:
            raise WheelError(
                "Wheel has embedded signature(s) %s that will be invalidated "
                "by the rewrite. Pass remove_signatures=True to remove them, "
                "or re-sign the wheel after rewriting." % signature_paths
            )

        entries = {}
        for zinfo in wf.infolist():
            entries[zinfo.filename] = (zinfo, wf.read(zinfo.filename))

        for path, content in replacements.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            original_zinfo, _ = entries[path]
            entries[path] = (original_zinfo, content)

    if dest is None:
        dest = wheel_path

    dest_dir = os.path.dirname(os.path.abspath(dest))
    dest_basename = os.path.basename(dest)
    # Create the temp archive with a valid wheel filename so that WheelFile
    # can derive the correct .dist-info path from the filename.
    tmp_dir = tempfile.mkdtemp(dir=dest_dir)
    tmp_path = os.path.join(tmp_dir, dest_basename)
    try:
        wf = WheelFile(tmp_path, "w")
        try:
            for path, (zinfo, data) in entries.items():
                if path == wf.record_path:
                    continue
                # Signatures are invalidated by the modification; exclude them.
                if remove_signatures and path.endswith(_SIGNATURE_SUFFIXES):
                    continue

                new_zinfo = ZipInfo(path, date_time=zinfo.date_time)
                new_zinfo.external_attr = zinfo.external_attr
                new_zinfo.compress_type = (
                    compression if compression is not None else zinfo.compress_type
                )
                if timestamp is not None:
                    new_zinfo.date_time = (
                        timestamp.year, timestamp.month, timestamp.day,
                        timestamp.hour, timestamp.minute, timestamp.second,
                    )

                wf.writestr(new_zinfo, data)

            # Write RECORD ourselves so we can control its timestamp and
            # compression. WheelFile.close() would write it with the current
            # time, which would break timestamp preservation.
            record_data = _build_record(wf)
            record_zinfo = ZipInfo(wf.record_path, date_time=zinfo.date_time)
            record_zinfo.external_attr = (0o664 | 0o100000) << 16
            record_zinfo.compress_type = (
                compression if compression is not None else zinfo.compress_type
            )
            if timestamp is not None:
                record_zinfo.date_time = (
                    timestamp.year, timestamp.month, timestamp.day,
                    timestamp.hour, timestamp.minute, timestamp.second,
                )
            wf.writestr(record_zinfo, record_data)
        finally:
            # Close via ZipFile.close() to avoid WheelFile.close() writing
            # RECORD a second time.
            ZipFile.close(wf)

        shutil.move(tmp_path, dest)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if signature_paths:
        warnings.warn(
            "Removed %d embedded signature(s) from the rewritten wheel "
            "because they were invalidated by the modification. Re-sign the "
            "wheel if a valid signature is required." % len(signature_paths),
            stacklevel=2,
        )
