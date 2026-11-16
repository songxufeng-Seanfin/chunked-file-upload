"""回归测试：把「分片 → 幂等 → 乱序 → 合并 → 校验 → 秒传 → 续传」逐项钉死。

测试用独立的临时 MEDIA_ROOT，不会碰演示页面正在展示的数据。
"""
import io
import json
import random
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from . import service
from .hashing import (
    clean_filename,
    expected_chunk_size,
    expected_total,
    human_size,
    safe_extension,
    sha256_bytes,
    sha256_stream,
)
from .models import FileRecord, UploadChunk, UploadEvent, UploadTask
from .service import UploadError

TEST_MEDIA = Path(tempfile.mkdtemp(prefix="xj-upload-test-"))


def blob(seed=1, size=None):
    size = size or 5 * settings.DEMO_CHUNK_SIZE
    return random.Random(seed).randbytes(size)


def png_blob(target=None):
    """用 Pillow 造一张真实 PNG，再补字节到目标大小（PNG 允许 IEND 后有尾部数据）。"""
    target = target or 5 * settings.DEMO_CHUNK_SIZE
    img = Image.new("RGB", (160, 120), (60, 130, 220))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    raw = buf.getvalue()
    if len(raw) < target:
        raw += random.Random(99).randbytes(target - len(raw))
    return raw[:target]


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class UploadCoreTests(TestCase):
    def setUp(self):
        service.reset_demo()
        self.raw = blob(seed=7)
        self.cs = settings.DEMO_CHUNK_SIZE

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def _check(self, raw=None, name="demo.png"):
        raw = raw if raw is not None else self.raw
        return service.check_upload(sha256_bytes(raw), name, len(raw), self.cs, source="demo")

    def _task(self, checked):
        return UploadTask.objects.get(task_id=checked["task_id"])

    def _send(self, task, index, raw=None, data=None):
        raw = raw if raw is not None else self.raw
        content = data if data is not None else raw[index * self.cs:(index + 1) * self.cs]
        return service.receive_chunk(task, index, sha256_bytes(content), content)

    # ---------- 基础工具 ----------
    def test_hash_helpers(self):
        data = b"hello chunk"
        self.assertEqual(sha256_bytes(data), sha256_bytes(data))
        self.assertNotEqual(sha256_bytes(data), sha256_bytes(b"hello chunK"))
        self.assertEqual(sha256_stream(io.BytesIO(data)), sha256_bytes(data))
        self.assertEqual(expected_total(1025, 512), 3)
        self.assertEqual(expected_chunk_size(1025, 512, 2, 3), 1)
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(safe_extension("a/../photo.PNG"), ".png")
        self.assertEqual(clean_filename("C:\\tmp\\photo.png"), "photo.png")

    def test_check_creates_task(self):
        checked = self._check()
        self.assertFalse(checked["instant"])
        self.assertEqual(checked["total_chunks"], 5)
        self.assertEqual(checked["uploaded"], [])
        self.assertEqual(UploadTask.objects.count(), 1)
        self.assertEqual(UploadEvent.objects.filter(event_type="CHECK").count(), 1)

    def test_check_rejects_bad_hash(self):
        with self.assertRaises(UploadError):
            service.check_upload("not-a-hash", "a.png", 1024, self.cs)

    # ---------- 分片落盘与幂等 ----------
    def test_receive_chunk_writes_disk(self):
        task = self._task(self._check())
        result = self._send(task, 0)
        self.assertEqual(result["result"], "APPLIED")
        self.assertEqual(UploadChunk.objects.count(), 1)
        self.assertEqual(task.received_indices(), {0})
        task.refresh_from_db()
        self.assertEqual(task.bytes_sent, self.cs)
        self.assertTrue((TEST_MEDIA / UploadChunk.objects.first().rel_path).exists())

    def test_duplicate_chunk_is_idempotent(self):
        task = self._task(self._check())
        self._send(task, 0)
        second = self._send(task, 0)
        self.assertEqual(second["result"], "DUPLICATE")
        self.assertEqual(UploadChunk.objects.count(), 1)
        task.refresh_from_db()
        self.assertEqual(task.bytes_sent, self.cs)
        self.assertEqual(UploadEvent.objects.filter(result="DUPLICATE").count(), 1)

    def test_chunk_hash_mismatch_rejected(self):
        task = self._task(self._check())
        content = self.raw[:self.cs]
        result = service.receive_chunk(task, 0, sha256_bytes(b"wrong"), content)
        self.assertEqual(result["result"], "CORRUPT")
        self.assertEqual(UploadChunk.objects.count(), 0)

    def test_chunk_wrong_size_rejected(self):
        task = self._task(self._check())
        result = service.receive_chunk(task, 0, sha256_bytes(b"short"), b"short")
        self.assertEqual(result["result"], "REJECTED")
        self.assertEqual(UploadChunk.objects.count(), 0)

    def test_conflicting_chunk_not_overwritten(self):
        task = self._task(self._check())
        other = blob(seed=99)[:self.cs]
        self._send(task, 0, data=other)
        result = self._send(task, 0)
        self.assertEqual(result["result"], "CONFLICT")
        self.assertEqual(UploadChunk.objects.get(index=0).chunk_hash, sha256_bytes(other))

    def test_unique_constraint_on_chunk(self):
        task = self._task(self._check())
        self._send(task, 0)
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UploadChunk.objects.create(
                    file_hash=task.file_hash, index=0, chunk_hash="x" * 64,
                    size=1, rel_path="chunks/duplicate.part",
                )

    # ---------- 乱序 / 合并 / 校验 ----------
    def test_out_of_order_upload_merges_correctly(self):
        task = self._task(self._check())
        for index in (2, 0, 4, 1, 3):
            self.assertEqual(self._send(task, index)["result"], "APPLIED")
        merged = service.merge_task(task)
        self.assertEqual(merged["result"], "MERGED")
        record = FileRecord.objects.get(file_hash=task.file_hash)
        with open(TEST_MEDIA / record.rel_path, "rb") as fh:
            self.assertEqual(fh.read(), self.raw)
        self.assertEqual(sha256_stream(open(TEST_MEDIA / record.rel_path, "rb")), task.file_hash)
        task.refresh_from_db()
        self.assertEqual(task.status, "merged")
        self.assertTrue(UploadEvent.objects.filter(result="VERIFIED").exists())
        # 合并成功后临时块被清理，但分片记录保留下来给位图看
        self.assertEqual(UploadChunk.objects.count(), 5)
        self.assertFalse((TEST_MEDIA / "chunks" / task.file_hash[:16] / "00000.part").exists())

    def test_merge_rejects_missing_chunks(self):
        task = self._task(self._check())
        self._send(task, 0)
        self._send(task, 3)
        result = service.merge_task(task)
        self.assertEqual(result["result"], "INCOMPLETE")
        self.assertEqual(result["missing"], [1, 2, 4])
        self.assertEqual(FileRecord.objects.count(), 0)

    def test_merge_rejects_wrong_whole_hash(self):
        # 声明的是 A 的指纹，实际上传的是 B 的分片：单片校验都能过，
        # 只有合并后的「整文件二次校验」能抓出来。
        other = blob(seed=42)
        task = self._task(self._check())
        for index in range(5):
            content = other[index * self.cs:(index + 1) * self.cs]
            service.receive_chunk(task, index, sha256_bytes(content), content)
        result = service.merge_task(task)
        self.assertEqual(result["result"], "CORRUPT")
        self.assertEqual(FileRecord.objects.count(), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "failed")

    def test_thumbnail_generated_for_image(self):
        raw = png_blob()
        task = self._task(self._check(raw=raw, name="photo.png"))
        for index in range(5):
            self._send(task, index, raw=raw)
        service.merge_task(task)
        record = FileRecord.objects.get(file_hash=task.file_hash)
        self.assertTrue(record.thumbnail)
        self.assertTrue((TEST_MEDIA / record.thumbnail).exists())

    # ---------- 秒传 / 断点续传 ----------
    def test_instant_upload_hits_existing_record(self):
        task = self._task(self._check())
        for index in range(5):
            self._send(task, index)
        service.merge_task(task)
        second = self._check(name="同名文件.png")
        self.assertTrue(second["instant"])
        self.assertEqual(second["saved_bytes"], len(self.raw))
        self.assertEqual(UploadTask.objects.get(task_id=second["task_id"]).bytes_sent, 0)
        self.assertEqual(FileRecord.objects.get(file_hash=task.file_hash).ref_count, 1)
        self.assertEqual(service.build_state()["stats"]["instant"], 1)

    def test_resume_only_uploads_missing_chunks(self):
        first = self._check()
        t1 = self._task(first)
        self._send(t1, 0)
        self._send(t1, 1)
        second = self._check(name="再传一次.png")  # 模拟刷新页面 / 换浏览器
        self.assertTrue(second["resumed"])
        self.assertEqual(second["uploaded"], [0, 1])
        t2 = self._task(second)
        for index in (2, 3, 4):
            self.assertEqual(self._send(t2, index)["result"], "APPLIED")
        self.assertEqual(service.merge_task(t2)["result"], "MERGED")
        self.assertEqual(UploadTask.objects.filter(resumed=True).count(), 1)
        self.assertEqual(FileRecord.objects.count(), 1)

    # ---------- 演示 / 状态 / 清空 ----------
    def test_all_demo_scenarios(self):
        result = service.run_demo_scenario("all")
        self.assertGreater(result["applied"], 10)
        self.assertGreaterEqual(result["duplicate"], 1)
        self.assertEqual(result["instant"], 1)
        state = service.build_state()
        self.assertEqual(state["stats"]["files"], 3)
        self.assertTrue(state["tasks"][0]["mask"])
        self.assertIn("chunk_size", state["limits"])

    def test_reset_clears_everything(self):
        service.run_demo_scenario("all")
        service.reset_demo()
        for model in (FileRecord, UploadTask, UploadChunk):
            self.assertEqual(model.objects.count(), 0)
        self.assertEqual(UploadEvent.objects.filter(event_type="RESET").count(), 1)
        for sub in ("chunks", "files", "thumbs"):
            self.assertTrue((TEST_MEDIA / sub).exists())


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class UploadApiTests(TestCase):
    def setUp(self):
        service.reset_demo()
        self.raw = blob(seed=11)
        self.cs = settings.DEMO_CHUNK_SIZE

    def test_console_page_and_state(self):
        response = self.client.get(reverse("console"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="app"')
        self.assertIn("csrftoken", response.cookies)
        state = self.client.get(reverse("api_state")).json()
        self.assertTrue(state["ok"])
        self.assertIn("limits", state)

    def test_csrf_rejects_post_without_token(self):
        strict = Client(enforce_csrf_checks=True)
        response = strict.post(reverse("api_check"),
                               data=json.dumps({"file_hash": "a" * 64, "size": 100, "chunk_size": self.cs}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_full_http_roundtrip(self):
        checked = self.client.post(
            reverse("api_check"),
            data=json.dumps({"file_hash": sha256_bytes(self.raw), "filename": "http.png",
                             "size": len(self.raw), "chunk_size": self.cs}),
            content_type="application/json",
        ).json()
        self.assertFalse(checked["instant"])
        task_id = checked["task_id"]
        for index in range(5):
            chunk = self.raw[index * self.cs:(index + 1) * self.cs]
            form = {"task_id": task_id, "index": str(index),
                    "chunk_hash": sha256_bytes(chunk),
                    "file": SimpleUploadedFile(f"part{index}", chunk)}
            response = self.client.post(reverse("api_chunk"), form).json()
            self.assertEqual(response["result"], "APPLIED")
        merged = self.client.post(
            reverse("api_merge"), data=json.dumps({"task_id": task_id}),
            content_type="application/json",
        ).json()
        self.assertEqual(merged["result"], "MERGED")
        self.assertEqual(FileRecord.objects.count(), 1)

        # 同一个文件再走一次 check：秒传命中
        instant = self.client.post(
            reverse("api_check"),
            data=json.dumps({"file_hash": sha256_bytes(self.raw), "filename": "http.png",
                             "size": len(self.raw), "chunk_size": self.cs}),
            content_type="application/json",
        ).json()
        self.assertTrue(instant["instant"])

    def test_demo_and_reset_endpoints(self):
        response = self.client.post(reverse("api_demo"),
                                   data=json.dumps({"scenario": "duplicate"}),
                                   content_type="application/json").json()
        self.assertTrue(response["ok"])
        self.assertGreaterEqual(response["duplicate"], 1)
        self.client.post(reverse("api_reset"), data="{}", content_type="application/json")
        self.assertEqual(FileRecord.objects.count(), 0)

    def test_unknown_task_returns_404(self):
        response = self.client.post(
            reverse("api_merge"), data=json.dumps({"task_id": "does-not-exist"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["ok"])
