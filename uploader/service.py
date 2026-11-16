"""上传核心链路（不依赖 HTTP 层，所以测试可以直接调用）。

一次完整上传的六个动作：
    算指纹 → check（秒传 / 断点） → 分片并发到达 → 每片校验落盘
    → merge 按序拼接 → 校验整文件指纹 → 生成档案与缩略图

这一层只处理「字节 + 数据库 + 磁盘」，views.py 负责把 HTTP 请求翻译成调用。
"""
import io
import os
import random
import re
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .hashing import (
    clean_filename,
    expected_chunk_size,
    expected_total,
    human_size,
    image_extensions,
    limits,
    safe_extension,
    sha256_bytes,
    sha256_stream,
)
from .models import FileRecord, UploadChunk, UploadEvent, UploadTask

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class UploadError(Exception):
    """参数不合法或前置条件不满足，views 层会翻成 400 / 409。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 磁盘路径
# ---------------------------------------------------------------------------
def _chunk_abs(file_hash: str, index: int) -> Path:
    return settings.MEDIA_ROOT / "chunks" / file_hash[:16] / f"{index:05d}.part"


def _file_abs(file_hash: str, ext: str) -> Path:
    return settings.MEDIA_ROOT / "files" / file_hash[:2] / f"{file_hash}{ext}"


def _thumb_abs(file_hash: str) -> Path:
    return settings.MEDIA_ROOT / "thumbs" / f"{file_hash}.png"


def _rel(path: Path) -> str:
    return path.relative_to(settings.MEDIA_ROOT).as_posix()


def _atomic_write(path: Path, data: bytes):
    """先写同目录的 .tmp，再 os.replace —— 避免断电留下半个分片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _log(task, file_hash, index, event_type, result, detail=""):
    return UploadEvent.objects.create(
        task=task,
        file_hash=file_hash or "",
        chunk_index=index,
        event_type=event_type,
        result=result,
        detail=detail[:255],
    )


# ---------------------------------------------------------------------------
# 1) 上传前检查：秒传命中 or 返回已收分片（断点续传）
# ---------------------------------------------------------------------------
def check_upload(file_hash, filename, size, chunk_size, source="web"):
    if not isinstance(file_hash, str) or not HEX64.match(file_hash.lower()):
        raise UploadError("文件指纹必须是 64 位 SHA-256 十六进制字符串")
    file_hash = file_hash.lower()
    try:
        size, chunk_size = int(size), int(chunk_size)
    except (TypeError, ValueError):
        raise UploadError("文件大小 / 分片大小必须是整数")
    if size <= 0 or size > settings.MAX_UPLOAD_SIZE:
        raise UploadError(f"文件大小超出范围（上限 {human_size(settings.MAX_UPLOAD_SIZE)}）")
    if chunk_size < 16 * 1024 or chunk_size > 16 * 1024 * 1024:
        raise UploadError("分片大小必须在 16KB ~ 16MB 之间")

    filename = clean_filename(filename)
    total = expected_total(size, chunk_size)
    record = FileRecord.objects.filter(file_hash=file_hash).first()
    if record:
        # 秒传：服务器已经有这份文件，一个字节都不用再传
        task = UploadTask.objects.create(
            task_id=uuid.uuid4().hex,
            file_hash=file_hash,
            original_name=filename,
            size=size,
            chunk_size=chunk_size,
            total_chunks=total,
            status="instant",
            source=source,
            bytes_sent=0,
            instant=True,
            finished_at=timezone.now(),
        )
        FileRecord.objects.filter(pk=record.pk).update(ref_count=F("ref_count") + 1)
        _log(task, file_hash, None, "INSTANT", "INSTANT",
             f"命中已有档案 {record.original_name}，本次传输 0 字节")
        return {
            "instant": True,
            "task_id": task.task_id,
            "file_hash": file_hash,
            "filename": record.original_name,
            "size": record.size,
            "saved_bytes": size,
            "message": f"秒传命中：服务器已有同一份文件，跳过 {human_size(size)} 的传输",
        }

    uploaded = sorted(
        UploadChunk.objects.filter(file_hash=file_hash).values_list("index", flat=True)
    )
    resumed = bool(uploaded)
    task = UploadTask.objects.create(
        task_id=uuid.uuid4().hex,
        file_hash=file_hash,
        original_name=filename,
        size=size,
        chunk_size=chunk_size,
        total_chunks=total,
        source=source,
        resumed=resumed,
    )
    _log(task, file_hash, None, "CHECK", "INFO",
         f"新建任务，共 {total} 片，服务端已有 {len(uploaded)} 片")
    if resumed:
        _log(task, file_hash, None, "RESUME", "RESUMED",
             f"断点续传：只需补传 {total - len(uploaded)} 片（已有编号 {uploaded}）")
    return {
        "instant": False,
        "task_id": task.task_id,
        "file_hash": file_hash,
        "filename": filename,
        "size": size,
        "chunk_size": chunk_size,
        "total_chunks": total,
        "uploaded": uploaded,
        "resumed": resumed,
        "message": ("断点续传：已收 " + str(len(uploaded)) + " 片，只补缺的"
                    if resumed else f"开始上传，共 {total} 片"),
    }


