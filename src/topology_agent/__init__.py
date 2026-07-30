"""Public M1 and M2 contracts for the network topology agent."""

from .config import load_app_config, load_device_mapping
from .image import (
    ImageBundle,
    ImageView,
    create_crop_view,
    load_image_bundle,
    original_point_to_view,
    scale_original_point,
    view_bbox_to_original,
    view_point_to_original,
)
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
    "ImageBundle",
    "ImageView",
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
    "create_crop_view",
    "load_app_config",
    "load_device_mapping",
    "load_image_bundle",
    "original_point_to_view",
    "scale_original_point",
    "view_bbox_to_original",
    "view_point_to_original",
]
