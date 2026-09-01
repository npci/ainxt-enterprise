#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Load Test — Locust script.

Usage:
    pip install locust
    locust -f scripts/load_test.py --host http://localhost:8000 --users 50 --spawn-rate 5

Simulates:
  - 70% /ask (chat) requests
  - 20% /agents list/get
  - 10% /index/health checks
"""

import json
import random
from locust import HttpUser, task, between

SAMPLE_QUESTIONS = [
    "How does ISOMsg work in the payment flow?",
    "What is the SLA for UPI transactions?",
    "Show me the architecture of the settlement service",
    "How do I fix a NullPointerException in PaymentProcessor?",
    "What are the PCI DSS requirements for card storage?",
    "Explain the retry logic in the authorization service",
    "How does the circuit breaker work in the API gateway?",
    "What GitLab pipelines are configured for ainxt/payments?",
    "Create a Jira ticket for the latency spike in prod",
    "What is the on-call runbook for P1 incidents?",
]

JWT_TOKEN = "test-token-replace-with-real"  # replace before running


class AiNxtUser(HttpUser):
    wait_time = between(1, 5)

    def on_start(self):
        self.headers = {
            "Authorization": f"Bearer {JWT_TOKEN}",
            "Content-Type": "application/json",
        }

    @task(7)
    def ask_question(self):
        question = random.choice(SAMPLE_QUESTIONS)
        self.client.post(
            "/ainxt/v1/api/ask",
            json={"question": question, "stream": False},
            headers=self.headers,
            name="/ask",
            catch_response=True,
        )

    @task(2)
    def list_agents(self):
        self.client.get(
            "/ainxt/v1/api/agents",
            headers=self.headers,
            name="/agents",
        )

    @task(1)
    def index_health(self):
        self.client.get(
            "/ainxt/v1/api/index/health",
            headers=self.headers,
            name="/index/health",
        )
