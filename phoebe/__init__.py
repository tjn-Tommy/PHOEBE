"""PHOEBE — unified lab-instrument control platform.

Architecture per refactor.md v2: control plane (Gateway + EventBus) and data
plane (RunWriter) are physically separated; instruments are composed from
Transport / Driver / Controller / Capability layers; experiments are plugins
that only depend on capability protocols and receive devices via DI.
"""

__version__ = "0.1.0"
