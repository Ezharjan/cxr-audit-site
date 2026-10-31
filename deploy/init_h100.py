"""Generate a fresh Linux server .env, without printing secrets or replacing files."""
import argparse
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parents[1]


def build_env(template, hostname, revision):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", hostname):
        raise ValueError("hostname must be a DNS name or IPv4 address, without scheme or port")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("model revision must be a full 40-character Hugging Face commit SHA")
    app_password, audit_password = secrets.token_hex(32), secrets.token_hex(32)
    values = {
        "DJANGO_SECRET_KEY": secrets.token_hex(48),
        "API_SECRET_KEY": secrets.token_hex(32),
        "OLLAMA_API_KEY": secrets.token_hex(32),
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "DATABASE_PASSWORD": app_password,
        "POSTGRES_APP_PASSWORD": app_password,
        "AUDIT_DATABASE_PASSWORD": audit_password,
        "POSTGRES_AUDIT_PASSWORD": audit_password,
        "DJANGO_ALLOWED_HOSTS": f"{hostname},localhost,127.0.0.1",
        "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{hostname}",
        "FASTAPI_CORS_ORIGINS": f"https://{hostname}",
        # Create the administrator interactively after startup.
        "DJANGO_SUPERUSER_USERNAME": "",
        "DJANGO_SUPERUSER_PASSWORD": "",
        "DJANGO_SUPERUSER_EMAIL": "",
        "OLLAMA_BASE_URL": "http://llm:8000/v1",
        "OLLAMA_MODEL": "Qwen/Qwen3-32B",
        "OLLAMA_MAX_WORKERS": "8",
        "LLM_ALLOW_INSECURE_TRANSPORT": "True",
        "XLSX_DECRYPT_PASSWORD": "",
        "EMAIL_HOST": "",
        "EMAIL_HOST_USER": "",
        "EMAIL_HOST_PASSWORD": "",
    }
    lines = []
    for line in template.splitlines():
        key = line.partition("=")[0]
        lines.append(f"{key}={values[key]}" if key in values else line)
    lines.extend([
        "", "# Linux H100 overlay (colon is the Linux Compose path separator).",
        "COMPOSE_FILE=docker-compose.yml:docker-compose.h100.yml",
        "VLLM_IMAGE=vllm/vllm-openai:v0.11.0",
        "VLLM_MODEL=Qwen/Qwen3-32B",
        f"VLLM_MODEL_REVISION={revision.lower()}",
        "VLLM_MAX_MODEL_LEN=8192", "VLLM_MAX_NUM_SEQS=8",
        "VLLM_GPU_MEMORY_UTILIZATION=0.90", "LLM_DISABLE_THINKING=True",
    ])
    return "\n".join(lines) + "\n"


def write_new(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--model-revision", required=True)
    args = parser.parse_args()
    try:
        content = build_env((ROOT / ".env.example-prod").read_text(encoding="utf-8"),
                            args.hostname, args.model_revision)
        write_new(ROOT / ".env", content)
    except (ValueError, FileExistsError) as exc:
        parser.exit(1, f"No configuration written: {exc}\n")
    print("Created .env. Configure TLS and optional email/Excel settings; see docs/H100_SERVER.md.")


if __name__ == "__main__":
    main()
