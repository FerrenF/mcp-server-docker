"""
title: Docker (MCP over HTTP)
description: Manage Docker through an mcp-server-docker instance served over Streamable HTTP.
version: 0.1.0
"""

import json
from typing import Literal, Optional, Union

import requests
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo


class MCPError(Exception):
    pass


class MCPClient:
    """Minimal synchronous MCP client for the Streamable HTTP transport."""

    def __init__(self, url: str, timeout: float):
        self.url = url
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self._next_id = 0

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, message: dict) -> requests.Response:
        return requests.post(
            self.url, json=message, headers=self._headers(), timeout=self.timeout
        )

    @staticmethod
    def _read_response(response: requests.Response, request_id: int) -> dict:
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("text/event-stream"):
            # The server may interleave notifications; keep the reply to our request
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                message = json.loads(line[len("data:") :].strip())
                if message.get("id") == request_id:
                    return message
            raise MCPError("No response to the request in the event stream")
        return response.json()

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        response = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        response.raise_for_status()
        message = self._read_response(response, request_id)
        if "error" in message:
            raise MCPError(message["error"].get("message", str(message["error"])))
        return message["result"]

    def initialize(self) -> None:
        self.session_id = None
        self.protocol_version = None
        self._next_id += 1
        request_id = self._next_id
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "open-webui-docker-tool", "version": "0.1.0"},
                },
            }
        )
        response.raise_for_status()
        self.session_id = response.headers.get("Mcp-Session-Id")
        message = self._read_response(response, request_id)
        if "error" in message:
            raise MCPError(message["error"].get("message", str(message["error"])))
        self.protocol_version = message["result"]["protocolVersion"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict) -> dict:
        if self.protocol_version is None:
            self.initialize()
        try:
            return self._request("tools/call", {"name": name, "arguments": arguments})
        except requests.HTTPError as e:
            # The server forgets sessions after restarts or idle timeouts
            if e.response is not None and e.response.status_code in (400, 404):
                self.initialize()
                return self._request(
                    "tools/call", {"name": name, "arguments": arguments}
                )
            raise


