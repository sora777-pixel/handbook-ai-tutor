# 项目架构与技术说明

本文档面向开发者，说明 Handbook AI Tutor 的整体架构、技术栈选型理由、目录组织与开发规范。
只想「装上就能用」的话看 [README](../README.md) 即可。

---

## 一、项目概述

Handbook AI Tutor 是一个**完全本地运行**的学习助手：把一份 PDF 或 MP4 交给它，它会自动完成

1. 内容解析（PDF 抽文本 / 扫描页 OCR；视频抽音频 + 语音转写）
2. 结构化加工（摘要、大纲、知识点、学习笔记）
3. 练习题生成与批改（选择题 + 翻译 / 写作 / 口语题）
4. 基于原文的 AI 问答（每条回答附页码或时间戳出处）

数据（账号、资料、切片、答题记录）全部保存在使用者本机。

---

## 二、整体架构

```
                    ┌──────────────────────────────┐
   浏览器  ───────► │  Next.js 14 前端 (:3000)     │
   :3000            │  App Router + TanStack Query │
                    └───────────────┬──────────────┘
                                    │ REST / SSE  (:8000)
                    ┌───────────────▼──────────────┐
                    │  FastAPI 后端                │
                    │  api/ ─ services/ ─ models/   │
                    └───┬────────┬─────────┬───────┘
                        │        │         │
          ┌─────────────▼──┐ ┌───▼──────┐ ┌▼──────────────┐
          │ LLM 路由       │ │ 解析流水线 │ │ 存储          │
          │ ModelRouter    │ │ ingest    │ │ Local / MinIO │
          │ providers.yaml │ │           │ │               │
          └───┬────────────┘ └─┬───────┬─┘ └───────────────┘
              │                │       │
      ┌───────▼──────┐  ┌──────▼──┐ ┌──▼─────────────┐
      │ 云端模型供应商 │  │ ffmpeg  │ │ OCR           │
      │ deepseek /    │  │ 抽音频  │ │ rapidocr(离线) │
      │ dashscope /   │  └────┬────┘ │ 或视觉大模型    │
      │ siliconflow / │       │      └────────────────┘
      │ openai / ...  │  ┌────▼─────┐
      │ 或 mock(离线) │  │ STT      │
      └───────────────┘  │ mock /   │
                         │ whisper /│
                         │siliconflow│
                         └──────────┘
```

### 任务处理链路

- **PDF**：`pypdf` 逐页抽文本 → 若某页无文本层（扫描页）则取该页图片 → OCR → 切块 → 向量化 → 落库。
- **MP4**：`ffmpeg` 抽音轨为 16k 单声道 WAV → STT 转写（带时间戳）→ 长音频分段 + 失败窗口重试 → 切块 → 向量化 → 落库。
- **加工**：摘要 / 知识点 / 笔记 / 试题生成 / 批改，全部通过 `ModelRouter` 按 `task=` 路由到具体供应商。

### 关键设计决策

| 决策 | 理由 |
| --- | --- |
| 业务代码只传 `task=`，不写死厂商 SDK | 换模型只改 `providers.yaml` / 环境变量，不动业务代码 |
| 供应商配置走 `providers.yaml` + 环境变量 | 密钥只从环境变量读，绝不进配置文件或数据库 |
| `mock` 供应商 | 无任何密钥也能跑通完整链路，CI 与单测默认用它 |
| 缺密钥时**大声报错**而非静默降级 | 避免把假结果当成真结果（曾出现 `deepseek-chat` 被发到 `/embeddings` 的问题） |
| 把 ffmpeg 放在 `tools/ffmpeg/bin` | 不污染系统 PATH，也不会被误打成「已安装」 |
| 存储路径 / 供应商配置可在运行时改 | 存在 `backend/config/runtime.json`，页面改完即时生效、无需重启 |
| 删除题组用「墓碑」而非直接删行 | `quiz_attempts.quiz_id` 是 NOT NULL 外键，保留题组行才能让历史成绩仍可读 |

### 用户专属文件夹（User Workspace）

每位账号在存储根目录下拥有一个以**用户 ID** 命名的专属文件夹：

```
<storage_root>/<user_id>/
├── profile/account.json        账号元数据（用户 ID / 邮箱 / 注册时间）
├── records/*.json              学习记录存档（笔记、摘要、知识点、成绩、问答、用量）
└── resources/<source_id>/...   导入的学习资源原件（文档 / 视频 / 音频）
```

