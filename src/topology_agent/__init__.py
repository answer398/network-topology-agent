"""Public M1 contracts for the network topology agent."""

from .config import load_app_config, load_device_mapping
from .models import (
    ConfigurationError,
    InputError,
    ModelInvocationError,
    PayloadValidationError,
    PlatformResourceError,
    PlatformSubmissionError,
    PlatformTopologyPayload,
    ResolvedTopologyIR,
    SubmissionResult,
    SubmissionUncertainError,
    TaskInput,
    TopologyAgentError,
    TopologyIR,
    TopologyObservation,
    TopologyUnresolvedError,
    ValidationReport,
)

__version__ = "0.1.0"

__all__ = [
    "ConfigurationError",
    "InputError",
    "ModelInvocationError",
    "PayloadValidationError",
    "PlatformResourceError",
    "PlatformSubmissionError",
    "PlatformTopologyPayload",
    "ResolvedTopologyIR",
    "SubmissionResult",
    "SubmissionUncertainError",
    "TaskInput",
    "TopologyAgentError",
    "TopologyIR",
    "TopologyObservation",
    "TopologyUnresolvedError",
    "ValidationReport",
    "load_app_config",
    "load_device_mapping",
]
