"""Package root for bud, a personal AI teammate.
It exposes `__version__`, read from the installed package metadata.
"""

from importlib.metadata import version

__version__ = version("bud")