- **创建时机**：注册成功即创建；每次成功登录再幂等地跑一遍，所以手工删除或空目录清理之后会自动恢复。
- **命名规范**：目录名用用户主键（UUID），唯一且稳定；可读身份（邮箱）记在 `profile/account.json` 里，避免邮箱字符影响路径解析。
- **口令不进入该目录**：口令只以 bcrypt 单向哈希存在数据库 `users.hashed_password`；把哈希或明文复制一份到磁盘只会扩大泄露面，本项目不写。
- **数据库仍是运行时唯一事实来源**；`records/` 是磁盘镜像——提交成绩时自动刷新，也可用 `POST /api/v1/system/workspace/export` 主动导出，`GET /api/v1/system/workspace` 用于查看目录形状与占用。
- 相关实现集中在 `app/services/workspace.py`，测试见 `backend/tests/test_workspace.py`。

---

## 三、目录结构

```
.
├── backend/                     # FastAPI 后端
│   ├── app/
│   │   ├── api/                 # 路由层：auth / sources / quiz / tutor / knowledge
│   │   │                        #        notes / tasks / usage / system / llm / health
│   │   ├── core/                # 基础设施：config / db / deps / security
│   │   │                        #           ffmpeg / migrate / runtime_paths
│   │   ├── domain/              # 领域模型与出参 schema（schemas / mastery / flashcards）
│   │   ├── models/              # SQLAlchemy ORM：source / chunk / quiz / note / ...
│   │   ├── services/            # 业务逻辑
│   │   │   ├── llm/             #   ModelRouter 与各供应商适配（含 mock）
│   │   │   ├── rag/             #   检索：pgvector / llamaindex / intent
│   │   │   └── *.py             #   pipeline / pdf_parser / ocr / stt / quiz / tutor ...
│   │   ├── workers/             # arq 异步任务（容器化部署时启用）
│   │   ├── prompts/             # 提示词模板（带版本号，如 summarizer.v2.txt）
│   │   └── utils/
│   ├── config/providers.yaml    # 供应商与模型注册表（不含密钥）
│   ├── migrations/versions/     # Alembic 迁移
│   ├── tests/                   # pytest 测试
│   └── pyproject.toml
├── frontend/                    # Next.js 14 前端
│   └── src/
│       ├── app/                 # 页面：dashboard / sources/[id]/{quiz,tutor} / settings / login
│       ├── components/          # 组件（含 ui/ 基础组件）
│       └── lib/                 # api 客户端 / 类型 / 工具
├── infrastructure/              # 容器化部署：docker / nginx / postgres / redis / minio
├── scripts/                     # 运维与自检脚本（启动检查、样例生成、OCR 模型下载、E2E）
├── docs/                        # 项目文档（本文件、deferred.md）
├── tools/ffmpeg/bin/            # 本地 ffmpeg/ffprobe（按需下载，不入库）
├── data/storage/                # 用户数据根目录（运行时生成，不入库）
│                                #   <user_id>/{profile,records,resources}
├── test/input/                  # 自测用的样例 mp4 / pdf（不入库）
├── start.vbs / start.bat        # 本地启动（vbs 静默调用 bat）
├── stop.bat                     # 停止服务
├── docker-compose.yml           # 容器化编排
└── Makefile                     # make test / backend / frontend / compose / samples
```

---

## 四、技术栈选型

### 后端

| 技术 | 用途 | 选型理由 |
| --- | --- | --- |
| Python 3.11+ | 运行环境 | 类型语法与 asyncio 生态成熟 |
| FastAPI + Uvicorn | Web 框架 | 原生 async、自动生成 OpenAPI 文档、依赖注入清晰 |
| SQLAlchemy 2.0 (async) | ORM | 同一套模型同时支持 SQLite 与 PostgreSQL |
| aiosqlite / asyncpg | 数据库驱动 | 本地零依赖用 SQLite；容器用 PostgreSQL + pgvector |
| Alembic | 迁移 | PostgreSQL 走标准迁移；SQLite 另有幂等补列逻辑（`core/migrate.py`） |
| pydantic-settings | 配置 | 支持多层 `.env` 叠加，且能区分「进程环境变量」与「文件配置」 |
| PyJWT + bcrypt | 鉴权 | 单机应用，无需引入完整 OAuth 栈 |
| pypdf | PDF 文本层抽取 | 纯 Python、无外部依赖 |
| Pillow + rapidocr-onnxruntime | 离线 OCR | 无需密钥、整本书不出本机；识别差时可回退视觉大模型 |
| ffmpeg（本地二进制） | 音视频处理 | 视频抽音轨的事实标准 |
| faster-whisper / SiliconFlow | 语音转写 | 前者离线、后者云端更准，按需二选一 |
| httpx | 调用模型 API | 支持 async 且自动遵循 `HTTP_PROXY` / `HTTPS_PROXY` |
| arq + Redis | 异步任务队列 | 仅容器化部署需要；本地直接用 `inline` 模式 |
| MinIO | 对象存储 | 仅容器化部署需要；本地写文件系统 |

