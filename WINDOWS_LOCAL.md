# PRIMER-LLM Windows 原生本地运行（开发模式）

本文说明如何在 **Windows 11 原生环境**（不用 WSL、Docker、虚拟机）里，用已有的 Conda 环境 `cg` 和本机 Windows 版 Ollama 跑通 PRIMER-LLM 的核心流程：

上传 CARPL/RIS → 解析与校验 → LLM 分级 → 保存 → 数据查看 → 报表 → CSV 导出

> 这是**单机开发/复现模式**，只用于仓库自带的合成测试数据。它不等同于 Docker/Linux 生产部署，安全隔离更弱（见“开发模式的限制”）。不要导入真实患者数据。

目标机器：Windows 11 Home 64 位，Intel Core 7 240H，32 GB 内存，RTX 5060 Laptop GPU（约 8 GB 显存），约 60 GB 可用磁盘，已装 Windows 版 Ollama，已有 Conda 环境 `cg`。

---

## 1. 快速开始

下面所有命令都在 **Anaconda Prompt**（或已执行过 `conda init` 的 cmd/PowerShell）里、仓库根目录下执行。

```bat
conda activate cg
```

```bat
cd 你的路径\cxr-audit-site
```

1. **检查依赖（只检查，不安装）**

   ```bat
   windows\check-deps.cmd
   ```

   如果有缺失，脚本会打印只包含缺失/过旧包的最小安装命令（见第 4 节），先用 `--dry-run` 预览再安装。

2. **准备模型**（只需一次，约 2.5 GB）

   ```bat
   ollama pull qwen3:4b
   ```

3. **初始化**：生成本地配置和随机密钥，迁移 SQLite，收集静态文件，初始化上传格式，发布内置报表布局

   ```bat
   windows\setup.cmd
   ```

4. **创建管理员**（用户名和密码在控制台输入，密码不回显，不会出现在命令行或日志里）

   ```bat
   windows\create-admin.cmd
   ```

5. **启动**（只监听 127.0.0.1）

   ```bat
   windows\start.cmd
   ```

   然后用浏览器打开 <http://127.0.0.1:8000/> 并登录。

6. **真实推理冒烟测试**（调用本机 Ollama，不是 mock）

   ```bat
   windows\llm-smoke.cmd
   ```

7. **合成数据端到端测试**（需要服务已启动；会提示输入管理员用户名和密码）

   ```bat
   windows\e2e-synthetic.cmd
   ```

8. **停止**

   ```bat
   windows\stop.cmd
   ```

---

## 2. 脚本一览（`windows\` 目录）

| 脚本 | 作用 |
|---|---|
| `check-deps.cmd` | 检查 Python 版本和每个依赖的已装版本/最低版本，给出最小安装方案；**从不自动安装** |
| `setup.cmd` | `init` + `migrate`（主库和审计库）+ `collectstatic` + `configure_uploads` + 发布内置报表布局；可重复运行，不覆盖已有配置和已发布报表 |
| `create-admin.cmd` | 交互式 `createsuperuser` |
| `start.cmd` | 启动前检查（依赖、端口、`manage.py check` 本地模式安全检查、迁移、静态文件、Ollama），再启动 FastAPI 和 Django 并等待健康检查；同时验证内部 API 密钥：带密钥返回 200，不带密钥返回 403 |
| `stop.cmd` | 只停止本启动器启动的进程（核对 PID、进程创建时间和可执行文件路径，防止误杀复用了同一 PID 的其他程序）；如果还有分析任务在运行会拒绝停止，确需强停时用 `stop.cmd --force` |
| `status.cmd` | 进程、端口、`/health`、内部 API 认证、Django 登录页、Ollama 状态，以及最近日志里的错误行数 |
| `doctor.cmd` | 全面诊断：环境、依赖、配置（只显示密钥是否已设置和长度）、Django 检查、迁移、**解析器自检**、服务状态、GPU |
| `ollama-check.cmd` | 检查 `http://127.0.0.1:11434/v1`、模型是否存在、已加载模型的显存占用 |
| `llm-smoke.cmd` | 用应用自己的严格流水线（`strict=True`）对 3 份合成报告做真实推理，校验结构化输出并报告耗时 |
| `e2e-synthetic.cmd` | 通过 HTTP 跑完整流程（见第 7 节） |
| `collect-logs.cmd` | 把脱敏后的日志末尾和 doctor 输出打包成 zip |
| `primer.cmd <命令>` | 上面各脚本的统一入口 |

