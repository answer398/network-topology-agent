"""Simple function wrappers for the four platform HTTP APIs."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import requests
import yaml


_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "app.yaml"
_DEVICE_MAPPING_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "device_mapping.yaml"
)
try:
    _raw_config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    _platform = _raw_config["platform"]
    _paths = _platform["paths"]
    BASE_URL = _platform["baseUrl"]
    LOGIN_PATH = _paths["login"]
    IMAGE_LIST_PATH = _paths["imageList"]
    FLAVOR_LIST_PATH = _paths["flavorList"]
    TOPOLOGY_CLEAR_PATH = _paths["topologyClear"]
    TOPOLOGY_IMPORT_PATH = _paths["topologyImport"]
    SUCCESS_CODES = set(_platform["successCodes"])
    TIMEOUT_SECONDS = float(_platform["timeoutSeconds"])
    _defaults = _raw_config["defaults"]
    DEFAULT_VERSION = str(_defaults["version"])
    DEFAULT_MTU = int(_defaults["mtu"])
    DEFAULT_ENABLE_DHCP = bool(_defaults["enableDhcp"])
    DEFAULT_DNS = str(_defaults["dns"])
except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
    raise RuntimeError(f"invalid platform configuration: {_CONFIG_PATH}") from exc

_session = requests.Session()
_token: str | None = None
_credentials: tuple[str, str, bool] | None = None


class TopologyPlatformClient:
    """Maintain a private token and import complete topologies."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._token: str | None = None
        self._credentials: tuple[str, str, bool] | None = None

    def login(
        self, username: str, password: str, rememberMe: bool = False
    ) -> None:
        """Log in and retain the token inside this client instance."""

        _validate_login_input(username, password, rememberMe)
        response = self._post(
            LOGIN_PATH,
            json={
                "username": username,
                "password": password,
                "rememberMe": rememberMe,
            },
        )
        self._token = _login_token(response)
        self._credentials = (username, password, rememberMe)

    def list_images(
        self,
        *,
        pageIndex: int = 1,
        pageSize: int = 100,
        fetchAll: bool = True,
        imageName: str | None = None,
        status: str | None = "ACTIVE",
        virtualization: str | None = None,
        visibility: str | None = None,
        osType: str | None = None,
        osVersion: str | None = None,
        platformId: int | None = None,
        platformType: str | None = None,
        hardwareArchitecture: str | None = None,
        hasEdr: str | None = None,
        virtio: bool | None = None,
        qga: bool | None = None,
        cloud: bool | None = None,
        nodeType: str | None = None,
        id: str | None = None,
    ) -> list[dict[str, object]]:
        """Query every available image page with this client's token."""

        filters = _filters(
            imageName=imageName,
            status=status,
            virtualization=virtualization,
            visibility=visibility,
            osType=osType,
            osVersion=osVersion,
            platformId=platformId,
            platformType=platformType,
            hardwareArchitecture=hardwareArchitecture,
            hasEdr=hasEdr,
            virtio=virtio,
            qga=qga,
            cloud=cloud,
            nodeType=nodeType,
            id=id,
        )
        return _list_all_for_client(
            self, IMAGE_LIST_PATH, pageIndex, pageSize, fetchAll, filters
        )

    def list_flavors(
        self,
        *,
        pageIndex: int = 1,
        pageSize: int = 100,
        fetchAll: bool = True,
        id: str | None = None,
        flavorName: str | None = None,
        cpu: int | None = None,
        disk: int | None = None,
        ram: int | None = None,
    ) -> list[dict[str, object]]:
        """Query every available flavor page with this client's token."""

        filters = _filters(
            id=id, flavorName=flavorName, cpu=cpu, disk=disk, ram=ram
        )
        return _list_all_for_client(
            self, FLAVOR_LIST_PATH, pageIndex, pageSize, fetchAll, filters
        )

    def import_topology(
        self, payload: Mapping[str, object]
    ) -> dict[str, object]:
        """Import a topology using the internally retained token."""

        body, params = _topology_request(payload)
        self._clear_topology(params["projectId"], params["network"])
        try:
            response = self._authorized_request(
                "POST",
                TOPOLOGY_IMPORT_PATH,
                params=params,
                json=body,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TimeoutError(
                "topology import transport failed; write state is unknown and was not retried"
            ) from exc
        return _response_object(response, "topology import")

    def formatData(
        self,
        obs_data: Mapping[str, object],
        projectId: str | None,
        networkId: str | None,
        *,
        image_items: Sequence[Mapping[str, object]] | None = None,
        flavor_items: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, object]:
        """Format an observation using supplied or currently available resources."""

        if not isinstance(obs_data, Mapping):
            raise TypeError("obs_data must be an object")
        if image_items is None:
            image_items = self.list_images()
        if flavor_items is None:
            flavor_items = self.list_flavors()
        device_mapping = _load_device_mapping()
        return _format_data(
            obs_data,
            _catalog_items(image_items, "image"),
            _catalog_items(flavor_items, "flavor"),
            device_mapping,
            projectId,
            networkId,
        )

    def _clear_topology(self, project_id: str, network_id: str) -> None:
        path = TOPOLOGY_CLEAR_PATH.format(
            projectId=quote(project_id, safe=""),
            networkId=quote(network_id, safe=""),
        )
        try:
            response = self._authorized_request("DELETE", path)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TimeoutError(
                "topology clear transport failed; state is unknown and import was not attempted"
            ) from exc
        if response.status_code != 204:
            _response_object(response, "topology clear")

    def close(self) -> None:
        """Close the HTTP session and clear credentials held in memory."""

        self._token = None
        self._credentials = None
        self._session.close()

    def _authorized_request(
        self, method: str, path: str, **kwargs: object
    ) -> requests.Response:
        if self._token is None:
            raise RuntimeError(
                "call client.login() before querying resources or importing a topology"
            )
        response = self._request(
            method,
            path,
            headers={"authorization": self._token},
            **kwargs,
        )
        if not _authentication_failed(response):
            return response
        if self._credentials is None:
            raise RuntimeError("authorization token is invalid")

        self.login(*self._credentials)
        return self._request(
            method,
            path,
            headers={"authorization": self._token},
            **kwargs,
        )

    def _post(self, path: str, **kwargs: object) -> requests.Response:
        return self._request("POST", path, **kwargs)

    def _request(
        self, method: str, path: str, **kwargs: object
    ) -> requests.Response:
        response = self._session.request(
            method,
            f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}",
            timeout=TIMEOUT_SECONDS,
            **kwargs,
        )
        if response.status_code not in {401, 403}:
            response.raise_for_status()
        return response