### 前端

| 技术 | 用途 | 选型理由 |
| --- | --- | --- |
| Next.js 14（App Router） | 框架 | 路由 / 构建 / SSR 一体，本地 `next start` 即可 |
| TypeScript | 语言 | 与后端 schema 对齐，减少契约错误 |
| Tailwind CSS | 样式 | 无需额外样式文件，便于统一浅色系风格 |
| TanStack Query | 服务端状态 | 缓存失效与轮询 / 流式刷新控制清晰 |
| Zustand | 客户端状态 | 轻量，避免为少量全局态引入大型状态库 |
| Radix Slot + lucide-react | 基础组件与图标 | 无障碍行为完备、体积小 |

### 基础设施

PostgreSQL(pgvector) · Redis · MinIO · Nginx，全部通过 `docker-compose.yml` 编排；
`nginx` 处于 `with-nginx` profile，默认不启动。

---

## 五、开发规范

### 环境与配置

- **绝不提交 `.env`**（已在 `.gitignore` 中忽略）；新增配置项一律先写进 `.env.example` 并附中文注释。
- 密钥只从环境变量读取；配置文件（`providers.yaml`）里只写 `api_key_env` 名称。
- 本地默认 `LLM_DEFAULT_PROVIDER=mock`，无需密钥即可跑通全流程。

### 代码风格

- Python：`ruff`，`line-length = 110`，`target-version = py311`（见 `backend/pyproject.toml`）。
- 前端：ESLint（`next lint`），TypeScript 严格模式。
- 新增供应商/模型：只改 `backend/config/providers.yaml`；若需新适配器，放到 `app/services/llm/`。
- 提示词改动：新建带版本号的文件（如 `xxx.v2.txt`），不要就地改旧版本。

### 测试

```bash
cd backend && python -m pytest -q       # 全量
make test                               # 等价，显式固定 mock + inline + local
```

- `pytest-asyncio`，`asyncio_mode = "auto"`。
- `tests/conftest.py` 会把 `DATABASE_URL`/`STORAGE_BACKEND`/`LLM_*` 全部固定为测试值，
  **测试不得依赖真实 API、真实网络或本机数据库**。
- 需要视频的用例用 `tests/helpers.py::tiny_mp4_bytes()` 现场用 ffmpeg 生成 2 秒素材。
- 新增功能请补对应测试；删除类功能务必覆盖「只影响目标对象、不影响其它模块」。

### Git 约定

- 主分支 `main`；改动走特性分支（历史命名如 `cursor/<主题>-<短哈希>`）后提 PR。
- 提交信息用祈使句、说明「为什么」（如 `Fix SiliconFlow STT integration and make tests hermetic`）。
- 提交前确认 `git status` 干净、测试通过、且没有把 `.env` / 数据库 / 上传文件 / 样例媒体带进来。

### 启动方式

- 本地：双击 `start.vbs`（静默调用 `start.bat`），`stop.bat` 停止；两个端口 8000 / 3000。
- 容器：`docker compose up --build`；迁移由容器启动命令中的 `alembic upgrade head` 完成。

---

## 六、接口与数据约定

- 路由统一前缀 `/api/v1`，健康检查为 `/health`、`/health/llm`、`/health/schema`。
- 出参 schema 集中在 `app/domain/schemas.py`，前端类型在 `frontend/src/lib/types.ts`，两侧需同步修改。
- 长耗时任务（解析、生成）通过任务表 + SSE 进度流反馈；流断开时前端回退轮询。
- 删除类接口返回「删除报告」（删了多少题、多少条记录等），而不是空响应，便于前端如实提示。