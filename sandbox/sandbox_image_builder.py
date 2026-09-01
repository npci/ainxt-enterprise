# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt SANDBOX IMAGE BUILDER
# Builds per-repo Docker images during codebase indexing.
#
# Each image contains:
#   - Language toolchain (javac, node, go, python, etc.)
#   - All project dependencies pre-downloaded at BUILD time
#     (Maven jars, node_modules, go modules, pip packages, etc.)
#   - /sandbox directory (empty — generated files mounted here at SDLC runtime)
#
# Network policy:
#   - ENABLED during docker build   (to download deps from Maven Central / npm / etc.)
#   - DISABLED at container runtime (--network none enforced by docker_executor)
#
# Usage:
#   builder = SandboxImageBuilder()
#   tag = builder.build(repo_name="payment-service", repo_path="/tmp/repos/payment-service")
#   # tag == "ainxt-sandbox/payment-service:latest"
# ============================================================

import base64
import io
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

try:
    import docker
    from docker.errors import ImageNotFound
    _DOCKER_SDK = True
except ImportError:
    _DOCKER_SDK = False

from core.logger import logger
from core.config import SANDBOX_PREFIX as _sandbox_prefix


# Build timeout: 20 minutes (Maven cold starts can be slow)
BUILD_TIMEOUT = 1200


# ============================================================
# REGISTRY CONFIGURATION
# ============================================================
# All registry endpoints are configurable via environment variables.
# Defaults target public internet registries (fine for dev).
# In production (AiNxt air-gapped network), set all SANDBOX_* vars
# to internal Nexus / Artifactory endpoints.
#
# Environment variables:
#   SANDBOX_DOCKER_REGISTRY      Docker image pull-through cache
#                                e.g. 
#                                Rewrites every FROM line — the Nexus group
#                                is expected to proxy Docker Hub, MCR, GHCR, etc.
#   SANDBOX_MAVEN_REPO_URL       Maven mirror (mirrors "central" + "*")
#                                e.g. https:///repository/maven-public/
#   SANDBOX_NPM_REGISTRY_URL     npm / yarn / pnpm registry
#                                e.g. https:///repository/npm-group/
#   SANDBOX_GO_PROXY             GOPROXY value
#                                e.g. https:///repository/go-proxy/,direct
#   SANDBOX_PIP_INDEX_URL        pip index URL
#                                e.g. https:///repository/pypi/simple/
#   SANDBOX_GEM_SOURCE_URL       RubyGems source URL
#                                e.g. https:///repository/rubygems/
#   SANDBOX_NUGET_SOURCE_URL     NuGet feed URL
#                                e.g. https:///repository/nuget/index.json
#   SANDBOX_CARGO_REGISTRY_URL   Cargo (Rust) registry URL (optional)
#                                e.g. https:///repository/cargo/
#   SANDBOX_COMPOSER_REPO_URL    Composer/Packagist mirror URL (optional)
#                                e.g. https:///repository/packagist/
# ============================================================

_PUBLIC_REGISTRY_DEFAULTS = {
    "maven_repo":     "https://repo1.maven.org/maven2/",
    "npm_registry":   "https://registry.npmjs.org/",
    "go_proxy":       "https://proxy.golang.org,direct",
    "pip_index":      "https://pypi.org/simple/",
    "gem_source":     "https://rubygems.org",
    "nuget_source":   "https://api.nuget.org/v3/index.json",
    "cargo_registry": "",
    "composer_repo":  "",
}


