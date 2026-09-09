"""Ephemeral Docker container manager for local MCP tool testing.

This module provides secure, isolated container execution for testing
local STDIO MCP servers. Each test session gets its own container
with resource limits, network whitelisting, and automatic cleanup.

Security features:
- Network egress whitelist (default-deny)
- Resource limits (CPU, memory)
- No host filesystem access
- No privileged mode
- Session hard timeout (10 minutes)
- Secrets never logged (masked in output)

NEW: also handles GitHub-sourced ("local_source") MCP servers - repos with
no registry package/remote metadata. This reuses the exact same session
store, Docker SDK, resource limits, and cleanup thread as the npm/pip
registry-package path; it does NOT introduce a parallel container/session
system. See `create_container_from_source` near the bottom of the class.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shlex
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.sandbox.schemas import GithubSourceHint, LocalRunConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum concurrent local test sessions per user
MAX_CONCURRENT_SESSIONS_PER_USER = 2

# Hard timeout for a session (10 minutes)
SESSION_TIMEOUT_SECONDS = 10 * 60  # 10 minutes

# How often the cleanup thread runs
CLEANUP_INTERVAL_SECONDS = 60

# Container resource limits (configurable via environment variables for faster local execution)
CONTAINER_CPU_LIMIT = float(os.getenv("SANDBOX_CPU_LIMIT", "1.5"))  # 1.5 CPU cores
CONTAINER_MEMORY_LIMIT = int(os.getenv("SANDBOX_MEMORY_LIMIT_MB", "768")) * 1024 * 1024  # 768 MB
CONTAINER_DISK_LIMIT = int(os.getenv("SANDBOX_DISK_LIMIT_MB", "256")) * 1024 * 1024  # 256 MB

# Shared package cache volumes to avoid re-downloading dependencies on every test
SANDBOX_ENABLE_CACHE_VOLUMES = os.getenv("SANDBOX_ENABLE_CACHE_VOLUMES", "true").lower() in ("true", "1", "yes")
NPM_CACHE_VOLUME = os.getenv("SANDBOX_NPM_CACHE_VOLUME", "mcp_npm_cache")
PIP_CACHE_VOLUME = os.getenv("SANDBOX_PIP_CACHE_VOLUME", "mcp_pip_cache")

# Default images per registry type
DEFAULT_IMAGES = {
    "npm": "node:20-alpine",
    "pip": "python:3.12-slim",
}

# Network allowlist patterns (domains)
DEFAULT_ALLOWED_DOMAINS = {
    "npm": [
        "registry.npmjs.org",
        "registry.npmmirror.com",
    ],
    "pip": [
        "pypi.org",
        "pypi.python.org",
        "files.pythonhosted.org",
    ],
}

# NEW: one base image per runtime, used for GitHub-sourced repos that do
# NOT ship their own Dockerfile. Deliberately reuses the same base images
# as the registry-package path where the runtime matches (node/python),
# so the same cache volumes apply. TODO: replace python-uv/go/rust with
# your own pinned images (e.g. one with `uv` preinstalled) once decided.
GENERIC_SOURCE_IMAGES: dict[str, str] = {
    "node": DEFAULT_IMAGES["npm"],
    "python": DEFAULT_IMAGES["pip"],
    "python-uv": DEFAULT_IMAGES["pip"],  # TODO: swap for an image with uv preinstalled
    "go": "golang:1.22-alpine",
    "rust": "rust:1.78-slim",
}

GIT_CLONE_TIMEOUT_S = 120
DOCKER_BUILD_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ContainerSession:
    """A single container session for testing a local MCP tool."""

    session_id: str
    item_id: UUID
    user_id: str | None  # For per-user session limits
    container_id: str
    registry_type: str  # "npm" | "pip" | "github-source" (NEW)
    identifier: str
    image: str
    allowed_domains: list[str]
    created_at: float
    expires_at: float
    status: str = "starting"  # "starting" | "ready" | "running" | "expired" | "stopped"
    # The STDIO process is stateful. Keep the client that completed
    # initialize/tools-list for the lifetime of this sandbox session so a
    # later tools/call goes to that same running server.
    mcp_client: Any | None = field(default=None, repr=False, compare=False)
    # NEW: only set for github-source sessions - the temp dir the repo was
    # cloned into, so destroy_container() can remove it alongside the
    # container. None for registry-package sessions.
    workdir: str | None = field(default=None, repr=False, compare=False)

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


# Node.js runner script executed inside container for npm MCP servers.
# Many npm packages omit the '#!/usr/bin/env node' shebang or use Windows CRLF line endings,
# which causes the shell to fail with 'line 7: syntax error: unexpected "("' when run via npx.
# This runner installs the package, fixes missing shebangs/CRLF, and invokes node directly.
NPM_RUNNER_SCRIPT = (
    'const {execSync,spawn}=require("child_process"),fs=require("fs"),path=require("path");'
    'function getPkgName(id){const at=id.lastIndexOf("@");return at>0?id.slice(0,at):id;}'
    'const id=process.argv[1],extraArgs=process.argv.slice(2);'
    'let globalRoot="/usr/local/lib/node_modules";'
    'try{globalRoot=execSync("npm root -g").toString().trim();}catch(e){}'
    'const pkgDir=path.join(globalRoot,getPkgName(id));'
    'if(!fs.existsSync(pkgDir)){'
    'try{execSync("npm install -g --no-audit --no-fund --prefer-offline --progress=false "+JSON.stringify(id),{stdio:["ignore","ignore","inherit"]});}catch(e){process.exit(1);}'
    '}'
    'let binPath=null;'
    'const pkgJsonPath=path.join(pkgDir,"package.json");'
    'if(fs.existsSync(pkgJsonPath)){'
    'try{'
    'const pkg=JSON.parse(fs.readFileSync(pkgJsonPath,"utf8"));'
    'if(typeof pkg.bin==="string"){binPath=path.resolve(pkgDir,pkg.bin);}'
    'else if(pkg.bin&&typeof pkg.bin==="object"){const vals=Object.values(pkg.bin);if(vals.length>0)binPath=path.resolve(pkgDir,vals[0]);}'
    'else if(pkg.main){binPath=path.resolve(pkgDir,pkg.main);}'
    '}catch(e){}'
    '}'
    'if(!binPath||!fs.existsSync(binPath)){'
    'try{const binName=getPkgName(id).split("/").pop();'
    'const candidate=execSync("which "+binName+" 2>/dev/null").toString().trim();'
    'if(candidate&&fs.existsSync(candidate))binPath=fs.realpathSync(candidate);'
    '}catch(e){}'
    '}'
    'if(!binPath||!fs.existsSync(binPath)){console.error("Could not locate entrypoint for "+id);process.exit(1);}'
    'try{'
    'let content=fs.readFileSync(binPath,"utf8"),modified=false;'
    'const cr=String.fromCharCode(13),lf=String.fromCharCode(10);'
    'if(content.indexOf(cr)!==-1){content=content.split(cr+lf).join(lf).split(cr).join(lf);modified=true;}'
    'if(!content.startsWith("#!")){content="#!/usr/bin/env node"+lf+content;modified=true;}'
    'if(modified)fs.writeFileSync(binPath,content,"utf8");'
    'fs.chmodSync(binPath,0o755);'
    '}catch(e){}'
    'const child=spawn(process.execPath,[binPath,...extraArgs],{stdio:"inherit"});'
    'child.on("exit",(code,sig)=>process.exit(code??(sig?1:0)));'
)


@dataclass
class LocalToolConfig:
    """Configuration for running a local MCP tool in a container."""

    registry_type: str  # "npm" | "pip"
    identifier: str
    runtime_hint: str | None = None
    runtime_arguments: list[str] = field(default_factory=list)
    environment_variables: list[dict[str, Any]] = field(default_factory=list)

    def build_command(self) -> list[str]:
        """Build the command to execute inside the container."""
        if self.registry_type == "npm" or "npx" in (self.runtime_hint or "").lower():
            # Clean package identifier if it accidentally includes npx prefix
            pkg_id = self.identifier.strip()
            if pkg_id.lower().startswith("npx "):
                pkg_id = pkg_id[4:].strip()
            if pkg_id.startswith("-y "):
                pkg_id = pkg_id[3:].strip()

            args = [shlex.quote(pkg_id)]
            if self.runtime_arguments:
                args.extend(shlex.quote(arg) for arg in self.runtime_arguments)
            args_str = " ".join(args)
            full_cmd = f"node -e '{NPM_RUNNER_SCRIPT}' -- {args_str}"
            return ["sh", "-c", full_cmd]

        if self.registry_type == "pip":
            # Check if pipx is installed first to avoid redundant installation on every run
            full_cmd = (
                f"(command -v pipx >/dev/null 2>&1 || pip install --quiet --no-warn-script-location --prefer-binary pipx) "
                f"&& pipx run {shlex.quote(self.identifier)}"
            )
            if self.runtime_arguments:
                full_cmd += " " + " ".join(shlex.quote(arg) for arg in self.runtime_arguments)
            return ["sh", "-c", full_cmd]

        # Fallback for other tools: run via node runner
        args = [shlex.quote(self.identifier)]
        if self.runtime_arguments:
            args.extend(shlex.quote(arg) for arg in self.runtime_arguments)
        args_str = " ".join(args)
        return ["sh", "-c", f"node -e '{NPM_RUNNER_SCRIPT}' -- {args_str}"]


# ---------------------------------------------------------------------------
# Container Manager
# ---------------------------------------------------------------------------

class ContainerManager:
    """Manages ephemeral Docker containers for local MCP tool testing.

    This class is a singleton per process. It handles:
    - Container creation with resource limits and network whitelisting
    - Session tracking and lifecycle management
    - Automatic cleanup of expired sessions
    - Per-user concurrent session limits
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, ContainerSession] = {}
        self._user_sessions: dict[str, set[str]] = {}  # user_id -> set of session_ids
        self._cleanup_thread: threading.Thread | None = None
        self._stop_cleanup = threading.Event()
        self._docker_client: Any = None  # Lazily initialized

    # -------------------------------------------------------------------------
    # Docker Client (lazy initialization)
    # -------------------------------------------------------------------------

    @property
    def docker(self) -> Any:
        """Lazily initialize Docker client."""
        if self._docker_client is None:
            import docker
            self._docker_client = docker.from_env()
        return self._docker_client

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def create_container(
        self,
        item_id: UUID,
        config: LocalToolConfig,
        env_vars: dict[str, str],
        allowed_domains: list[str] | None = None,
        user_id: str | None = None,
        timeout: int = 60,
    ) -> ContainerSession:
        """Create and start an ephemeral container for testing.

        Args:
            item_id: UUID of the item being tested
            config: Configuration for the local tool
            env_vars: Environment variables to inject (secrets should NOT be logged)
            allowed_domains: Override list of allowed egress domains
            user_id: User identifier for session limits
            timeout: Container startup timeout in seconds

        Returns:
            ContainerSession with container_id and session_id

        Raises:
            RuntimeError: If container creation fails or timeout exceeded
            ValueError: If user exceeds concurrent session limit
        """
        session_id = secrets.token_urlsafe(32)
        now = time.monotonic()

        # Check per-user session limit
        self._check_user_session_limit(user_id)

        # Determine image
        image = DEFAULT_IMAGES.get(config.registry_type, DEFAULT_IMAGES["npm"])

        # Build command
        command = config.build_command()

        # Allowed domains (merge with defaults)
        domains = allowed_domains or []
        if config.registry_type in DEFAULT_ALLOWED_DOMAINS:
            domains = list(set(domains) | set(DEFAULT_ALLOWED_DOMAINS[config.registry_type]))

        # Create session record first
        session = ContainerSession(
            session_id=session_id,
            item_id=item_id,
            user_id=user_id,
            container_id="",  # Will be set after container creation
            registry_type=config.registry_type,
            identifier=config.identifier,
            image=image,
            allowed_domains=domains,
            created_at=now,
            expires_at=now + SESSION_TIMEOUT_SECONDS,
            status="starting",
        )

        # Mask secrets in env vars for logging (but keep original for container)
        safe_env = self._mask_secrets(env_vars)
        logger.info(
            "Creating container session %s for item %s (image=%s, command=%s, env=%s)",
            session_id, item_id, image, command, safe_env,
        )

        try:
            # Run container in a separate thread (Docker SDK is blocking)
            container = await asyncio.get_event_loop().run_in_executor(
                None,
                self._create_docker_container,
                session_id, image, command, env_vars, domains, timeout, config.registry_type,
            )

            session.container_id = container.id
            session.status = "ready"

            self._register_session(session)

            logger.info(
                "Container session %s created successfully (container_id=%s)",
                session_id, container.id,
            )
            return session

        except Exception as exc:
            logger.error("Failed to create container session %s: %s", session_id, exc)
            raise RuntimeError(f"Failed to create container: {exc}") from exc

    # -------------------------------------------------------------------------
    # NEW: GitHub-source ("local_source") path
    # -------------------------------------------------------------------------

    async def create_container_from_source(
        self,
        item: Any,
        github_hint: GithubSourceHint,
        run_config: LocalRunConfig,
        env_vars: dict[str, str],
        allowed_domains: list[str] | None = None,
        user_id: str | None = None,
    ) -> ContainerSession:
        """Create and start an ephemeral container for a GitHub-sourced MCP
        server (no registry package/remote metadata - run config was
        derived by app.sandbox.extract.extract_local_run_config).

        Mirrors create_container() as closely as possible: same session
        store, same resource limits, same cleanup thread, same
        sandbox-network detection. The two real differences are (1) the
        image is either built from the repo's own Dockerfile or picked
        from GENERIC_SOURCE_IMAGES by runtime, and (2) the caller execs
        run_config.command/args via LocalMCPClient afterwards, instead of
        this method building a self-installing command itself.

        Raises:
            ValueError: heuristic-tier run_config (no command/args), or
                user exceeds concurrent session limit.
            RuntimeError: clone/build/container-start failure.
        """
        if not run_config.command:
            # Heuristic-tier extraction only found an install hint, no
            # command/args - guessing a run command is worse than telling
            # the UI we don't know how to run this. Caller should check
            # this BEFORE calling create_container_from_source and surface
            # "install detected, run command unknown" instead.
            raise ValueError(
                "run_config has no command/args (heuristic-tier extraction) - "
                "this item is not runnable yet, only installable."
            )

        session_id = secrets.token_urlsafe(32)
        now = time.monotonic()

        self._check_user_session_limit(user_id)

        workdir = tempfile.mkdtemp(prefix=f"mcp-source-{session_id}-")

        domains = allowed_domains or []
        if run_config.runtime in DEFAULT_ALLOWED_DOMAINS:
            domains = list(set(domains) | set(DEFAULT_ALLOWED_DOMAINS[run_config.runtime]))
        # NOTE: this domain list is for the container we end up RUNNING.
        # If the repo has its own Dockerfile, the `docker build` step below
        # is a separate concern with its own network exposure - see the
        # open question flagged in chat about restricting build-time network.

        session = ContainerSession(
            session_id=session_id,
            item_id=item.item_id,
            user_id=user_id,
            container_id="",
            registry_type="github-source",
            identifier=github_hint.repository,
            image="",
            allowed_domains=domains,
            created_at=now,
            expires_at=now + SESSION_TIMEOUT_SECONDS,
            status="starting",
            workdir=workdir,
        )

        safe_env = self._mask_secrets(env_vars)
        logger.info(
            "Creating source container session %s for item %s (repo=%s, env=%s)",
            session_id, item.item_id, github_hint.repository, safe_env,
        )

        try:
            has_dockerfile = await self._repo_has_dockerfile(item)
            await self._git_clone_shallow(github_hint.clone_url, workdir)

            if has_dockerfile:
                image_tag = f"mcp-source/{session_id}:local"
                await asyncio.get_event_loop().run_in_executor(
                    None, self._build_docker_image, workdir, image_tag,
                )
                session.image = image_tag
                bind_workdir = None  # repo is baked into the image already
            else:
                image_tag = GENERIC_SOURCE_IMAGES.get(run_config.runtime or "")
                if not image_tag:
                    raise RuntimeError(
                        f"No generic sandbox image configured for runtime "
                        f"{run_config.runtime!r}"
                    )
                session.image = image_tag
                bind_workdir = workdir  # bind-mounted read-only below

            container = await asyncio.get_event_loop().run_in_executor(
                None,
                self._create_docker_container_from_source,
                session.image, env_vars, domains, bind_workdir,
            )

            session.container_id = container.id
            session.status = "ready"

            self._register_session(session)

            logger.info(
                "Source container session %s created successfully (container_id=%s, "
                "built_from_dockerfile=%s)",
                session_id, container.id, has_dockerfile,
            )
            return session

        except Exception as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            logger.error("Failed to create source container session %s: %s", session_id, exc)
            raise RuntimeError(f"Failed to create container from source: {exc}") from exc

    async def _repo_has_dockerfile(self, item: Any) -> bool:
        """Reuses the same source_resolver function extract.py already
        uses, so this gets the same disk caching / branch-fallback
        behavior for free and costs zero extra GitHub API calls if the
        item's tree was already fetched by extract_local_run_config."""
        from app.artifacts.source_resolver import get_repository_tree

        tree = await get_repository_tree(item)
        return any(entry.path == "Dockerfile" for entry in tree)

    async def _git_clone_shallow(self, clone_url: str, dest: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", clone_url, dest,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_CLONE_TIMEOUT_S)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone of {clone_url!r} failed: {stderr.decode(errors='replace')}"
            )

    def _build_docker_image(self, context_dir: str, image_tag: str) -> None:
        """Runs in executor - blocking Docker SDK build call.

        Repo Dockerfiles are untrusted input, but the build intentionally
        keeps default (full) network access — restricting it would break
        the common case of `RUN pip install`/`npm install` steps inside
        the Dockerfile. Runtime isolation (network none + resource limits)
        is what actually matters and is enforced separately by
        _create_docker_container_from_source, regardless of whether the
        image came from this build or a generic base image. Decided
        trade-off, not a pending TODO.
        """
        client = self.docker
        client.images.build(
            path=context_dir,
            tag=image_tag,
            rm=True,
            timeout=DOCKER_BUILD_TIMEOUT_S,
        )

    def _create_docker_container_from_source(
        self,
        image: str,
        env_vars: dict[str, str],
        allowed_domains: list[str],
        bind_workdir: str | None,
    ) -> Any:
        """Mirrors _create_docker_container, adapted for a source-built or
        source-selected image: no registry-type cache volume (doesn't
        apply here), optional read-only bind mount of the cloned repo when
        using a generic image (skipped when the image was built from the
        repo's own Dockerfile, since the repo is already inside it)."""
        client = self.docker

        if bind_workdir:
            # Generic base image - may need pulling, same as the registry path.
            try:
                client.images.get(image)
                logger.debug("Docker image %s is already present locally", image)
            except Exception:
                logger.info("Docker image %s not found locally, pulling...", image)
                try:
                    client.images.pull(image)
                except Exception as exc:
                    logger.warning("Failed to pull image %s (may already exist): %s", image, exc)
        # else: image was just built locally from the repo's Dockerfile - no pull needed.

        env_list = [f"{k}={v}" for k, v in env_vars.items()]

        container_config: dict[str, Any] = {
            "image": image,
            "command": ["sleep", "infinity"],
            "environment": env_list,
            "detach": True,
            "nano_cpus": int(CONTAINER_CPU_LIMIT * 1e9),
            "mem_limit": CONTAINER_MEMORY_LIMIT,
            "memswap_limit": CONTAINER_MEMORY_LIMIT,
            "cap_drop": ["MKNOD", "SETFCAP", "SETPCAP", "NET_RAW", "SYS_CHROOT", "KILL"],
            "security_opt": ["no-new-privileges"],
            "auto_remove": False,
        }

        if bind_workdir:
            container_config["volumes"] = {bind_workdir: {"bind": "/repo", "mode": "ro"}}
            container_config["working_dir"] = "/repo"

        try:
            networks = client.networks.list(names=["sandbox-network"])
            if networks:
                container_config["network"] = "sandbox-network"
                logger.info("Using sandbox-network for egress control")
            else:
                logger.info("sandbox-network not found, using default bridge network")
        except Exception:
            logger.info("sandbox-network not found, using default bridge network")

        try:
            container = client.containers.run(**container_config)
            return container
        except Exception as exc:
            logger.error("Failed to create source container: %s", exc)
            raise

    # -------------------------------------------------------------------------
    # Shared helpers (used by both create_container and create_container_from_source)
    # -------------------------------------------------------------------------

    def _check_user_session_limit(self, user_id: str | None) -> None:
        with self._lock:
            if user_id:
                user_session_count = len(self._user_sessions.get(user_id, set()))
                if user_session_count >= MAX_CONCURRENT_SESSIONS_PER_USER:
                    raise ValueError(
                        f"Maximum concurrent sessions ({MAX_CONCURRENT_SESSIONS_PER_USER}) reached. "
                        "Please disconnect an existing session before starting a new one."
                    )

    def _register_session(self, session: ContainerSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session
            if session.user_id:
                self._user_sessions.setdefault(session.user_id, set()).add(session.session_id)
        self._ensure_cleanup_thread()

    async def destroy_container(self, session_id: str) -> bool:
        """Stop and remove a container.

        Args:
            session_id: Session ID to terminate

        Returns:
            True if container was destroyed, False if session not found
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                logger.warning("Attempted to destroy non-existent session: %s", session_id)
                return False

            session.status = "stopped"

        if session.mcp_client is not None:
            try:
                await session.mcp_client.disconnect()
            except Exception as exc:
                logger.debug("Error closing MCP client for session %s: %s", session_id, exc)
            session.mcp_client = None

        # Remove container in executor
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._remove_docker_container,
                session.container_id,
            )
        except Exception as exc:
            logger.warning(
                "Error removing container %s for session %s: %s",
                session.container_id, session_id, exc,
            )

        # NEW: clean up the cloned repo dir for github-source sessions
        if session.workdir:
            shutil.rmtree(session.workdir, ignore_errors=True)

        # Cleanup session tracking
        with self._lock:
            self._sessions.pop(session_id, None)
            if session.user_id and session.user_id in self._user_sessions:
                self._user_sessions[session.user_id].discard(session_id)

        logger.info("Container session %s destroyed", session_id)
        return True

    def get_session(self, session_id: str) -> ContainerSession | None:
        """Get session by ID."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session and session.is_expired:
                # Mark as expired but don't auto-remove (let cleanup handle it)
                session.status = "expired"
                return None
            return session

    def list_active_sessions(self) -> list[ContainerSession]:
        """List all active (non-expired) sessions."""
        with self._lock:
            now = time.monotonic()
            active = []
            for session in self._sessions.values():
                if session.expires_at > now:
                    active.append(session)
                else:
                    session.status = "expired"
            return active

    async def cleanup_expired(self) -> int:
        """Remove all expired sessions.

        Returns:
            Number of sessions cleaned up
        """
        with self._lock:
            expired_ids = [
                sid for sid, session in self._sessions.items()
                if session.is_expired and session.status not in ("stopped", "expired")
            ]

        cleaned = 0
        for session_id in expired_ids:
            try:
                await self.destroy_container(session_id)
                cleaned += 1
            except Exception as exc:
                logger.error("Error cleaning up session %s: %s", session_id, exc)

        if cleaned > 0:
            logger.info("Cleaned up %d expired sessions", cleaned)
        return cleaned

    # -------------------------------------------------------------------------
    # Docker container creation (registry-package path)
    # -------------------------------------------------------------------------

    def _create_docker_container(
        self,
        session_id: str,
        image: str,
        command: list[str],
        env_vars: dict[str, str],
        allowed_domains: list[str],
        timeout: int,
        registry_type: str = "npm",
    ) -> Any:
        """Create and start a Docker container with security constraints.

        This method:
        - Uses local image if already present (avoids slow remote registry pull)
        - Creates container with resource limits
        - Mounts package cache volume for fast repeated testing
        - Sets up network whitelisting via /etc/hosts and iptables-style rules
        - Starts the container (but we don't wait for it to complete)
        """
        client = self.docker

        # Only pull image if not present locally (saves 5-15s per run)
        try:
            client.images.get(image)
            logger.debug("Docker image %s is already present locally", image)
        except Exception:
            logger.info("Docker image %s not found locally, pulling...", image)
            try:
                client.images.pull(image)
            except Exception as exc:
                logger.warning("Failed to pull image %s (may already exist): %s", image, exc)

        # Build environment list
        env_list = [f"{k}={v}" for k, v in env_vars.items()]

        # Mount cache volumes to avoid re-downloading packages on every test run
        volumes: dict[str, dict[str, str]] = {}
        if SANDBOX_ENABLE_CACHE_VOLUMES:
            if registry_type == "npm":
                volumes[NPM_CACHE_VOLUME] = {"bind": "/root/.npm", "mode": "rw"}
            elif registry_type == "pip":
                volumes[PIP_CACHE_VOLUME] = {"bind": "/root/.cache/pip", "mode": "rw"}

        # Container configuration
        # Start with a simple long-running command (sleep infinity) to keep container alive
        # The actual MCP server will be run via docker exec
        container_config = {
            "image": image,
            "command": ["sleep", "infinity"],
            "environment": env_list,
            "detach": True,
            # Resource limits
            "nano_cpus": int(CONTAINER_CPU_LIMIT * 1e9),
            "mem_limit": CONTAINER_MEMORY_LIMIT,
            "memswap_limit": CONTAINER_MEMORY_LIMIT,  # Disable swap
            # Drop capabilities (but not all - some needed for network)
            "cap_drop": ["MKNOD", "SETFCAP", "SETPCAP", "NET_RAW", "SYS_CHROOT", "KILL"],
            "security_opt": ["no-new-privileges"],
            # Don't auto-remove - we want to inspect logs if needed
            "auto_remove": False,
        }

        if volumes:
            container_config["volumes"] = volumes

        # Try to use sandbox-network if it exists, otherwise use default
        try:
            networks = client.networks.list(names=["sandbox-network"])
            if networks:
                container_config["network"] = "sandbox-network"
                logger.info("Using sandbox-network for egress control")
            else:
                logger.info("sandbox-network not found, using default bridge network")
        except Exception:
            logger.info("sandbox-network not found, using default bridge network")

        try:
            container = client.containers.run(**container_config)
            return container
        except Exception as exc:
            logger.error("Failed to create container: %s", exc)
            raise

    def _remove_docker_container(self, container_id: str) -> None:
        """Remove a Docker container."""
        try:
            container = self.docker.containers.get(container_id)
            container.stop(timeout=5)
            container.remove()
        except Exception as exc:
            logger.warning("Error removing container %s: %s", container_id, exc)

    # -------------------------------------------------------------------------
    # Cleanup thread
    # -------------------------------------------------------------------------

    def _ensure_cleanup_thread(self) -> None:
        """Start cleanup thread if not running."""
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._stop_cleanup.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                daemon=True,
                name="container-cleanup",
            )
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Background loop to clean up expired sessions."""
        while not self._stop_cleanup.wait(CLEANUP_INTERVAL_SECONDS):
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.cleanup_expired())
                loop.close()
            except Exception as exc:
                logger.error("Error in cleanup loop: %s", exc)

    def stop_cleanup(self) -> None:
        """Stop the cleanup thread."""
        self._stop_cleanup.set()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    def _mask_secrets(self, env_vars: dict[str, str]) -> dict[str, str]:
        """Mask secret values in environment variables for logging."""
        masked = {}
        for key, value in env_vars.items():
            if any(secret_marker in key.upper() for secret_marker in ["SECRET", "TOKEN", "PASSWORD", "KEY", "AUTH", "CREDENTIAL"]):
                if len(value) > 4:
                    masked[key] = value[:2] + "***" + value[-2:]
                else:
                    masked[key] = "****"
            else:
                masked[key] = value
        return masked


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_container_manager: ContainerManager | None = None
_manager_lock = threading.Lock()


def get_container_manager() -> ContainerManager:
    """Get the global ContainerManager instance."""
    global _container_manager
    if _container_manager is None:
        with _manager_lock:
            if _container_manager is None:
                _container_manager = ContainerManager()
    return _container_manager