def login(username: str, password: str, rememberMe: bool = False) -> str:
    """Log in and return the token used by later calls."""

    global _token, _credentials
    _validate_login_input(username, password, rememberMe)

    response = _post(
        LOGIN_PATH,
        json={
            "username": username,
            "password": password,
            "rememberMe": rememberMe,
        },
    )
    _token = _login_token(response)
    _credentials = (username, password, rememberMe)
    return _token


def list_images(
    token: str | None = None,
    *,
    pageIndex: int = 1,
    pageSize: int = 100,
    fetchAll: bool = True,
    imageName: str | None = None,
    status: str | None = "ACTIVE",
    virtualization: str | None = None,
    visibility: str | None = None,
    osType: str | None = None,
    osVersion: str | None = None,
    platformId: int | None = None,
    platformType: str | None = None,
    hardwareArchitecture: str | None = None,
    hasEdr: str | None = None,
    virtio: bool | None = None,
    qga: bool | None = None,
    cloud: bool | None = None,
    nodeType: str | None = None,
    id: str | None = None,
) -> list[dict[str, object]]:
    """Query images and fetch every page by default."""

    filters = _filters(
        imageName=imageName,
        status=status,
        virtualization=virtualization,
        visibility=visibility,
        osType=osType,
        osVersion=osVersion,
        platformId=platformId,
        platformType=platformType,
        hardwareArchitecture=hardwareArchitecture,
        hasEdr=hasEdr,
        virtio=virtio,
        qga=qga,
        cloud=cloud,
        nodeType=nodeType,
        id=id,
    )
    return _list_all(IMAGE_LIST_PATH, token, pageIndex, pageSize, fetchAll, filters)


def list_flavors(
    token: str | None = None,
    *,
    pageIndex: int = 1,
    pageSize: int = 100,
    fetchAll: bool = True,
    id: str | None = None,
    flavorName: str | None = None,
    cpu: int | None = None,
    disk: int | None = None,
    ram: int | None = None,
) -> list[dict[str, object]]:
    """Query flavors and fetch every page by default."""

    filters = _filters(id=id, flavorName=flavorName, cpu=cpu, disk=disk, ram=ram)
    return _list_all(FLAVOR_LIST_PATH, token, pageIndex, pageSize, fetchAll, filters)


