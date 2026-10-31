# Single-H100 server deployment

Use **vLLM in Docker**, with **Qwen/Qwen3-32B** in BF16, on the supplied H100 80 GB.
Ollama is optional and is not installed in this setup. `OLLAMA_*` are legacy
application variable names for an OpenAI-compatible endpoint, not an Ollama requirement.
The BF16 weights occupy approximately 61 GiB; the remaining GPU memory supports
runtime overhead and the KV cache. Start with 8,192 tokens and eight concurrent
sequences. This is a starting configuration, not a measured capacity guarantee.
Changing from the previous quantized model requires checking grading agreement on
your approved validation set before using its results clinically.

The supplied driver (580.173.02) should support this CUDA 12.8 container baseline.
`nvidia-smi`'s CUDA 13.0 label describes driver capability; it does not require you
to install a CUDA 13 toolkit. Keep the working driver. No host PyTorch, CUDA toolkit,
Ollama, or host vLLM installation is needed.

This guide assumes **Ubuntu 22.04/24.04, x86-64, sudo access**, and an otherwise
unused GPU. The GPU output alone does not establish the OS, RAM, disk, or network
policy. Aim for 128 GB host RAM and at least 150 GB free Docker storage initially,
plus storage for your database, uploads, exports and backups. These are planning
allowances, not hard minimums. Use your administrator's installation process for
other operating systems or managed compute instances.

## 1. Check the host and repository

```bash
cd ~/Project2PRIMER/cxr-audit-site
cat /etc/os-release
uname -m
free -h
df -h . /var/lib
nvidia-smi
git status --short
```

Transfer/pull the deployment changes from this checkout before continuing. Run
commands from the directory containing `docker-compose.yml`; do not assume a
nested `django/` directory. These instructions provision **new empty databases**.
They do not migrate an existing SQLite deployment or copy patient data.

## 2. Install Docker Engine and Compose

If `sudo docker version` and `sudo docker compose version` already work, retain
the existing installation. Otherwise, on Ubuntu with no conflicting Docker packages:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
```

Use Compose v2 with `up --wait` support. On an existing machine, resolve package
conflicts using the [official Docker instructions](https://docs.docker.com/engine/install/ubuntu/)
rather than removing packages blindly. Commands below use `sudo`, so no Docker
group membership is required.

## 3. Enable GPU access in Docker

Skip installation if the toolkit is already installed and the GPU test succeeds.
Configuring/restarting Docker affects other running containers; schedule this on
a shared server.

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
sudo docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
```