每个 `.cmd` 都会先检查 `CONDA_DEFAULT_ENV` 是否等于 `cg`，并显式使用 `%CONDA_PREFIX%\python.exe`；Python 侧还会再检查一次 `sys.prefix` 是否等于 `CONDA_PREFIX`。任一条件不满足就停止，并提示先执行 `conda activate cg`。所有子进程（Django、FastAPI、解析器、迁移、自检）都用这个解释器的 `sys.executable` 启动。

---

## 3. 配置与数据位置

| 内容 | 位置 |
|---|---|
| 本地配置（含密钥） | `%LOCALAPPDATA%\PRIMER-LLM\config\local.env` |
| SQLite 主库 / 审计库 | `%LOCALAPPDATA%\PRIMER-LLM\db\db.sqlite3`、`audit.sqlite3` |
| 媒体、静态文件、审计附件、报表定义、快照 | `%LOCALAPPDATA%\PRIMER-LLM\{media,static,audit_artifacts,private}` |
| 邮件（写成文件，不真正发送） | `%LOCALAPPDATA%\PRIMER-LLM\mail` |
| 日志、PID 记录 | `%LOCALAPPDATA%\PRIMER-LLM\{logs,run}` |

这些都在 Git 仓库之外。想换位置，可以在运行脚本前设置 `PRIMER_LOCAL_HOME`。想完全重置，先 `stop.cmd`，再删除整个数据目录。

**配置不依赖 `.env` 自动生效。** 启动器自己读取 `local.env`，再通过子进程环境变量传给 Django 和 FastAPI；密钥不会出现在命令行参数里，也不会打印出来。仓库根目录的 `.env` 只给 Docker Compose 用，本地模式会忽略它。

`local.env` 里**可以修改**的键（模板见 `windows\local.env.example`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3:4b` | 4b 跑通后再试 `qwen3:8b` |
| `OLLAMA_MAX_WORKERS` | `1` | LLM 并发数；8 GB 显存建议保持 1 |
| `LLM_DISABLE_THINKING` | `False` | `True` 时在提示末尾追加 Qwen3 的 `/no_think` 开关：更快，但分级结果可能变化 |
| `DJANGO_PORT` / `API_PORT` | `8000` / `1221` | 本机端口 |
| `XLSX_DECRYPT_PASSWORD` | 空 | 只在测试加密 XLSX 时需要 |
| `DJANGO_SECRET_KEY` / `API_SECRET_KEY` | init 随机生成 | 想轮换密钥时，删除 `local.env` 后重新运行 `setup.cmd` |

由启动器**固定、不能通过 local.env 覆盖**的值（文件里写了也会被忽略，并给出提示）：

`PRIMER_LOCAL_DEV=True`、`DJANGO_DEBUG=False`、`DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost`、两个数据库都用 SQLite、`CXR_API_IP=127.0.0.1`、FastAPI 绑定 `127.0.0.1`、`API_REQUIRE_AUTH=True`、`OLLAMA_BASE_URL=http://127.0.0.1:11434/v1`、`NO_PROXY` 包含本机地址（防止系统代理截获本机流量）、`PYTHONUTF8=1`（防止中文 Windows 的 GBK 控制台编码让日志里的 emoji 导致崩溃）。

另外，Django 和 FastAPI 自己也有二次防线：只要 `PRIMER_LOCAL_DEV=True`，以下任一情况都会**拒绝启动**（不会静默降级）：主机名/API/LLM 地址不是回环地址、数据库不是 SQLite、Django 仍在使用默认密钥、`API_SECRET_KEY` 为空/是占位值/少于 32 个字符、`API_REQUIRE_AUTH` 不是 True、FastAPI 绑定地址不是回环地址。