# ---------------------------------------------------------------------------
# 2) 单个分片到达：校验 → 幂等闸门 → 原子落盘
# ---------------------------------------------------------------------------
def receive_chunk(task, index, chunk_hash, data: bytes):
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise UploadError("分片序号必须是整数")
    if index < 0 or index >= task.total_chunks:
        raise UploadError(f"分片序号越界：应在 0 ~ {task.total_chunks - 1} 之间")
    if not isinstance(chunk_hash, str) or not HEX64.match(chunk_hash.lower()):
        raise UploadError("分片指纹格式不合法")
    chunk_hash = chunk_hash.lower()

    expected = expected_chunk_size(task.size, task.chunk_size, index, task.total_chunks)
    if len(data) != expected:
        _log(task, task.file_hash, index, "CHUNK", "REJECTED",
             f"大小不符：收到 {len(data)} 字节，应为 {expected} 字节")
        return {"result": "REJECTED", "index": index,
                "message": f"第 {index} 片大小不符（应为 {expected} 字节）"}

    actual = sha256_bytes(data)
    if actual != chunk_hash:
        _log(task, task.file_hash, index, "CHUNK", "CORRUPT",
             "分片内容与声明的指纹不一致，拒绝落盘")
        return {"result": "CORRUPT", "index": index,
                "message": f"第 {index} 片内容与指纹不符，拒绝落盘"}

    with transaction.atomic():
        existing = UploadChunk.objects.filter(
            file_hash=task.file_hash, index=index
        ).first()
        if existing:
            if existing.chunk_hash == actual:
                _log(task, task.file_hash, index, "CHUNK", "DUPLICATE",
                     "同一片重复投递，指纹一致 → 直接跳过，不重复写盘")
                return {"result": "DUPLICATE", "index": index,
                        "message": f"第 {index} 片重复到达，已拦下"}
            _log(task, task.file_hash, index, "CHUNK", "CONFLICT",
                 "同一编号带了不同内容，拒绝覆盖已有分片")
            return {"result": "CONFLICT", "index": index,
                    "message": f"第 {index} 片与已存内容冲突，拒绝覆盖"}

        path = _chunk_abs(task.file_hash, index)
        _atomic_write(path, data)
        try:
            UploadChunk.objects.create(
                file_hash=task.file_hash,
                index=index,
                chunk_hash=actual,
                size=len(data),
                rel_path=_rel(path),
            )
        except IntegrityError:
            # 并发下另一个请求抢先写了同一片：唯一约束兜底，视作重复投递
            _log(task, task.file_hash, index, "CHUNK", "DUPLICATE",
                 "并发抢写同一片，唯一约束兜底 → 幂等")
            return {"result": "DUPLICATE", "index": index,
                    "message": f"第 {index} 片并发重复，已拦下"}

        UploadTask.objects.filter(pk=task.pk).update(bytes_sent=F("bytes_sent") + len(data))
        _log(task, task.file_hash, index, "CHUNK", "APPLIED",
             f"第 {index} 片 {human_size(len(data))} 已原子落盘")
    return {"result": "APPLIED", "index": index,
            "message": f"第 {index} 片已落盘",
            "received": len(UploadChunk.objects.filter(file_hash=task.file_hash))}