class Tools:
    class Valves(BaseModel):
        MCP_URL: str = Field(
            "http://host.docker.internal:8000/mcp",
            description="Streamable HTTP endpoint of mcp-server-docker",
        )
        TIMEOUT: float = Field(
            300, description="Seconds to wait for a tool call (pulls/builds are slow)"
        )

    def __init__(self):
        self.valves = self.Valves()
        self._client: Optional[MCPClient] = None

    def _call(self, tool: str, /, **arguments) -> str:
        # Omitted arguments arrive as their Field(...) defaults; drop those and
        # Nones so the server applies its own defaults.
        arguments = {
            key: value
            for key, value in arguments.items()
            if value is not None and not isinstance(value, FieldInfo)
        }

        if self._client is None or self._client.url != self.valves.MCP_URL:
            self._client = MCPClient(self.valves.MCP_URL, self.valves.TIMEOUT)
        self._client.timeout = self.valves.TIMEOUT

        try:
            result = self._client.call_tool(tool, arguments)
        except (requests.RequestException, MCPError, ValueError) as e:
            return f"Error calling {tool}: {e}"

        # Text content splits lists into one block per item, so prefer the
        # structured result; list returns are wrapped as {"result": [...]}.
        if "structuredContent" in result:
            structured = result["structuredContent"]
            if isinstance(structured, dict) and structured.keys() == {"result"}:
                structured = structured["result"]
            text = json.dumps(structured, indent=2)
        else:
            text = "\n".join(
                item.get("text", "")
                for item in result.get("content", [])
                if item.get("type") == "text"
            )
        if result.get("isError"):
            return f"Error from {tool}: {text}"
        return text

    # Containers

    def list_containers(
        self,
        all: bool = Field(
            False, description="Show all containers (default shows just running)"
        ),
        label: Optional[list[str]] = Field(
            None, description="Filter by label, either `key` or `key=value` format"
        ),
        name: Optional[list[str]] = Field(
            None, description="Filter by container name (matches part of the name)"
        ),
        ancestor: Optional[list[str]] = Field(
            None,
            description="Filter by image the container was created from, either `image`, `image:tag`, or image ID",
        ),
        status: Optional[
            list[
                Literal[
                    "created",
                    "restarting",
                    "running",
                    "removing",
                    "paused",
                    "exited",
                    "dead",
                ]
            ]
        ] = Field(None, description="Filter by container status"),
    ) -> str:
        """
        List Docker containers created by this server
        """
        filters = {
            key: value
            for key, value in {
                "label": label,
                "name": name,
                "ancestor": ancestor,
                "status": status,
            }.items()
            if isinstance(value, list) and value
        }
        return self._call("list_containers", all=all, filters=filters or None)

    def create_container(
        self,
        image: str = Field(..., description="Docker image name"),
        detach: bool = Field(True, description="Run container in the background"),
        name: Optional[str] = Field(None, description="Container name"),
        entrypoint: Optional[str] = Field(
            None, description="Entrypoint to run in container"
        ),
        command: Optional[str] = Field(None, description="Command to run in container"),
        network: Optional[str] = Field(
            None, description="Network to attach the container to"
        ),
        environment: Optional[dict[str, str]] = Field(
            None, description="Environment variables dictionary"
        ),
        ports: Optional[dict[str, Union[int, list[int], None]]] = Field(
            None, description="Container-to-host port bindings"
        ),
        volumes: Optional[Union[dict[str, dict[str, str]], list[str]]] = Field(
            None, description="Volume mappings"
        ),
        labels: Optional[Union[dict[str, str], list[str]]] = Field(
            None, description="Container labels"
        ),
        auto_remove: bool = Field(
            False, description="Automatically remove the container"
        ),
    ) -> str:
        """
        Create a new Docker container
        """
        return self._call(
            "create_container",
            image=image,
            detach=detach,
            name=name,
            entrypoint=entrypoint,
            command=command,
            network=network,
            environment=environment,
            ports=ports,
            volumes=volumes,
            labels=labels,
            auto_remove=auto_remove,
        )

    def run_container(
        self,
        image: str = Field(..., description="Docker image name"),
        detach: bool = Field(True, description="Run container in the background"),
        name: Optional[str] = Field(None, description="Container name"),
        entrypoint: Optional[str] = Field(
            None, description="Entrypoint to run in container"
        ),
        command: Optional[str] = Field(None, description="Command to run in container"),
        network: Optional[str] = Field(
            None, description="Network to attach the container to"
        ),
        environment: Optional[dict[str, str]] = Field(
            None, description="Environment variables dictionary"
        ),
        ports: Optional[dict[str, Union[int, list[int], None]]] = Field(
            None, description="Container-to-host port bindings"
        ),
        volumes: Optional[Union[dict[str, dict[str, str]], list[str]]] = Field(
            None, description="Volume mappings"
        ),
        labels: Optional[Union[dict[str, str], list[str]]] = Field(
            None, description="Container labels"
        ),
        auto_remove: bool = Field(
            False, description="Automatically remove the container"
        ),
    ) -> str:
        """
        Run an image in a new Docker container (preferred over `create_container` + `start_container`)
        """
        return self._call(
            "run_container",
            image=image,
            detach=detach,
            name=name,
            entrypoint=entrypoint,
            command=command,
            network=network,
            environment=environment,
            ports=ports,
            volumes=volumes,
            labels=labels,
            auto_remove=auto_remove,
        )

    def recreate_container(
        self,
        image: str = Field(..., description="Docker image name"),
        container_id: Optional[str] = Field(None, description="Container ID or name"),
        name: Optional[str] = Field(None, description="Container name"),
        detach: bool = Field(True, description="Run container in the background"),
        entrypoint: Optional[str] = Field(
            None, description="Entrypoint to run in container"
        ),
        command: Optional[str] = Field(None, description="Command to run in container"),
        network: Optional[str] = Field(
            None, description="Network to attach the container to"
        ),
        environment: Optional[dict[str, str]] = Field(
            None, description="Environment variables dictionary"
        ),
        ports: Optional[dict[str, Union[int, list[int], None]]] = Field(
            None, description="Container-to-host port bindings"
        ),
        volumes: Optional[Union[dict[str, dict[str, str]], list[str]]] = Field(
            None, description="Volume mappings"
        ),
        labels: Optional[Union[dict[str, str], list[str]]] = Field(
            None, description="Container labels"
        ),
        auto_remove: bool = Field(
            False, description="Automatically remove the container"
        ),
    ) -> str:
        """
        Stop and remove a container, then run a new container. Fails if the container does not exist or was not created by this server.
        """
        return self._call(
            "recreate_container",
            image=image,
            container_id=container_id,
            name=name,
            detach=detach,
            entrypoint=entrypoint,
            command=command,
            network=network,
            environment=environment,
            ports=ports,
            volumes=volumes,
            labels=labels,
            auto_remove=auto_remove,
        )

    def start_container(
        self,
        container_id: str = Field(..., description="Container ID or name"),
    ) -> str:
        """
        Start a Docker container
        """
        return self._call("start_container", container_id=container_id)

    def fetch_container_logs(
        self,
        container_id: str = Field(..., description="Container ID or name"),
        tail: Union[int, Literal["all"]] = Field(
            100, description="Number of lines to show from the end"
        ),
    ) -> str:
        """
        Fetch logs for a Docker container
        """
        return self._call("fetch_container_logs", container_id=container_id, tail=tail)

    def exec_container(
        self,
        container_id: str = Field(..., description="Container ID or name"),
        command: Union[str, list[str]] = Field(
            ..., description="Command to run in the container"
        ),
        workdir: Optional[str] = Field(
            None, description="Working directory to run the command in"
        ),
        environment: Optional[dict[str, str]] = Field(
            None, description="Environment variables dictionary"
        ),
    ) -> str:
        """
        Execute a command in a running Docker container. Only works on containers created by this server.
        """
        return self._call(
            "exec_container",
            container_id=container_id,
            command=command,
            workdir=workdir,
            environment=environment,
        )

    def stop_container(
        self,
        container_id: str = Field(..., description="Container ID or name"),
    ) -> str:
        """
        Stop a Docker container
        """
        return self._call("stop_container", container_id=container_id)

    def remove_container(
        self,
        container_id: str = Field(..., description="Container ID or name"),
        force: bool = Field(False, description="Force remove the container"),
    ) -> str:
        """
        Remove a Docker container. Only works on containers created by this server.
        """
        return self._call("remove_container", container_id=container_id, force=force)

    # Images

    def list_images(
        self,
        name: Optional[str] = Field(
            None, description="Filter images by repository name"
        ),
        all: bool = Field(
            False, description="Show all images (default hides intermediate)"
        ),
        dangling: Optional[bool] = Field(None, description="Show dangling images"),
        label: Optional[list[str]] = Field(
            None, description="Filter by label, either `key` or `key=value` format"
        ),
    ) -> str:
        """
        List Docker images
        """
        filters = {}
        if isinstance(dangling, bool):
            filters["dangling"] = dangling
        if isinstance(label, list) and label:
            filters["label"] = label
        return self._call("list_images", name=name, all=all, filters=filters or None)

    def pull_image(
        self,
        repository: str = Field(..., description="Image repository"),
        tag: Optional[str] = Field("latest", description="Image tag"),
    ) -> str:
        """
        Pull a Docker image
        """
        return self._call("pull_image", repository=repository, tag=tag)

    def push_image(
        self,
        repository: str = Field(..., description="Image repository"),
        tag: Optional[str] = Field("latest", description="Image tag"),
    ) -> str:
        """
        Push a Docker image
        """
        return self._call("push_image", repository=repository, tag=tag)

    def build_image(
        self,
        path: str = Field(..., description="Path to build context"),
        tag: str = Field(..., description="Image tag"),
        dockerfile: Optional[str] = Field(None, description="Path to Dockerfile"),
    ) -> str:
        """
        Build a Docker image from a Dockerfile
        """
        return self._call("build_image", path=path, tag=tag, dockerfile=dockerfile)

    def remove_image(
        self,
        image: str = Field(..., description="Image ID or name"),
        force: bool = Field(False, description="Force remove the image"),
    ) -> str:
        """
        Remove a Docker image
        """
        return self._call("remove_image", image=image, force=force)

    # Networks

    def list_networks(
        self,
        label: Optional[list[str]] = Field(
            None, description="Filter by label, either `key` or `key=value` format"
        ),
    ) -> str:
        """
        List Docker networks
        """
        return self._call(
            "list_networks",
            filters={"label": label} if isinstance(label, list) and label else None,
        )

    def create_network(
        self,
        name: str = Field(..., description="Network name"),
        driver: Optional[str] = Field("bridge", description="Network driver"),
        internal: bool = Field(False, description="Create an internal network"),
        labels: Optional[dict[str, str]] = Field(None, description="Network labels"),
    ) -> str:
        """
        Create a Docker network
        """
        return self._call(
            "create_network", name=name, driver=driver, internal=internal, labels=labels
        )

    def remove_network(
        self,
        network_id: str = Field(..., description="Network ID or name"),
    ) -> str:
        """
        Remove a Docker network
        """
        return self._call("remove_network", network_id=network_id)

    # Volumes

    def list_volumes(self) -> str:
        """
        List Docker volumes created by this server
        """
        return self._call("list_volumes")

    def create_volume(
        self,
        name: str = Field(..., description="Volume name"),
        driver: Optional[str] = Field("local", description="Volume driver"),
        labels: Optional[dict[str, str]] = Field(None, description="Volume labels"),
    ) -> str:
        """
        Create a Docker volume
        """
        return self._call("create_volume", name=name, driver=driver, labels=labels)

    def remove_volume(
        self,
        volume_name: str = Field(..., description="Volume name"),
        force: bool = Field(False, description="Force remove the volume"),
    ) -> str:
        """
        Remove a Docker volume. Only works on volumes created by this server.
        """
        return self._call("remove_volume", volume_name=volume_name, force=force)
