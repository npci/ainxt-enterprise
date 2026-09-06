# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt AGENTIC PLATFORM — DOCKER EXECUTION SANDBOX
# ============================================================
# Enables Claude to safely execute code in isolated Docker containers
#
# Security Guarantees:
# - No host system access
# - No secret leakage
# - Full isolation
# - Auto cleanup
#
# Used by:
# - autonomous bug fixing
# - code execution
# - test execution
# - build execution
#
# Equivalent to CRED execution engine
# ============================================================

import docker
import uuid
import os
import tempfile
import shutil

from core.logger import logger
from agents.compliance_engine import compliance_engine


# ============================================================
# CONFIG
# ============================================================

DOCKER_IMAGE = "python:3.11-slim"
EXECUTION_TIMEOUT = 60


# ============================================================
# INIT CLIENT
# ============================================================

client = docker.from_env()

logger.info("Docker execution tool initialized")


# ============================================================
# CREATE SANDBOX DIRECTORY
# ============================================================

def create_sandbox():

    sandbox_id = str(uuid.uuid4())

    path = os.path.join(tempfile.gettempdir(), f"agent_sandbox_{sandbox_id}")

    os.makedirs(path, exist_ok=True)

    return sandbox_id, path


# ============================================================
# WRITE CODE TO SANDBOX
# ============================================================

def write_code(sandbox_path, code):

    file_path = os.path.join(sandbox_path, "main.py")

    with open(file_path, "w") as f:
        f.write(code)

    return file_path


# ============================================================
# EXECUTE CODE IN DOCKER
# ============================================================

def execute_in_docker(sandbox_path):

    container = None

    try:

        container = client.containers.run(

            image=DOCKER_IMAGE,

            command="python main.py",

            volumes={
                sandbox_path: {
                    "bind": "/sandbox",
                    "mode": "rw"
                }
            },

            working_dir="/sandbox",

            detach=True,

            mem_limit="512m",

            network_disabled=True,

            security_opt=["no-new-privileges"]

        )

        # Wait for container to finish
        exit_status = container.wait(timeout=EXECUTION_TIMEOUT)

        logs = container.logs()

        return logs.decode()

    except Exception as e:

        logger.error(f"Docker execution failed: {e}")

        return str(e)

    finally:

        if container:

            try:
                container.remove(force=True)
            except:
                pass

# ============================================================
# CLEANUP SANDBOX
# ============================================================

def cleanup_sandbox(path):

    try:
        shutil.rmtree(path)
    except:
        pass


# ============================================================
# MAIN EXECUTION FUNCTION
# ============================================================

def execute_code(code: str):

    logger.info("Executing code in sandbox")

    # ========================================================
    # COMPLIANCE CHECK
    # ========================================================

    validation = compliance_engine.validate_input(code)

    if validation["blocked"]:

        logger.critical("Blocked unsafe code execution")

        return {
            "success": False,
            "error": "Compliance violation"
        }

    # ========================================================
    # SANDBOX CREATE
    # ========================================================

    sandbox_id, sandbox_path = create_sandbox()

    try:

        write_code(sandbox_path, code)

        output = execute_in_docker(sandbox_path)

        return {
            "success": True,
            "output": output
        }

    finally:

        cleanup_sandbox(sandbox_path)


# ============================================================
# TOOL ENTRY POINT
# ============================================================

def docker_execution_tool(state):

    logger.info("Docker execution tool invoked")

    code = state.question

    result = execute_code(code)

    state.execution_result = result

    return state