---

## 4. 最小依赖安装方案

先运行 `windows\check-deps.cmd`。它用 `importlib.metadata` 读取 `cg` 里已装的版本，并和下面的最低版本比较：

| 包 | 最低版本 | 用途 |
|---|---|---|
| Django | 5.1 | Web |
| whitenoise | 6.0 | 静态文件 |
| requests | 2.31 | Django↔API |
| jsonschema / PyYAML | 4.17 / 6.0 | 报表定义 |
| pandas / numpy | 2.0 / 1.24 | 数据处理 |
| openpyxl / xlrd / msoffcrypto-tool / olefile | 3.1 / 2.0.1 / 5.0 / 0.46 | 解析 CSV/XLSX/XLS |
| scikit-learn / statsmodels / matplotlib | 1.3 / 0.14 / 3.7 | 分析服务导入的统计模块 |
| openai | 1.40 | OpenAI 兼容客户端（结构化输出） |
| tqdm / json-repair | 4.65 / 0.7.0 | 批处理、JSON 修复 |
| fastapi / uvicorn / python-multipart / pydantic | 0.104.1 / 0.24.0 / 0.0.6 / 2.0 | 分析 API |

Python 需要 3.10 及以上；已在云端用 3.11 和 3.13 验证。

脚本只为**缺失或低于最低版本**的包生成命令，例如：

```bat
python -m pip install --dry-run --upgrade-strategy only-if-needed "json-repair>=0.7.0" "fastapi>=0.104.1"
```

```bat
python -m pip install --upgrade-strategy only-if-needed "json-repair>=0.7.0" "fastapi>=0.104.1"
```

- 第一条只预览变化，确认没有意外的大版本升级后再执行第二条。
- `--upgrade-strategy only-if-needed` 是 pip 的默认策略，这里写明是为了避免顺带升级其他已经满足要求的包。
- 如果 `cg` 主要用 conda 管理，也可以用 `conda install` 装同名包；同一个包不要同时用 pip 和 conda 装。
- **不需要**：gunicorn（不支持 Windows，本地模式用 Django 开发服务器）、psycopg（本地模式只用 SQLite）、playwright（仅可选 PDF 功能，见第 8 节）。

---

## 5. Windows 运行障碍与处理方式