# ---------------------------------------------------------------------------
# 3) 合并：按序拼接 → 整文件指纹校验 → 档案 / 缩略图 → 清理临时块
# ---------------------------------------------------------------------------
def merge_task(task):
    if task.status == "instant":
        return {"result": "INSTANT", "message": "秒传任务无需合并", "task_id": task.task_id}
    if FileRecord.objects.filter(file_hash=task.file_hash).exists():
        UploadTask.objects.filter(pk=task.pk).update(
            status="merged", finished_at=timezone.now()
        )
        _log(task, task.file_hash, None, "MERGE", "INFO", "同名档案已存在，直接复用")
        return {"result": "ALREADY", "message": "文件档案已存在，直接复用", "task_id": task.task_id}

    received = UploadChunk.objects.filter(file_hash=task.file_hash).order_by("index")
    have = {c.index: c for c in received}
    missing = [i for i in range(task.total_chunks) if i not in have]
    if missing:
        _log(task, task.file_hash, None, "MERGE", "REJECTED",
             f"还缺 {len(missing)} 片，拒绝合并：{missing[:8]}")
        return {"result": "INCOMPLETE", "missing": missing,
                "message": f"分片没收齐，还缺 {len(missing)} 片"}

    ext = safe_extension(task.original_name)
    final_path = _file_abs(task.file_hash, ext)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(final_path.name + ".merging")

    # 严格按编号 0 → n-1 拼接，就算分片是乱序到达的，拼出来也是原文件
    with open(tmp_path, "wb") as out:
        for index in range(task.total_chunks):
            part = _chunk_abs(task.file_hash, index)
            if not part.exists():
                tmp_path.unlink(missing_ok=True)
                _log(task, task.file_hash, index, "MERGE", "CORRUPT", "临时块丢失")
                return {"result": "CORRUPT", "message": f"第 {index} 片临时块丢失"}
            with open(part, "rb") as fh:
                out.write(fh.read())

    with open(tmp_path, "rb") as fh:
        whole_hash = sha256_stream(fh)
    if whole_hash != task.file_hash:
        tmp_path.unlink(missing_ok=True)
        _log(task, task.file_hash, None, "VERIFY", "CORRUPT",
             f"合并后指纹 {whole_hash[:12]} ≠ 声明指纹，丢弃文件")
        UploadTask.objects.filter(pk=task.pk).update(status="failed", finished_at=timezone.now())
        return {"result": "CORRUPT", "message": "合并后整文件指纹与声明不一致，已丢弃"}

    # 校验通过才让文件"正式亮相"，避免半成品被当成完整文件
    os.replace(tmp_path, final_path)

    FileRecord.objects.get_or_create(
        file_hash=task.file_hash,
        defaults={
            "original_name": task.original_name,
            "size": task.size,
            "chunk_size": task.chunk_size,
            "total_chunks": task.total_chunks,
            "rel_path": _rel(final_path),
            "mime": "image/png" if ext in image_extensions() else "application/octet-stream",
        },
    )
    record = FileRecord.objects.get(file_hash=task.file_hash)
    if ext in image_extensions():
        _make_thumbnail(final_path, record)

    UploadTask.objects.filter(pk=task.pk).update(status="merged", finished_at=timezone.now())
    _log(task, task.file_hash, None, "MERGE", "MERGED",
         f"按 0→{task.total_chunks - 1} 顺序拼接 {human_size(task.size)}，临时块已清理")
    _log(task, task.file_hash, None, "VERIFY", "VERIFIED",
         f"整文件 SHA-256 = {whole_hash[:16]}…，与上传前声明一致")
    for chunk in received:
        _chunk_abs(task.file_hash, chunk.index).unlink(missing_ok=True)
    return {
        "result": "MERGED",
        "task_id": task.task_id,
        "file_hash": task.file_hash,
        "size": task.size,
        "total_chunks": task.total_chunks,
        "message": f"合并完成：{task.total_chunks} 片拼接，整文件指纹校验通过",
    }


def _make_thumbnail(path: Path, record: FileRecord):
    """Pillow 生成 160×160 缩略图；图片损坏时静默跳过，不阻塞上传主链路。"""
    from PIL import Image

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((160, 160))
            thumb = _thumb_abs(record.file_hash)
            thumb.parent.mkdir(parents=True, exist_ok=True)
            img.save(thumb, "PNG")
        FileRecord.objects.filter(pk=record.pk).update(thumbnail=_rel(thumb))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 4) 看板状态
