"""Tektronix AWG5204 arbitrary waveform generator (AWG5200 series).

Migrated from ``awg5204/awg5204_tm``; the tm_devices command-tree backend was
replaced by raw SCPI over the platform's injected ScpiTransport.
"""

from .controller import AWG5204Controller, AwgOptions, build_awg5204  # noqa: F401
from .driver import AWG5204Driver  # noqa: F401

__all__ = [
    "AWG5204Driver",
    "AWG5204Controller",
    "AwgOptions",
    "build_awg5204",
]
