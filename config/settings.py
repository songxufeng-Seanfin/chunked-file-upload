"""大文件分片上传与秒传 · 项目配置。

设计取向：开箱即跑。SQLite + 本地磁盘，不需要 Redis / 对象存储 / npm 构建；
Vue 3 的全局构建已放在 static/vendor/，断网也能打开页面。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "UPLOAD_SECRET_KEY", "demo-only-upload-secret-key-change-me-in-production"
)
DEBUG = os.environ.get("UPLOAD_DEBUG", "1") != "0"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "uploader",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# 分片参数
#   UPLOAD_CHUNK_SIZE  浏览器真实上传的默认分片大小（2MB）
#   DEMO_CHUNK_SIZE    服务端一键演示用的分片大小，调小是为了让位图看得清
# ---------------------------------------------------------------------------
UPLOAD_CHUNK_SIZE = int(os.environ.get("UPLOAD_CHUNK_SIZE", 2 * 1024 * 1024))
DEMO_CHUNK_SIZE = int(os.environ.get("DEMO_CHUNK_SIZE", 256 * 1024))
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", 512 * 1024 * 1024))
UPLOAD_CONCURRENCY = int(os.environ.get("UPLOAD_CONCURRENCY", 3))
