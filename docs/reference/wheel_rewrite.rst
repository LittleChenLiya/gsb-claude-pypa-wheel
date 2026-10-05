wheel rewrite
=============

Usage
-----

::

    wheel rewrite <wheel_file> <path>=<content> [<path>=<content> ...]


Description
-----------

Rewrite a wheel by replacing only the specified existing entries. All other
entries are preserved byte-for-byte and the RECORD file is regenerated with
correct hashes for every member.

This command is useful when you need to modify one or two files in a wheel
without unpacking and repacking the entire archive. Unlike wheel pack,
which rebuilds the wheel from a directory, wheel rewrite operates directly
on the wheel archive and preserves the original compression and timestamps of
unmodified entries.

Only entries that are explicitly named and already exist in the wheel are
replaced. Selection ambiguity and entry paths are validated up-front, before
any output archive is written. The output is written to a temporary archive
and only moved into place after every replacement has succeeded; on failure
the original wheel is left untouched and readable.


Options
-------

.. option:: -d, --dest <path>

    Destination path for the rewritten wheel. If not given, the original wheel
    is overwritten in place.

.. option:: --compression <strategy>

    Compression strategy for the output entries. One of preserve (the
    default; keep the original compression of each entry), stored (no
    compression), or deflated (ZIP deflate).

.. option:: --timestamp <iso8601>

    Timestamp for the output entries, as an ISO-8601 string (e.g.
    2020-06-15T12:30:00). If not given, the original timestamp of each
    entry is preserved.

.. option:: --remove-signatures

    Remove embedded signature attachments (.jws / .p7s) from the
    rewritten wheel. Signatures are invalidated by any modification; by
    default the command refuses to rewrite a signed wheel unless this option
    is given. When signatures are removed, a warning is emitted.


Examples
--------

* Replace a single text file in a wheel::

    $ wheel rewrite someproject-1.5.0-py2.py3-none-any.whl someproject/__init__.py=__version__ = '1.5.1'

* Replace a file using content from another file::

    $ wheel rewrite someproject-1.5.0-py2.py3-none-any.whl someproject/__init__.py=@new_init.py

* Replace multiple files at once::

    $ wheel rewrite someproject-1.5.0-py2.py3-none-any.whl someproject/__init__.py=@new_init.py someproject/module.py=@new_module.py

* Write the rewritten wheel to a different file::

    $ wheel rewrite --dest someproject-1.5.1-py2.py3-none-any.whl someproject-1.5.0-py2.py3-none-any.whl someproject/__init__.py=@new_init.py

* Rewrite a signed wheel, removing the invalidated signatures::

    $ wheel rewrite --remove-signatures someproject-1.5.0-py2.py3-none-any.whl someproject/__init__.py=@new_init.py
