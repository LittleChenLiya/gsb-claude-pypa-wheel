User Guide
==========

Building Wheels
---------------

To build a wheel for your project::

    python -m pip install build
    python -m build --wheel

This will build any C extensions in the project and then package those and the
pure Python code into a ``.whl`` file in the ``dist`` directory.

If your project contains no C extensions and is expected to work on both
Python 2 and 3, you will want to tell wheel to produce universal wheels by
adding this to your ``setup.cfg`` file:

.. code-block:: ini

    [bdist_wheel]
    universal = 1


Including license files in the generated wheel file
---------------------------------------------------

Several open source licenses require the license text to be included in every
distributable artifact of the project. By default, ``wheel`` conveniently
includes files matching the following glob_ patterns in the ``.dist-info``
directory:

* ``AUTHORS*``
* ``COPYING*``
* ``LICEN[CS]E*``
* ``NOTICE*``

This can be overridden by setting the ``license_files`` option in the
``[metadata]`` section of the project's ``setup.cfg``. For example:

.. code-block:: cfg

   [metadata]
   license_files =
      license.txt
      3rdparty/*.txt

No matter the path, all the matching license files are written in the wheel in
the ``.dist-info`` directory based on their file name only.

By specifying an empty ``license_files`` option, you can disable this
functionality entirely.

.. note:: There used to be an option called ``license_file`` (singular).
    As of wheel v0.32, this option has been deprecated in favor of the more
    versatile ``license_files`` option.

.. _glob: https://docs.python.org/library/glob.html

Converting Eggs to Wheels
-------------------------

The wheel tool is capable of converting eggs to the wheel format.
It works on both ``.egg`` files and ``.egg`` directories, and you can convert
multiple eggs with a single command::

    wheel convert blah-1.2.3-py2.7.egg foo-2.0b1-py3.5.egg

The command supports wildcard expansion as well (via :func:`~glob.iglob`) to
accommodate shells that do not do such expansion natively::

    wheel convert *.egg

By default, the resulting wheels are written to the current working directory.
This can be changed with the ``--dest-dir`` option::

    wheel convert --dest-dir /tmp blah-1.2.3-py2.7.egg

Installing Wheels
-----------------

To install a wheel file, use pip_::

    $ pip install someproject-1.5.0-py2-py3-none.whl

.. _pip: https://pypi.org/project/pip/


Inspecting Wheels
-----------------

To inspect the metadata and contents of a wheel file without installing it,
use the ``wheel info`` command::

    $ wheel info someproject-1.5.0-py2-py3-none.whl

This will display information about the wheel including:

* Package name and version
* Supported Python versions and platforms
* Dependencies and other metadata
* File count and total size

For more detailed information including a complete file listing, use the
``--verbose`` flag::

    $ wheel info --verbose someproject-1.5.0-py2-py3-none.whl

This is useful for:

* Verifying wheel contents before installation
* Debugging packaging issues
* Understanding wheel structure and metadata
* Checking supported platforms and Python versions


Rewriting Wheels
----------------

To modify one or more files in a wheel without unpacking and repacking the
entire archive, use the ``wheel rewrite`` command::

    $ wheel rewrite someproject-1.5.0-py2.py3-none-any.whl someproject/__init__.py=__version__ = '1.5.1'

This replaces only the specified entries and regenerates the ``RECORD`` file
with correct hashes. All other entries are preserved byte-for-byte, including
their original compression and timestamps.

Replacement content can be given as a string (``PATH=CONTENT``) or read from a
file (``PATH=@FILE``). Multiple replacements can be given at once::

    $ wheel rewrite someproject-1.5.0-py2.py3-none-any.whl \
        someproject/__init__.py=@new_init.py \
        someproject/module.py=@new_module.py

By default the original wheel is overwritten in place. To write the rewritten
wheel to a different file, use the ``--dest`` option::

    $ wheel rewrite --dest someproject-1.5.1-py2.py3-none-any.whl \
        someproject-1.5.0-py2.py3-none-any.whl \
        someproject/__init__.py=@new_init.py

The command validates all replacement targets before writing anything. If any
target is missing, ambiguous, or has an unsafe path, the command fails without
modifying the original wheel. The output is written to a temporary archive and
only moved into place after every replacement has succeeded.

To preserve reproducibility, the original compression and timestamps of
unmodified entries are kept. The ``--compression`` and ``--timestamp`` options
can be used to override these::

    $ wheel rewrite --compression stored --timestamp 2020-06-15T12:30:00 \
        someproject-1.5.0-py2.py3-none-any.whl \
        someproject/__init__.py=@new_init.py

If the wheel has embedded signature attachments (``.jws`` or ``.p7s``), they are
invalidated by the modification. By default the command refuses to rewrite a
signed wheel. Use the ``--remove-signatures`` option to remove the signatures
and proceed::

    $ wheel rewrite --remove-signatures someproject-1.5.0-py2.py3-none-any.whl \
        someproject/__init__.py=@new_init.py

A warning is emitted when signatures are removed. Re-sign the wheel if a valid
signature is required.
