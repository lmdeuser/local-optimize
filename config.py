#!/usr/bin/env python3
"""
Unified Configuration & Security Settings Manager for local_optimize.

Provides centralized, environment-aware configuration with strict secret masking,
atomic secrets.env file management, and zero secret leakage in logs or reprs.
"""

import ipaddress
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("local_optimize.config")

# Protected user configuration directory
SECRETS_DIR = Path.home() / ".config" / "local-optimize"
SECRETS_FILE = SECRETS_DIR / "secrets.env"
PROJECTS_FILE = SECRETS_DIR / "projects.json"
OWNED_SECRET_KEYS = frozenset(
    {
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "DB_SSLMODE", "DB_SSLROOTCERT", "DB_SSLCERT", "DB_SSLKEY",
        "RAG_BEARER_TOKEN",
    }
)

VALID_DB_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)


class SecretString:
    """Safe wrapper for sensitive values that masks content in repr and str."""

    def __init__(self, value: Optional[str] = ""):
        self._value = value or ""

    def get_secret_value(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "**********" if self._value else ""

    def __repr__(self) -> str:
        return "SecretString('**********')" if self._value else "SecretString('')"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SecretString):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return False


def load_secrets_file(file_path: Path | None = None) -> Dict[str, str]:
    """Reads secrets from ~/.config/local-optimize/secrets.env without logging values."""
    secrets = {}
    target = file_path or SECRETS_FILE
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'").strip('"')
                        if k and v:
                            secrets[k] = v
        except Exception as e:
            log.warning("Could not read secrets file: %s", e.__class__.__name__)
    return secrets


def save_secrets_file(
    secrets: Dict[str, str], backup: bool = True, file_path: Path | None = None
) -> bool:
    """
    Atomically writes key-value pairs to ~/.config/local-optimize/secrets.env
    Setting 0700 dir permissions and 0600 file permissions.
    """
    import tempfile
    tmp_path = None
    target = file_path or SECRETS_FILE
    target_dir = target.parent
    try:
        target_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(target_dir, 0o700)

        if backup and target.exists():
            import time
            backup_file = target_dir / f"{target.name}.backup-{time.time_ns()}"
            with open(backup_file, "xb") as backup_stream:
                os.chmod(backup_file, 0o600)
                backup_stream.write(target.read_bytes())
                backup_stream.flush()
                os.fsync(backup_stream.fileno())

        with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8") as tf:
            tmp_path = Path(tf.name)
            os.chmod(tmp_path, 0o600)
            for k, v in secrets.items():
                if k not in OWNED_SECRET_KEYS:
                    continue
                if k and v is not None and str(v).strip():
                    tf.write(f"{k}={v}\n")
            tf.flush()
            os.fsync(tf.fileno())

        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
        tmp_path = None
        os.chmod(target, 0o600)

        # Fsync parent directory
        dir_fd = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        return True
    except Exception as e:
        log.error("Failed to save secrets file: %s", e)
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return False


