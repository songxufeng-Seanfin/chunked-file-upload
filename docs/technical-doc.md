# 大文件分片上传与秒传 · 技术文档

> v1.0 · 2026-11-16 · Django 5.2 + Vue 3 + Pillow + SQLite · 22 个自动化测试全部通过
>
> 这份文档按「由外向内」的顺序写：先看你打开页面能看见什么，再看一次请求怎么走一圈，然后进到 Django 内部的路由、视图、业务层、数据表，最后才看一行行实现细节。**专业术语第一次出现时，都会用大白话解释一遍**，读不懂代码也没关系，先记住骨架。

---

## 0. 先站远一点看：这个项目到底是什么

一句话：**把大文件切成小块分别上传，服务器按编号拼回去；如果服务器上已经有同样的文件，就直接跳过上传；如果传到一半断了，就只补缺的那几块。**

「大文件」在这里指几十 MB 到几百 MB 的东西。一次性 POST 上去会遇到三个麻烦：请求太大会被服务器或网关拒绝、网一断就得从头再来、大请求占满连接让别的操作变慢。分片（chunk，把文件按固定大小切开的小块）就是为了解决这三件事。

打开 `http://127.0.0.1:8001/` 能亲眼看到四件事：

| 你点的东西 | 背后发生的事 | 页面上能看到的证据 |
|---|---|---|
| 一键跑全部场景 | 服务端用真实代码跑四个场景 | 统计卡数字变化、事件时间线多出十几条 |
| 选一个本地文件 | 浏览器真的切片、并发 POST、请求合并 | 进度条、分片位图从灰变绿 |
| 模拟中断 + 续传 | 新任务只补缺片 | 任务标「续传」，本次传输字节明显少 |
| 同一个文件传第二次 | 指纹命中档案，0 字节 | 任务标「秒传命中 · 0 字节」 |

看板从上到下分成：术语卡（分片 / 秒传 / 断点续传）、一键演示按钮区、真实上传区、六张统计卡、文件档案表、上传任务与分片位图、事件时间线、六步流程图。**所有表格数据都来自 `/api/state/` 这一个 JSON 接口**，页面只是把状态画出来。

为什么这个项目没用对象存储（OSS / S3 这类云存储服务）？因为它是用来讲清楚原理的：本地磁盘 + SQLite 就能跑，不注册任何云账号，断网也能演示。文末第 15 节会诚实列出上线前必须补的东西。

---

## 1. 技术栈：用了什么，各自负责哪一层

| 名称 | 是什么（白话） | 在这个项目里的职责 |
|---|---|---|
| Python 3.13 | 写后端逻辑的语言 | 所有服务端代码 |
| Django 5.2 | 一个「网站工具箱」：路由、数据库、模板、后台管理都替你做好 | 接住 HTTP 请求、操作数据库、渲染页面 |
| MTV | Django 的分工方式：**M**odel 管数据、**T**emplate 管页面、**V**iew 管逻辑 | 本项目页面几乎全交给 Vue，所以 Template 只是一层壳 |
| ORM | 用 Python 类操作数据库，不用手写 SQL | `FileRecord.objects.filter(...)` 就是查表 |
| SQLite | 一个文件就是整个数据库，零安装 | 存四张表 |
| Vue 3（全局构建） | 前端框架，让数据和界面自动同步 | 看板渲染、进度条、按钮状态 |
| `File.slice()` | 浏览器把文件按字节范围切一块的 API | 每片 2MB，切完直接 POST |
| Web Crypto `crypto.subtle` | 浏览器自带的加密计算能力 | 算 SHA-256 指纹 |
| FormData + fetch | 浏览器发「带文件的表单」和发请求的标准方式 | 分片上传、所有 JSON 接口 |
| SHA-256 | 给任意字节算一串 64 位「身份证号」的算法 | 整文件指纹、每片指纹、合并后二次校验 |
| Pillow | Python 的图像处理库 | 生成演示样本图、合并后生成缩略图 |
| CSRF Token | 一个一次性凭证，证明「这个请求确实是从本站页面发出的」 | 保护所有 POST 接口 |
| 原子写 / `os.replace` | 先写临时文件，写完瞬间改名，避免留下半个文件 | 分片落盘、合并文件正式亮相 |

