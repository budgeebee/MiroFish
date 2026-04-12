"""
图谱构建服务
接口2：使用graphiti API构建知识图谱
"""

import os
import uuid
import time
import threading

import requests
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

from .graphiti_client import GraphitiClient

from ..config import Config
from ..models.task import TaskManager, TaskStatus
from .text_processor import TextProcessor
from ..utils.locale import t, get_locale, set_locale


@dataclass
class GraphInfo:
    """图谱信息"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


class GraphBuilderService:
    """
    图谱构建服务
    负责调用graphiti API构建知识图谱
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or Config.LLM_API_KEY
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置")
        
        self.client = GraphitiClient(api_key=self.api_key, base_url=Config.GRAPHITI_URL)
        self.task_manager = TaskManager()
    
    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3
    ) -> str:
        """
        异步构建图谱
        
        Args:
            text: 输入文本
            ontology: 本体定义（来自接口1的输出）
            graph_name: 图谱名称
            chunk_size: 文本块大小
            chunk_overlap: 块重叠大小
            batch_size: 每批发送的块数量
            
        Returns:
            任务ID
        """
        # 创建任务
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            }
        )
        
        # Capture locale before spawning background thread
        current_locale = get_locale()

        # 在后台线程中执行构建
        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size, current_locale)
        )
        thread.daemon = True
        thread.start()
        
        return task_id
    
    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int,
        locale: str = 'zh'
    ):
        """图谱构建工作线程"""
        import sys
        print(f"[DEBUG _build_graph_worker] STARTING task_id={task_id}", file=sys.stderr)
        set_locale(locale)
        try:
            self.task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=5,
                message=t('progress.startBuildingGraph')
            )
            
            # 1. 创建图谱
            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(
                task_id,
                progress=10,
                message=t('progress.graphCreated', graphId=graph_id)
            )
            
            # 2. 设置本体
            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(
                task_id,
                progress=15,
                message=t('progress.ontologySet')
            )
            
            # 3. 文本分块
            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            total_chunks = len(chunks)
            self.task_manager.update_task(
                task_id,
                progress=20,
                message=t('progress.textSplit', count=total_chunks)
            )
            
            # 4. 分批发送数据
            print(f"[DEBUG _build_graph_worker] ABOUT TO CALL add_text_batches with graph_id={graph_id}, chunk_count={len(chunks)}, batch_size={batch_size}")
            episode_uuids = self.add_text_batches(
                graph_id, chunks, batch_size,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 0.4),  # 20-60%
                    message=msg
                )
            )
            
            # 5. 等待Zep处理完成
            self.task_manager.update_task(
                task_id,
                progress=60,
                message=t('progress.waitingZepProcess')
            )
            
            print(f"[DEBUG _build_graph_worker] total_sent from add_text_batches = {episode_uuids}")

            self._wait_for_episodes(
                graph_id,
                episode_uuids,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=60 + int(prog * 0.3),  # 60-90%
                    message=msg
                )
            )
            
            print(f"[DEBUG] Episodes waited, fetching graph info...")
            
            # 6. 获取图谱信息
            self.task_manager.update_task(
                task_id,
                progress=90,
                message=t('progress.fetchingGraphInfo')
            )
            
            graph_info = self._get_graph_info(graph_id)
            
            print(f"[DEBUG] Graph info fetched: nodes={graph_info.node_count}, edges={graph_info.edge_count}")
            
            # 完成
            self.task_manager.complete_task(task_id, {
                "graph_id": graph_id,
                "graph_info": graph_info.to_dict(),
                "chunks_processed": total_chunks,
            })
            
        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.task_manager.fail_task(task_id, error_msg)
    
    def create_graph(self, name: str) -> str:
        """创建图谱（graphiti隐式创建，group_id即graph_id）"""
        graph_id = f"mirofish_{uuid.uuid4().hex[:16]}"
        return graph_id
    
    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]):
        """设置图谱本体（公开方法）— graphiti自动提取，无需显式本体定义"""
        self.client.set_ontology(graph_id, ontology)
    
    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None
    ) -> int:
        """分批添加文本到图谱，返回发送的 episode 总数"""
        total_sent = 0
        total_chunks = len(chunks)

        for i in range(0, total_chunks, batch_size):
            batch_chunks = chunks[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_chunks + batch_size - 1) // batch_size

            if progress_callback:
                progress = (i + len(batch_chunks)) / total_chunks
                progress_callback(
                    t('progress.sendingBatch', current=batch_num, total=total_batches, chunks=len(batch_chunks)),
                    progress
                )

            # 构建episode数据
            class EpData:
                def __init__(self, data, ep_type):
                    self.data = data
                    self.type = ep_type

            episodes = [
                EpData(data=chunk, ep_type="text")
                for chunk in batch_chunks
            ]

            # 发送到graphiti
            try:
                batch_count = self.client.add_batch(
                    graph_id=graph_id,
                    episodes=episodes
                )
                total_sent += batch_count if isinstance(batch_count, int) else len(batch_chunks)

                # 避免请求过快
                time.sleep(1)

            except Exception as e:
                if progress_callback:
                    progress_callback(t('progress.batchFailed', batch=batch_num, error=str(e)), 0)
                raise

        print(f"[DEBUG add_text_batches] RETURNING total_sent={total_sent}")
        return total_sent
    
    def _wait_for_episodes(
        self,
        graph_id: str,
        expected_count: int,
        progress_callback: Optional[Callable] = None,
        timeout: int = 600
    ):
        """Wait for graphiti to finish processing all queued episodes by polling episode count."""
        import sys
        print(f"[DEBUG _wait_for_episodes] graph_id={graph_id}, expected_count={expected_count}", file=sys.stderr)

        if expected_count <= 0:
            if progress_callback:
                progress_callback(t('progress.noEpisodesWait'), 1.0)
            return

        start_time = time.time()
        last_count = 0
        stable_checks = 0

        while time.time() - start_time < timeout:
            try:
                resp = requests.get(
                    f"{self.client.base_url}/episodes/{graph_id}",
                    params={"last_n": 1000},
                    headers=self.client._headers(),
                    timeout=self.client.timeout,
                )
                resp.raise_for_status()
                episodes = resp.json()
                current_count = len(episodes) if isinstance(episodes, list) else 0
            except Exception as e:
                print(f"[DEBUG _wait_for_episodes] poll error: {e}", file=sys.stderr)
                time.sleep(5)
                continue

            if progress_callback:
                elapsed = int(time.time() - start_time)
                prog = min(current_count / expected_count, 1.0) if expected_count > 0 else 1.0
                progress_callback(
                    f"Processing {current_count}/{expected_count} episodes ({elapsed}s)",
                    prog,
                )

            if current_count >= expected_count:
                # All episodes processed
                print(f"[DEBUG _wait_for_episodes] done: {current_count}/{expected_count}", file=sys.stderr)
                return

            # Detect stall: each episode takes ~10-15s (LLM extraction), so wait
            # at least 20 checks (~200s) of no change before assuming done.
            if current_count == last_count:
                stable_checks += 1
                if stable_checks >= 20 and current_count > 0:
                    print(f"[DEBUG _wait_for_episodes] stable at {current_count}/{expected_count} after {stable_checks} checks, proceeding", file=sys.stderr)
                    return
            else:
                stable_checks = 0
                last_count = current_count

            time.sleep(10)

        print(f"[DEBUG _wait_for_episodes] timeout after {timeout}s with {last_count}/{expected_count}", file=sys.stderr)
    
    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """获取图谱信息"""
        nodes = self.client.fetch_all_nodes(graph_id)
        edges = self.client.fetch_all_edges(graph_id)

        entity_types = set()
        for node in nodes:
            if hasattr(node, 'labels') and node.labels:
                for label in node.labels:
                    if label not in ["Entity", "Node"]:
                        entity_types.add(label)

        return GraphInfo(
            graph_id=graph_id,
            node_count=len(nodes),
            edge_count=len(edges),
            entity_types=list(entity_types)
        )
    
    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """
        获取完整图谱数据（包含详细信息）
        
        Args:
            graph_id: 图谱ID
            
        Returns:
            包含nodes和edges的字典，包括时间信息、属性等详细数据
        """
        nodes = self.client.fetch_all_nodes(graph_id)
        edges = self.client.fetch_all_edges(graph_id)

        # 创建节点映射用于获取节点名称
        node_map = {}
        for node in nodes:
            node_map[node.uuid_] = node.name or ""
        
        nodes_data = []
        for node in nodes:
            # 获取创建时间
            created_at = getattr(node, 'created_at', None)
            if created_at:
                created_at = str(created_at)
            
            nodes_data.append({
                "uuid": node.uuid_,
                "name": node.name,
                "labels": node.labels or [],
                "summary": node.summary or "",
                "attributes": node.attributes or {},
                "created_at": created_at,
            })
        
        edges_data = []
        for edge in edges:
            # 获取时间信息
            created_at = getattr(edge, 'created_at', None)
            valid_at = getattr(edge, 'valid_at', None)
            invalid_at = getattr(edge, 'invalid_at', None)
            expired_at = getattr(edge, 'expired_at', None)
            
            # 获取 episodes
            episodes = getattr(edge, 'episodes', None) or getattr(edge, 'episode_ids', None)
            if episodes and not isinstance(episodes, list):
                episodes = [str(episodes)]
            elif episodes:
                episodes = [str(e) for e in episodes]
            
            # 获取 fact_type
            fact_type = getattr(edge, 'fact_type', None) or edge.name or ""
            
            edges_data.append({
                "uuid": edge.uuid_,
                "name": edge.name or "",
                "fact": edge.fact or "",
                "fact_type": fact_type,
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "source_node_name": node_map.get(edge.source_node_uuid, ""),
                "target_node_name": node_map.get(edge.target_node_uuid, ""),
                "attributes": edge.attributes or {},
                "created_at": str(created_at) if created_at else None,
                "valid_at": str(valid_at) if valid_at else None,
                "invalid_at": str(invalid_at) if invalid_at else None,
                "expired_at": str(expired_at) if expired_at else None,
                "episodes": episodes or [],
            })
        
        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }
    
    def delete_graph(self, graph_id: str):
        """删除图谱"""
        self.client.graph_delete(graph_id)