class Settings:
    """Centralized System Configuration."""

    def __init__(self):
        file_secrets = load_secrets_file()

        # Secret parameters (Priority: Process env > secrets.env)
        raw_db_pass = os.getenv("DB_PASSWORD") or file_secrets.get("DB_PASSWORD", "")
        self._db_password = SecretString(raw_db_pass)
        raw_bearer_token = os.getenv("RAG_BEARER_TOKEN") or file_secrets.get("RAG_BEARER_TOKEN", "")
        self._bearer_token = SecretString(raw_bearer_token)

        # Database connection options
        self.db_host: str = os.getenv("DB_HOST") or file_secrets.get("DB_HOST", "127.0.0.1")
        self.db_port: int = int(os.getenv("DB_PORT") or file_secrets.get("DB_PORT", "5432"))
        self.db_name: str = os.getenv("DB_NAME") or file_secrets.get("DB_NAME", "aiagent")
        self.db_user: str = os.getenv("DB_USER") or file_secrets.get("DB_USER", "aiagent")
        self.db_sslmode: str = (
            os.getenv("DB_SSLMODE") or file_secrets.get("DB_SSLMODE", "prefer")
        ).strip().lower()
        if self.db_sslmode not in VALID_DB_SSLMODES:
            raise ValueError(
                "DB_SSLMODE must be one of: " + ", ".join(sorted(VALID_DB_SSLMODES))
            )
        self.db_sslrootcert: str = os.getenv("DB_SSLROOTCERT") or file_secrets.get("DB_SSLROOTCERT", "")
        self.db_sslcert: str = os.getenv("DB_SSLCERT") or file_secrets.get("DB_SSLCERT", "")
        self.db_sslkey: str = os.getenv("DB_SSLKEY") or file_secrets.get("DB_SSLKEY", "")

        # Network / HTTP Server Configuration
        self.rag_server_url: str = os.getenv("RAG_SERVER_URL", "http://127.0.0.1:8181")
        self.bind_address: str = os.getenv("BIND_ADDRESS", "127.0.0.1")
        self.server_port: int = int(os.getenv("RAG_SERVER_PORT", "8181"))
        self.server_agent_id: str = os.getenv("SERVER_AGENT_ID", "local-optimize-server").strip()
        if not self.server_agent_id:
            raise ValueError("SERVER_AGENT_ID must not be empty")
        
        raw_cors = os.getenv("CORS_ORIGINS", "http://127.0.0.1:8181,http://localhost:8181")
        self.cors_origins: List[str] = [x.strip() for x in raw_cors.split(",") if x.strip()]

        # Project Root, Registry & shared request/indexing limits
        self.project_root: Path = Path(__file__).resolve().parent
        model_policy = json.loads((self.project_root / "local_model.json").read_text())
        self.local_model = model_policy["model"]
        self.local_model_context = model_policy["num_ctx"]
        if not isinstance(self.local_model, str) or not self.local_model.strip():
            raise ValueError("local_model.json requires a model name")
        if type(self.local_model_context) is not int or not 2048 <= self.local_model_context <= 32768:
            raise ValueError("local_model.json requires num_ctx between 2048 and 32768")
        if os.getenv("OLLAMA_MODEL", self.local_model) != self.local_model:
            raise ValueError("OLLAMA_MODEL conflicts with the shared local_model.json policy")
        if os.getenv("OLLAMA_NUM_CTX", str(self.local_model_context)) != str(self.local_model_context):
            raise ValueError("OLLAMA_NUM_CTX conflicts with the shared local_model.json policy")
        self.projects_file: Path = Path(
            os.getenv("PROJECTS_REGISTRY_FILE", str(PROJECTS_FILE))
        ).expanduser()
        if not self.projects_file.is_absolute():
            raise ValueError("PROJECTS_REGISTRY_FILE must be an absolute path")
        self.allowed_index_roots: List[Path] = [
            Path(p).expanduser().resolve()
            for p in os.getenv("ALLOWED_INDEX_ROOTS", str(self.project_root)).split(":")
            if p
        ]
        self.max_request_body_bytes: int = int(os.getenv("MAX_REQUEST_BODY_BYTES", "10485760"))  # 10 MB
        self.max_file_size_bytes: int = int(os.getenv("MAX_FILE_SIZE_BYTES", "5242880"))        # 5 MB
        self.max_query_chars: int = int(os.getenv("MAX_QUERY_CHARS", "2000"))
        self.max_save_chars: int = int(os.getenv("MAX_SAVE_CHARS", "100000"))
        self.max_search_limit: int = int(os.getenv("MAX_SEARCH_LIMIT", "50"))
        self.rag_search_timeout_seconds: float = float(
            os.getenv("RAG_SEARCH_TIMEOUT_SECONDS", "60")
        )
        self.max_agentic_limit: int = int(os.getenv("MAX_AGENTIC_LIMIT", "30"))
        self.max_agentic_depth: int = int(os.getenv("MAX_AGENTIC_DEPTH", "3"))
        self.max_agentic_entities: int = int(os.getenv("MAX_AGENTIC_ENTITIES", "50"))
        self.max_agentic_relations: int = int(os.getenv("MAX_AGENTIC_RELATIONS", "100"))
        self.max_agentic_timeout_ms: int = int(os.getenv("MAX_AGENTIC_TIMEOUT_MS", "5000"))
        # Final evidence relevance, distinct from E5/RRF candidate scores.
        # The ranker also enforces its versioned policy minimum (currently 0.5).
        self.relevance_floor: float = float(os.getenv("RAG_RELEVANCE_FLOOR", "0.50"))
        if any(
            value < 1
            for value in (
                self.max_request_body_bytes,
                self.max_file_size_bytes,
                self.max_query_chars,
                self.max_save_chars,
                self.max_search_limit,
                self.max_agentic_limit,
                self.max_agentic_depth,
                self.max_agentic_entities,
                self.max_agentic_relations,
                self.max_agentic_timeout_ms,
            )
        ):
            raise ValueError("Request and indexing limits must be positive")
        if (
            not math.isfinite(self.rag_search_timeout_seconds)
            or self.rag_search_timeout_seconds <= 0
        ):
            raise ValueError("RAG_SEARCH_TIMEOUT_SECONDS must be a positive finite number")

        # Skills paths
        self.skills_dir: Path = self.project_root / "skills"
        self.manifest_file: Path = self.skills_dir / "manifest.json"

    @property
    def db_password(self) -> SecretString:
        return self._db_password

    @property
    def bearer_token(self) -> SecretString:
        return self._bearer_token

    def require_safe_bind(self, address: str | None = None) -> None:
        """Reject externally reachable binds unless bearer authentication is configured."""
        host = (address or self.bind_address).strip().lower()
        is_loopback = host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback and not self._bearer_token:
            raise RuntimeError(
                "Non-loopback bind requires a bearer token in local-optimize secrets"
            )

    def get_db_params(self) -> Dict[str, Any]:
        """Returns parameters dictionary for psycopg2 connection."""
        params = {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self._db_password.get_secret_value(),
            "sslmode": self.db_sslmode,
            "connect_timeout": 5,
            "options": "-c statement_timeout=15000 -c lock_timeout=2000",
        }
        for key, value in (
            ("sslrootcert", self.db_sslrootcert),
            ("sslcert", self.db_sslcert),
            ("sslkey", self.db_sslkey),
        ):
            if value:
                params[key] = value
        return params

    def to_diagnostic_dict(self) -> Dict[str, Any]:
        """Returns safe diagnostic information without any plain secret exposure."""
        return {
            "db_host": self.db_host,
            "db_port": self.db_port,
            "db_name": self.db_name,
            "db_user": self.db_user,
            "db_password": str(self._db_password),
            "db_sslmode": self.db_sslmode,
            "rag_server_url": self.rag_server_url,
            "bind_address": self.bind_address,
            "server_port": self.server_port,
            "bearer_token": str(self._bearer_token),
            "cors_origins": self.cors_origins,
            "project_root": str(self.project_root),
        }
        
    def reload(self):
        """Reloads settings from environment and secrets file."""
        self.__init__()