**Vue 为什么不用 npm？** 正常项目会用 Node.js 打包，但那需要装一整套前端工具链。这里把 `vue.global.prod.js`（157KB）直接放进 `static/vendor/`，页面用 `<script>` 引入即可，代价是没有单文件组件，好处是**克隆仓库后 `python manage.py runserver` 就能跑，断网也能打开**。这对一个用来学习的演示项目是划算的取舍。

---

## 2. 第一层：一次点击，请求怎么走一圈

以「选文件 → 开始上传」为例，浏览器和服务端之间实际发生了这些往返：

```text
浏览器                                       Django 服务
   |                                                |
   |-- GET /  -------------------------------------->|  返回 HTML 外壳 + 种下 csrftoken
   |-- GET /api/state/ ---------------------------->|  返回当前统计 / 文件 / 任务 / 事件
   |                                                |
   | [本地] crypto.subtle 算整文件 SHA-256           |
   |-- POST /api/upload/check/ {指纹,大小,分片大小} -->|  查档案 / 查已有分片，建任务
   |<-------------------- {task_id, uploaded:[]} ---|
   |                                                |
   | [本地] File.slice 切第 0、1、2 片，3 路并发      |
   |-- POST /api/upload/chunk/ (multipart #0) ------>|  校验 → 原子落盘 → 记一条事件
   |-- POST /api/upload/chunk/ (multipart #1) ------>|
   |-- POST /api/upload/chunk/ (multipart #2) ------>|
   |<--------------- {result: APPLIED / DUPLICATE} --|
   |                     …… 直到所有片到齐 ……          |
   |                                                |
   |-- POST /api/upload/merge/ {task_id} ----------->|  按序拼接 → 整文件再算一次指纹
   |<------------------ {result: MERGED} -----------|  生成档案 + 缩略图 + 清理临时块
   |-- GET /api/state/ ----------------------------->|  刷新看板
```

几个之前学过的状态码，在这个项目里对应什么：

| 状态码 | 白话 | 本项目的触发点 |
|---|---|---|
| 200 | 成功 | 页面、状态接口、检查接口 |
| 400 | 参数不合法 | 指纹不是 64 位十六进制、分片大小越界（`UploadError` 默认 400） |
| 403 | 不认凭证 | POST 没带 `X-CSRFToken`（`test_csrf_rejects_post_without_token` 专门测了） |
| 404 | 找不到 | 合并时 `task_id` 不存在（清空重来后再点旧任务） |
| 409 / 413 | 冲突 / 太大 | 同一编号带来不同内容、单片超过 16MB |
| 500 | 服务端崩了 | 兜底异常，`views.py` 会记日志并返回 JSON 错误 |

**同步和异步**：页面上的 `fetch` 都是异步（async），意思是「发出请求后不卡住界面，等结果回来再继续」；`await` 就是「这一行要等结果」。并发池里三个 worker 同时 `await`，所以叫并发上传。

---

## 3. 第二层：Django 内部的五站流水线

请求进到服务端后，依次经过：

```text
config/urls.py          ① 总路由：/ 交给 uploader.urls，/media/ 指向本地磁盘
   ↓
uploader/urls.py        ② 子路由：把 7 个 URL 分别指到 views 里的函数
   ↓
CsrfViewMiddleware      ③ 安检口：每个 POST 校验 X-CSRFToken
   ↓
uploader/views.py       ④ 翻译层：JSON / multipart → 调 service → 返回 JsonResponse
   ↓
uploader/service.py     ⑤ 业务层：真正的上传链路，不认识 HTTP
   ↓
uploader/models.py      ⑥ 数据层：ORM 读写四张表
   ↓
media/ 目录             ⑦ 磁盘：临时分片、合并文件、缩略图
```

