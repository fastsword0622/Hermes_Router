"""Isolated, tool-free Hermes invocations using the user's existing provider credentials."""
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from dotenv import dotenv_values
from .providers.hermes import HermesSubprocessProvider
from .registry import get_spec
from .redaction import redact_text, register_secrets

PROVIDER_ENV = {
    "minimax": (("MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"), "MINIMAX_CN_BASE_URL"),
    "glm": (("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "ZHIPU_API_KEY"), "GLM_BASE_URL"),
    "deepseek": (("DEEPSEEK_API_KEY",), "DEEPSEEK_BASE_URL"),
}


class HermesFactory:
    def __init__(self, data_dir: Path, *, executable="hermes", credentials_path: Path | None = None, environ=None):
        self.data_dir = data_dir
        self.executable = shutil.which(executable) or executable
        self.credentials_path = credentials_path or Path.home() / ".hermes" / ".env"
        self.environ = dict(os.environ if environ is None else environ)
        self._secrets = []

    def make(self, provider: str, job_id: str, cwd: Path):
        spec = get_spec(provider)
        key_names, base_name = PROVIDER_ENV[provider]
        values = {**dotenv_values(self.credentials_path), **self.environ}
        self._secrets = [v for k, v in values.items() if v and any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))]
        register_secrets(self._secrets)
        key = next((values.get(k) for k in key_names if values.get(k)), None)
        if not key:
            raise ValueError(f"credential not configured for {provider}; key value is never logged")
        home = self.data_dir / "hermes-homes" / job_id / provider
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        cwd.mkdir(parents=True, exist_ok=True)
        config = {"model": {"provider": spec.provider_id, "default": spec.model_id},
            "platform_toolsets": {"cli": []}, "agent": {"disabled_toolsets": ["web"], "max_turns": 1},
            "mcp_servers": {}, "fallback_providers": []}
        env = {"PATH": self.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "HERMES_HOME": str(home),
            "TMPDIR": str(home), "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", key_names[0]: key}
        if base := values.get(base_name):
            url = urlsplit(base)
            if url.username or url.password or url.query or url.scheme not in {"https", "http"}:
                raise ValueError("provider endpoint must not contain URL credentials or query parameters")
            env[base_name] = base
        cfg = home / "config.yaml"
        cfg.write_text(json.dumps(config))
        cfg.chmod(0o600)
        adapter = HermesSubprocessProvider(provider, executable=self.executable, cwd=str(cwd), extra_env=env,
            toolsets="web", inherit_env=False)
        adapter.configure(provider_id=spec.provider_id, model_id=spec.model_id)
        return adapter

    def __call__(self, provider, workspace, job_id):
        return self.make(provider, job_id, workspace.path)

    def redact_payload(self, value):
        return redact_text(value, extra_values=self._secrets)

    def available_providers(self):
        values = {**dotenv_values(self.credentials_path), **self.environ}
        return {name for name, (keys, _) in PROVIDER_ENV.items() if any(values.get(k) for k in keys)}