def build_entity_stable_key(
    project_id: str,
    relative_path: str,
    symbol_type: str,
    qualified_name: str
) -> str:
    """
    Constructs a deterministic stable_key identifier for code/doc entities.
    Format: <project_id>:<normalized_relative_path>:<symbol_type>:<qualified_name>

    Rules:
    - Normalizes path separators to '/'
    - Prohibits '..' inside relative_path
    - Lowercases symbol_type
    - Pure deterministic result
    """
    proj = (project_id or "default_project").strip()

    # Path normalization & security validation
    clean_path = (relative_path or "").strip().replace("\\", "/")
    parts = [p for p in clean_path.split("/") if p and p != "."]
    if ".." in parts:
        raise ValueError(f"Path traversal '..' forbidden in stable_key relative_path: {relative_path}")
    norm_path = "/".join(parts) if parts else "root"

    sym_type = (symbol_type or "concept").strip().lower()
    qual_name = (qualified_name or "").strip()

    return f"{proj}:{norm_path}:{sym_type}:{qual_name}"


def build_dedup_scope(
    project_name: str,
    source_uri: str,
    file_path: str,
    content_hash: str,
    source_type: str,
    visibility: str,
    agent_id: str,
    session_id: str,
    validation_status: str
) -> tuple:
    """
    Unified deduplication scope tuple used consistently across save(), indexing,
    memory_optimize(), cleanup, and compaction procedures.
    """
    if not project_name or not project_name.strip():
        raise ValueError("project_name is required for deduplication")
    return (
        project_name.strip(),
        (source_uri or "").strip(),
        (file_path or "").strip(),
        (content_hash or "").strip(),
        (source_type or "general").strip(),
        (visibility or "shared").strip(),
        (agent_id or "").strip(),
        (session_id or "").strip(),
        (validation_status or "unverified").strip()
    )


# Singleton instance
settings = Settings()
