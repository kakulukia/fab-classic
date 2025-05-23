*fab-classic* is a Python library and command-line tool for streamlining
the use of SSH for application deployment or systems administration tasks.

fab-classic is forked from `Fabric 1.14 <https://github.com/fabric/fabric/tree/1.14>`_
and is intended to add only bug fixes and compatibility patches, for projects
already using Fabric-1.x in some way. It includes python3 compatibility patches
from `Fabric3 <https://github.com/mathiasertl/fabric/>`_.

Upstream *Fabric* is now on version 2.x. Fabric-2.x is python3 compatible,
and comes *after* Fabric3. Fabric-2.x has significant compatibility-breaking
changes, in order to fix some long-standing limitations.


Install
-------

fab-classic is `on PyPI <https://pypi.org/project/fab-classic/>`_,
so you can ``pip install fab-classic``

Be sure to ``pip uninstall fabric`` first if you happen to have it installed,
because *fab-classic* uses the same import name "fabric" and entrypoint (tool)
name "fab".

Starting with version 1.16, *fab-classic* depends on
`paramiko <https://github.com/paramiko/paramiko/>`_ instead of the
`paramiko-ng <https://github.com/ploxiln/paramiko-ng/>`_ fork. To avoid
conflicts, make sure you uninstall *paramiko-ng* before installing
*fab-classic*, and ensure *paramiko* is available.


Documentation
-------------

API Documentation: https://ploxiln.github.io/fab-classic/

Changelog: https://github.com/ploxiln/fab-classic/releases

For a quick command reference, run ``fab --help``


Usage Introduction
------------------

fab-classic provides a basic suite of operations for executing local or remote shell
commands (normally or via ``sudo``) and uploading/downloading files, as well as
auxiliary functionality such as prompting the running user for input, or
aborting execution.

Typical use involves creating a Python module containing one or more functions,
then executing them via the ``fab`` command-line tool. Below is a small but
complete "fabfile" containing a single task:

.. code-block:: python

    from fabric.api import run

    def host_type():
        run('uname -s')

If you save the above as ``fabfile.py`` (the default module that
``fab`` loads), you can run the tasks defined in it on one or more
servers, like so::

    $ fab -H localhost,linuxbox host_type
    [localhost] run: uname -s
    [localhost] out: Darwin
    [linuxbox] run: uname -s
    [linuxbox] out: Linux

    Done.
    Disconnecting from localhost... done.
    Disconnecting from linuxbox... done.

In addition to use via the ``fab`` tool, Fabric's components may be imported
into other Python code, providing a Pythonic interface to the SSH protocol
suite at a higher level than that provided by the ``paramiko`` library
(which Fabric itself uses).