# ---------------------------------------------------------------------------
def build_state():
    tasks = list(UploadTask.objects.all()[:12])
    events = list(UploadEvent.objects.all()[:40])
    dup_pairs = set(
        UploadEvent.objects.filter(event_type="CHUNK", result="DUPLICATE")
        .values_list("file_hash", "chunk_index")
    )
    task_rows = []
    for task in tasks:
        have = task.received_indices()
        task_rows.append({
            "id": task.short_id,
            "task_id": task.task_id,
            "filename": task.original_name,
            "hash": task.file_hash[:12],
            "status": task.status,
            "status_label": task.get_status_display(),
            "source": task.get_source_display(),
            "instant": task.instant,
            "resumed": task.resumed,
            "bytes_sent": task.bytes_sent,
            "bytes_sent_h": human_size(task.bytes_sent),
            "size_h": human_size(task.size),
            "total": task.total_chunks,
            "received": len(have),
            "progress": task.progress_percent(),
            "finished": bool(task.finished_at),
            "mask": [
                {"i": i, "on": i in have,
                 "dup": (task.file_hash, i) in dup_pairs}
                for i in range(min(task.total_chunks, 40))
            ],
            "created": timezone.localtime(task.created_at).strftime("%H:%M:%S"),
        })

    return {
        "stats": {
            "files": FileRecord.objects.count(),
            "tasks": UploadTask.objects.count(),
            "chunks": UploadChunk.objects.count(),
            "duplicates": UploadEvent.objects.filter(result="DUPLICATE").count(),
            "instant": UploadTask.objects.filter(instant=True).count(),
            "resumes": UploadTask.objects.filter(resumed=True).count(),
            "saved_bytes": sum(
                UploadTask.objects.filter(instant=True).values_list("size", flat=True)
            ),
            "saved_h": human_size(sum(
                UploadTask.objects.filter(instant=True).values_list("size", flat=True)
            )),
        },
        "files": [
            {
                "name": f.original_name,
                "hash": f.short_hash,
                "size_h": human_size(f.size),
                "chunks": f.total_chunks,
                "refs": f.ref_count,
                "thumb": (settings.MEDIA_URL + f.thumbnail) if f.thumbnail else "",
                "created": timezone.localtime(f.created_at).strftime("%m-%d %H:%M"),
            }
            for f in FileRecord.objects.all()[:12]
        ],
        "tasks": task_rows,
        "events": [
            {
                "time": timezone.localtime(e.created_at).strftime("%H:%M:%S"),
                "type": e.get_event_type_display(),
                "result": e.result,
                "result_label": e.get_result_display(),
                "index": e.chunk_index,
                "detail": e.detail,
            }
            for e in events
        ],
        "limits": limits(),
    }


