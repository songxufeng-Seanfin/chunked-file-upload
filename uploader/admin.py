from django.contrib import admin

from .models import FileRecord, UploadChunk, UploadEvent, UploadTask


@admin.register(FileRecord)
class FileRecordAdmin(admin.ModelAdmin):
    list_display = ("original_name", "short_hash", "size", "total_chunks", "ref_count", "created_at")
    search_fields = ("file_hash", "original_name")


@admin.register(UploadTask)
class UploadTaskAdmin(admin.ModelAdmin):
    list_display = ("short_id", "original_name", "status", "source", "bytes_sent", "created_at")
    list_filter = ("status", "source")


@admin.register(UploadChunk)
class UploadChunkAdmin(admin.ModelAdmin):
    list_display = ("file_hash", "index", "size", "created_at")


@admin.register(UploadEvent)
class UploadEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event_type", "result", "chunk_index", "detail")
    list_filter = ("result", "event_type")