| 文件 | 行数 | 职责 | 为什么不合并 |
|---|---|---|---|
| `config/settings.py` | 82 | 数据库、静态/媒体目录、分片大小参数 | 配置集中放，环境变量可覆盖 |
| `uploader/urls.py` | 13 | URL → 视图的映射表 | 路由单独一张表，改地址不用翻业务代码 |
| `uploader/views.py` | 116 | 解析请求、错误翻译成 400/404/500 | 只做「翻译」，不含业务逻辑 |
| `uploader/service.py` | 547 | 检查、接收、合并、演示、状态快照 | **关键**：不依赖 HTTP，所以测试和命令行演示能直接调用同一套代码 |
| `uploader/models.py` | 140 | 四张表的结构、约束、辅助属性 | ORM 定义与业务动作分离 |
| `uploader/hashing.py` | 78 | 指纹、分片大小、文件名清洗、单位换算 | 纯函数，最容易写测试 |
| `static/app.js` | 298 | 浏览器端上传器与看板 | 前端独立，可单独替换成 Vue CLI 工程 |

把业务逻辑放进 `service.py` 是本项目最重要的结构决定：一键演示不是「往表里塞假数据」，而是在服务端调用 `check_upload → receive_chunk → merge_task` 这一模一样的函数（`service.py:474` 的 `run_demo_scenario`）。这样演示跑通，等于真实链路跑通。

**CSRF 安检口**在这里怎么过？页面视图 `console()` 上有 `@ensure_csrf_cookie`（`views.py:54`），Django 会在响应里种下 `csrftoken` cookie；前端 `app.js:41` 的 `postJSON` 和分片上传都把它放进 `X-CSRFToken` 请求头。浏览器自动带 cookie、手动带头，两边对上才放行。

---

## 4. 第三层：四张数据表，各司其职

| 表 | 一句话职责 | 关键字段 |
|---|---|---|
| `FileRecord` | 「服务器上已经有这份文件」的档案 | `file_hash`（唯一）、`size`、`total_chunks`、`rel_path`、`thumbnail`、`ref_count` |
| `UploadTask` | 一次上传会话 | `task_id`（唯一）、`status`、`bytes_sent`、`instant`、`resumed`、`source` |
| `UploadChunk` | 已落盘的某一片 | `file_hash` + `index`（联合唯一）、`chunk_hash`、`rel_path` |
| `UploadEvent` | 可观测性流水：每次检查、拦下、合并都记一条 | `event_type`、`result`、`chunk_index`、`detail` |

### 一个决定成败的设计：分片挂在 `file_hash` 上，不挂在 `task_id` 上

断点续传的本质是「换个会话也能接着传」。如果把分片绑在任务上，刷新页面新建一个 task 后，旧的片就查不到了，只能从头传。本项目让 `UploadChunk` 以 `(file_hash, index)` 为身份：

```python
# uploader/models.py:82
class UploadChunk(models.Model):
    file_hash = models.CharField(max_length=64, db_index=True)
    index = models.PositiveIntegerField()
    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["file_hash", "index"], name="uniq_file_chunk_index")]
```

于是 `check_upload()` 只要按 `file_hash` 查一次，就能告诉新任务「0、1 号片已经有了，只补 2、3、4」（`service.py:88`）。联合唯一约束同时充当并发闸门：两个请求同时写同一片，数据库只会让一个成功，另一个收到 `IntegrityError`，被 `service.py:169` 捕获后当作「重复投递」处理。

任务状态是一个很小的状态机（state machine，规定状态只能怎么变）：

```text
uploading ──收齐分片并合并成功──> merged
    │
    └──整文件指纹校验失败──────> failed
check 命中档案 ─────────────────> instant（直接终态，bytes_sent = 0）
```

`FileRecord.ref_count` 记录这份档案被秒传命中了多少次，`build_state()` 里用它算「省下流量」。

---

## 5. 第四层：核心链路六步

### 5.1 算指纹（浏览器，`app.js:196`）

```js
const whole = await sha256(await picked.value.arrayBuffer());
```

`crypto.subtle.digest("SHA-256", buffer)` 返回整文件的 64 位十六进制指纹。白话：给这堆字节拍一张「证件照」，内容变一个字节，照片就完全不同。**注意**：Web Crypto 只在安全上下文（secure context，HTTPS 或 `127.0.0.1` / `localhost`）可用，所以本地必须用这两个地址访问，不能直接开文件。

### 5.2 上传前检查（`service.py:88`）