# ---------------------------------------------------------------------------
# 5) 一键演示：在服务端真跑一遍完整链路，页面一开始就有数据可看
# ---------------------------------------------------------------------------
def _make_sample(seed: bytes, target: int) -> bytes:
    """用 Pillow 画一张图，再补确定性字节到目标大小（PNG 尾部的额外字节不影响解码）。"""
    from PIL import Image, ImageDraw

    width, height = 320, 240
    img = Image.new("RGB", (width, height))
    px = img.load()
    a, b, c = seed[0], seed[1], seed[2]
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x * 3 + a) % 256, (y * 4 + b) % 256, (x + y + c) % 256)
    draw = ImageDraw.Draw(img)
    draw.rectangle((18, 18, width - 18, height - 18), outline=(255, 255, 255), width=3)
    draw.text((26, 24), f"CHUNK DEMO {seed[:3].hex()}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    raw = buf.getvalue()
    if len(raw) < target:
        raw += random.Random(int.from_bytes(seed[:4], "big")).randbytes(target - len(raw))
    return raw[:target]


def _demo_upload(raw: bytes, filename: str, order=None, repeat=None, seed=None):
    """走真实链路：check → 逐片 receive_chunk → merge。返回本次的动作计数。"""
    chunk_size = settings.DEMO_CHUNK_SIZE
    checked = check_upload(sha256_bytes(raw), filename, len(raw), chunk_size, source="demo")
    if checked["instant"]:
        return {"instant": 1, "applied": 0, "duplicate": 0, "merged": 0,
                "hash": checked["file_hash"]}
    task = UploadTask.objects.get(task_id=checked["task_id"])
    indices = list(order) if order else list(range(task.total_chunks))
    if repeat is not None:
        pos = min(1, len(indices))
        indices.insert(pos, repeat)
    counter = {"instant": 0, "applied": 0, "duplicate": 0, "merged": 0,
               "hash": task.file_hash}
    for index, blob in [(i, raw[i * chunk_size:(i + 1) * chunk_size]) for i in indices]:
        res = receive_chunk(task, index, sha256_bytes(blob), blob)
        if res["result"] == "APPLIED":
            counter["applied"] += 1
        elif res["result"] == "DUPLICATE":
            counter["duplicate"] += 1
    merged = merge_task(task)
    if merged["result"] in ("MERGED", "ALREADY"):
        counter["merged"] += 1
    return counter


def _base_hash():
    """保证「秒传」场景有可命中的档案：没有就先造一份。"""
    record = FileRecord.objects.order_by("id").first()
    if record:
        return record.file_hash
    raw = _make_sample(b"\x01\x02\x03\x04", 5 * settings.DEMO_CHUNK_SIZE)
    result = _demo_upload(raw, "demo-base.png")
    return result["hash"]


def run_demo_scenario(name: str):
    size = 5 * settings.DEMO_CHUNK_SIZE
    total = {"instant": 0, "applied": 0, "duplicate": 0, "merged": 0, "resumed": 0}
    label = ""

    if name == "normal":
        raw = _make_sample(os.urandom(8), size)
        order = [2, 0, 4, 1, 3]
        r = _demo_upload(raw, "乱序到达-渐变图.png", order=order)
        total.update({k: v for k, v in r.items() if k in total})
        label = "普通分片：5 片故意打乱顺序到达，合并时按编号拼回原文件"
    elif name == "duplicate":
        raw = _make_sample(os.urandom(8), size)
        r = _demo_upload(raw, "重复投递-渐变图.png", repeat=1)
        total.update({k: v for k, v in r.items() if k in total})
        label = "重复分片：第 1 片故意连投两次，第二次被幂等闸门拦下"
    elif name == "resume":
        raw = _make_sample(os.urandom(8), size)
        chunk_size = settings.DEMO_CHUNK_SIZE
        first = check_upload(sha256_bytes(raw), "中断后续传-渐变图.png",
                             len(raw), chunk_size, source="demo")
        if not first["instant"]:
            t1 = UploadTask.objects.get(task_id=first["task_id"])
            for index in (0, 1):
                blob = raw[index * chunk_size:(index + 1) * chunk_size]
                receive_chunk(t1, index, sha256_bytes(blob), blob)
            _log(t1, t1.file_hash, None, "DEMO", "INFO", "模拟断网：任务停在「上传中」")
            second = check_upload(sha256_bytes(raw), "中断后续传-渐变图.png",
                                  len(raw), chunk_size, source="demo")
            t2 = UploadTask.objects.get(task_id=second["task_id"])
            for index in range(t2.total_chunks):
                if index in second["uploaded"]:
                    continue
                blob = raw[index * chunk_size:(index + 1) * chunk_size]
                res = receive_chunk(t2, index, sha256_bytes(blob), blob)
                if res["result"] == "APPLIED":
                    total["applied"] += 1
            if merge_task(t2)["result"] in ("MERGED", "ALREADY"):
                total["merged"] += 1
            total["resumed"] = 1
            label = f"断点续传：先传 2 片后中断，新任务只补 {t2.total_chunks - 2} 片"
    elif name == "instant":
        file_hash = _base_hash()
        record = FileRecord.objects.get(file_hash=file_hash)
        result = check_upload(file_hash, "秒传-同名文件.png", record.size,
                              record.chunk_size, source="demo")
        total["instant"] = 1 if result.get("instant") else 0
        label = f"秒传：同样内容再次上传，命中已有档案，0 字节传输，省下 {human_size(record.size)}"
    elif name == "all":
        for sub in ("normal", "duplicate", "resume", "instant"):
            part = run_demo_scenario(sub)
            for key in total:
                total[key] += part.get(key, 0)
        label = "已串起四个场景：乱序分片 → 重复拦下 → 断点续传 → 秒传命中"
    else:
        raise UploadError(f"未知演示场景：{name}")

    _log(None, "", None, "DEMO", "INFO", f"[{name}] {label}")
    return {"scenario": name, "message": label, **total}


def reset_demo():
    """清空数据库与临时文件，只作用于本项目 media/ 下自建的三个目录。"""
    UploadEvent.objects.all().delete()
    UploadChunk.objects.all().delete()
    UploadTask.objects.all().delete()
    FileRecord.objects.all().delete()
    for sub in ("chunks", "files", "thumbs"):
        path = Path(settings.MEDIA_ROOT) / sub
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    _log(None, "", None, "RESET", "INFO", "演示数据已清空，可以重新跑一遍")
    return {"ok": True, "message": "已清空文件档案、上传任务、分片与事件"}