class RegistryConfig:
    """
    Holds all configurable registry endpoints for a sandbox build.
    Read once at module import — changing env vars at runtime has no effect.
    """
    def __init__(self) -> None:
        self.docker_registry = os.environ.get("SANDBOX_DOCKER_REGISTRY", "").rstrip("/")
        self.maven_repo      = os.environ.get("SANDBOX_MAVEN_REPO_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["maven_repo"])
        self.npm_registry    = os.environ.get("SANDBOX_NPM_REGISTRY_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["npm_registry"])
        self.go_proxy        = os.environ.get("SANDBOX_GO_PROXY",
                                              _PUBLIC_REGISTRY_DEFAULTS["go_proxy"])
        self.pip_index       = os.environ.get("SANDBOX_PIP_INDEX_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["pip_index"])
        self.gem_source      = os.environ.get("SANDBOX_GEM_SOURCE_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["gem_source"])
        self.nuget_source    = os.environ.get("SANDBOX_NUGET_SOURCE_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["nuget_source"])
        self.cargo_registry  = os.environ.get("SANDBOX_CARGO_REGISTRY_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["cargo_registry"])
        self.composer_repo   = os.environ.get("SANDBOX_COMPOSER_REPO_URL",
                                              _PUBLIC_REGISTRY_DEFAULTS["composer_repo"])
        self.maven_repo_user = os.environ.get("SANDBOX_MAVEN_REPO_USER", "")
        self.maven_repo_pwd  = os.environ.get("SANDBOX_MAVEN_REPO_PWD", "")
        if self.docker_registry:
            logger.info(f"SandboxImageBuilder: Docker registry mirror → {self.docker_registry}")

    def is_public(self, key: str) -> bool:
        """Return True if this setting is still the public-internet default."""
        return getattr(self, key, "") == _PUBLIC_REGISTRY_DEFAULTS.get(key, "")


# Module-level singleton — read once at startup
_REGISTRY_CFG = RegistryConfig()


# ── Registry helper functions ─────────────────────────────────

def _prefix_image(image: str, registry: str) -> str:
    """
    Rewrite a Docker image reference to use the AiNxt internal registry mirror.

    The Nexus/Artifactory group at `registry` is assumed to proxy all upstream
    registries (Docker Hub, MCR, GHCR, etc.) in a single endpoint.

    Strip the original registry host (if any) and prepend the AiNxt mirror:
        "node:20-alpine"                   → "/node:20-alpine"
        "mcr.microsoft.com/dotnet/sdk:8.0" → "/dotnet/sdk:8.0"
        "virtuslab/scala-cli:latest"        → "/virtuslab/scala-cli:latest"
    """
    if not registry:
        return image
    parts = image.split("/")
    # First segment with a '.' (or 'localhost') → it's a registry host, strip it
    if len(parts) > 1 and ("." in parts[0] or parts[0] == "localhost"):
        image_path = "/".join(parts[1:])
    else:
        image_path = image
    return f"{registry}/{image_path}"


def _rewrite_from_lines(dockerfile: str, registry: str) -> str:
    """Rewrite every FROM <image> line to use the internal Docker registry mirror."""
    if not registry:
        return dockerfile
    out = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FROM "):
            tokens = stripped.split()
            # FROM <image> [AS <alias>]
            image  = _prefix_image(tokens[1], registry)
            rest   = " ".join(tokens[2:])   # "AS builder" or ""
            line   = f"FROM {image}" + (f" {rest}" if rest else "")
        out.append(line)
    return "\n".join(out) + "\n"


def _urlhost(url: str) -> str:
    """Extract hostname from a URL (used for pip --trusted-host)."""
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def _maven_settings_xml(cfg: RegistryConfig) -> str:
    """
    Generate a Maven settings.xml that redirects ALL repository traffic
    (central + plugins + everything else) through the AiNxt internal mirror.
    <mirrorOf>*</mirrorOf> catches every repository ID.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">\n'
        '  <mirrors>\n'
        '    <mirror>\n'
        '      <id>ainxt-mirror</id>\n'
        '      <name>AiNxt Internal Maven Repository</name>\n'
        f'      <url>{cfg.maven_repo}</url>\n'
        '      <mirrorOf>*</mirrorOf>\n'
        '    </mirror>\n'
        '  </mirrors>\n'
        '<servers>\n'
        '<server>\n'
        '   <id>ainxt-repo</id>\n'
        f'   <username>{cfg.maven_repo_user}</username>\n'
        f'   <password>{cfg.maven_repo_pwd}</password>\n'
        '   </server>\n'
        '</servers>\n'
        '</settings>\n'
    )


# ── Per-build-system registry injection ───────────────────────

def _apply_maven_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Inject Maven settings.xml that mirrors Central to internal repo."""
    if cfg.is_public("maven_repo"):
        return dockerfile
    settings_b64 = base64.b64encode(
        _maven_settings_xml(cfg).encode()
    ).decode()
    inject_line = f"RUN echo '{settings_b64}' | base64 -d > /root/maven-settings.xml"
    flag        = "-s /root/maven-settings.xml"
    lines, injected = [], False
    for line in dockerfile.splitlines():
        if not injected and "RUN mvn" in line:
            lines.append(inject_line)
            injected = True
        if "mvn " in line and flag not in line:
            line = line.replace("mvn ", f"mvn {flag} ", 1)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _apply_gradle_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """
    Inject a Gradle init script that redirects all repository declarations
    (mavenCentral(), jcenter(), google(), etc.) to the AiNxt internal Maven mirror.
    The init script is written to /root/.gradle/init.d/ so it applies globally
    without touching the project's own build files.
    """
    if cfg.is_public("maven_repo"):
        return dockerfile
    # Use \\n so printf interprets escape sequences inside the shell heredoc
    init_groovy = (
            "allprojects {\\n"
            "  buildscript { repositories { maven { url = uri('" + cfg.maven_repo + "') } } }\\n"
                                                                                    "  repositories { maven { url = uri('" + cfg.maven_repo + "') } }\\n"
                                                                                                                                              "}\\n"
    )
    init_line = (
        "RUN mkdir -p /root/.gradle/init.d && "
        f"printf '{init_groovy}' > /root/.gradle/init.d/ainxt-repos.gradle"
    )
    lines, injected = [], False
    for line in dockerfile.splitlines():
        if not injected and "WORKDIR /workspace" in line:
            lines.append(line)
            lines.append(init_line)
            injected = True
            continue
        lines.append(line)
    if not injected:
        lines.insert(2, init_line)
    return "\n".join(lines) + "\n"


def _apply_npm_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """
    Write /root/.npmrc with the internal registry.
    Works for npm, yarn (classic), and pnpm which all honour .npmrc.
    """
    if cfg.is_public("npm_registry"):
        return dockerfile
    npmrc_line = f"RUN echo 'registry={cfg.npm_registry}' > /root/.npmrc"
    lines, injected = [], False
    for line in dockerfile.splitlines():
        if not injected and "WORKDIR /workspace" in line:
            lines.append(line)
            lines.append(npmrc_line)
            injected = True
            continue
        lines.append(line)
    if not injected:
        lines.insert(2, npmrc_line)
    return "\n".join(lines) + "\n"


def _apply_go_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Set GOPROXY + GONOSUMCHECK ENV vars directly after the FROM line."""
    if cfg.is_public("go_proxy"):
        return dockerfile
    env_lines = [
        f"ENV GOPROXY={cfg.go_proxy}",
        "ENV GONOSUMCHECK=*",
        "ENV GOFLAGS=-mod=mod",
    ]
    lines, injected = [], False
    for line in dockerfile.splitlines():
        lines.append(line)
        if not injected and line.strip().upper().startswith("FROM "):
            lines.extend(env_lines)
            injected = True
    return "\n".join(lines) + "\n"


def _apply_pip_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Write /root/.pip/pip.conf so ALL pip invocations use the internal index."""
    if cfg.is_public("pip_index"):
        return dockerfile
    trusted = _urlhost(cfg.pip_index)
    # Use \\n so printf renders newlines
    conf = f"[global]\\nindex-url = {cfg.pip_index}\\ntrusted-host = {trusted}\\n"
    pip_conf_line = f"RUN mkdir -p /root/.pip && printf '{conf}' > /root/.pip/pip.conf"
    lines, injected = [], False
    for line in dockerfile.splitlines():
        if not injected and "WORKDIR /workspace" in line:
            lines.append(line)
            lines.append(pip_conf_line)
            injected = True
            continue
        lines.append(line)
    if not injected:
        lines.insert(2, pip_conf_line)
    return "\n".join(lines) + "\n"


def _apply_gem_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Write /root/.gemrc that replaces the default RubyGems source."""
    if cfg.is_public("gem_source"):
        return dockerfile
    gemrc_line = f"RUN printf ':sources:\\n  - {cfg.gem_source}\\n' > /root/.gemrc"
    lines, injected = [], False
    for line in dockerfile.splitlines():
        if not injected and "WORKDIR /workspace" in line:
            lines.append(line)
            lines.append(gemrc_line)
            injected = True
            continue
        lines.append(line)
    if not injected:
        lines.insert(2, gemrc_line)
    return "\n".join(lines) + "\n"


def _apply_nuget_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Add --source flag to every dotnet restore command."""
    if cfg.is_public("nuget_source"):
        return dockerfile
    lines = []
    for line in dockerfile.splitlines():
        if "dotnet restore" in line and "--source" not in line:
            line = line.replace("dotnet restore", f"dotnet restore --source {cfg.nuget_source}")
        lines.append(line)
    return "\n".join(lines) + "\n"


def _apply_cargo_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Write /root/.cargo/config.toml that replaces crates.io with the internal registry."""
    if not cfg.cargo_registry:
        return dockerfile
    cargo_toml = (
        "[source.crates-io]\\n"
        "replace-with = 'ainxt-crates'\\n\\n"
        "[source.ainxt-crates]\\n"
        f"registry = '{cfg.cargo_registry}'\\n"
    )
    cargo_line = (
        "RUN mkdir -p /root/.cargo && "
        f"printf '{cargo_toml}' > /root/.cargo/config.toml"
    )
    lines, injected = [], False
    for line in dockerfile.splitlines():
        if not injected and "WORKDIR /workspace" in line:
            lines.append(line)
            lines.append(cargo_line)
            injected = True
            continue
        lines.append(line)
    if not injected:
        lines.insert(2, cargo_line)
    return "\n".join(lines) + "\n"


def _apply_composer_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Add a Composer repository config step before composer install."""
    if not cfg.composer_repo:
        return dockerfile
    lines = []
    for line in dockerfile.splitlines():
        if "composer install" in line and "--no-scripts" in line:
            lines.append(
                f"RUN composer config repositories.ainxt composer {cfg.composer_repo} 2>&1 || true"
            )
        lines.append(line)
    return "\n".join(lines) + "\n"


def _apply_scala_registry(dockerfile: str, cfg: RegistryConfig) -> str:
    """Add --repository flag to scala-cli compile commands."""
    if cfg.is_public("maven_repo"):
        return dockerfile
    lines = []
    for line in dockerfile.splitlines():
        if "scala-cli compile" in line and "--repository" not in line:
            line = line.replace(
                "scala-cli compile",
                f"scala-cli compile --repository {cfg.maven_repo}"
            )
        lines.append(line)
    return "\n".join(lines) + "\n"


def _apply_registry_cfg(dockerfile: str, btype: str, cfg: RegistryConfig) -> str:
    """
    Apply all AiNxt internal registry settings to a Dockerfile string.

    Step 1 — always: rewrite FROM lines to use the Docker registry mirror.
    Step 2 — per build system: inject package manager repository configuration.

    All modifications are additive (write config files / inject ENV / add flags).
    The project's own build files are never modified.
    """
    result = _rewrite_from_lines(dockerfile, cfg.docker_registry)

    _PER_SYSTEM: Dict[str, callable] = {
        "maven":          _apply_maven_registry,
        "gradle":         _apply_gradle_registry,
        "gradle-kotlin":  _apply_gradle_registry,
        "npm":            _apply_npm_registry,
        "yarn":           _apply_npm_registry,
        "pnpm":           _apply_npm_registry,
        "go":             _apply_go_registry,
        "pip":            _apply_pip_registry,
        "poetry":         _apply_pip_registry,
        "gemfile":        _apply_gem_registry,
        "dotnet":         _apply_nuget_registry,
        "cargo":          _apply_cargo_registry,
        "composer":       _apply_composer_registry,
        "scala":          _apply_scala_registry,
    }
    apply_fn = _PER_SYSTEM.get(btype)
    if apply_fn:
        result = apply_fn(result, cfg)

    return result

# ── Image name sanitizer ──────────────────────────────────────
def _tag_safe(name: str) -> str:
    """Convert a repo name to a valid Docker image tag component (lowercase, alphanumeric + hyphens)."""
    safe = re.sub(r"[^a-z0-9\-]", "-", name.lower())
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe or "repo"


# ── Dockerfile templates per build system ─────────────────────
# These run with NETWORK ENABLED during docker build.
# Key principle: use `|| true` on compile steps so image builds even if
# existing project source has errors — we only need the deps layer.

_DOCKERFILE_MAVEN = """\
FROM maven:3-eclipse-temurin-21
WORKDIR /workspace
COPY pom.xml .
RUN mvn dependency:go-offline -q --batch-mode 2>&1 || true
RUN mvn dependency:copy-dependencies -DincludeScope=test \
    -DoutputDirectory=target/dependency -q --batch-mode 2>&1 || true
COPY src ./src
RUN mvn compile -q --batch-mode -DskipTests 2>&1 || true
RUN mvn test-compile -q --batch-mode 2>&1 || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_GRADLE = """\
FROM eclipse-temurin:21-jdk-alpine
RUN apk add --no-cache bash curl findutils
WORKDIR /workspace
COPY build.gradle* settings.gradle* ./
COPY gradle ./gradle
COPY gradlew .
RUN chmod +x gradlew && ./gradlew dependencies --quiet --no-daemon 2>&1 || true
COPY src ./src
RUN ./gradlew compileJava --quiet --no-daemon -x test 2>&1 || true
RUN ./gradlew compileTestJava --quiet --no-daemon 2>&1 || true
RUN mkdir -p /workspace/target/dependency && \
    find /root/.gradle/caches -name '*.jar' \
      -not -name '*-sources.jar' -not -name '*-javadoc.jar' \
      2>/dev/null | xargs -I{} cp -n "{}" /workspace/target/dependency/ 2>/dev/null || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_NPM = """\
FROM node:20-alpine
WORKDIR /workspace
COPY package.json ./
COPY package-lock.json* ./
RUN npm ci --prefer-offline --no-audit --silent 2>&1 || \
    npm install --no-audit --silent 2>&1 || true
RUN mkdir -p /sandbox
RUN cat > /usr/local/bin/check_jsx.js << 'JSEOF'
const f=process.argv[2],src=require('fs').readFileSync(f,'utf8');
try{const p=require('/workspace/node_modules/@babel/parser');
p.parse(src,{sourceType:'module',plugins:['jsx','typescript','decorators-legacy','classProperties']});
console.log('OK: '+f);}
catch(e){if(e.code==='MODULE_NOT_FOUND'){
  try{const ts=require('/workspace/node_modules/typescript');
  ts.transpileModule(src,{compilerOptions:{jsx:1,target:99,experimentalDecorators:true}});
  console.log('OK(ts): '+f);}
  catch(t){if(t.code==='MODULE_NOT_FOUND')console.log('OK(unchecked): '+f);
  else{console.error(t.message);process.exit(1);}}}
else{console.error(e.message);process.exit(1);}}
JSEOF
RUN cat > /usr/local/bin/check_vue.js << 'VUEOF'
const f=process.argv[2],src=require('fs').readFileSync(f,'utf8');
try{const {parse}=require('/workspace/node_modules/@vue/compiler-sfc');
const {errors}=parse(src);
if(errors.length){errors.forEach(e=>console.error(e.message));process.exit(1);}
console.log('OK: '+f);}
catch(e){if(e.code==='MODULE_NOT_FOUND'){
  const m=src.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if(m)try{new Function(m[1]);console.log('OK(basic)');}
  catch(e2){console.error(e2.message);process.exit(1);}
  else console.log('OK(no-script)');}
else{console.error(e.message);process.exit(1);}}
VUEOF
"""

_DOCKERFILE_YARN = """\
FROM node:20-alpine
WORKDIR /workspace
COPY package.json yarn.lock* ./
RUN yarn install --frozen-lockfile --silent 2>&1 || \
    yarn install --silent 2>&1 || true
RUN mkdir -p /sandbox
RUN cat > /usr/local/bin/check_jsx.js << 'JSEOF'
const f=process.argv[2],src=require('fs').readFileSync(f,'utf8');
try{const p=require('/workspace/node_modules/@babel/parser');
p.parse(src,{sourceType:'module',plugins:['jsx','typescript','decorators-legacy','classProperties']});
console.log('OK: '+f);}
catch(e){if(e.code==='MODULE_NOT_FOUND'){
  try{const ts=require('/workspace/node_modules/typescript');
  ts.transpileModule(src,{compilerOptions:{jsx:1,target:99,experimentalDecorators:true}});
  console.log('OK(ts): '+f);}
  catch(t){if(t.code==='MODULE_NOT_FOUND')console.log('OK(unchecked): '+f);
  else{console.error(t.message);process.exit(1);}}}
else{console.error(e.message);process.exit(1);}}
JSEOF
RUN cat > /usr/local/bin/check_vue.js << 'VUEOF'
const f=process.argv[2],src=require('fs').readFileSync(f,'utf8');
try{const {parse}=require('/workspace/node_modules/@vue/compiler-sfc');
const {errors}=parse(src);
if(errors.length){errors.forEach(e=>console.error(e.message));process.exit(1);}
console.log('OK: '+f);}
catch(e){if(e.code==='MODULE_NOT_FOUND'){
  const m=src.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if(m)try{new Function(m[1]);console.log('OK(basic)');}
  catch(e2){console.error(e2.message);process.exit(1);}
  else console.log('OK(no-script)');}
else{console.error(e.message);process.exit(1);}}
VUEOF
"""

_DOCKERFILE_PNPM = """\
FROM node:20-alpine
RUN npm install -g pnpm --quiet 2>&1 || true
WORKDIR /workspace
COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile 2>&1 || pnpm install 2>&1 || true
RUN mkdir -p /sandbox
RUN cat > /usr/local/bin/check_jsx.js << 'JSEOF'
const f=process.argv[2],src=require('fs').readFileSync(f,'utf8');
try{const p=require('/workspace/node_modules/@babel/parser');
p.parse(src,{sourceType:'module',plugins:['jsx','typescript','decorators-legacy','classProperties']});
console.log('OK: '+f);}
catch(e){if(e.code==='MODULE_NOT_FOUND'){
  try{const ts=require('/workspace/node_modules/typescript');
  ts.transpileModule(src,{compilerOptions:{jsx:1,target:99,experimentalDecorators:true}});
  console.log('OK(ts): '+f);}
  catch(t){if(t.code==='MODULE_NOT_FOUND')console.log('OK(unchecked): '+f);
  else{console.error(t.message);process.exit(1);}}}
else{console.error(e.message);process.exit(1);}}
JSEOF
RUN cat > /usr/local/bin/check_vue.js << 'VUEOF'
const f=process.argv[2],src=require('fs').readFileSync(f,'utf8');
try{const {parse}=require('/workspace/node_modules/@vue/compiler-sfc');
const {errors}=parse(src);
if(errors.length){errors.forEach(e=>console.error(e.message));process.exit(1);}
console.log('OK: '+f);}
catch(e){if(e.code==='MODULE_NOT_FOUND'){
  const m=src.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if(m)try{new Function(m[1]);console.log('OK(basic)');}
  catch(e2){console.error(e2.message);process.exit(1);}
  else console.log('OK(no-script)');}
else{console.error(e.message);process.exit(1);}}
VUEOF
"""

_DOCKERFILE_GO = """\
FROM golang:1.21-alpine
WORKDIR /workspace
COPY go.mod .
COPY go.sum* ./
RUN go mod download 2>&1 || true
COPY . .
RUN go build ./... 2>&1 || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_PIP = """\
FROM python:3.11-slim
WORKDIR /workspace
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements*.txt ./
RUN for f in requirements.txt requirements-dev.txt requirements-base.txt \
              requirements-test.txt dependencies.txt; do \
      [ -f "$f" ] && pip install -r "$f" --no-cache-dir -q 2>&1 || true; \
    done
RUN pip install pytest pytest-cov --no-cache-dir -q
RUN mkdir -p /sandbox
"""

_DOCKERFILE_POETRY = """\
FROM python:3.11-slim
WORKDIR /workspace
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install poetry --quiet 2>&1 || true
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --no-root --quiet 2>&1 || true
RUN pip install pytest pytest-cov --no-cache-dir -q
RUN mkdir -p /sandbox
"""

_DOCKERFILE_CARGO = """\
FROM rust:1.77-alpine
RUN apk add --no-cache musl-dev
WORKDIR /workspace
COPY Cargo.toml .
COPY Cargo.lock* ./
RUN mkdir -p src && echo "fn main() {}" > src/main.rs && cargo fetch 2>&1 || true
COPY src ./src
RUN cargo build --release 2>&1 || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_GEMFILE = """\
FROM ruby:3.3-alpine
WORKDIR /workspace
COPY Gemfile Gemfile.lock* ./
RUN bundle install --quiet 2>&1 || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_COMPOSER = """\
FROM php:8.3-cli-alpine
RUN apk add --no-cache curl unzip && \
    curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer 2>&1 || true
WORKDIR /workspace
COPY composer.json composer.lock* ./
RUN composer install --no-scripts --no-autoloader --quiet 2>&1 || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_DOTNET = """\
FROM mcr.microsoft.com/dotnet/sdk:8.0-alpine
WORKDIR /workspace
COPY . .
RUN dotnet restore -v q 2>&1 || true
RUN dotnet build -c Release -v q 2>&1 || true
RUN mkdir -p /sandbox
"""

_DOCKERFILE_SCALA = """\
FROM virtuslab/scala-cli:latest
WORKDIR /workspace
COPY . .
RUN scala-cli compile . 2>&1 || true
RUN mkdir -p /sandbox
"""

# Kotlin with Gradle: eclipse-temurin + apk install of kotlinc so self-healing
# can call `kotlinc /sandbox/Main.kt` directly in addition to gradlew.
_DOCKERFILE_GRADLE_KOTLIN = """\
FROM eclipse-temurin:21-jdk-alpine
RUN apk add --no-cache bash curl kotlin findutils
WORKDIR /workspace
COPY build.gradle* settings.gradle* ./
COPY gradle ./gradle
COPY gradlew .
RUN chmod +x gradlew && ./gradlew dependencies --quiet --no-daemon 2>&1 || true
COPY src ./src
RUN ./gradlew compileKotlin --quiet --no-daemon -x test 2>&1 || true
RUN ./gradlew compileTestKotlin --quiet --no-daemon 2>&1 || true
RUN mkdir -p /workspace/target/dependency && \
    find /root/.gradle/caches -name '*.jar' \
      -not -name '*-sources.jar' -not -name '*-javadoc.jar' \
      2>/dev/null | xargs -I{} cp -n "{}" /workspace/target/dependency/ 2>/dev/null || true
RUN mkdir -p /sandbox
"""

# Swift Package Manager — downloads deps via `swift package resolve`.
# Note: swift:5.10 is Ubuntu 22.04 based (~1.5 GB). Pre-pull on prod server.
_DOCKERFILE_SPM = """\
FROM swift:5.10
WORKDIR /workspace
COPY Package.swift .
COPY Package.resolved* ./
RUN swift package resolve 2>&1 || true
COPY Sources ./Sources
COPY Tests ./Tests
RUN swift build 2>&1 || true
RUN mkdir -p /sandbox
"""

# Bare language image (no build system detected — just the toolchain + /sandbox)
_DOCKERFILE_BARE = """\
FROM {base_image}
RUN mkdir -p /sandbox
"""

# Build system → Dockerfile template
_SYSTEM_TO_DOCKERFILE: dict[str, str] = {
    "maven":          _DOCKERFILE_MAVEN,
    "gradle":         _DOCKERFILE_GRADLE,           # Java Gradle (build.gradle)
    "gradle-kotlin":  _DOCKERFILE_GRADLE_KOTLIN,    # Kotlin Gradle (build.gradle.kts)
    "npm":            _DOCKERFILE_NPM,
    "yarn":           _DOCKERFILE_YARN,
    "pnpm":           _DOCKERFILE_PNPM,
    "go":             _DOCKERFILE_GO,
    "pip":            _DOCKERFILE_PIP,
    "poetry":         _DOCKERFILE_POETRY,
    "cargo":          _DOCKERFILE_CARGO,
    "gemfile":        _DOCKERFILE_GEMFILE,
    "composer":       _DOCKERFILE_COMPOSER,
    "dotnet":         _DOCKERFILE_DOTNET,
    "scala":          _DOCKERFILE_SCALA,
    "spm":            _DOCKERFILE_SPM,              # Swift Package Manager
}

# Language → bare base image (used when no build system is detected)
_LANG_BASE_IMAGES: dict[str, str] = {
    "python":     "python:3.11-slim",
    "javascript": "node:20-alpine",
    "typescript": "node:20-alpine",
    "java":       "eclipse-temurin:21-jdk-alpine",
    "kotlin":     "eclipse-temurin:21-jdk-alpine",
    "go":         "golang:1.21-alpine",
    "golang":     "golang:1.21-alpine",
    "rust":       "rust:1.77-alpine",
    "ruby":       "ruby:3.3-alpine",
    "php":        "php:8.3-cli-alpine",
    "csharp":     "mcr.microsoft.com/dotnet/sdk:8.0-alpine",
    "scala":      "virtuslab/scala-cli:latest",
    "bash":       "bash:5-alpine",
    "shell":      "bash:5-alpine",
    "cpp":        "gcc:13-alpine",
    "c":          "gcc:13-alpine",
    "swift":      "swift:5.10",          # no slim variant — ubuntu 22.04 based
    "r":          "r-base:4.3.3",
    "elixir":     "elixir:1.16-alpine",
    "dart":       "dart:3.3-sdk",
    "lua":        "nickblah/lua:5.4-alpine",
    "groovy":     "eclipse-temurin:21-jdk-alpine",
    "perl":       "perl:5.38-slim",
    "powershell": "mcr.microsoft.com/powershell:alpine-3.18",
    "default":    "python:3.11-slim",
}

# ── Build system detection order ──────────────────────────────
# Checked in order — first match wins.
_DETECTION_CHECKS = [
    ("pom.xml",          {"type": "maven",          "lang": "java"}),
    ("build.gradle.kts", {"type": "gradle-kotlin",  "lang": "kotlin"}),  # Kotlin DSL → Kotlin image with kotlinc
    ("build.gradle",     {"type": "gradle",          "lang": "java"}),
    ("pnpm-lock.yaml",   {"type": "pnpm",            "lang": "javascript"}),
    ("yarn.lock",        {"type": "yarn",            "lang": "javascript"}),
    ("package.json",     {"type": "npm",             "lang": "javascript"}),
    ("go.mod",           {"type": "go",              "lang": "go"}),
    ("Cargo.toml",       {"type": "cargo",           "lang": "rust"}),
    ("Package.swift",    {"type": "spm",             "lang": "swift"}),    # Swift Package Manager
    ("pyproject.toml",   {"type": "poetry",          "lang": "python"}),
    ("requirements.txt", {"type": "pip",             "lang": "python"}),
    ("Gemfile",          {"type": "gemfile",         "lang": "ruby"}),
    ("composer.json",    {"type": "composer",        "lang": "php"}),
    ("build.sbt",        {"type": "scala",           "lang": "scala"}),
]

# Extension → language name (for dominant-language detection)
_EXT_TO_LANG: dict[str, str] = {
    ".java": "java",   ".kt": "kotlin",  ".scala": "scala",
    ".py":   "python", ".js": "javascript", ".ts": "typescript",
    ".go":   "go",     ".rs": "rust",    ".rb": "ruby",
    ".php":  "php",    ".cs": "csharp",  ".cpp": "cpp",
    ".c":    "c",      ".sh": "bash",    ".swift": "swift",
}


# ============================================================
# SANDBOX IMAGE BUILDER
# ============================================================

class SandboxImageBuilder:
    """
    Builds and manages per-repo Docker sandbox images for SDLC execution.

    Call flow (index time):
        index_worker._do_index() completes
        → SandboxImageBuilder.build(repo_name, repo_path)
        → detect build system (pom.xml / package.json / go.mod / etc.)
        → generate Dockerfile
        → docker build -f <dockerfile> -t ainxt-sandbox/{repo}:latest <repo_path>
        → store tag in repo_index_status.sandbox_image_tag

    Call flow (SDLC time):
        CodingStateMachine.__init__
        → resolve image tag from repo_index_status
        → docker_executor.execute_in_repo_image(code=..., image_tag=tag, command=...)
        → container runs with --network none (PCI safe)
    """

    def __init__(self):
        self._docker_client = None

    def _get_docker_client(self):
        if not _DOCKER_SDK:
            raise RuntimeError("docker-py not installed. Run: pip install docker")
        if self._docker_client is None:
            self._docker_client = docker.from_env()
            self._docker_client.ping()
        return self._docker_client

    # ── Public API ────────────────────────────────────────────

    def get_image_tag(self, repo_name: str) -> str:
        """Return the canonical tag for a repo's sandbox image."""
        return f"ainxt-sandbox/{_tag_safe(repo_name)}:latest"

    def image_exists(self, repo_name: str) -> bool:
        """Return True if the sandbox image for this repo is locally cached."""
        try:
            client = self._get_docker_client()
            client.images.get(self.get_image_tag(repo_name))
            return True
        except ImageNotFound:
            return False
        except Exception:
            return False

    def remove_image(self, repo_name: str) -> None:
        """Remove the sandbox image for this repo (called on re-index)."""
        try:
            client = self._get_docker_client()
            tag = self.get_image_tag(repo_name)
            client.images.remove(tag, force=True)
            logger.info(f"SandboxImageBuilder: removed {tag}")
        except Exception as e:
            logger.debug(f"SandboxImageBuilder: remove_image skipped ({e})")

    def build(self, repo_name: str, repo_path: str, force_rebuild: bool = False) -> tuple:
        """
        Build a repo-specific Docker sandbox image.

        Returns (image_tag, build_root) where:
          image_tag  — "ainxt-sandbox/{repo_name}:latest"
          build_root — relative path from repo_path to the build file directory,
                       e.g. "." for root, "payment-service" for a monorepo submodule.

        Network is ENABLED during build so deps can be downloaded.
        /sandbox is created as an empty directory — generated files
        are volume-mounted there at SDLC runtime (network disabled then).

        force_rebuild=True  → passes --no-cache (re-downloads all deps; use for drop_index=True)
        force_rebuild=False → uses Docker layer cache  (fast for unchanged pom.xml/package.json)
        """
        tag  = self.get_image_tag(repo_name)
        path = Path(repo_path)

        if not path.is_dir():
            raise RuntimeError(f"SandboxImageBuilder: repo_path does not exist: {repo_path}")

        build_system = self._detect_build_system(path)
        build_root   = build_system.get("build_root", ".")
        dockerfile   = self._generate_dockerfile(path, build_system)

        logger.info(
            f"SandboxImageBuilder: building {tag} "
            f"[{build_system['type']}/{build_system['lang']}] "
            f"build_root={build_root!r} from {repo_path} "
            f"force_rebuild={force_rebuild}"
        )
        start = time.time()
        self._docker_build(path, dockerfile, tag,
                           force_rebuild=force_rebuild, build_root=build_root)
        elapsed = time.time() - start
        logger.info(f"SandboxImageBuilder: {tag} built in {elapsed:.0f}s build_root={build_root!r}")
        return tag, build_root

    # ── Detection ─────────────────────────────────────────────

    def _detect_build_system(self, repo_path: Path) -> Dict:
        """
        Detect the primary build/package manager.
        Returns {"type": "maven", "lang": "java", "build_root": "."} etc.

        Detection order:
        1. Root-level build file (preferred — covers typical repos and multi-module roots)
        2. Shallowest nested build file (monorepos and projects in a subdirectory)
        3. Dominant file extension fallback
        """
        # .csproj → .NET (always recursive because csproj is rarely at the root)
        csproj_matches = sorted(repo_path.rglob("*.csproj"), key=lambda p: len(p.parts))
        if csproj_matches:
            build_root = str(csproj_matches[0].parent.relative_to(repo_path))
            return {"type": "dotnet", "lang": "csharp", "build_root": build_root or "."}

        # Root-first check — most repos have the build file at the top level
        for filename, info in _DETECTION_CHECKS:
            if (repo_path / filename).exists():
                return {**info, "build_root": "."}

        # Recursive search: take the shallowest match for each build file type
        for filename, info in _DETECTION_CHECKS:
            matches = sorted(repo_path.rglob(filename), key=lambda p: len(p.parts))
            if matches:
                build_root = str(matches[0].parent.relative_to(repo_path))
                logger.info(
                    f"SandboxImageBuilder: found nested build file "
                    f"{filename} at {build_root!r} — using as build root"
                )
                return {**info, "build_root": build_root or "."}

        # No known build file — fall back to dominant file extension
        lang = self._dominant_language(repo_path)
        return {"type": "none", "lang": lang, "build_root": "."}

    def _dominant_language(self, repo_path: Path) -> str:
        counts: dict[str, int] = {}
        for fpath in repo_path.rglob("*"):
            if fpath.is_file():
                lang = _EXT_TO_LANG.get(fpath.suffix.lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
        return max(counts, key=counts.get) if counts else "python"

    # ── Dockerfile generation ─────────────────────────────────

    def _generate_dockerfile(self, repo_path: Path, build_system: Dict) -> str:
        btype = build_system["type"]
        lang  = build_system.get("lang", "python")

        if btype in _SYSTEM_TO_DOCKERFILE:
            template = _SYSTEM_TO_DOCKERFILE[btype]
        else:
            # No recognised build system → bare image with /sandbox only
            base     = _LANG_BASE_IMAGES.get(lang, _LANG_BASE_IMAGES["default"])
            template = _DOCKERFILE_BARE.format(base_image=base)

        # Apply AiNxt internal registry settings (FROM rewrite + package manager config)
        return _apply_registry_cfg(template, btype, _REGISTRY_CFG)

    # ── Docker build ──────────────────────────────────────────

    def _docker_build(
            self,
            repo_path: Path,
            dockerfile_content: str,
            tag: str,
            force_rebuild: bool = False,
            build_root: str = ".",
    ) -> None:
        """
        Write Dockerfile to a temp file, then call `docker build` via subprocess.
        Streams each output line to the logger in real time so progress is visible
        in worker logs during long Maven/npm/Gradle builds.

        Uses subprocess (not docker-py SDK) to avoid SDK timeout issues.

        force_rebuild=True  → adds --no-cache flag (full dep re-download)
        force_rebuild=False → omits --no-cache (Docker reuses cached layers when
                              pom.xml / package.json / go.mod haven't changed)
        build_root          → subdirectory within repo_path used as Docker build context.
                              "." means repo root; "payment-service" means the submodule dir.
        """
        import threading

        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".dockerfile", delete=False, prefix=os.getenv("SANDBOX_PREFIX", _sandbox_prefix)
        ) as f:
            f.write(dockerfile_content)
            dockerfile_path = f.name
        logger.info(f"dockerfile_content - {dockerfile_content}")
        logger.info(f"dockerfile_path - {dockerfile_path}")

        # Use build_root as the Docker build context so that COPY commands in
        # Dockerfile templates work unchanged even for nested/monorepo projects.
        build_context = str((repo_path / build_root).resolve())
        cmd = ["docker", "build", "--network=host"]
        if force_rebuild:
            cmd.append("--no-cache")
        cmd.extend(["-f", dockerfile_path, "-t", tag, build_context])
        logger.info(f"SandboxImageBuilder: docker build cmd: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr into stdout
                text=True,
                bufsize=1,                  # line-buffered
            )

            output_lines: list[str] = []

            def _stream_output():
                for raw in proc.stdout:
                    line = raw.rstrip()
                    if not line:
                        continue
                    output_lines.append(line)
                    # Key progress markers at INFO; everything else at DEBUG
                    # so Maven download noise doesn't flood the log.
                    if (line.startswith("Step ") or line.startswith("Successfully")
                            or "error" in line.lower() or "failed" in line.lower()):
                        logger.info(f"[docker build {tag}] {line}")
                    else:
                        logger.debug(f"[docker build {tag}] {line}")

            reader = threading.Thread(target=_stream_output, daemon=True)
            reader.start()

            try:
                proc.wait(timeout=BUILD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"docker build timed out after {BUILD_TIMEOUT}s for {tag} — "
                    f"last output: {output_lines[-3:] if output_lines else '(none)'}"
                )

            reader.join(timeout=5)

            if proc.returncode != 0:
                err = "\n".join(output_lines[-25:])
                raise RuntimeError(
                    f"docker build failed for {tag} (exit {proc.returncode}):\n{err}"
                )

        finally:
            try:
                os.unlink(dockerfile_path)
            except Exception:
                pass


# ── Singleton ─────────────────────────────────────────────────
sandbox_image_builder = SandboxImageBuilder()