服务端做三件事：校验参数；自己按 `size / chunk_size` 算 `total_chunks`（**不信前端报的片数**）；查 `FileRecord`。

```text
FileRecord 里有这个指纹？
    ├─ 有 → 建 instant 任务、bytes_sent=0、ref_count+1、返回 {instant: true}
    └─ 没有 → 查 UploadChunk 里已有哪些编号 → 建 uploading 任务、返回 {uploaded: [...]}
```

两条分支都会写一条 `UploadEvent`，所以看板能看到「为什么没传」。

### 5.3 分片并发（浏览器，`app.js:167` / `app.js:196`）

```js
const worker = async () => {
  while (queue.length) {
    if (interrupted.value) return;
    while (paused.value && !interrupted.value) await sleep(120);
    const index = queue.shift();
    await sendOne(index);
  }
};
await Promise.all(Array.from({length: concurrency}, worker));
```

这是一个**并发池**：开 3 个 worker，共享一个待传队列，谁空了就从队列取下一片。暂停只是让 worker 停在 `while (paused)` 里，不取消正在飞的请求；「模拟中断」则让 worker 不再取新片，已经发出的请求照常落盘——这正是真实断网的样子，也正是续传要处理的情况。

每一片单独算片指纹，连同 `task_id`、`index`、文件内容一起放进 `FormData` POST 出去。

### 5.4 幂等闸门（服务端，`service.py:169`）

「幂等」（idempotency）的意思是：同一件事做 100 次，效果和做 1 次一样。分片可能被重传、被并发重复提交，服务端按这个顺序判断：

| 检查 | 不通过的结果 | 白话 |
|---|---|---|
| 编号越界 / 大小不符 | `REJECTED` | 这不是我要的那一块 |
| 片内容与声明的片指纹不符 | `CORRUPT` | 传坏了，不落盘 |
| 该 `(file_hash, index)` 已存在且指纹相同 | `DUPLICATE` | 来过了，跳过，不重复写盘 |
| 已存在但指纹不同 | `CONFLICT` | 同编号带了两个不同内容，拒绝覆盖 |
| 都没问题 | `APPLIED` | 原子落盘 + 记一条 `UploadChunk` + 累加 `bytes_sent` |

落盘用「先写 `.tmp` 再 `os.replace`」（`service.py:65`），避免断电留下半个分片。写入和记库放在 `transaction.atomic()` 里，要么都成功要么都回滚。

### 5.5 按序合并（`service.py:237`）

```python
with open(tmp_path, "wb") as out:
    for index in range(task.total_chunks):   # 严格 0 → n-1
        with open(_chunk_abs(task.file_hash, index), "rb") as fh:
            out.write(fh.read())
```

**分片到达顺序完全不影响结果**，因为顺序由编号决定，不由网络决定。演示里的「乱序分片」故意按 `2, 0, 4, 1, 3` 提交，合并出来仍与原文件逐字节相同。

### 5.6 二次校验、正式亮相、缩略图

合并完还要用 `sha256_stream()` 边读边算一次整文件指纹，和上传前声明的比对：

- 不一致 → 删临时文件、任务标 `failed`、返回 `CORRUPT`，**绝不入库**；
- 一致 → `os.replace(tmp, final)` 让文件正式可见 → `get_or_create(FileRecord)` → Pillow 生成 160×160 缩略图 → 删临时分片、保留 `UploadChunk` 记录（位图还要显示）。

这道「二次校验」不是多余的：每一片自己的指纹都能对上，但**把别人的分片拼进来**也是每片合法的，只有整文件指纹能抓出来。`test_merge_rejects_wrong_whole_hash` 就是专门伪造这种攻击的测试。

---

## 6. 三个卖点，分别落在哪几行代码

| 卖点 | 专业说法 | 落地位置 |
|---|---|---|
| 秒传 | 内容寻址（content-addressable）：用内容指纹而不是文件名当身份 | `service.py:88` 查 `FileRecord`；命中后 `bytes_sent=0` |
| 断点续传 | 幂等记录 + 状态查询，客户端只补差集 | `service.py:88` 返回 `uploaded`；`models.py:82` 分片挂 `file_hash`；`app.js:196` 重新 check |
| 乱序与并发 | 编号即全序，服务端无状态接收 | `service.py:169` 按 `index` 落盘；`service.py:237` 按编号拼接；`app.js:196` 并发池 |

