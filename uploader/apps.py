from django.apps import AppConfig


class UploaderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "uploader"
    verbose_name = "大文件分片上传与秒传"
