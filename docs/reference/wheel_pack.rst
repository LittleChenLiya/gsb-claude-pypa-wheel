wheel pack
==========

Usage
-----

::

    wheel pack <wheel_directory>


Description
-----------

Repack a previously unpacked wheel file.

This command can be used to repack a wheel file after its contents have been modified.
This is the equivalent of ``zip -r <wheel_file> <wheel_directory>`` except that it regenerates the
``RECORD`` file which contains hashes of all included files.

Before anything is written, the identity of the wheel is verified explicitly:
the ``.dist-info`` directory name, the ``Name``/``Version`` fields of
``.dist-info/METADATA`` and the tags of ``.dist-info/WHEEL`` must all agree,
as they jointly determine the wheel file name. The directory name alone is
never trusted. The ``Name`` field is treated as the display name and is
compared against the directory name in normalized (PEP 503) form, while the
file name uses the normalized name. A directory is rejected if it contains
more than one ``.dist-info`` directory, if the ``METADATA`` fields do not
match the directory name, or if the ``WHEEL`` tags are missing or conflict
with each other. The build tag (from ``--build-number`` or an existing
``Build`` field in ``WHEEL``) must begin with a digit and must not contain
``-``. The unpacked source tree is never modified; any metadata changes
requested via the options below are applied only to the files written into
the new wheel.


Options
-------

.. option:: -d, --dest-dir <dir>

    Directory to put the new wheel file into.

.. option:: --build-number <tag>

    Override the build tag in the new wheel file name.

Examples
--------

* Unpack a wheel, add a dummy module and then repack it (with a new build number)::

    $ wheel unpack someproject-1.5.0-py2-py3-none.whl
    Unpacking to: ./someproject-1.5.0
    $ touch someproject-1.5.0/somepackage/module.py
    $ wheel pack --build-number 2 someproject-1.5.0
    Repacking wheel as ./someproject-1.5.0-2-py2-py3-none.whl...OK
