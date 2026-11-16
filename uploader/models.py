"""四张表，各司其职：

FileRecord    —— 「这份文件服务器上已经有了」的档案，秒传靠它命中
UploadTask    —— 一次上传会话（可能中断、可能秒传、可能是演示造的）
UploadChunk   —— 已经落盘的分片，挂在 file_hash 上而不是某次会话上，
                 所以换个会话/刷新页面依然能续传
UploadEvent   —— 可观测性：每次校验、拦下、合并都留痕，看板才讲得清过程
"""
from django.db import models


class FileRecord(models.Model):
    file_hash = models.CharField("文件指纹 SHA-256", max_length=64, unique=True)
    original_name = models.CharField("原始文件名", max_length=255)
    size = models.BigIntegerField("文件大小(字节)")
    chunk_size = models.IntegerField("分片大小")
    total_chunks = models.IntegerField("分片总数")
    rel_path = models.CharField("合并后相对路径", max_length=300)
    thumbnail = models.CharField("缩略图相对路径", max_length=300, blank=True)
    mime = models.CharField("文件类型", max_length=120, blank=True)
    ref_count = models.IntegerField("被秒传命中次数", default=0)
    created_at = models.DateTimeField("入库时间", auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "文件档案"

    def __str__(self):
        return f"{self.original_name} · {self.file_hash[:12]}"

    @property
    def short_hash(self):
        return self.file_hash[:12]


class UploadTask(models.Model):
    STATUS_CHOICES = [
        ("uploading", "上传中"),
        ("merged", "已完成"),
        ("instant", "秒传命中"),
        ("failed", "失败"),
    ]
    SOURCE_CHOICES = [("web", "浏览器上传"), ("demo", "一键演示")]

    task_id = models.CharField("任务 ID", max_length=36, unique=True)
    file_hash = models.CharField("文件指纹", max_length=64, db_index=True)
    original_name = models.CharField("原始文件名", max_length=255)
    size = models.BigIntegerField("文件大小(字节)")
    chunk_size = models.IntegerField("分片大小")
    total_chunks = models.IntegerField("分片总数")
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default="uploading")
    source = models.CharField("来源", max_length=10, choices=SOURCE_CHOICES, default="web")
    bytes_sent = models.BigIntegerField("本次实际传输字节", default=0)
    instant = models.BooleanField("秒传命中", default=False)
    resumed = models.BooleanField("断点续传", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    finished_at = models.DateTimeField("完成时间", null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "上传任务"

    def __str__(self):
        return f"{self.task_id[:8]} · {self.get_status_display()}"

    @property
    def short_id(self):
        return self.task_id[:8]

    def received_indices(self):
        """已落盘的分片编号；分片按 file_hash 共享，天然支持跨会话续传。"""
        return set(
            UploadChunk.objects.filter(file_hash=self.file_hash).values_list("index", flat=True)
        )

    def progress_percent(self):
        if not self.total_chunks:
            return 0
        return round(len(self.received_indices()) * 100 / self.total_chunks)


class UploadChunk(models.Model):
    file_hash = models.CharField("文件指纹", max_length=64, db_index=True)
    index = models.PositiveIntegerField("分片序号")
    chunk_hash = models.CharField("分片指纹 SHA-256", max_length=64)
    size = models.PositiveIntegerField("分片字节数")
    rel_path = models.CharField("临时块相对路径", max_length=300)
    created_at = models.DateTimeField("落盘时间", auto_now_add=True)

    class Meta:
        ordering = ["file_hash", "index"]
        constraints = [
            models.UniqueConstraint(fields=["file_hash", "index"], name="uniq_file_chunk_index")
        ]
        verbose_name = "已收分片"

    def __str__(self):
        return f"{self.file_hash[:10]} #{self.index}"


class UploadEvent(models.Model):
    EVENT_CHOICES = [
        ("CHECK", "上传前检查"),
        ("CHUNK", "分片到达"),
        ("MERGE", "合并"),
        ("VERIFY", "整文件校验"),
        ("INSTANT", "秒传"),
        ("RESUME", "断点续传"),
        ("RESET", "清空重来"),
        ("DEMO", "演示"),
    ]
    RESULT_CHOICES = [
        ("APPLIED", "已落盘"),
        ("DUPLICATE", "重复拦下"),
        ("CONFLICT", "指纹冲突"),
        ("CORRUPT", "内容校验失败"),
        ("REJECTED", "参数/前置拒绝"),
        ("INSTANT", "秒传命中"),
        ("RESUMED", "续传补齐"),
        ("MERGED", "合并完成"),
        ("VERIFIED", "校验通过"),
        ("INFO", "记录"),
    ]

    task = models.ForeignKey(
        UploadTask, null=True, blank=True, on_delete=models.CASCADE, related_name="events"
    )
    file_hash = models.CharField("文件指纹", max_length=64, blank=True)
    chunk_index = models.IntegerField("分片序号", null=True, blank=True)
    event_type = models.CharField("事件", max_length=16, choices=EVENT_CHOICES)
    result = models.CharField("结果", max_length=16, choices=RESULT_CHOICES)
    detail = models.CharField("说明", max_length=255, blank=True)
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "上传事件"

    def __str__(self):
        return f"{self.get_event_type_display()} · {self.get_result_display()}"
