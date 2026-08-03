"""Public convenience functions for the platform APIs.

Typical usage::

    from api import login, list_images

    token = login("username", "password", True)
    images = list_images(token=token)
"""

from .platform import (
    TopologyPlatformClient,
    import_topology,
    list_flavors,
    list_images,
    login,
)

__all__ = [
    "TopologyPlatformClient",
    "import_topology",
    "list_flavors",
    "list_images",
    "login",
]
