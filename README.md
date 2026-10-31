# 🫁 PRIMER-LLM — PRIME Review-LLM

**Automated chest X-ray report analysis powered by Large Language Models.**

**Deploying on an NVIDIA H100 server?** Follow the [H100 server guide](docs/H100_SERVER.md)
for the vLLM Compose profile, GPU setup, generated secrets, PostgreSQL, TLS and
verification steps. Ollama is not required for that deployment.

PRIMER-LLM is a medical imaging AI audit system that compares AI-generated predictions (from [Lunit INSIGHT CXR](https://www.lunit.io/en/products/insight-cxr)) against radiologist ground truth reports. It uses LLMs to grade radiology reports on a standardised R1–R5 scale, then generates accuracy metrics, false negative/positive identification, and statistical reports — enabling continuous quality assurance of AI-assisted chest X-ray workflows.

---

## 📋 Table of Contents

- [Features](#-features)
- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Quick Start (Docker)](#-quick-start-docker)
- [Local Development](#-local-development)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Grading System](#-grading-system)
- [Project Structure](#-project-structure)
- [API Reference](#-api-reference)
- [Authentication & Access Control](#-authentication--access-control)
- [License](#-license)

---

## ✨ Features

- **LLM-Powered Report Grading** — Automatically grades radiology reports on an R1–R5 severity scale using configurable LLM backends (Ollama, OpenAI-compatible APIs)
- **Multiple Grading Methods** — Semi-algorithmic, direct LLM, hybrid, and LLM-as-judge approaches
- **Accuracy Metrics** — Confusion matrix, sensitivity, specificity, PPV, NPV, ROC-AUC with per-site breakdowns
- **False Negative / Positive Detection** — Identifies discrepancies between LLM grades and manual ground truth
- **Trend Analysis** — Weekly sensitivity, specificity, and ROC-AUC trends with 95% CI
- **Manual vs LLM Comparison** — Cohen's kappa, McNemar's test, and agreement analysis between manual and LLM ground truth
- **Time Analysis** — Turnaround time statistics (median, percentiles, outliers)
- **Background Processing** — Long-running LLM tasks survive page navigation; results are auto-saved
- **Multi-Site Support** — Site-specific Lunit score thresholds and per-site reporting
- **Role-Based Access Control** — Login-protected with admin-only views for database management and task monitoring
- **CSV/Excel Import & Export** — Import historical data and export filtered results
- **Docker Deployment** — Production-ready with Nginx (HTTPS), Gunicorn, and persistent volumes

---

## 🏗 Architecture

PRIMER-LLM is a three-tier application:

```
┌──────────────────────────────────────────────────────────────┐
│                     Nginx (HTTPS)                            │
│                   Port 80 → 443 redirect                     │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│              Django Web App (Gunicorn)                        │
│                    Port 8000                                  │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │
│  │  Upload   │  │  Viewer  │  │  Report  │  │  Admin Panel │ │
│  │ /upload/  │  │  /view/  │  │ /report/ │  │   /admin/    │ │
│  └────┬─────┘  └──────────┘  └──────────┘  └──────────────┘ │
│       │              │             │                          │
│       │         SQLite DB ◄────────┘                         │
│       │        (CXRStudy)                                    │
└───────┼──────────────────────────────────────────────────────┘
        │ proxies file uploads
┌───────▼──────────────────────────────────────────────────────┐
│            FastAPI Analysis API (Uvicorn)                     │
│                    Port 1221                                  │
│                                                              │
│  ┌─────────────────┐   ┌────────────────────────────────┐    │
│  │ ProcessCarpl    │   │  CXR Grading Engine            │    │
│  │ (data pipeline) │──▶│  • Semi-algorithmic grading    │    │
│  │                 │   │  • LLM grading (R1–R5)         │    │
│  │                 │   │  • Hybrid grading              │    │
│  │                 │   │  • Supplemental findings       │    │
│  └─────────────────┘   └──────────┬─────────────────────┘    │
└───────────────────────────────────┼──────────────────────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │    Ollama LLM Server         │
                     │    (e.g. qwen3:32b)          │
                     │    Port 11434                 │
                     └─────────────────────────────┘
```

| Component | Technology | Role |
|-----------|-----------|------|
| **Web Frontend** | Django 6 + vanilla JS | Upload UI, database viewer, report dashboard |
| **Analysis API** | FastAPI + Uvicorn | File processing, LLM grading pipeline |
| **LLM Backend** | Ollama (OpenAI-compatible) | Report grading and findings extraction |
| **Reverse Proxy** | Nginx | HTTPS termination, request proxying |
| **Database** | SQLite | Study records, task tracking |

---

## 📦 Prerequisites

- **Docker** and **Docker Compose** (recommended for deployment)
- An **OpenAI-compatible inference endpoint**: use the [vLLM H100 profile](docs/H100_SERVER.md)
  on an H100 server, or an existing endpoint such as Ollama for local development.

For local development without Docker:
- Python 3.11+
- An Ollama instance or any OpenAI-compatible API endpoint

---

## 🚀 Quick Start (Docker)

### 1. Clone and configure

```bash
git clone <repo-url> cxr-audit-site
cd cxr-audit-site
cp .env.example .env
```

Edit `.env` with your settings:

```dotenv
DJANGO_SECRET_KEY=your-secure-random-key
DJANGO_SUPERUSER_USERNAME=admin
DJANGO_SUPERUSER_PASSWORD=your-secure-password
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_MODEL=qwen3:32b-q4_K_M
```

### 2. Generate TLS certificates (development)

```bash
cd nginx && bash generate-certs.sh && cd ..
```

> For production, replace the self-signed certs in `nginx/certs/` with real certificates (e.g. from Let's Encrypt).

### 3. Start the stack

```bash
docker compose up --build -d
```

The base stack starts these five containers (the H100 profile also adds vLLM):

| Container | Port | Description |
|-----------|------|-------------|
| `primer-nginx` | 80, 443 | Reverse proxy (HTTPS) |
| `primer-django` | 8000 (internal) | Web application |
| `primer-api` | 1221 (internal) | Analysis API |
| `primer-parser` | Unix socket only | Isolated spreadsheet parser |
| `primer-postgres` | 5432 (internal) | Application and audit databases when selected |

### 4. Access the application

Open **https://localhost** and log in with the superuser credentials from your `.env`.

---

## 🛠 Local Development

### FastAPI backend

```bash
cd cxr-audit-api
pip install -r api_requirements.txt
python combined_server.py          # API on :1221, Static on :1220
```

### Django frontend

```bash
cd django-app
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver         # Dev server on :8000
```

> **Note:** The Django app expects the FastAPI backend to be running. Configure the API address via environment variables `CXR_API_IP` and `CXR_API_PORT` (defaults: `localhost:1221`).

### Running tests

```bash
cd cxr-audit-api
python -m unittest unittests.py
```

---

## ⚙ Configuration

### Environment Variables

All configuration is via environment variables (see [.env.example](.env.example)):

| Variable | Default | Description |
|----------|---------|-------------|
| `DJANGO_SECRET_KEY` | — | Django secret key (**required** in production) |
| `DJANGO_DEBUG` | `False` | Enable debug mode |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://localhost` | CSRF trusted origins |
| `DJANGO_SUPERUSER_USERNAME` | — | Auto-created superuser username |
| `DJANGO_SUPERUSER_PASSWORD` | — | Auto-created superuser password |
| `GUNICORN_WORKERS` | `3` | Number of Gunicorn workers |
| `GUNICORN_TIMEOUT` | `120` | Gunicorn request timeout (seconds) |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434/v1` | LLM server URL |
| `OLLAMA_MODEL` | `qwen3:32b-q4_K_M` | LLM model name |
| `OLLAMA_API_KEY` | `dummy` | API key (Ollama ignores this) |
| `OLLAMA_MAX_WORKERS` | `8` | Max concurrent LLM requests |
| `NGINX_HTTP_PORT` | `80` | Nginx HTTP port |
| `NGINX_HTTPS_PORT` | `443` | Nginx HTTPS port |

### Site-Specific Thresholds

Lunit score thresholds are configured in the FastAPI backend (`combined_server.py`). Default thresholds apply to all sites, with per-site overrides:

```python
thresholds = {
    'default': {'Nodule': 15, ...},  # Score ≥ threshold → flagged
    'YIS':     {'Nodule': 5, ...}    # Lower threshold for YIS site
}
```

---

## 📖 Usage

### Upload & Analyse

1. Navigate to **/upload/**
2. Upload CARPL CSV files (Lunit AI scores) and GE/RIS Excel files (ground truth reports)
3. The system auto-categorises files by type, merges on accession number, and runs LLM grading
4. Processing runs in the background — you can navigate away and return later

### View Database

Navigate to **/view/** (admin only) to browse, search, filter, inline-edit, and export all CXR study records.

### Generate Reports

1. Navigate to **/report/**
2. Select a date range
3. View accuracy metrics, per-site breakdowns, trend charts, false negatives/positives, and time analysis
4. Export results as CSV

---

## 🏥 Grading System

Reports are graded on an **R1–R5 scale**:

| Grade | Classification | Description | Binary |
|-------|---------------|-------------|--------|
| **R1** | Normal | No findings | 0 |
| **R2** | Normal variant | Minor pathology, no follow-up needed | 0 |
| **R3** | Abnormal | Non-urgent follow-up (atelectasis, stable nodules, cardiomegaly) | 1 |
| **R4** | Abnormal | Potentially important (new consolidation, malignancy suspicion) | 1 |
| **R5** | Critical | Urgent action required (pneumothorax, aortic dissection) | 1 |

**Binary conversion:** Grades 1–2 → **Normal (0)**, Grades 3–5 → **Abnormal (1)**

### Grading Methods

| Method | Description |
|--------|-------------|
| **Semi-Algorithmic** | Extracts findings via structured LLM call, then calculates priority using dictionary lookups with temporal/uncertainty modifiers |
| **LLM Direct** | Single-prompt LLM grading with a detailed rubric |
| **Hybrid** | Semi-algorithmic grade passed as context to LLM for re-evaluation |
| **LLM-as-Judge** | LLM compares and adjudicates between algorithmic and direct LLM grades |

### Supplemental Findings Extraction

The system also extracts 11 Lunit-compatible boolean findings from each report: atelectasis, calcification, cardiomegaly, consolidation, fibrosis, mediastinal widening, nodule, pleural effusion, pneumoperitoneum, pneumothorax, and pulmonary tuberculosis.

---

## 📁 Project Structure

```
django/
├── .env.example                    # Environment variable template
├── docker-compose.yml              # Three-service stack definition
│
├── nginx/                          # Reverse proxy
│   ├── nginx.conf                  # HTTPS config with proxy rules
│   └── generate-certs.sh           # Self-signed cert generator
│
├── django-app/                     # Django web application
│   ├── Dockerfile
│   ├── entrypoint.sh               # Migrations, collectstatic, Gunicorn
│   ├── requirements.txt
│   ├── manage.py
│   ├── lunit_audit/                # Django project settings
│   │   ├── settings.py
│   │   ├── urls.py
│   │   └── wsgi.py
│   ├── upload/                     # File upload & processing app
│   │   ├── models.py               # CXRStudy, ProcessingTask, UploadedFile
│   │   ├── views.py                # Upload proxy, background workers, CSV import
│   │   ├── context_processors.py   # Admin status for templates
│   │   └── templates/upload/
│   ├── viewer/                     # Database browser app (admin only)
│   │   ├── views.py                # Paginated table, inline edit, export
│   │   └── templates/viewer/
│   ├── report/                     # Analysis report app
│   │   ├── views.py                # Metrics, trends, FN/FP, exports
│   │   ├── static/report/          # JS (report.js, charts.js), CSS
│   │   └── templates/report/
│   └── templates/                  # Shared templates
│       ├── base.html               # Layout, nav, CSS variables
│       └── registration/
│           └── login.html
│
├── cxr-audit-api/                  # FastAPI analysis backend
│   ├── Dockerfile
│   ├── api_requirements.txt
│   ├── combined_server.py          # FastAPI server (port 1221)
│   ├── class_process_carpl.py      # Data processing pipeline
│   ├── open_protected_xlsx.py      # Password-protected Excel reader
│   ├── cxr_audit/                  # LLM grading engine
│   │   ├── lib_audit_cxr_v2.py     # CXRClassifier (4 grading methods)
│   │   ├── grade_batch_async.py    # Concurrent batch processor
│   │   └── prompts.py              # R1–R5 rubric & prompt templates
│   ├── padchest_op.json            # Medical findings dictionary
│   ├── padchest_tubes_lines.json   # Tubes/lines classifications
│   └── diagnoses.json              # Diagnosis mappings
│
└── documentation/                  # Development session notes
```

---

## 🔌 API Reference

The FastAPI backend exposes the following endpoints on port **1221**:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check and endpoint listing |
| `/analyze-auto-sort` | POST | Upload mixed CSV/Excel files for auto-categorisation and analysis |
| `/analyze-multiple` | POST | Upload pre-categorised CARPL + GE file sets |
| `/status/{task_id}` | GET | Poll processing progress (percentage, current step) |
| `/results/{task_id}` | GET | Fetch completed analysis (metrics, CSV data, false negatives) |
| `/tasks` | GET | List all processing tasks |
| `/tasks/{task_id}` | DELETE | Remove a task |
| `/docs` | GET | Interactive Swagger API documentation |

---

## 🔐 Authentication & Access Control

All pages require login. Access is role-based:

| Route | Regular User | Admin | Superuser |
|-------|:----------:|:-----:|:---------:|
| `/upload/` | ✅ | ✅ | ✅ |
| `/report/` | ✅ | ✅ | ✅ |
| `/upload/tasks/` | ❌ | ✅ | ✅ |
| `/view/` (Database) | ❌ | ✅ | ✅ |
| `/admin/` | ❌ | ❌ | ✅ |

**Admin users** are members of the Django `admins` group. Manage users via the Django admin panel (`/admin/`) or the management shell:

```bash
python manage.py createsuperuser

python manage.py shell
>>> from django.contrib.auth.models import User, Group
>>> admins, _ = Group.objects.get_or_create(name='admins')
>>> user = User.objects.create_user('alice', password='...')
>>> user.groups.add(admins)  # promote to admin
```

---

## 📄 License

PRIMER is released under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.
The full license text is available in the [LICENSE](../LICENSE) file at the project root, or at <https://www.gnu.org/licenses/agpl-3.0.html>.

In short:
- You are free to use, modify, and redistribute this software.
- If you distribute modified versions — or run a modified version as a network service (e.g. hosting the FastAPI server or Django app for third parties) — you **must** make the complete corresponding source code of your modified version available to its users under the same AGPL-3.0 terms.
- The software is provided "as is", without warranty of any kind.

If the AGPL-3.0 is not compatible with your intended use (e.g. integration into proprietary clinical software), please contact the authors to discuss alternative licensing arrangements.

### Citation

If you use PRIMER in academic work, please cite us!


