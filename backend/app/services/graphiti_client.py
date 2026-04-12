"""
graphiti_client.py

HTTP client that replaces zep_cloud for MiroFish.
Calls the graphiti REST API server instead of Zep Cloud.
"""

import time
from typing import Any

import requests

from ..config import Config


class GraphitiClient:
    """
    MiroFish client for the self-hosted graphiti REST API.
    Replaces zep_cloud.client.Zep.
    """

    def __init__(self, api_key: str | None = None, base_url: str = "http://localhost:8000"):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url.rstrip("/")
        self.timeout = 60

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def create_graph(self, graph_id: str, name: str, description: str = "") -> None:
        """Create graph is implicit in graphiti — group_id is used on first message.
        No-op here for compatibility."""
        pass

    def set_ontology(self, graph_id: str, ontology: dict | None = None) -> None:
        """Store ontology metadata for the group via the graphiti ontology endpoint."""
        if not graph_id or not ontology:
            return
        entity_types = {}
        edge_types = {}

        # MiroFish passes {entity_types: [...], edge_types: [...]} in ontology
        for entity_def in ontology.get("entity_types", []):
            name = entity_def.get("name", "")
            description = entity_def.get("description", f"A {name} entity")
            entity_types[name] = {"description": description}

        for edge_def in ontology.get("edge_types", []):
            name = edge_def.get("name", "")
            description = edge_def.get("description", f"A {name} relationship")
            edge_types[name] = {"description": description}

        payload = {
            "entity_types": entity_types,
            "edge_types": edge_types,
        }
        resp = requests.post(
            f"{self.base_url}/graphs/{graph_id}/ontology",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def add_batch(self, graph_id: str, episodes: list) -> list:
        """
        Send text chunks as episodes via POST /messages.
        Each episode has .data attribute containing the text.
        Returns list of objects with uuid_ attribute.
        """
        messages = []
        for ep in episodes:
            text = getattr(ep, "data", str(ep))
            messages.append({
                "content": text,
                "role_type": "user",
                "role": "",
                "source_description": getattr(ep, "type", "text"),
            })

        payload = {"group_id": graph_id, "messages": messages}
        resp = requests.post(
            f"{self.base_url}/messages",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()

        # graphiti returns 202 (queued) — return count for polling
        return len(messages)

    def episode_get(self, uuid: str) -> Any:
        """
        Poll a single episode by UUID.
        graphiti doesn't expose per-episode status via REST, so we fetch
        all episodes for the group and find our uuid.
        Returns an object with .processed = True if found.
        """
        # graphiti REST doesn't have per-episode status endpoint.
        # We approximate by returning a mock object — caller should use
        # the batch polling approach instead.
        class EpisodeResult:
            def __init__(self, uuid):
                self.uuid_ = uuid
                self.processed = True  # Assume processed; caller should use wait_for_episodes

        return EpisodeResult(uuid)

    def wait_for_episodes(
        self,
        graph_id: str,
        episode_uuids: list[str],
        progress_callback=None,
        timeout: int = 600,
    ) -> None:
        """
        Wait for all episodes to be processed by polling GET /episodes/{group_id}.
        graphiti processes synchronously via LLM extraction, so we poll until
        the episode count matches what we expect.
        """
        print(f"[DEBUG wait_for_episodes] episode_uuids = {repr(episode_uuids)}, type = {type(episode_uuids)}")
        if not episode_uuids:
            return
        if not isinstance(episode_uuids, (list, set, tuple)):
            err_msg = f"FATAL: episode_uuids must be list/tuple/set, got {type(episode_uuids)}: {repr(episode_uuids)[:200]}"
            import sys
            print(err_msg, file=sys.stderr)
            sys.exit(1)

        start_time = time.time()
        pending = set(episode_uuids)

        while pending:
            if time.time() - start_time > timeout:
                break

            try:
                resp = requests.get(
                    f"{self.base_url}/episodes/{graph_id}",
                    params={"last_n": 1000},
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                episodes = resp.json()
                if not isinstance(episodes, list):
                    episodes = []

                # Mark any returned episode uuid as completed
                for ep in episodes:
                    ep_uuid = ep.get("uuid") or ep.get("uuid_")
                    if ep_uuid in pending:
                        pending.discard(ep_uuid)

            except Exception:
                pass

            if progress_callback and pending:
                elapsed = int(time.time() - start_time)
                progress_callback(
                    f"Processing {len(pending)} pending, {len(episode_uuids) - len(pending)} done ({elapsed}s)",
                    (len(episode_uuids) - len(pending)) / len(episode_uuids),
                )

            if pending:
                time.sleep(3)

    def fetch_all_nodes(self, graph_id: str, page_size: int = 100) -> list:
        """
        Fetch all nodes for a group via GET /graphs/{group_id}/nodes with cursor pagination.
        Returns list of objects with uuid_, name, labels, summary, attributes, created_at.
        """
        all_nodes = []
        cursor = None

        while True:
            params = {"limit": page_size}
            if cursor:
                params["cursor"] = cursor

            resp = requests.get(
                f"{self.base_url}/graphs/{graph_id}/nodes",
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            nodes = data.get("nodes", [])
            all_nodes.extend(nodes)
            cursor = data.get("next_cursor")
            if not cursor or len(nodes) < page_size:
                break

        # Wrap in objects that mimic zep_cloud Node
        class NodeResult:
            def __init__(self, data: dict):
                self.uuid_ = data.get("uuid", "")
                self.uuid = data.get("uuid", "")
                self.name = data.get("name", "")
                self.labels = data.get("labels", [])
                self.summary = data.get("summary", "")
                self.attributes = data.get("attributes", {})
                self.created_at = data.get("created_at")

        return [NodeResult(n) for n in all_nodes]

    def fetch_all_edges(self, graph_id: str, page_size: int = 100) -> list:
        """
        Fetch all edges for a group via GET /graphs/{group_id}/edges with cursor pagination.
        Returns list of objects with uuid_, name, fact, source_node_uuid, target_node_uuid, etc.
        """
        all_edges = []
        cursor = None

        while True:
            params = {"limit": page_size}
            if cursor:
                params["cursor"] = cursor

            resp = requests.get(
                f"{self.base_url}/graphs/{graph_id}/edges",
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            edges = data.get("edges", [])
            all_edges.extend(edges)
            cursor = data.get("next_cursor")
            if not cursor or len(edges) < page_size:
                break

        # Wrap in objects that mimic zep_cloud Edge
        class EdgeResult:
            def __init__(self, data: dict):
                self.uuid_ = data.get("uuid", "")
                self.uuid = data.get("uuid", "")
                self.name = data.get("name", "")
                self.fact = data.get("fact", "")
                self.fact_type = data.get("name", "")
                self.source_node_uuid = data.get("source_node_uuid", "")
                self.target_node_uuid = data.get("target_node_uuid", "")
                self.attributes = data.get("attributes", {})
                self.created_at = data.get("created_at")
                self.valid_at = data.get("valid_at")
                self.invalid_at = data.get("invalid_at")
                self.expired_at = data.get("expired_at")
                self.episodes = data.get("episodes", [])
                self.episode_ids = data.get("episodes", [])

        return [EdgeResult(e) for e in all_edges]

    def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 20,
        scope: str = "both",
        reranker: str = "rrf",
    ) -> dict:
        """
        Hybrid search via POST /search.
        Returns {edges: [...], nodes: [...]} matching zep_cloud response shape.
        """
        payload = {
            "group_ids": [graph_id],
            "query": query,
            "max_facts": limit,
        }
        resp = requests.post(
            f"{self.base_url}/search",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        facts = data.get("facts", [])
        return {"edges": facts, "nodes": []}

    def graph_delete(self, graph_id: str) -> None:
        """Delete group and all its data via DELETE /group/{group_id}."""
        resp = requests.delete(
            f"{self.base_url}/group/{graph_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
