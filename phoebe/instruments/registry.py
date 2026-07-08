"""Built-in controller factory registrations (composition-root helper).

Real-device factories import lazily so an offline dev machine (no pyvisa /
nidaqmx / vendor DLL) can still build a fully working sim runtime.
"""
from __future__ import annotations

from ..core.factory import ControllerFactoryRegistry, ControllerKey
from .sim.controllers import register_sim_factories


def register_builtin_factories(registry: ControllerFactoryRegistry) -> None:
    from .yokogawa_aq637x.controller import build_aq637x
    from .santec_slm200.controller import build_slm200
    from .rs_rto6.controller import build_rto6
    from .ni_daq.controller import build_ni_daq
    from .tek_awg5204.controller import build_awg5204

    registry.register(ControllerKey("spectrum_analyzer", "yokogawa", "aq6370"),
                      build_aq637x)
    registry.register(ControllerKey("spectrum_analyzer", "yokogawa", "aq6370d"),
                      build_aq637x)
    registry.register(ControllerKey("spectrum_analyzer", "yokogawa", "aq6374"),
                      build_aq637x)
    registry.register(ControllerKey("pattern_modulator", "santec", "slm-200"),
                      build_slm200)
    registry.register(ControllerKey("oscilloscope", "rohde-schwarz", "rto6"),
                      build_rto6)
    registry.register(ControllerKey("analog_input", "ni", "usb-6251"),
                      build_ni_daq)
    registry.register(ControllerKey("waveform_generator", "tektronix", "awg5204"),
                      build_awg5204)

    register_sim_factories(registry)