一个容易被忽略的点：**秒传不是「跳过校验」**。它跳过的只是字节传输，任务、事件、引用计数照样记录，看板上才有「0 字节」和「省下流量」可看。

---

## 7. 前端：Vue 3 看板与上传器

### Django 模板和 Vue 的 `{{ }}` 撞车怎么办

Django 模板也用 `{{ 变量 }}`，Vue 也用 `{{ 变量 }}`。解决办法是把整个 `#app` 用 `{% verbatim %}` 包起来（`templates/console.html`），意思是「Django 别解析这一段，原样输出」，让 Vue 自己接管。代价是这一段里不能写 `{% url %}` 之类 Django 标签，所以接口地址在 `app.js` 里用字面量 `/api/...`。

### Vue 管的状态

`app.js` 的 `setup()` 里用 `ref` 声明响应式状态（数据一变，界面自动重画）：`state`（看板数据）、`picked`（选中的文件）、`progress`、`sent`、`phase`、`chunkStates`（每片是 todo / doing / done / dup）、`paused`、`interrupted`、`busy`、`notice`。`computed` 负责派生值，比如 `expectedTotal = ceil(文件大小 / 分片大小)`。

### 界面适配

`templates/base.html` 里是一份自包含 CSS：CSS 变量定义浅色/深色两套配色，`@media (prefers-color-scheme: dark)` 自动跟随系统，`@media (max-width: 520px)` 把六列统计卡收成两列、按钮换行、表格放进 `.table-wrap` 横向滚动。手机端 500px 宽度已用无头浏览器截图核对过，没有横向溢出。

---

## 8. 接口清单

| 方法 | 路径 | 主要入参 | 返回 | 作用 |
|---|---|---|---|---|
| GET | `/` | — | HTML | 演示看板，顺带种 CSRF cookie |
| GET | `/api/state/` | — | 统计 / 文件 / 任务 / 事件 / 参数 | 看板唯一数据源 |
| POST | `/api/upload/check/` | `file_hash, filename, size, chunk_size` | `instant` 或 `task_id + uploaded[]` | 秒传判定 / 断点查询 |
| POST | `/api/upload/chunk/` | multipart：`task_id, index, chunk_hash, file` | `APPLIED / DUPLICATE / CONFLICT / CORRUPT / REJECTED` | 收一片 |
| POST | `/api/upload/merge/` | `task_id` | `MERGED / INCOMPLETE / CORRUPT` | 拼接 + 二次校验 |
| POST | `/api/demo/run/` | `scenario` | 场景摘要 | 服务端跑演示 |
| POST | `/api/demo/reset/` | — | 清空结果 | 重置演示数据 |

所有响应都是 JSON，外层统一带 `ok`；业务错误由 `views.py:40` 的 `_handle` 翻译成结构化错误码，不会把一页 HTML 报错丢给前端。

---

## 9. 文件清单与规模

| 文件 | 行数 | 说明 |
|---|---:|---|
| `config/settings.py` | 82 | 配置与分片参数 |
| `uploader/models.py` | 140 | 四张表 |
| `uploader/hashing.py` | 78 | 指纹与分片工具 |
| `uploader/service.py` | 547 | 完整业务链路、演示、状态快照 |
| `uploader/views.py` | 116 | 7 个 HTTP 接口 |
| `uploader/tests.py` | 322 | 22 个测试 |
| `uploader/migrations/0001_initial.py` | 92 | 自动生成的建表迁移 |
| `templates/base.html` | 176 | 全站样式（浅色/深色/手机端） |
| `templates/console.html` | 190 | Vue 看板骨架 |
| `static/app.js` | 298 | 浏览器上传器 |
| `static/vendor/vue.global.prod.js` | — | Vue 3.5.13，157KB，本地化 |
| `docs/` | — | 本文档、博客、离线阅读版与生成脚本 |

后端 + 前端 + 测试约 2,100 行；`media/` 与 `db.sqlite3` 是运行期产物，不进版本库。

---

## 10. 一键演示与可观测性

