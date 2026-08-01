"""Simple function wrappers for the four platform HTTP APIs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

import requests
import yaml


_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "app.yaml"
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
        except requests.Timeout as exc:
            raise TimeoutError(
                "topology import timed out; write state is unknown and was not retried"
            ) from exc
        return _response_object(response, "topology import")

    def _clear_topology(self, project_id: str, network_id: str) -> None:
        path = TOPOLOGY_CLEAR_PATH.format(
            projectId=quote(project_id, safe=""),
            networkId=quote(network_id, safe=""),
        )
        try:
            response = self._authorized_request("DELETE", path)
        except requests.Timeout as exc:
            raise TimeoutError(
                "topology clear timed out; state is unknown and import was not attempted"
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
            raise RuntimeError("call client.login() before importing a topology")
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
    except requests.Timeout as exc:
        raise TimeoutError(
            "topology import timed out; write state is unknown and was not retried"
        ) from exc
    return _response_object(response, "topology import")


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