| 障碍（原代码） | 本地模式的处理 |
|---|---|
| 解析器在 Docker 中通过 **Unix socket** 与无网络的 parser 容器通信 | 本地不设置 `PARSER_SOCKET_PATH`，走仓库已有的**子进程解析**路径：每个文件一个 `python -I` 子进程，只传入 `SYSTEMROOT/WINDIR/PATH/TEMP/TMP/XLSX_DECRYPT_PASSWORD` 这几个环境变量（不传 API 密钥和 Django 密钥），超时 25 秒，总时长 60 秒 |
| Linux 用 **`resource` rlimit + Landlock + seccomp** 限制解析器 | Windows 用 **Job Object**：单进程内存 768 MiB、CPU 时间 20 秒、**禁止创建子进程**（本次新增）、崩溃时不弹对话框（本次新增，避免卡到超时）。如果限制无法生效，解析会直接失败，不会跳过限制继续解析 |
| 文件格式、大小、行数、列数、公式、宏、外部链接、精度等**校验** | 完全不变：同一份 `parser_worker.py` 和 `ingestion` 校验代码。解析失败时拒绝上传，不会退回到 Django 进程内解析 |
| **Gunicorn**（不支持 Windows） | 改用 `manage.py runserver 127.0.0.1:8000 --noreload`，配合 WhiteNoise 提供静态文件 |
| FastAPI **硬编码监听 `0.0.0.0:1221`** | 新增 `--host/--port`（以及 `API_BIND_HOST/API_BIND_PORT`），默认值仍是 `0.0.0.0:1221`，Docker 行为不变；本地模式强制 `127.0.0.1` |
| 硬编码 **`/tmp`**（批处理中间文件） | 改用 `tempfile.gettempdir()`（Linux 上通常仍是 `/tmp`） |
| `DEBUG=False` 时**强制 HTTPS 跳转、HSTS 和 Secure Cookie** | 仅在 `PRIMER_LOCAL_DEV=True` 时，这三项默认关闭（回环地址上的纯 HTTP）；其他安全头保持不变 |
| 静态文件、媒体、报表定义的**默认目录在仓库内** | 新增可选环境变量 `DJANGO_STATIC_ROOT`、`DJANGO_MEDIA_ROOT`、`REPORT_V2_ROOT`、`REPORT_V2_SNAPSHOT_ROOT`、`EMAIL_FILE_PATH`；不设置时默认值不变 |
| **`.env` 只由 Docker Compose 加载** | 启动器读取 `local.env`，通过子进程环境变量传入 |
| **内部 API 认证** | 保持开启并强制要求；密钥比较改为常量时间比较（`hmac.compare_digest`） |
| **数据库**：PostgreSQL 可选 | 本地模式只允许 SQLite（系统检查 E102） |
| **中文 Windows 默认编码 GBK**，日志里 `print` emoji 会抛错 | 所有子进程设置 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` |
| 系统代理可能截获对 127.0.0.1 的请求 | 子进程设置 `NO_PROXY`；启动器自己的健康检查不使用代理 |
| **PDF 导出**依赖 Playwright 自带的 Chromium | 不是核心流程的一部分，见第 8 节 |

---

## 6. 小模型（qwen3:4b）结构化输出兼容性修复

`cxr-audit-api/cxr_audit/helpers.py` 的 `getLLMJSON` 修复了几个会让小模型或新版 SDK 静默出错的问题：

1. **SDK 版本**：原代码调用 `client.beta.chat.completions.parse`，openai 2.x 起这个方法已删除，导致结构化输出**每次都失败**并退回到无约束文本。现在优先用 `client.chat.completions.parse`，旧 SDK 再退回 `beta`。云端已分别用 openai 3.41 和 1.40 验证。
2. **思考内容**：Qwen3 等推理模型可能把 `<think>…</think>` 混进正文。回退路径会去掉思考块（包括没有闭合的思考块）和 Markdown 代码围栏，再提取 JSON 对象。
3. **schema 校验**：原回退路径没有按 Pydantic schema 校验，还用到了没有 import 的 `re`。现在回退结果必须通过同一个严格 schema（例如 `grade` 必须是 1–6 的整数，`"3"` 这种字符串不接受；各项 findings 必须是真正的布尔值），否则发一次修复请求；仍不合格就报错，由严格流水线把任务标记为失败，**不会把无效结果写入数据库**。
4. **可选 `/no_think`**：`LLM_DISABLE_THINKING=True` 时在最后一条用户消息末尾追加 Qwen3 的 `/no_think` 开关。默认关闭。
5. 模型的原始输出**不会打印**（报告文本可能含 ePHI）。

结构化输出需要较新的 Ollama（支持 OpenAI 兼容接口的 `response_format` JSON schema）。RTX 50 系列显卡也需要较新的 Ollama 才能用上 GPU，建议安装最新版。`llm-smoke.cmd` 会报告回退次数：持续出现回退说明 Ollama 版本较旧或模型不遵守 schema，此时可以先试 `LLM_DISABLE_THINKING=True`。

**资源建议**：`qwen3:4b` 模型文件约 2.5 GB，`qwen3:8b` 约 5.2 GB，都能放进 8 GB 显存（需同时考虑上下文占用）。并发保持 1。`ollama-check.cmd` 会显示已加载模型的显存占比，低于 100% 说明有部分层在 CPU 上跑，速度会明显变慢。不要下载 32B 模型。

---

## 7. 端到端测试内容（`e2e-synthetic.cmd`）

只上传 `docs/test-data/uploader/` 里的合成文件：

1. 超级用户登录；Django → FastAPI 带密钥连通；FastAPI → LLM 连通，并判断 LLM 是真实端点还是 mock
2. 校验仍然生效：分数 >100、非整数登记号、冲突报告都会被排除并带错误码（同一上传里的其他有效研究继续处理）；不支持的扩展名、超过 49 MB 的上传、没有 CSRF 令牌的 POST 都会被拒绝
3. 预检：CARPL CSV + RIS XLSX、CARPL CSV + RIS CSV 都应识别出 6 个完整研究
4. 提交 → FastAPI 用 LLM 分级（含补充 findings）→ 后台线程取回结果 → 6 条新记录写入 SQLite
5. 数据查看列表和详情页，查看器 CSV 导出包含 6 个合成登记号，LLM 分级在 1–6 之间
6. report_v2 报表列表和 overview 页面；三种报表 CSV 导出（full / false_negatives / false_positives，使用页面上的签名 context 令牌）；旧版报表生成和 CSV 导出

可以重复运行：数据已存在时会跳过重新分级，其他检查照常执行。

---

## 8. PDF 导出（可选）

- 普通报表页面上的“打印/PDF”是**浏览器打印 HTML 页面**（`/report/.../print/...` 和旧版的 `download-pdf/`），用浏览器自带的“另存为 PDF”即可，**不需要 Chromium 或其他组件**。本次没有在 Windows 浏览器里实际验证打印效果。
- 只有 **Agent API 的服务端 PDF**（`agent_api/pdf.py`，默认关闭）需要 `playwright` 以及另外下载的 Chromium（`python -m playwright install chromium`，约 150 MB 以上）。按要求没有安装它，因此这一功能**未验证**；不装它不影响上传、分级、查看、报表和 CSV。云端环境同样没有 Chromium，仓库里相关的 4 个 PDF/浏览器测试在云端也无法运行（修改前后都一样）。

---

## 9. 开发模式的限制（与生产部署不等价）

如实说明，在不额外安装系统软件的前提下，以下隔离在 Windows 本地模式中**无法保留**：

1. **解析器隔离较弱**：Docker 版的 parser 容器没有网络（`network_mode: none`）、文件系统只读，Linux 版子进程还有 Landlock 和 seccomp。Windows 子进程只有 Job Object 的内存、CPU、进程数限制以及精简后的环境变量，**仍然可以读取当前用户能读的文件，也可以访问网络**。解析代码本身是纯 Python（csv/openpyxl/xlrd），且只处理合成数据，但这不等同于生产隔离。
2. **传输未加密**：浏览器↔Django、Django↔FastAPI、FastAPI↔Ollama 都是回环地址上的 HTTP，没有 TLS；Session/CSRF Cookie 没有 Secure 标记。只监听 127.0.0.1，但本机其他进程都能访问这些端口。
3. **Ollama 本身没有认证**：本机任何进程都能调用 `127.0.0.1:11434`。
4. **Django 开发服务器**不适合生产，只有单进程；FastAPI 任务状态保存在进程内存里，停止 API 后正在运行的任务会丢失。
5. **密钥以明文保存在** `%LOCALAPPDATA%\PRIMER-LLM\config\local.env`，只靠当前 Windows 用户目录的权限保护。
6. **日志**会记录请求路径和合成登记号；`collect-logs` 会做密钥脱敏，但分享前仍需人工检查。
7. 没有 Nginx、没有容器资源配额、没有 PostgreSQL；审计日志写入本地 SQLite。

---

## 10. 验证状态

### 已在云端验证（Linux 隔离环境，**不是**你的电脑，也不是你的 `cg`）

- Django 全量测试：617 个（新增 14 个）。失败 1、错误 5，与修改前的基线**完全相同**，都是已有问题：4 个需要 Playwright/Chromium，2 个是该云端内核的 Landlock 规则拦截了 `/etc/mime.types`，导致 Linux 解析器处理 XLSX 失败。Windows 不走 Landlock，不受这个问题影响。
- FastAPI 测试 29 个、启动器测试 11 个全部通过；Python 3.11 + openai 1.40 + FastAPI 0.104.1 + Django 5.1 这套较旧版本组合也都通过。
- **用 mock LLM 跑应用流程**（`windows/tests/cloud_e2e_mock.py`，mock 会标明自己是 `primer-mock-llm`）：setup → 启动 → 状态 → 重复启动 → e2e 25/25（重复运行 21/21）→ smoke（标记为 MOCK）→ collect-logs → 停止。在 Python 3.13/openai 3.41 和 Python 3.11/openai 1.40 下都通过，另外用“思考内容泄漏到正文”的模式验证了回退路径。诊断 zip 和服务日志里都没有出现密钥或管理员密码。
- 在不启用 Landlock、只施加资源限制的情况下（也就是 Windows 解析器的工作方式），解析器能正确解析合成 XLSX（6 行）。

**mock 通过只说明应用流程正确，不代表真实 Ollama 推理通过。**

### 未验证

- 任何 Windows 实机行为：`.cmd` 脚本、Job Object 实际生效、`taskkill`、Windows 进程身份核对、中文路径/编码、Windows Defender 或防火墙弹窗
- 真实的 Ollama 和 `qwen3:4b`/`qwen3:8b` 推理、速度、显存占用、分级质量
- 云端：XLSX 端到端预检（受上面的 Landlock 问题影响）
- 加密 XLSX、.xls 文件
- Agent API 服务端 PDF（Playwright/Chromium）；浏览器打印效果

### 需要你在本机验证（按顺序执行，结果全部为 OK/PASS 才算通过）

1. `windows\check-deps.cmd`（按提示安装缺失依赖）
2. `windows\setup.cmd`，然后 `windows\create-admin.cmd`
3. `windows\doctor.cmd`：解析器自检的所有项都应是 PASS，尤其是 **“child process creation blocked (Job Object)”** 和 **“1.5 GiB allocation blocked”**，这两项直接证明 Windows 资源限制已经生效
4. `windows\start.cmd`，然后 `windows\status.cmd`
5. `windows\llm-smoke.cmd`：输出必须是 `PASS (REAL (local endpoint))`；同时记下每份报告的耗时和回退次数
6. `windows\e2e-synthetic.cmd`：输出中“LLM endpoint type”必须是 REAL，结果全部通过
7. 在浏览器里手动走一遍上传页面、查看页面和报表页面
8. `windows\stop.cmd`
9. 如果有失败：运行 `windows\collect-logs.cmd`，检查 zip 内容后再分享

---

## 11. 故障排查

| 现象 | 处理 |
|---|---|
| `ERROR: the Conda environment "cg" is not active` | 先执行 `conda activate cg`，并且在同一个窗口里运行脚本 |
| 端口被占用 | 提示里会给出占用进程的 PID，可以用 `tasklist /FI "PID eq <PID>"` 查看是什么程序；也可以在 `local.env` 里改端口 |
| `Ollama 未响应` | 启动 Windows 版 Ollama（托盘图标），然后运行 `ollama-check.cmd` |
| `未找到模型` | 执行 `ollama pull qwen3:4b` |
| `manage.py check` 报 `lunit_audit.E10x` | 本地模式安全检查未通过；不要修改启动器固定的值，必要时删除 `local.env` 后重新运行 `setup.cmd` |
| llm-smoke 显示“strict pipeline rejected” | 更新 Ollama；试 `LLM_DISABLE_THINKING=True`；确认模型已经完整下载 |
| 解析器自检中 Job Object 相关项失败 | 运行 `collect-logs.cmd` 并保留 zip；此时上传会失败（按设计不会绕过限制） |
| 关闭终端后服务停止 | 某些终端会在关闭时结束子进程；请保持终端窗口打开，或重新运行 `start.cmd` |
| 想从头再来 | `stop.cmd`，删除 `%LOCALAPPDATA%\PRIMER-LLM`，再从 `setup.cmd` 开始 |

---

## 12. 与 Docker/Linux 部署的关系

所有改动都需要显式开启：不设置 `PRIMER_LOCAL_DEV`、`API_BIND_HOST`、`DJANGO_STATIC_ROOT` 等新增变量时，Django 设置、FastAPI 监听地址（`0.0.0.0:1221`）、Unix socket 解析服务、Gunicorn 入口和 Compose 文件的行为都和修改前一致。有测试专门断言了默认部署设置没有变化。