`run_demo_scenario()`（`service.py:474`）支持五个场景，每个都**真的调用同一个 service 链路**：

| 场景 | 怎么造出来的 | 验证什么 |
|---|---|---|
| `normal` | 5 片按 `2,0,4,1,3` 乱序提交 | 乱序不影响合并结果 |
| `duplicate` | 第 1 片连投两次 | 第二次被 `DUPLICATE` 拦下，不重复写盘 |
| `resume` | 先传 2 片，模拟断网，再新建任务补 3 片 | `check` 返回 `uploaded=[0,1]`，只补缺片 |
| `instant` | 拿已存在档案的指纹再 check 一次 | 0 字节、`ref_count+1` |
| `all` | 串起上面四个 | 一次看清全貌 |

命令行也能跑：`python manage.py demo_upload --scenario all`，或 `--scenario reset` 清空。

「可观测性」（observability，系统把内部发生了什么讲清楚的能力）靠两张东西：`UploadEvent` 流水（每次检查/拦下/合并都留痕）和分片位图（灰=缺、绿=已到、紫=重复被拦）。没有这两样，重复被拦在页面上的数字会永远是 0，和项目 2 里踩过的坑一模一样。

---

## 11. 测试策略：22 个测试分五组

| 组 | 代表用例 | 保证什么 |
|---|---|---|
| 工具层 | `test_hash_helpers` | 指纹一致性、片数计算、文件名清洗 |
| 幂等与校验 | `test_duplicate_chunk_is_idempotent`、`test_conflicting_chunk_not_overwritten`、`test_unique_constraint_on_chunk` | 重复不写盘、冲突不覆盖、唯一约束兜底 |
| 合并与完整性 | `test_out_of_order_upload_merges_correctly`、`test_merge_rejects_missing_chunks`、`test_merge_rejects_wrong_whole_hash` | 乱序可拼回、缺片拒绝、伪造分片被二次校验抓出 |
| 秒传 / 续传 / 缩略图 | `test_instant_upload_hits_existing_record`、`test_resume_only_uploads_missing_chunks`、`test_thumbnail_generated_for_image` | 0 字节命中、只补缺片、Pillow 出图 |
| HTTP 层 | `test_full_http_roundtrip`、`test_csrf_rejects_post_without_token`、`test_unknown_task_returns_404` | 接口串联、安检口、404 |

测试用 `@override_settings(MEDIA_ROOT=临时目录)` 把磁盘产物隔离到临时文件夹，**跑测试不会清掉演示页面正在展示的数据**。

除了单元测试，还用无头浏览器做过一次真实端到端：生成 1.3MB 文件、算指纹、6 片上传、合并、再传一次秒传，输出 `RESULT OK`；桌面 1280px 与手机 500px 各截图核对过布局。

---

## 12. 真实踩坑复盘

1. **合并时临时文件被提前删掉**。第一版用 `try/finally` 无条件删 `.merging` 临时文件，结果校验通过后还得重新拼一遍文件。改成「校验通过才 `os.replace` 正式亮相，失败才删临时文件」，代码短了一半，也讲得清。
2. **Django 模板缓存让 CSS 修复看不到**。改完 `base.html` 后，运行中 `--noreload` 的服务仍返回旧模板，`curl` 抓到的 CSS 少一条规则。重启 `runserver` 后才生效——以后遇到「改了没反应」，先确认服务是否重新加载了模板。
3. **暗色主题「记录」标签白底白字**。`.pill.muted` 在浅色下是浅灰底，深色下必须单独覆盖背景与文字色，否则文字糊成一片。截图复核后确认修好。
4. **`{% verbatim %}` 与 Vue 模板冲突**。不放进 verbatim，Django 会先把 `{{ stats.files }}` 当自己的变量解析掉，页面剩空白。
5. **演示样本既要够大、又得是能出缩略图的图片**。解决办法是用 Pillow 画一张 PNG，再往 IEND 之后追加确定性字节凑到目标大小；PNG 解码器忽略尾部数据，Pillow 仍能生成缩略图，而整文件指纹按完整字节流计算。
6. **CSRF cookie 从哪来**。独立静态页做端到端测试时没有 csrftoken，先 `fetch('/')` 让 `@ensure_csrf_cookie` 种下 cookie，再发 POST 才通。
7. **分片不能绑任务**。早期思路把 `UploadChunk` 挂在 `task_id` 上，写续传逻辑时发现刷新页面后旧片全查不到，改成按 `file_hash` 共享（见第 4 节）。
8. **别信前端报的片数**。客户端可能算错或恶意伪造，`total_chunks` 一律由服务端按 `size / chunk_size` 重算；每片期望大小也由服务端算，最后一片才允许变小。

