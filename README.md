# 大文件分片上传与秒传

> 2MB 分片 + 3 路并发 + SHA-256 指纹秒传 + 断点续传 + 乱序合并 + 整文件二次校验
> 一个能亲眼看见每一步的演示项目：Django 5.2 + Vue 3（本地化）+ Pillow + SQLite，**不需要 Redis、对象存储或 npm**。

打开页面就能做四件事：一键跑四个演示场景、选本地文件真的切片上传、模拟中断后续传、把同一个文件再传一次看 0 字节秒传。文件档案、分片位图、事件流水实时反映服务端真实状态。

---

## 快速开始

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py demo_upload --scenario all       # 可选：先造一份演示数据
python manage.py runserver 127.0.0.1:8001
```

浏览器打开 **http://127.0.0.1:8001/**。

> 端口 8001 是为了避开项目 2 的本地演示服务，换成任意空闲端口都可以。
> **必须用 `127.0.0.1` / `localhost` 或 HTTPS 访问**：浏览器的 `crypto.subtle`（算指纹用）只在安全上下文开放。

`static/vendor/vue.global.prod.js` 已经放进仓库，断网也能打开页面。

---

## 页面能做什么

| 区域 | 操作 | 能看到什么 |
|---|---|---|
| 一键演示 | 全部场景 / 乱序分片 / 重复投递 / 断点续传 / 秒传命中 / 清空重来 | 统计卡、分片位图、事件时间线同步变化 |
| 真实上传 | 拖入或选择本地文件 → 开始上传 | 指纹、进度条、本次传输字节、每片状态 |
| 中断演示 | 上传中点「模拟中断」→ 点「续传」 | 新任务标「续传」，只补缺的片 |
| 秒传 | 同一个文件再传一次 | 「秒传命中 · 0 字节」，省下流量增加 |
| 任务位图 | — | 灰=缺片，绿=已到，紫=重复被拦 |

演示按钮在**服务端真实调用** `check_upload → receive_chunk → merge_task`，不是往表里塞假数据；所以演示跑通就等于链路跑通。

---

## 核心链路

```text
浏览器算整文件 SHA-256
  → POST /api/upload/check/        查档案（命中即秒传）/ 查已有分片（断点）
  → File.slice 按 2MB 切片，3 路并发 POST /api/upload/chunk/
      服务端：编号与大小校验 → 片指纹校验 → 唯一约束幂等 → 原子落盘
  → POST /api/upload/merge/       严格按编号 0→n-1 拼接
  → 整文件再算一次 SHA-256，一致才入库 + Pillow 缩略图 + 清理临时块
  → GET /api/state/                刷新看板
```

三个关键设计：

- **分片挂在 `file_hash` 上，不挂在 `task_id` 上** —— 刷新页面、换浏览器也能续传。
- **`(file_hash, index)` 联合唯一约束** —— 重复投递与并发抢写都靠数据库判定，不用应用层 `if`。
- **合并后二次校验** —— 每一片都合法，也可能拼出别人的文件，只有整文件指纹能抓出来。

---

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 演示看板（顺带种 CSRF cookie） |
| GET | `/api/state/` | 统计、文件、任务、事件、参数（看板唯一数据源） |
| POST | `/api/upload/check/` | 秒传判定 / 返回已收分片编号 |
| POST | `/api/upload/chunk/` | 收一片，返回 `APPLIED / DUPLICATE / CONFLICT / CORRUPT / REJECTED` |
| POST | `/api/upload/merge/` | 合并并二次校验，返回 `MERGED / INCOMPLETE / CORRUPT` |
| POST | `/api/demo/run/` | 跑演示场景 `normal / duplicate / resume / instant / all` |
| POST | `/api/demo/reset/` | 清空演示数据 |

所有响应为 JSON，业务错误由 `views.py` 统一翻成 400 / 404 / 413 / 500。

---

## 命令行与测试

```bash
python manage.py demo_upload --scenario all        # 全部场景
python manage.py demo_upload --scenario normal    # 乱序分片
python manage.py demo_upload --scenario duplicate  # 重复投递
python manage.py demo_upload --scenario resume     # 断点续传
python manage.py demo_upload --scenario instant    # 秒传命中
python manage.py demo_upload --scenario reset     # 清空重来
python manage.py test uploader                   # 22 个测试
```

测试用临时目录做 `MEDIA_ROOT`，**不会清掉页面正在展示的演示数据**。

---

## 目录结构

```text
config/                     Django 配置、根路由（settings.py 里可调分片大小）
uploader/
  models.py                 四张表：FileRecord / UploadTask / UploadChunk / UploadEvent
  hashing.py               SHA-256、片数计算、文件名清洗、单位换算
  service.py               完整业务链路 + 演示 + 状态快照（不依赖 HTTP）
  views.py                 7 个 HTTP 接口
  urls.py / admin.py       路由与后台
  management/commands/      demo_upload 命令
  tests.py                 22 个回归测试
  migrations/0001_initial.py
templates/base.html          自包含 CSS（浅色/深色/手机端）
templates/console.html        Vue 看板骨架（{% verbatim %} 包住，避免与 Django 模板冲突）
static/app.js                浏览器上传器：算指纹、切片、并发池、暂停/续传
static/vendor/               Vue 3.5.13 全局构建（157KB，本地化）
docs/                        技术文档、博客、离线阅读版与生成脚本
media/                       运行期产物：临时分片、合并文件、缩略图（不入库）
```

---

## 可调参数（环境变量）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `UPLOAD_CHUNK_SIZE` | 2MB | 浏览器真实上传的分片大小 |
| `DEMO_CHUNK_SIZE` | 256KB | 演示用小分片，让位图看得清 |
| `UPLOAD_CONCURRENCY` | 3 | 并发路数 |
| `MAX_UPLOAD_SIZE` | 512MB | 单文件大小上限 |
| `UPLOAD_SECRET_KEY` / `UPLOAD_DEBUG` | 演示默认值 | 密钥 / 调试开关 |

---

## 文档

| 文档 | 内容 |
|---|---|
| [技术文档](./docs/technical-doc.md) | 由外向内分四层：请求往返 → Django 五站流水线 → 四张表 → 六步核心链路；含 12 张表、13 段代码、8 条真实踩坑复盘、术语白话速查、上线边界 |
| [博客文章](./docs/blog-post.md) | 第一人称故事线：为什么分片要绑指纹、幂等不能靠 `if`、每一片都对不等于拼起来对 |
| [离线阅读版](./docs/reader.html) | 两份文档合在一起的自包含 HTML：双 Tab、左侧目录、深浅色自适应，双击即开、零依赖 |
| [生成脚本](./docs/build_reader.py) | 纯标准库 Markdown → HTML，改完 md 跑一次 `python docs/build_reader.py` 即同步 |

---

## 边界说明

这是原理演示项目，不是生产级网盘。上线前需补：按用户隔离文件与秒传权限、对象存储替代本地磁盘、GB 级文件的增量哈希 / Web Worker、过期分片清理、合并异步化、限流配额。详见技术文档第 15 节。

关联简历计划：`项目工作区/01-两周行动计划.md` · 项目 03。