The last command must show the H100. See the
[NVIDIA toolkit guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

## 4. Generate server configuration

Choose the DNS name users will access and make it resolve to the server's reachable
address. Replace `primer.example.org` below. An IPv4 address is also accepted but
the TLS certificate must cover it. Standard ports 80/443 are assumed.

Resolve the model's current revision once and save that immutable SHA in `.env`:

```bash
MODEL_REVISION=$(curl --fail --silent --show-error https://huggingface.co/api/models/Qwen/Qwen3-32B | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')
python3 deploy/init_h100.py --hostname primer.example.org --model-revision "$MODEL_REVISION"
chmod 600 .env
```

The helper refuses to overwrite `.env`. For an existing deployment, back it up
securely and reconcile the H100 settings manually; do not regenerate database
passwords against an initialized PostgreSQL volume. The helper generates separate
application, audit, PostgreSQL administrator, Django and API secrets; paired
database passwords match. An administrator is created interactively later.

The generated `COMPOSE_FILE` selects both Compose files automatically on Linux.
Always run Compose from this repository directory. Do not pass only
`-f docker-compose.yml`, since that excludes the GPU overlay.

Edit `.env` to configure optional SMTP and `XLSX_DECRYPT_PASSWORD` if your input
files need decryption. Email is not configured by the helper. Do not send reports
until the approved email transport is configured. Never commit `.env` or post
rendered Compose configuration containing its secrets.

The profile pins `vllm/vllm-openai:v0.11.0` as a documented CUDA 12.8 compatibility
baseline, **not a claim that it is the newest release or security-cleared**. Review
images with your deployment vulnerability process. Keep upgrades deliberate and
repeat the smoke/validation checks after changes. The model SHA is immutable;
for exact image replication also record/pin the image digest after pulling.
Existing application Python requirements and other base image tags remain partly
unpinned: this is a repeatable setup procedure, not a bit-for-bit locked build.

## 5. Install TLS certificates and restrict access

Nginx expects these files, including the certificate chain in `cert.pem`:

```bash
mkdir -p nginx/certs
sudo install -m 644 /path/to/fullchain.pem nginx/certs/cert.pem
sudo install -m 600 /path/to/privkey.pem nginx/certs/key.pem
```

Obtain the certificate through your institution's CA or existing certificate
automation. Copy actual files, not symlinks to locations outside the mounted
directory. On renewal replace them and run `sudo docker compose exec nginx nginx -s reload`.
For a disposable, non-PHI test only, `bash nginx/generate-certs.sh` creates a
self-signed localhost certificate; it will not validate for your server DNS name.

Allow inbound HTTPS from the approved VPN/client network, HTTP for redirects if
needed, and SSH only from administrators. Do not open 8000, 1221, 5432 or 11434.
Docker-published ports can bypass UFW rules: enforce the cloud/network policy or
Docker-aware host rules as described in Docker's installation documentation.

## 6. Download the model and start the stack

```bash
sudo docker compose config --quiet
sudo docker compose --profile download run --rm --no-deps model-download
sudo docker compose build --pull
sudo docker compose up -d --wait --wait-timeout 1800
sudo docker compose ps
nvidia-smi
```

Downloading weights and images needs outbound access and may take considerable
time. Downloads are cached in the `llm-cache` Docker volume and can be retried.
The running inference container mounts it read-only and uses offline mode.
The one-shot downloader is not a continuously running service and has no patient
data mounts or application secrets.

The stack contains Nginx, Django, FastAPI, the isolated parser, PostgreSQL, and vLLM.
FastAPI waits for vLLM readiness; Django runs application and audit migrations.
First model load/compilation can take several minutes. If startup times out, inspect
`sudo docker compose logs --tail=100 llm` before retrying `up`; do not delete volumes.
Only Nginx publishes host ports. API-to-vLLM HTTP stays on the internal same-host
bridge, with an API key. This scoped transport choice is documented here and
enables `LLM_ALLOW_INSECURE_TRANSPORT=True`; Django will still emit W001.
For inference on a different host use authenticated HTTPS instead.

## 7. Create the administrator and verify

```bash
sudo docker compose exec --user appuser django python manage.py createsuperuser
sudo docker compose exec --user appuser django python manage.py check --deploy
sudo docker compose exec --user appuser django python manage.py check_api_pdf
sudo docker compose exec -T api python - < deploy/smoke_llm.py
curl --fail --silent --show-error -o /dev/null https://primer.example.org/login/
```

Use `curl --cacert /path/to/institution-ca.pem` for a private CA. Review deployment
check findings rather than silencing them. The inference smoke check uses synthetic
input, verifies unauthenticated requests are rejected, verifies model discovery
and exercises the application's JSON helper.
It does not establish clinical correctness or throughput. Log in over HTTPS and
perform a synthetic CSV upload, wait for grading, inspect viewer/report output,
and test manual GT and PDF export. Configure and test email separately if used.
If enabling the public Agent API, first ensure `check_api_pdf` passes.

## Operation and troubleshooting

- **GPU not visible:** rerun the CUDA container test, check toolkit installation and
  GPU permissions. Do not reinstall the working host driver as a first step.
- **Out of GPU memory:** check for other GPU processes. Try `VLLM_MAX_MODEL_LEN=4096`
  and `VLLM_MAX_NUM_SEQS=4`, then `sudo docker compose up -d llm api`. Lowering
  context can reject long reports; validate representative prompt lengths. If
  needed evaluate a smaller model, changing both `VLLM_MODEL` and `OLLAMA_MODEL`,
  resolving its SHA and rerunning the downloader before startup.
- **Model not found/offline cache miss:** rerun the downloader with the same model
  and SHA; verify sufficient Docker disk space. Do not remove offline isolation.
- **401 from LLM:** ensure `.env`'s `OLLAMA_API_KEY` is nonempty and recreate both
  `llm` and `api` after changing it. Ollama model tags such as `qwen3:32b-q4_K_M`
  are not Hugging Face model IDs.
- **403/CSRF or bad host:** host/origin settings must match the actual browser URL.
- **Database authentication failure after editing secrets:** PostgreSQL bootstrap
  variables only initialize empty storage; rotate existing roles with your DBA.
- **Restart:** `sudo docker compose restart`. **Stop:** `sudo docker compose stop`.
  Never use `down -v` on a deployment whose data you need. In-flight FastAPI tasks
  are held in process memory; drain work before upgrades/restarts.

Before processing ePHI, arrange encrypted host/storage volumes, access control,
backups of both PostgreSQL databases and media/audit artifacts, retention policy,
and a tested restore procedure. This deployment does not itself establish HIPAA
compliance. The same-host HTTP exception does not encrypt bridge traffic. Existing
application logs still require access restrictions and review.

Record the Git commit (`git rev-parse HEAD`), model SHA and image digests
(`sudo docker compose images`, `sudo docker image inspect IMAGE --format '{{json .RepoDigests}}'`)
in your deployment record. Preserve approved built application images in your
registry for replication/rollback instead of rebuilding floating dependencies.

## Reference and local validation

- [vLLM Docker deployment](https://docs.vllm.ai/en/v0.11.0/deployment/docker.html)
- [vLLM CUDA compatibility](https://docs.vllm.ai/en/v0.11.0/getting_started/installation/gpu.html)
- [Qwen3-32B model and thinking modes](https://huggingface.co/Qwen/Qwen3-32B)

`python -m unittest discover -s deploy -p test_h100.py -v` checks secret pairing,
input validation, overwrite protection and the inference isolation configuration
(requires PyYAML). Actual Docker builds, GPU load, startup checks and workflow
acceptance must be run on the server; the authoring Windows environment has no
Docker or H100 runtime.
