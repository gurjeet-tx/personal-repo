# Autodetecting setup.py script for building the Python extensions

import argparse
import importlib._bootstrap
import importlib.machinery
import importlib.util
import os
import re
import sys
import sysconfig
from glob import glob, escape
import _osx_support


try:
    import subprocess
    del subprocess
    SUBPROCESS_BOOTSTRAP = False
except ImportError:
    # Bootstrap Python: distutils.spawn uses subprocess to build C extensions,
    # subprocess requires C extensions built by setup.py like _posixsubprocess.
    #
    # Use _bootsubprocess which only uses the os module.
    #
    # It is dropped from sys.modules as soon as all C extension modules
    # are built.
    import _bootsubprocess
    sys.modules['subprocess'] = _bootsubprocess
    del _bootsubprocess
    SUBPROCESS_BOOTSTRAP = True


from distutils import log
from distutils.command.build_ext import build_ext
from distutils.command.build_scripts import build_scripts
from distutils.command.install import install
from distutils.command.install_lib import install_lib
from distutils.core import Extension, setup
from distutils.errors import CCompilerError, DistutilsError
from distutils.spawn import find_executable


# Compile extensions used to test Python?
TEST_EXTENSIONS = (sysconfig.get_config_var('TEST_MODULES') == 'yes')

# This global variable is used to hold the list of modules to be disabled.
DISABLED_MODULE_LIST = []

# --list-module-names option used by Tools/scripts/generate_module_names.py
LIST_MODULE_NAMES = False


def get_platform():
    # Cross compiling
    if "_PYTHON_HOST_PLATFORM" in os.environ:
        return os.environ["_PYTHON_HOST_PLATFORM"]

    # Get value of sys.platform
    if sys.platform.startswith('osf1'):
        return 'osf1'
    return sys.platform


CROSS_COMPILING = ("_PYTHON_HOST_PLATFORM" in os.environ)
HOST_PLATFORM = get_platform()
MS_WINDOWS = (HOST_PLATFORM == 'win32')
CYGWIN = (HOST_PLATFORM == 'cygwin')
MACOS = (HOST_PLATFORM == 'darwin')
AIX = (HOST_PLATFORM.startswith('aix'))
VXWORKS = ('vxworks' in HOST_PLATFORM)


SUMMARY = """
Python is an interpreted, interactive, object-oriented programming
language. It is often compared to Tcl, Perl, Scheme or Java.
Python combines remarkable power with very clear syntax. It has
modules, classes, exceptions, very high level dynamic data types, and
dynamic typing. There are interfaces to many system calls and
libraries, as well as to various windowing systems (X11, Motif, Tk,
Mac, MFC). New built-in modules are easily written in C or C++. Python
is also usable as an extension language for applications that need a
programmable interface.
The Python implementation is portable: it runs on many brands of UNIX,
on Windows, DOS, Mac, Amiga... If your favorite system isn't
listed here, it may still be supported, if there's a C compiler for
it. Ask around on comp.lang.python -- or just try compiling Python
yourself.
"""

CLASSIFIERS = """
Development Status :: 6 - Mature
License :: OSI Approved :: Python Software Foundation License
Natural Language :: English
Programming Language :: C
Programming Language :: Python
Topic :: Software Development
"""


def run_command(cmd):
    status = os.system(cmd)
    return os.waitstatus_to_exitcode(status)


# Set common compiler and linker flags derived from the Makefile,
# reserved for building the interpreter and the stdlib modules.
# See bpo-21121 and bpo-35257
def set_compiler_flags(compiler_flags, compiler_py_flags_nodist):
    flags = sysconfig.get_config_var(compiler_flags)
    py_flags_nodist = sysconfig.get_config_var(compiler_py_flags_nodist)
    sysconfig.get_config_vars()[compiler_flags] = flags + ' ' + py_flags_nodist


def add_dir_to_list(dirlist, dir):
    """Add the directory 'dir' to the list 'dirlist' (after any relative
    directories) if:
    1) 'dir' is not already in 'dirlist'
    2) 'dir' actually exists, and is a directory.
    """
    if dir is None or not os.path.isdir(dir) or dir in dirlist:
        return
    for i, path in enumerate(dirlist):
        if not os.path.isabs(path):
            dirlist.insert(i + 1, dir)
            return
    dirlist.insert(0, dir)


def sysroot_paths(make_vars, subdirs):
    """Get the paths of sysroot sub-directories.
    * make_vars: a sequence of names of variables of the Makefile where
      sysroot may be set.
    * subdirs: a sequence of names of subdirectories used as the location for
      headers or libraries.
    """

    dirs = []
    for var_name in make_vars:
        var = sysconfig.get_config_var(var_name)
        if var is not None:
            m = re.search(r'--sysroot=([^"]\S*|"[^"]+")', var)
            if m is not None:
                sysroot = m.group(1).strip('"')
                for subdir in subdirs:
                    if os.path.isabs(subdir):
                        subdir = subdir[1:]
                    path = os.path.join(sysroot, subdir)
                    if os.path.isdir(path):
                        dirs.append(path)
                break
    return dirs


MACOS_SDK_ROOT = None
MACOS_SDK_SPECIFIED = None

def macosx_sdk_root():
    """Return the directory of the current macOS SDK.
    If no SDK was explicitly configured, call the compiler to find which
    include files paths are being searched by default.  Use '/' if the
    compiler is searching /usr/include (meaning system header files are
    installed) or use the root of an SDK if that is being searched.
    (The SDK may be supplied via Xcode or via the Command Line Tools).
    The SDK paths used by Apple-supplied tool chains depend on the
    setting of various variables; see the xcrun man page for more info.
    Also sets MACOS_SDK_SPECIFIED for use by macosx_sdk_specified().
    """
    global MACOS_SDK_ROOT, MACOS_SDK_SPECIFIED

    # If already called, return cached result.
    if MACOS_SDK_ROOT:
        return MACOS_SDK_ROOT

    cflags = sysconfig.get_config_var('CFLAGS')
    m = re.search(r'-isysroot\s*(\S+)', cflags)
    if m is not None:
        MACOS_SDK_ROOT = m.group(1)
        MACOS_SDK_SPECIFIED = MACOS_SDK_ROOT != '/'
    else:
        MACOS_SDK_ROOT = _osx_support._default_sysroot(
            sysconfig.get_config_var('CC'))
        MACOS_SDK_SPECIFIED = False

    return MACOS_SDK_ROOT


def macosx_sdk_specified():
    """Returns true if an SDK was explicitly configured.
    True if an SDK was selected at configure time, either by specifying
    --enable-universalsdk=(something other than no or /) or by adding a
    -isysroot option to CFLAGS.  In some cases, like when making
    decisions about macOS Tk framework paths, we need to be able to
    know whether the user explicitly asked to build with an SDK versus
    the implicit use of an SDK when header files are no longer
    installed on a running system by the Command Line Tools.
    """
    global MACOS_SDK_SPECIFIED

    # If already called, return cached result.
    if MACOS_SDK_SPECIFIED:
        return MACOS_SDK_SPECIFIED

    # Find the sdk root and set MACOS_SDK_SPECIFIED
    macosx_sdk_root()
    return MACOS_SDK_SPECIFIED