---

## 13. 想自己从零做出来，按这个顺序

1. `django-admin startproject` 建骨架，`startapp uploader`，`settings.py` 配上 `MEDIA_ROOT` 和 SQLite。
2. 先只写四张表 + `python manage.py makemigrations && migrate`。
3. 写 `hashing.py`，用 `python manage.py shell` 手算一次 SHA-256，把「指纹」这个感觉建立起来。
4. 写 `service.py` 的三个函数：`check_upload` → `receive_chunk` → `merge_task`，**先不接 HTTP**，直接在 shell 里传 bytes 调。
5. 给这三个函数写测试（乱序、重复、缺片、伪造），测试过了再接 `views.py`。
6. 接 7 个 URL，用 `curl` 或浏览器 DevTools 的 Network 面板看请求往返。
7. 最后写 Vue 看板：先只做 `/api/state/` 渲染，再加文件选择、切片、并发池、进度条、位图。

这个顺序就是「由外向内理解、由内向外实现」：先懂流程与骨架，再一点点添细节。

---

## 14. 术语速查（白话版）

| 术语 | 白话 |
|---|---|
| 分片 chunk | 把大文件切成固定大小的小块，编号后分别传 |
| 哈希 / 指纹 hash | 给字节算的身份证号，改一个字节就完全不同 |
| 秒传 instant upload | 服务器已有同指纹文件，直接复用，0 字节传输 |
| 断点续传 resumable | 断了只补没传过的片，不从头来 |
| 幂等 idempotency | 同一操作重复做多次，结果和做一次一样 |
| 内容寻址 | 用内容指纹当身份，不用文件名 |
| 并发 concurrency | 同时传好几片，`Promise.all` + 共享队列 |
| 原子写 atomic write | 先写临时文件再瞬间改名，不留半成品 |
| 唯一约束 unique constraint | 数据库层面的「这个组合只能有一条」，并发也拦得住 |
| 事务 transaction | 一组数据库操作要么全成功，要么全回滚 |
| 状态机 state machine | 规定状态只能怎么变，比如不允许从「已完成」退回「上传中」 |
| 可观测性 observability | 系统把内部发生了什么讲清楚，靠事件流水和位图 |
| CSRF | 证明请求来自本站页面的凭证，防止别的网站冒名提交 |
| ORM | 用 Python 类操作数据库，不手写 SQL |
| 安全上下文 secure context | 浏览器只在 HTTPS / localhost 下开放加密相关 API |
| MTV | Django 的分工：数据 Model / 页面 Template / 逻辑 View |

---

## 15. 上线前必须补的东西（诚实边界）

这个项目的目标是把原理讲清楚，不是生产级网盘。真要上线，至少还要：

- **鉴权与隔离**：现在所有文件全局共享，任何人算得出指纹就能引用；生产要按用户/租户隔离，并对秒传做权限校验。
- **存储换成对象存储**：本地磁盘不适合多机部署，分片应写 OSS/S3 并用服务端签发的直传凭证。
- **增量哈希**：现在浏览器 `arrayBuffer()` 把整个文件读进内存，几百 MB 还行，GB 级要改成按片增量计算或 Web Worker。
- **过期清理**：中断后没人续传的分片要定时回收，否则磁盘会涨。
- **合并异步化**：大文件合并应丢到后台任务队列，HTTP 请求立刻返回。
- **限流与配额**：限制单用户并发数、总容量、单片重试次数。
- **指纹算法升级**：SHA-256 足够教学用，但对「哈希碰撞导致误命中」零容忍的场景要加长度 + 采样二次校验。

把这些写进文档，比假装它们不存在更专业——面试时被追问，也能讲清楚边界在哪。
