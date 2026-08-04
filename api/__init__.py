"""Public convenience functions for the platform APIs.

Typical usage::

    from api import login, list_images

    token = login("username", "password", True)
    images = list_images(token=token)
"""

from .platform import (
    TopologyPlatformClient,
    import_topology,
    load_resource_snapshot,
    list_flavors,
    list_images,
    login,
    validate_payload,
)

__all__ = [
    "TopologyPlatformClient",
    "import_topology",
    "load_resource_snapshot",
    "list_flavors",
    "list_images",
    "login",
    "validate_payload",
]
