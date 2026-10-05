"""
Wheel command-line utility.
"""

from __future__ import annotations

import argparse
import os
import sys
from argparse import ArgumentTypeError

from ..wheelfile import WheelError


def unpack_f(args: argparse.Namespace) -> None:
    from .unpack import unpack

    unpack(args.wheelfile, args.dest)


def pack_f(args: argparse.Namespace) -> None:
    from .pack import pack

    pack(args.directory, args.dest_dir, args.build_number, args.local_version)


def convert_f(args: argparse.Namespace) -> None:
    from .convert import convert

    convert(args.files, args.dest_dir, args.verbose)


def tags_f(args: argparse.Namespace) -> None:
    from .tags import tags

    names = (
        tags(
            wheel,
            args.python_tag,
            args.abi_tag,
            args.platform_tag,
            args.build,
            args.remove,
        )
        for wheel in args.wheel
    )

    for name in names:
        print(name)


def info_f(args: argparse.Namespace) -> None:
    from .info import info

    try:
        info(args.wheelfile, args.verbose)
    except FileNotFoundError as e:
        raise WheelError(str(e)) from e


def rewrite_f(args: argparse.Namespace) -> None:
    from .rewrite import rewrite

    replacements = {}
    for spec in args.replacements:
        if "=" not in spec:
            raise WheelError(
                f"Invalid replacement {spec!r}: expected PATH=CONTENT"
            )
        path, value = spec.split("=", 1)
        if value.startswith("@"):
            with open(value[1:], "rb") as f:
                replacements[path] = f.read()
        else:
            replacements[path] = value

    compression = None
    if args.compression == "stored":
        from zipfile import ZIP_STORED

        compression = ZIP_STORED
    elif args.compression == "deflated":
        from zipfile import ZIP_DEFLATED

        compression = ZIP_DEFLATED

    rewrite(
        args.wheelfile,
        replacements,
        dest=args.dest,
        compression=compression,
        timestamp=args.timestamp,
        remove_signatures=args.remove_signatures,
    )


def version_f(args: argparse.Namespace) -> None:
    from .. import __version__

    print(f"wheel {__version__}")


def parse_build_tag(build_tag: str) -> str:
    if build_tag and not build_tag[0].isdigit():
        raise ArgumentTypeError("build tag must begin with a digit")
    elif "-" in build_tag:
        raise ArgumentTypeError("invalid character ('-') in build tag")

    return build_tag


TAGS_HELP = """\
Make a new wheel with given tags. Any tags unspecified will remain the same.
Starting the tags with a "+" will append to the existing tags. Starting with a
"-" will remove a tag (use --option=-TAG syntax). Multiple tags can be
separated by ".". The original file will remain unless --remove is given.  The
output filename(s) will be displayed on stdout for further processing.
"""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    s = p.add_subparsers(help="commands")

    unpack_parser = s.add_parser("unpack", help="Unpack wheel")
    unpack_parser.add_argument(
        "--dest", "-d", help="Destination directory", default="."
    )
    unpack_parser.add_argument("wheelfile", help="Wheel file")
    unpack_parser.set_defaults(func=unpack_f)

    repack_parser = s.add_parser("pack", help="Repack wheel")
    repack_parser.add_argument("directory", help="Root directory of the unpacked wheel")
    repack_parser.add_argument(
        "--dest-dir",
        "-d",
        default=os.path.curdir,
        help="Directory to store the wheel (default %(default)s)",
    )
    repack_parser.add_argument(
        "--build-number",
        type=parse_build_tag,
        help="Build tag to use in the wheel name",
    )
    repack_parser.add_argument(
        "--local-version", help="Local version identifier to add or replace"
    )
    repack_parser.set_defaults(func=pack_f)

    convert_parser = s.add_parser("convert", help="Convert egg or wininst to wheel")
    convert_parser.add_argument("files", nargs="*", help="Files to convert")
    convert_parser.add_argument(
        "--dest-dir",
        "-d",
        default=os.path.curdir,
        help="Directory to store wheels (default %(default)s)",
    )
    convert_parser.add_argument("--verbose", "-v", action="store_true")
    convert_parser.set_defaults(func=convert_f)

    tags_parser = s.add_parser(
        "tags", help="Add or replace the tags on a wheel", description=TAGS_HELP
    )
    tags_parser.add_argument("wheel", nargs="*", help="Existing wheel(s) to retag")
    tags_parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the original files, keeping only the renamed ones",
    )
    tags_parser.add_argument(
        "--python-tag", metavar="TAG", help="Specify an interpreter tag(s)"
    )
    tags_parser.add_argument("--abi-tag", metavar="TAG", help="Specify an ABI tag(s)")
    tags_parser.add_argument(
        "--platform-tag", metavar="TAG", help="Specify a platform tag(s)"
    )
    tags_parser.add_argument(
        "--build", type=parse_build_tag, metavar="BUILD", help="Specify a build tag"
    )
    tags_parser.set_defaults(func=tags_f)

    info_parser = s.add_parser("info", help="Show information about a wheel file")
    info_parser.add_argument("wheelfile", help="Wheel file to show information for")
    info_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed file listing"
    )
    info_parser.set_defaults(func=info_f)

    rewrite_parser = s.add_parser(
        "rewrite",
        help="Rewrite a wheel, replacing only the specified entries",
        description=(
            "Rewrite a wheel by replacing only the specified existing entries. "
            "All other entries are preserved byte-for-byte and the RECORD file "
            "is regenerated. Replacements are given as PATH=CONTENT where CONTENT "
            "is a string, or PATH=@FILE to read replacement content from a file."
        ),
    )
    rewrite_parser.add_argument("wheelfile", help="Wheel file to rewrite")
    rewrite_parser.add_argument(
        "replacements",
        nargs="+",
        metavar="PATH=CONTENT",
        help="Entry path and new content (PATH=CONTENT), or PATH=@FILE",
    )
    rewrite_parser.add_argument(
        "--dest",
        "-d",
        default=None,
        help="Destination path (default: overwrite the original wheel)",
    )
    rewrite_parser.add_argument(
        "--compression",
        choices=("preserve", "stored", "deflated"),
        default="preserve",
        help="Compression strategy (default: preserve original)",
    )
    rewrite_parser.add_argument(
        "--timestamp",
        default=None,
        metavar="ISO8601",
        help="Timestamp for output entries (default: preserve original)",
    )
    rewrite_parser.add_argument(
        "--remove-signatures",
        action="store_true",
        help="Remove embedded signature attachments (.jws/.p7s) instead of failing",
    )
    rewrite_parser.set_defaults(func=rewrite_f)

    version_parser = s.add_parser("version", help="Print version and exit")
    version_parser.set_defaults(func=version_f)

    help_parser = s.add_parser("help", help="Show this help")
    help_parser.set_defaults(func=lambda args: p.print_help())

    return p


def main() -> int:
    p = parser()
    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
    else:
        try:
            args.func(args)
            return 0
        except WheelError as e:
            print(e, file=sys.stderr)

    return 1
