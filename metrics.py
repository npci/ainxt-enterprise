# SPDX-License-Identifier: Apache-2.0
# ============================================================
# IMPORTS
# ============================================================

from collections import defaultdict
import threading
import json
from core.config import RDB_CACHE
from core.kv import get_kv
from core.logger import logger
from prometheus_client import generate_latest, CollectorRegistry, Gauge, Histogram, Counter


# ============================================================
# KV INIT (DB=0, metrics cache)
# Backend selected via REDIS_CLIENT_CONFIG_DB0.
# ============================================================

redis_client = get_kv(RDB_CACHE, decode_responses=True)

METRICS_KEY = "ai_metrics"


# ============================================================
# PROMETHEUS METRICS
# ============================================================

registry = CollectorRegistry()

task_completions = Counter(
    "task_completions_total",
    "Total number of task completions",
    ["agent_name", "pipeline_stage"],
    registry=registry
)

task_failures = Counter(
    "task_failures_total",
    "Total number of task failures",
    ["agent_name", "pipeline_stage"],
    registry=registry
)

task_retries = Counter(
    "task_retries_total",
    "Total number of task retries",
    ["agent_name", "pipeline_stage"],
    registry=registry
)

agent_execution_duration = Histogram(
    "agent_execution_duration_seconds",
    "Histogram of agent execution duration",
    ["agent_name"],
    registry=registry
)

llm_response_latency = Histogram(
    "llm_response_latency_seconds",
    "Histogram of LLM response latency",
    ["model"],
    registry=registry
)

active_agents = Gauge(
    "active_agents",
    "Current number of active agents",
    registry=registry
)

queue_depth = Gauge(
    "queue_depth",
    "Current depth of the queue",
    registry=registry
)

pipeline_health = Gauge(
    "pipeline_health",
    "Health status of the pipeline",
    registry=registry
)

# ── Rate limiting / security counters ─────────────────────────────────────────
rate_limit_exceeded_total = Counter(
    "rate_limit_exceeded_total",
    "Total number of rate-limit rejections (429) by endpoint prefix and scope",
    ["prefix", "scope"],
    registry=registry,
)


# ============================================================
# METRICS CLASS (THREAD SAFE + PERSISTENT)
# ============================================================

class Metrics:

    def __init__(self):

        self.lock = threading.Lock()

        self.total_queries = 0
        self.local_queries = 0
        self.local_llm_queries = 0
        self.draft_queries = 0

        self.repo_usage = defaultdict(int)

        self.total_retrieval_score = 0.0
        self.total_latency = 0.0

        self._load()


    # ========================================================
    # LOAD FROM REDIS
    # ========================================================

    def _load(self):

        try:

            data = redis_client.get(METRICS_KEY)

            if not data:
                return

            obj = json.loads(data)

            self.total_queries = obj.get("total_queries", 0)
            self.local_queries = obj.get("local_queries", 0)
            self.local_llm_queries = obj.get("local_llm_queries", 0)
            self.draft_queries = obj.get("draft_queries", 0)

            self.repo_usage.update(
                obj.get("repo_usage", {})
            )

            self.total_retrieval_score = obj.get(
                "total_retrieval_score", 0
            )

            self.total_latency = obj.get(
                "total_latency", 0
            )

            logger.info("Metrics loaded from Redis")

        except Exception as e:

            logger.error(f"Metrics load failed: {e}")


    # ========================================================
    # SAVE TO REDIS
    # ========================================================

    def _save(self):

        try:

            obj = {

                "total_queries": self.total_queries,

                "local_queries": self.local_queries,

                "local_llm_queries": self.local_llm_queries,

                "draft_queries": self.draft_queries,

                "repo_usage": dict(self.repo_usage),

                "total_retrieval_score": self.total_retrieval_score,

                "total_latency": self.total_latency

            }

            redis_client.set(
                METRICS_KEY,
                json.dumps(obj)
            )

        except Exception as e:

            logger.error(f"Metrics save failed: {e}")


    # ========================================================
    # RECORD METRIC
    # ========================================================

    def record(
        self,
        execution_scope: str,
        retrieval_strength: float,
        latency: float = 0.0
    ):

        try:

            with self.lock:

                self.total_queries += 1

                self.total_retrieval_score += retrieval_strength

                self.total_latency += latency


                if execution_scope in ("local_llm", "neuron"):

                    self.local_llm_queries += 1


                elif execution_scope == "draft":

                    self.draft_queries += 1


                else:

                    self.local_queries += 1

                    if execution_scope:
                        self.repo_usage[execution_scope] += 1


                self._save()


        except Exception as e:

            logger.error(f"Metrics record failed: {e}")


    # ========================================================
    # SUMMARY
    # ========================================================

    def summary(self):

        try:

            with self.lock:

                if self.total_queries == 0:

                    return {

                        "total_queries": 0,

                        "local_queries": 0,

                        "local_llm_queries": 0,

                        "draft_queries": 0,

                        "escalation_rate": 0,

                        "average_retrieval_score": 0,

                        "average_latency_ms": 0,

                        "repo_usage": {}

                    }


                return {

                    "total_queries": self.total_queries,

                    "local_queries": self.local_queries,

                    "local_llm_queries": self.local_llm_queries,

                    "draft_queries": self.draft_queries,

                    "escalation_rate": round(
                        self.local_llm_queries / self.total_queries,
                        4
                    ),

                    "average_retrieval_score": round(
                        self.total_retrieval_score / self.total_queries,
                        4
                    ),

                    "average_latency_ms": round(
                        (self.total_latency / self.total_queries) * 1000,
                        2
                    ),

                    "repo_usage": dict(self.repo_usage)

                }


        except Exception as e:

            logger.error(f"Metrics summary failed: {e}")

            return {}


    # ========================================================
    # PROMETHEUS RECORDING METHODS
    # ========================================================

    def record_task_completion(self, agent_name: str, pipeline_stage: str):
        task_completions.labels(agent_name=agent_name, pipeline_stage=pipeline_stage).inc()

    def record_task_failure(self, agent_name: str, pipeline_stage: str):
        task_failures.labels(agent_name=agent_name, pipeline_stage=pipeline_stage).inc()

    def record_task_retry(self, agent_name: str, pipeline_stage: str):
        task_retries.labels(agent_name=agent_name, pipeline_stage=pipeline_stage).inc()

    def observe_agent_execution_duration(self, agent_name: str, duration_seconds: float):
        agent_execution_duration.labels(agent_name=agent_name).observe(duration_seconds)

    def observe_llm_response_latency(self, model: str, latency_seconds: float):
        llm_response_latency.labels(model=model).observe(latency_seconds)

    def set_active_agents(self, count: int):
        active_agents.set(count)

    def set_queue_depth(self, depth: int):
        queue_depth.set(depth)

    def set_pipeline_health(self, healthy: bool):
        pipeline_health.set(1 if healthy else 0)

    # ========================================================
    # METRICS ENDPOINT
    # ========================================================

    def get_prometheus_metrics(self):
        return generate_latest()


# ============================================================
# GLOBAL INSTANCE
# ============================================================

metrics = Metrics()

logger.info("Metrics initialized successfully")