def import_topology(
    payload: Mapping[str, object], token: str | None = None
) -> dict[str, object]:
    """Submit a topology with project and network IDs in the query string."""

    body, params = _topology_request(payload)
    try:
        response = _authorized_post(
            TOPOLOGY_IMPORT_PATH,
            token,
            params=params,
            json=body,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TimeoutError(
            "topology import transport failed; write state is unknown and was not retried"
        ) from exc
    return _response_object(response, "topology import")


def load_resource_snapshot(
    image_path: str | Path, flavor_path: str | Path
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Load image and flavor catalogs from local JSON snapshots."""

    return (
        _read_resource_snapshot(image_path, "image"),
        _read_resource_snapshot(flavor_path, "flavor"),
    )


def _catalog_items(
    value: Sequence[Mapping[str, object]], label: str
) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{label}_items must be a sequence of objects")
    if any(not isinstance(item, Mapping) for item in value):
        raise TypeError(f"{label}_items must be a sequence of objects")
    return [dict(item) for item in value]


def _read_resource_snapshot(path: str | Path, label: str) -> list[dict[str, object]]:
    snapshot_path = Path(path)
    try:
        value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read offline {label} snapshot: {snapshot_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid offline {label} snapshot: {snapshot_path}") from exc
    if isinstance(value, Mapping):
        if isinstance(value.get("items"), list):
            value = value["items"]
        elif isinstance(value.get("data"), Mapping):
            data = value["data"]
            if isinstance(data.get("items"), list):
                value = data["items"]
    return _catalog_items(value, f"offline {label}_items")


def _load_device_mapping() -> dict[str, dict[str, object]]:
    try:
        value = yaml.safe_load(_DEVICE_MAPPING_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid device mapping: {_DEVICE_MAPPING_PATH}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("device mapping root must be an object")
    return {
        str(key): dict(item)
        for key, item in value.items()
        if isinstance(item, Mapping)
    }


def _mapping_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{label} must be an array of objects")
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return " ".join(value.strip().split())


def _first_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(value.strip().split())
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return " ".join(item.strip().split())
    return None


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ValueError(f"{label} must be an integer")
    if result < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return result


def _image_types(image: Mapping[str, object]) -> set[str]:
    value = image.get("nodeType")
    if not isinstance(value, str):
        return set()
    return {
        item.strip().upper()
        for item in value.replace(";", ",").split(",")
        if item.strip()
    }


def _image_score(
    node_name: str,
    vendor_model: str | None,
    keywords: list[str],
    image_name: str,
) -> int:
    image_value = image_name.casefold()
    score = 0
    for term_value in (node_name, vendor_model or ""):
        term = term_value.casefold()
        if term == image_value:
            score = max(score, 100)
        elif image_value in term:
            score = max(score, 80)
        elif len(term) >= 3 and term in image_value:
            score = max(score, 60)
    for index, keyword_value in enumerate(keywords):
        keyword = keyword_value.casefold()
        if keyword == image_value:
            score = max(score, 50 - min(index, 10))
        elif keyword in image_value or image_value in keyword:
            score = max(score, 40 - min(index, 10))
    return score


def _select_image(
    node_name: str,
    vendor_model: str | None,
    keywords: list[str],
    dev_type: str,
    images: list[dict[str, object]],
) -> dict[str, object]:
    active = [
        image
        for image in images
        if str(image.get("status", "")).casefold() == "active"
        and image.get("access") is not False
        and isinstance(image.get("id"), str)
        and isinstance(image.get("imageName"), str)
    ]
    compatible = [image for image in active if dev_type in _image_types(image)]
    candidates = compatible
    if not candidates:
        candidates = [
            image
            for image in active
            if _image_score(
                node_name,
                vendor_model,
                keywords,
                str(image["imageName"]),
            )
            > 0
        ]
    if not candidates:
        raise ValueError(f"no image matches device {node_name}")
    return min(
        candidates,
        key=lambda image: (
            -_image_score(
                node_name,
                vendor_model,
                keywords,
                str(image["imageName"]),
            ),
            _integer(image.get("minRam"), "image.minRam"),
            _integer(image.get("minDisk"), "image.minDisk"),
            str(image["imageName"]).casefold(),
        ),
    )


def _select_flavor(
    image: Mapping[str, object], flavors: list[dict[str, object]]
) -> dict[str, object]:
    min_ram = _integer(image.get("minRam"), "image.minRam")
    min_disk = _integer(image.get("minDisk"), "image.minDisk")
    candidates = []
    for flavor in flavors:
        ram = _integer(flavor.get("ram"), "flavor.ram", minimum=1)
        disk = _integer(flavor.get("disk"), "flavor.disk", minimum=1)
        cpu = _integer(flavor.get("cpu"), "flavor.cpu", minimum=1)
        if ram >= min_ram and disk >= min_disk:
            candidates.append((flavor, cpu, ram, disk))
    if not candidates:
        raise ValueError(
            f"no flavor satisfies minRam={min_ram} and minDisk={min_disk}"
        )
    selected, _, _, _ = min(
        candidates,
        key=lambda item: (
            (item[2] - min_ram) / max(min_ram, 1)
            + (item[3] - min_disk) / max(min_disk, 1),
            item[1],
            item[2],
            item[3],
            str(item[0].get("id", "")),
        ),
    )
    return selected


def _format_coordinate(value: float) -> str:
    rounded = round(value, 2)
    if rounded == 0:
        return "0"
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _format_data(
    observation: Mapping[str, object],
    images: list[dict[str, object]],
    flavors: list[dict[str, object]],
    device_mapping: dict[str, dict[str, object]],
    project_id: str | None,
    network_id: str | None,
) -> dict[str, object]:
    if (project_id is None) != (network_id is None):
        raise ValueError("projectId and networkId must be supplied together")
    if project_id is not None and network_id is not None:
        project_id = _text(project_id, "projectId")
        network_id = _text(network_id, "networkId")
    blocking = [
        item
        for item in _mapping_list(observation.get("unresolvedItems", []), "unresolvedItems")
        if item.get("blocking") is True
    ]
    if blocking:
        raise ValueError("topology observation contains blocking unresolved items")

    observed_nodes = _mapping_list(observation.get("observedNodes"), "observedNodes")
    observed_links = _mapping_list(observation.get("observedLinks"), "observedLinks")
    observed_regions = _mapping_list(
        observation.get("observedRegions", []), "observedRegions"
    )
    image_info = observation.get("image")
    if not isinstance(image_info, Mapping):
        raise ValueError("topology observation image must be an object")
    image_width = _integer(image_info.get("width"), "image.width", minimum=1)
    image_height = _integer(image_info.get("height"), "image.height", minimum=1)

    issued_ids: set[str] = set()

    def new_id(prefix: str) -> str:
        while True:
            value = f"{prefix}{uuid4().hex}"
            if value not in issued_ids:
                issued_ids.add(value)
                return value

    nodes: dict[str, dict[str, object]] = {}
    node_order: list[str] = []
    for index, observed_node in enumerate(observed_nodes):
        observed_id = _text(
            observed_node.get("observationId"), f"observedNodes[{index}].observationId"
        )
        if observed_id in nodes:
            raise ValueError(f"duplicate observed node ID: {observed_id}")
        node_name = _first_text(observed_node.get("rawName")) or _first_text(
            observed_node.get("nameCandidates")
        )
        if node_name is None:
            raise ValueError(f"node {observed_id} has no usable name")
        semantic_type = _text(
            observed_node.get("semanticType"), f"node {observed_id}.semanticType"
        )
        mapping = device_mapping.get(semantic_type)
        if mapping is None:
            raise ValueError(f"node {observed_id} has no device mapping")
        node_type = _text(mapping.get("nodeType"), f"{semantic_type}.nodeType")
        dev_type = _text(mapping.get("devType"), f"{semantic_type}.devType")
        if node_type not in {"VM", "SW", "TSW"}:
            raise ValueError(f"unsupported platform node type: {node_type}")
        center = observed_node.get("center")
        if not isinstance(center, Mapping):
            raise ValueError(f"node {observed_id}.center must be an object")
        center_x = float(center.get("x", 0))
        center_y = float(center.get("y", 0))
        nodes[observed_id] = {
            "source": observed_node,
            "name": node_name,
            "semanticType": semantic_type,
            "nodeType": node_type,
            "devType": dev_type,
            "mapping": mapping,
            "id": new_id({"VM": "V", "SW": "W", "TSW": "T"}[node_type]),
            "centerX": center_x,
            "centerY": center_y,
        }
        node_order.append(observed_id)

    interfaces: dict[str, dict[str, object]] = {}
    interface_order: list[str] = []

    def add_interface(item: Mapping[str, object], implicit_owner: str | None) -> None:
        observed_id = _text(item.get("observationId"), "interface.observationId")
        if observed_id in interfaces:
            if implicit_owner and not interfaces[observed_id].get("owner"):
                interfaces[observed_id]["owner"] = implicit_owner
            return
        candidates = item.get("nodeCandidates")
        owner = _first_text(candidates) or implicit_owner
        if owner not in nodes:
            raise ValueError(f"interface {observed_id} has no valid owner")
        interface_name = _first_text(item.get("rawName")) or _first_text(
            item.get("nameCandidates")
        )
        ip_text = _first_text(item.get("ipCandidates")) or _first_text(
            item.get("rawIpText")
        )
        address = None
        network = None
        if ip_text:
            if "/" not in ip_text:
                raise ValueError(f"interface {observed_id} IP has no prefix")
            parsed = IPv4Interface(ip_text)
            address = str(parsed.ip)
            network = parsed.network
        interfaces[observed_id] = {
            "source": dict(item),
            "owner": owner,
            "name": interface_name,
            "ip": address,
            "network": network,
            "segment": None,
        }
        interface_order.append(observed_id)

    for item in _mapping_list(
        observation.get("observedInterfaces", []), "observedInterfaces"
    ):
        add_interface(item, None)
    for observed_node in observed_nodes:
        owner = _text(observed_node.get("observationId"), "node.observationId")
        for item in _mapping_list(
            observed_node.get("observedInterfaces", []), "node.observedInterfaces"
        ):
            add_interface(item, owner)

    link_records: list[dict[str, object]] = []
    for index, observed_link in enumerate(observed_links):
        source = _first_text(observed_link.get("sourceNodeCandidates"))
        target = _first_text(observed_link.get("targetNodeCandidates"))
        if source not in nodes or target not in nodes or source == target:
            raise ValueError(f"observedLinks[{index}] has invalid endpoints")
        link_observed_id = _text(
            observed_link.get("observationId"), f"observedLinks[{index}].observationId"
        )

        def endpoint_interface(field: str, owner: str) -> str | None:
            candidate = _first_text(observed_link.get(field))
            if candidate in interfaces and interfaces[candidate]["owner"] == owner:
                return candidate
            nearby = [
                interface_id
                for interface_id, interface in interfaces.items()
                if interface["owner"] == owner
                and link_observed_id
                in interface["source"].get("nearbyLinkIds", [])
            ]
            return nearby[0] if nearby else None

        link_records.append(
            {
                "source": source,
                "target": target,
                "sourceInterface": endpoint_interface(
                    "sourceInterfaceCandidates", source
                ),
                "targetInterface": endpoint_interface(
                    "targetInterfaceCandidates", target
                ),
                "id": new_id("L"),
            }
        )

    segments: list[dict[str, object]] = []
    priorities = {"DRT": 0, "PRT": 0, "FW": 1, "TSW": 2}
    switch_ids = [
        node_id
        for node_id in node_order
        if nodes[node_id]["semanticType"] == "switch_l2"
    ]

    def terminal_evidence_count(switch_id: str) -> int:
        count = 0
        for link in link_records:
            if link["source"] == switch_id:
                peer_id = str(link["target"])
                interface_id = link["targetInterface"]
            elif link["target"] == switch_id:
                peer_id = str(link["source"])
                interface_id = link["sourceInterface"]
            else:
                continue
            if (
                str(nodes[peer_id]["devType"]) not in priorities
                and isinstance(interface_id, str)
                and isinstance(interfaces[interface_id]["network"], IPv4Network)
            ):
                count += 1
        return count

    switch_ids.sort(key=lambda switch_id: -terminal_evidence_count(switch_id))
    for switch_id in switch_ids:
        member_ids: list[str] = []
        connections: list[tuple[dict[str, object], str, str]] = []
        for link in link_records:
            if link["source"] == switch_id:
                member_id = str(link["target"])
                interface_field = "targetInterface"
            elif link["target"] == switch_id:
                member_id = str(link["source"])
                interface_field = "sourceInterface"
            else:
                continue
            if member_id not in member_ids:
                member_ids.append(member_id)
            connections.append((link, member_id, interface_field))

        network_counts: Counter[IPv4Network] = Counter()
        for link, member_id, interface_field in connections:
            interface_id = link[interface_field]
            dev_type = str(nodes[member_id]["devType"])
            candidates: list[str] = []
            if (
                isinstance(interface_id, str)
                and interfaces[interface_id]["segment"] is None
                and isinstance(interfaces[interface_id]["network"], IPv4Network)
            ):
                candidates.append(interface_id)
            elif dev_type in priorities:
                candidates.extend(
                    candidate_id
                    for candidate_id, candidate in interfaces.items()
                    if candidate["owner"] == member_id
                    and candidate["segment"] is None
                    and isinstance(candidate["network"], IPv4Network)
                )
            for candidate_id in candidates:
                network = interfaces[candidate_id]["network"]
                if isinstance(network, IPv4Network):
                    network_counts[network] += 1 if dev_type in priorities else 2
        if not network_counts:
            raise ValueError(
                f"switch {nodes[switch_id]['name']} has no subnet evidence"
            )
        cidr = min(
            network_counts,
            key=lambda network: (
                -network_counts[network],
                int(network.network_address),
                network.prefixlen,
            ),
        )

        member_interfaces: list[str] = []
        for link, member_id, interface_field in connections:
            interface_id = link[interface_field]
            if (
                not isinstance(interface_id, str)
                or interfaces[interface_id]["network"] != cidr
                or interfaces[interface_id]["segment"] is not None
            ):
                alternatives = [
                    candidate_id
                    for candidate_id, candidate in interfaces.items()
                    if candidate["owner"] == member_id
                    and candidate["network"] == cidr
                    and candidate["segment"] is None
                ]
                if len(alternatives) == 1:
                    interface_id = alternatives[0]
                    link[interface_field] = interface_id
            if (
                isinstance(interface_id, str)
                and interfaces[interface_id]["network"] == cidr
                and interface_id not in member_interfaces
            ):
                member_interfaces.append(interface_id)

        used_ips = {
            str(interface["ip"])
            for interface in interfaces.values()
            if interface["network"] == cidr and interface["ip"] is not None
        }
        for link, member_id, interface_field in connections:
            interface_id = link[interface_field]
            if not isinstance(interface_id, str):
                continue
            interface = interfaces[interface_id]
            if (
                interface["network"] is not None
                or nodes[member_id]["nodeType"] != "VM"
            ):
                continue
            available_ip = next(
                (str(host) for host in cidr.hosts() if str(host) not in used_ips),
                None,
            )
            if available_ip is None:
                raise ValueError(f"subnet {cidr} has no address for {nodes[member_id]['name']}")
            interface["ip"] = available_ip
            interface["network"] = cidr
            used_ips.add(available_ip)
            if interface_id not in member_interfaces:
                member_interfaces.append(interface_id)

        gateway_candidates = []
        for interface_id in member_interfaces:
            interface = interfaces[interface_id]
            if interface["ip"] is None:
                continue
            owner = nodes[str(interface["owner"])]
            priority = priorities.get(str(owner["devType"]))
            if priority is not None:
                gateway_candidates.append(
                    (priority, int(IPv4Interface(f"{interface['ip']}/32").ip), interface["ip"])
                )
        gateway = min(gateway_candidates)[2] if gateway_candidates else None
        segment = {
            "switch": switch_id,
            "members": member_ids,
            "interfaces": member_interfaces,
            "cidr": str(cidr),
            "gateway": gateway,
            "networkId": new_id("G"),
            "subnetId": new_id("S"),
        }
        segments.append(segment)
        for interface_id in member_interfaces:
            interfaces[interface_id]["segment"] = segment

    regions_by_node: dict[str, tuple[str, str | None]] = {}
    region_by_id: dict[str, tuple[str, str | None]] = {}
    for region in observed_regions:
        region_id = _text(region.get("observationId"), "region.observationId")
        region_name = _first_text(region.get("rawName")) or _first_text(
            region.get("nameCandidates")
        )
        if region_name is None:
            continue
        color = region.get("fillColor")
        region_value = (region_name, color if isinstance(color, str) else None)
        region_by_id[region_id] = region_value
        for member in region.get("memberNodeCandidates", []):
            if isinstance(member, str):
                regions_by_node[member] = region_value
    for node_id, node in nodes.items():
        candidate = _first_text(node["source"].get("regionCandidates"))
        if candidate in region_by_id:
            regions_by_node[node_id] = region_by_id[candidate]

    resources: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    for node_id in node_order:
        node = nodes[node_id]
        if node["nodeType"] != "VM":
            continue
        mapping = node["mapping"]
        keyword_values = mapping.get("imageKeywords", [])
        keywords = [str(value) for value in keyword_values] if isinstance(keyword_values, list) else []
        vendor_model = node["source"].get("vendorModel")
        image = _select_image(
            str(node["name"]),
            vendor_model if isinstance(vendor_model, str) else None,
            keywords,
            str(node["devType"]),
            images,
        )
        resources[node_id] = (image, _select_flavor(image, flavors))

    scale = min(1200 / image_width, 800 / image_height)
    nic_names: dict[str, str] = {}
    nics_by_node: dict[str, list[dict[str, object]]] = defaultdict(list)
    for interface_id in interface_order:
        interface = interfaces[interface_id]
        if interface["ip"] is None:
            continue
        segment = interface["segment"]
        if not isinstance(segment, Mapping):
            continue
        owner_id = str(interface["owner"])
        interface_name = interface["name"]
        if not isinstance(interface_name, str) or not interface_name:
            raise ValueError(f"addressed interface {interface_id} has no name")
        node_name = str(nodes[owner_id]["name"])
        nic_name = (
            interface_name
            if interface_name.casefold().startswith(f"{node_name}-".casefold())
            else f"{node_name}-{interface_name}"
        )
        nic_names[interface_id] = nic_name
        nics_by_node[owner_id].append(
            {
                "id": new_id("P"),
                "ip": interface["ip"],
                "name": nic_name,
                "subnetId": segment["subnetId"],
            }
        )

    node_list = []
    for node_id in node_order:
        node = nodes[node_id]
        properties: dict[str, object] = {
            "devType": node["devType"],
            "id": node["id"],
            "nodeName": node["name"],
            "otherAttributeList": [],
            "singleNetwork": False,
            "transparent": 0,
            "x": _format_coordinate(
                (float(node["centerX"]) - image_width / 2) * scale
            ),
            "y": _format_coordinate(
                (float(node["centerY"]) - image_height / 2) * scale
            ),
        }
        if node_id in regions_by_node:
            district, color = regions_by_node[node_id]
            properties["district"] = district
            if color:
                properties["fillColor"] = color
        if node["nodeType"] == "VM":
            image, flavor = resources[node_id]
            properties.update(
                {
                    "flavor": {
                        "cpu": str(_integer(flavor.get("cpu"), "flavor.cpu", minimum=1)),
                        "disk": str(_integer(flavor.get("disk"), "flavor.disk", minimum=1)),
                        "ram": str(_integer(flavor.get("ram"), "flavor.ram", minimum=1)),
                    },
                    "imageId": _text(image.get("id"), "image.id"),
                    "imageName": _text(image.get("imageName"), "image.imageName"),
                    "metadata": [],
                    "sysType": _text(image.get("osType"), "image.osType"),
                    "userData": "",
                }
            )
        node_list.append(
            {
                "nicList": nics_by_node[node_id],
                "properties": properties,
                "type": node["nodeType"],
            }
        )

    network_list = [
        {
            "id": segment["networkId"],
            "mtu": DEFAULT_MTU,
            "name": f"sg-{nodes[str(segment['switch'])]['name']}",
            "nodeId": nodes[str(segment["switch"])]["id"],
            "transmitNodeIdList": [nodes[str(member)]["id"] for member in segment["members"]],
        }
        for segment in segments
    ]
    subnet_list = []
    for segment in segments:
        subnet = {
            "cidr": segment["cidr"],
            "dns": DEFAULT_DNS,
            "enableDhcp": DEFAULT_ENABLE_DHCP,
            "id": segment["subnetId"],
            "name": f"sn-{nodes[str(segment['switch'])]['name']}",
            "networkId": segment["networkId"],
        }
        if segment["gateway"]:
            subnet["gatewayIp"] = segment["gateway"]
        subnet_list.append(subnet)

    link_list = []
    for link in link_records:
        source_id = str(link["source"])
        target_id = str(link["target"])
        source_interface = link["sourceInterface"]
        target_interface = link["targetInterface"]
        source_is_switch = nodes[source_id]["nodeType"] in {"SW", "TSW"}
        target_is_switch = nodes[target_id]["nodeType"] in {"SW", "TSW"}
        if target_is_switch and not source_is_switch:
            source_id, target_id = target_id, source_id
            source_interface, target_interface = target_interface, source_interface
        formatted_link: dict[str, object] = {
            "dDevId": nodes[target_id]["id"],
            "id": link["id"],
            "sDevId": nodes[source_id]["id"],
        }
        if isinstance(source_interface, str) and source_interface in nic_names:
            formatted_link["sNicName"] = nic_names[source_interface]
        if isinstance(target_interface, str) and target_interface in nic_names:
            formatted_link["dNicName"] = nic_names[target_interface]
        link_list.append(formatted_link)

    payload: dict[str, object] = {
        "linkList": link_list,
        "networkList": network_list,
        "nodeList": node_list,
        "portMappingList": [],
        "subnetList": subnet_list,
        "version": DEFAULT_VERSION,
    }
    if project_id is not None and network_id is not None:
        payload["projectId"] = project_id
        payload["networkId"] = network_id
    _validate_formatted_data(payload, require_external_ids=project_id is not None)
    return payload


def validate_payload(payload: Mapping[str, object]) -> None:
    """Validate a complete platform payload before an import request."""

    _validate_formatted_data(payload)


def _validate_formatted_data(
    payload: Mapping[str, object], *, require_external_ids: bool = True
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be an object")
    if require_external_ids:
        _topology_request(payload)
    else:
        for field in (
            "networkList",
            "subnetList",
            "nodeList",
            "linkList",
            "portMappingList",
        ):
            if not isinstance(payload.get(field), list):
                raise ValueError(f"{field} must be an array")
    node_list = _mapping_list(payload["nodeList"], "nodeList")
    network_list = _mapping_list(payload["networkList"], "networkList")
    subnet_list = _mapping_list(payload["subnetList"], "subnetList")
    link_list = _mapping_list(payload["linkList"], "linkList")

    def unique_text(
        value: object, label: str, seen: set[str]
    ) -> str:
        result = _text(value, label)
        if result in seen:
            raise ValueError(f"duplicate {label} ID: {result}")
        seen.add(result)
        return result

    node_ids: set[str] = set()
    node_types: dict[str, str] = {}
    node_nic_names: dict[str, set[str]] = {}
    pending_nics: list[tuple[str, dict[str, object], int]] = []
    valid_dev_types = {"SERVER", "CLIENT", "DRT", "FW", "IDS", "WAF", "PRT", "DES"}

    for index, node in enumerate(node_list):
        node_type = _text(node.get("type"), f"nodeList[{index}].type")
        if node_type not in {"VM", "SW", "TSW"}:
            raise ValueError(f"unsupported node type: {node_type}")
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            raise ValueError(f"nodeList[{index}].properties must be an object")
        node_id = unique_text(
            properties.get("id"), f"nodeList[{index}].properties.id", node_ids
        )
        node_types[node_id] = node_type
        _text(properties.get("devType"), f"nodeList[{index}].properties.devType")
        _text(properties.get("nodeName"), f"nodeList[{index}].properties.nodeName")
        _text(properties.get("x"), f"nodeList[{index}].properties.x")
        _text(properties.get("y"), f"nodeList[{index}].properties.y")

        dev_type = str(properties["devType"])
        expected_dev_type = {"SW": "SW", "TSW": "TSW"}.get(node_type)
        if expected_dev_type is not None and dev_type != expected_dev_type:
            raise ValueError(
                f"node {node_id} type {node_type} conflicts with devType {dev_type}"
            )
        if node_type == "VM":
            if dev_type not in valid_dev_types:
                raise ValueError(f"unsupported VM devType: {dev_type}")
            for field in ("imageId", "imageName", "sysType"):
                _text(properties.get(field), f"node {node_id}.properties.{field}")
            flavor = properties.get("flavor")
            if not isinstance(flavor, Mapping):
                raise ValueError(f"node {node_id}.properties.flavor must be an object")
            for field in ("cpu", "ram", "disk"):
                _text(flavor.get(field), f"node {node_id}.properties.flavor.{field}")

        nic_list = _mapping_list(node.get("nicList", []), f"node {node_id}.nicList")
        if node_type != "VM" and nic_list:
            raise ValueError(f"switch node {node_id} must not contain NICs")
        names: set[str] = set()
        for nic_index, nic in enumerate(nic_list):
            nic_name = _text(nic.get("name"), f"node {node_id}.nicList[{nic_index}].name")
            if nic_name in names:
                raise ValueError(f"duplicate NIC name on node {node_id}: {nic_name}")
            names.add(nic_name)
            pending_nics.append((node_id, nic, nic_index))
        node_nic_names[node_id] = names

    network_ids: set[str] = set()
    for index, network in enumerate(network_list):
        network_id = unique_text(network.get("id"), f"networkList[{index}].id", network_ids)
        node_id = _text(network.get("nodeId"), f"network {network_id}.nodeId")
        if node_id not in node_ids:
            raise ValueError(f"formatted network references an unknown node: {node_id}")
        if node_types[node_id] not in {"SW", "TSW"}:
            raise ValueError(f"network {network_id} must be anchored to a switch")
        _text(network.get("name"), f"network {network_id}.name")
        mtu = network.get("mtu")
        if mtu is not None and (
            isinstance(mtu, bool) or not isinstance(mtu, int) or mtu <= 0
        ):
            raise ValueError(f"network {network_id}.mtu must be a positive integer")
        members = network.get("transmitNodeIdList", [])
        if not isinstance(members, list):
            raise ValueError(f"network {network_id}.transmitNodeIdList must be an array")
        member_ids: set[str] = set()
        for member in members:
            member_id = _text(member, f"network {network_id}.transmitNodeIdList")
            if member_id in member_ids:
                raise ValueError(f"duplicate network member: {member_id}")
            if member_id not in node_ids:
                raise ValueError(f"network {network_id} references an unknown member: {member_id}")
            member_ids.add(member_id)

    subnet_ids: set[str] = set()
    subnet_networks: dict[str, IPv4Network] = {}
    for index, subnet in enumerate(subnet_list):
        subnet_id = unique_text(subnet.get("id"), f"subnetList[{index}].id", subnet_ids)
        network_id = _text(subnet.get("networkId"), f"subnet {subnet_id}.networkId")
        if network_id not in network_ids:
            raise ValueError(f"formatted subnet references an unknown network: {network_id}")
        _text(subnet.get("name"), f"subnet {subnet_id}.name")
        cidr_text = _text(subnet.get("cidr"), f"subnet {subnet_id}.cidr")
        try:
            subnet_networks[subnet_id] = IPv4Network(cidr_text, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid CIDR in subnet {subnet_id}") from exc
        gateway = subnet.get("gatewayIp")
        if gateway is not None:
            gateway_text = _text(gateway, f"subnet {subnet_id}.gatewayIp")
            try:
                gateway_ip = IPv4Address(gateway_text)
            except ValueError as exc:
                raise ValueError(f"invalid gateway in subnet {subnet_id}") from exc
            if gateway_ip not in subnet_networks[subnet_id]:
                raise ValueError(f"gateway is outside subnet {subnet_id}")
        if "dns" in subnet:
            dns = subnet.get("dns")
            if not isinstance(dns, str):
                raise ValueError(f"subnet {subnet_id}.dns must be a string")
        if "enableDhcp" in subnet and not isinstance(subnet["enableDhcp"], bool):
            raise ValueError(f"subnet {subnet_id}.enableDhcp must be a boolean")

    nic_ids: set[str] = set()
    used_ips: set[IPv4Address] = set()
    for node_id, nic, nic_index in pending_nics:
        nic_id = unique_text(nic.get("id"), f"node {node_id}.nicList[{nic_index}].id", nic_ids)
        subnet_id = _text(nic.get("subnetId"), f"NIC {nic_id}.subnetId")
        subnet = subnet_networks.get(subnet_id)
        if subnet is None:
            raise ValueError(f"NIC {nic_id} references an unknown subnet: {subnet_id}")
        ip_text = _text(nic.get("ip"), f"NIC {nic_id}.ip")
        try:
            ip = IPv4Address(ip_text)
        except ValueError as exc:
            raise ValueError(f"invalid IP on NIC {nic_id}") from exc
        if ip in used_ips:
            raise ValueError(f"duplicate IP address: {ip}")
        if ip not in subnet:
            raise ValueError(f"IP {ip} is outside subnet {subnet_id}")
        used_ips.add(ip)

    link_keys: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    link_ids: set[str] = set()
    linked_node_ids: set[str] = set()
    for index, link in enumerate(link_list):
        link_id = unique_text(link.get("id"), f"linkList[{index}].id", link_ids)
        source_id = _text(link.get("sDevId"), f"link {link_id}.sDevId")
        target_id = _text(link.get("dDevId"), f"link {link_id}.dDevId")
        if source_id not in node_ids or target_id not in node_ids:
            raise ValueError(f"formatted link {link_id} references an unknown node")
        if source_id == target_id:
            raise ValueError(f"formatted link {link_id} cannot connect a node to itself")
        source_nic = ""
        target_nic = ""
        if link.get("sNicName") is not None:
            source_nic = _text(link.get("sNicName"), f"link {link_id}.sNicName")
            if source_nic not in node_nic_names[source_id]:
                raise ValueError(f"link {link_id} references an unknown source NIC")
        if link.get("dNicName") is not None:
            target_nic = _text(link.get("dNicName"), f"link {link_id}.dNicName")
            if target_nic not in node_nic_names[target_id]:
                raise ValueError(f"link {link_id} references an unknown target NIC")
        key = tuple(sorted(((source_id, source_nic), (target_id, target_nic))))
        if key in link_keys:
            raise ValueError(f"duplicate link endpoints: {link_id}")
        link_keys.add(key)
        linked_node_ids.update((source_id, target_id))

    for node_id in linked_node_ids:
        if node_types[node_id] == "VM" and not node_nic_names[node_id]:
            raise ValueError(f"linked VM node {node_id} must contain at least one NIC")

    port_mappings = payload.get("portMappingList")
    if not isinstance(port_mappings, list):
        raise ValueError("portMappingList must be an array")
    if any(not isinstance(item, str) for item in port_mappings):
        raise ValueError("portMappingList must contain strings")


def _list_all(
    path: str,
    token: str | None,
    page_index: int,
    page_size: int,
    fetch_all: bool,
    filters: dict[str, object],
) -> list[dict[str, object]]:
    _positive_integer("pageIndex", page_index)
    _positive_integer("pageSize", page_size)
    if not isinstance(fetch_all, bool):
        raise TypeError("fetchAll must be a boolean")

    result: list[dict[str, object]] = []
    page = page_index
    while True:
        response = _authorized_post(
            path,
            token,
            params={"pageIndex": page, "pageSize": page_size},
            json=filters,
        )
        data = _response_data(response, "resource query")
        if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
            raise RuntimeError("resource query response.data.items must be an array")
        items = data["items"]
        if any(not isinstance(item, Mapping) for item in items):
            raise RuntimeError("resource query items must be objects")
        result.extend(dict(item) for item in items if isinstance(item, Mapping))

        total = data.get("total")
        consumed = (page - 1) * page_size + len(items)
        reached_total = isinstance(total, int) and consumed >= total
        if not fetch_all or not items or len(items) < page_size or reached_total:
            return result
        page += 1


def _list_all_for_client(
    client: TopologyPlatformClient,
    path: str,
    page_index: int,
    page_size: int,
    fetch_all: bool,
    filters: dict[str, object],
) -> list[dict[str, object]]:
    _positive_integer("pageIndex", page_index)
    _positive_integer("pageSize", page_size)
    if not isinstance(fetch_all, bool):
        raise TypeError("fetchAll must be a boolean")

    result: list[dict[str, object]] = []
    page = page_index
    while True:
        response = client._authorized_request(
            "POST",
            path,
            params={"pageIndex": page, "pageSize": page_size},
            json=filters,
        )
        data = _response_data(response, "resource query")
        if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
            raise RuntimeError("resource query response.data.items must be an array")
        items = data["items"]
        if any(not isinstance(item, Mapping) for item in items):
            raise RuntimeError("resource query items must be objects")
        result.extend(dict(item) for item in items if isinstance(item, Mapping))

        total = data.get("total")
        consumed = (page - 1) * page_size + len(items)
        reached_total = isinstance(total, int) and consumed >= total
        if not fetch_all or not items or len(items) < page_size or reached_total:
            return result
        page += 1


def _authorized_post(
    path: str,
    token: str | None,
    **kwargs: object,
) -> requests.Response:
    request_token = token or _token
    if not request_token:
        raise RuntimeError("call login() first or pass token explicitly")

    response = _post(path, headers={"authorization": request_token}, **kwargs)
    if not _authentication_failed(response):
        return response
    if _credentials is None:
        raise RuntimeError("authorization token is invalid")

    refreshed_token = login(*_credentials)
    return _post(path, headers={"authorization": refreshed_token}, **kwargs)


def _post(path: str, **kwargs: object) -> requests.Response:
    response = _session.post(
        f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}",
        timeout=TIMEOUT_SECONDS,
        **kwargs,
    )
    if response.status_code not in {401, 403}:
        response.raise_for_status()
    return response


def _response_object(response: requests.Response, operation: str) -> dict[str, object]:
    try:
        body = response.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(f"{operation} returned a non-JSON response") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{operation} response root must be an object")

    code = body.get("code")
    success = body.get("success")
    failed = body.get("failed")
    accepted = code in SUCCESS_CODES or (success is True and failed is not True)
    if failed is True or success is False or not accepted:
        message = body.get("message")
        safe_message = " ".join(message.split())[:300] if isinstance(message, str) else ""
        detail = f"code={code!r}"
        if safe_message:
            detail += f", message={safe_message}"
        raise RuntimeError(f"{operation} failed ({detail})")
    return body


def _response_data(response: requests.Response, operation: str) -> object:
    return _response_object(response, operation).get("data")


def _login_token(response: requests.Response) -> str:
    data = _response_data(response, "login")
    token: object = data
    if isinstance(data, Mapping):
        token = next(
            (
                data.get(key)
                for key in ("token", "accessToken", "authorization")
                if data.get(key)
            ),
            None,
        )
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("login succeeded but response.data has no token")
    return token.strip()


def _validate_login_input(username: str, password: str, remember_me: bool) -> None:
    if not isinstance(username, str) or not username.strip():
        raise ValueError("username must be a non-empty string")
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    if not isinstance(remember_me, bool):
        raise TypeError("rememberMe must be a boolean")


def _topology_request(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    for field in ("projectId", "networkId"):
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
            raise ValueError(f"{field} must be a non-empty string")
    for field in (
        "networkList",
        "subnetList",
        "nodeList",
        "linkList",
        "portMappingList",
    ):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"{field} must be an array")
    return dict(payload), {
        "projectId": str(payload["projectId"]).strip(),
        "network": str(payload["networkId"]).strip(),
    }


def _authentication_failed(response: requests.Response) -> bool:
    if response.status_code in {401, 403}:
        return True
    try:
        body = response.json()
    except requests.JSONDecodeError:
        return False
    return isinstance(body, Mapping) and body.get("code") in {401, 403}


def _filters(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _positive_integer(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
