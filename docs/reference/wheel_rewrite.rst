wheel rewrite
=============

Usage
-----

::

    wheel rewrite <wheel_file> -r ARCNAME FILE ...
                  [-o OUTPUT] [--drop-signatures]
                  [--compression {preserve,stored,deflated}]
                  [--timestamps {preserve,current}]

Description
-----------

Replace the contents of one or more **existing** files inside a wheel. Only
the members named on the command line are changed; every other member is
copied with its original contents unchanged, and the ``RECORD`` file is
regenerated from the resulting contents so that its hashes and sizes verify.

Each ``-r/--replace`` option gives an archive member name (exactly as it
appears in the wheel, with ``/`` separators) followed by a local file whose
bytes become the new contents of that member. The member must already exist
in the wheel; adding new members, replacing ``RECORD`` itself, or naming a
directory entry is an error. Member names that would traverse outside the
archive (``..``, absolute paths, drive letters, backslashes) are rejected.

Every replacement is read and validated **before** anything is written. The
new archive is built in a temporary file next to the destination, verified
against its new ``RECORD``, and only then moved into place. If any
replacement fails, the destination wheel is not overwritten and remains
fully readable.

Signed wheels
-------------

Replacing any member invalidates detached signatures (``RECORD.jws`` /
``RECORD.p7s``), because those signatures cover ``RECORD``. Rather than keep
an attachment that no longer validates, ``wheel rewrite`` refuses to modify
a signed wheel unless ``--drop-signatures`` is given; the signature
attachments are then omitted from the rewritten wheel. The rewritten wheel
is unsigned; resign it out of band if needed.

Compression and timestamps
--------------------------

By default each copied member keeps its original ZIP compression method and
timestamp (``--compression preserve --timestamps preserve``). Pass
``--compression stored`` or ``--compression deflated`` to force one method
for every member, and ``--timestamps current`` to stamp every member with
the current time (honoring ``SOURCE_DATE_EPOCH`` when set).

Options
-------

.. option:: -r, --replace ARCNAME FILE

    Replace the existing member *ARCNAME* with the contents of *FILE*. May be
    repeated; every *ARCNAME* must already exist in the wheel.

.. option:: -o, --output OUTPUT

    Write the rewritten wheel to *OUTPUT* instead of replacing the input
    wheel in place. *OUTPUT* must keep the same distribution name and
    version (only tags may differ).

.. option:: --drop-signatures

    Drop ``RECORD.jws``/``RECORD.p7s`` signature attachments instead of
    refusing to rewrite a signed wheel. The output wheel is unsigned.

.. option:: --compression {preserve,stored,deflated}

    Compression strategy for the archive entries (default: ``preserve``).

.. option:: --timestamps {preserve,current}

    Timestamp strategy for the archive entries (default: ``preserve``).

Examples
--------

* Patch one module in a wheel in place::

    $ echo 'print("patched")' > module.py
    $ wheel rewrite someproject-1.5.0-py3-none-any.whl \
          --replace someproject/module.py module.py
    /path/to/someproject-1.5.0-py3-none-any.whl

* Rewrite a signed wheel, explicitly dropping the now-invalid signature::

    $ wheel rewrite someproject-1.5.0-py3-none-any.whl \
          --replace someproject/module.py module.py --drop-signatures

Tests
-----

The rewriter is covered by a focused test module. To run just those tests
locally::

    $ pytest tests/commands/test_rewrite.py
