"""HTTP 层：把浏览器的请求翻译成 service 里的动作，再把结果翻成 JSON。

Django 的「安检口」在这里起作用：CSRF 中间件会检查每个 POST 的凭证，
页面用 @ensure_csrf_cookie 种下 csrftoken，前端 fetch 把它放进 X-CSRFToken 头。
"""
import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import service
from .models import UploadTask
from .service import UploadError

logger = logging.getLogger(__name__)


def _body(request):
    try:
        return json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UploadError("请求体不是合法的 JSON")


def _task(task_id):
    try:
        return UploadTask.objects.get(task_id=task_id)
    except UploadTask.DoesNotExist:
        raise UploadError("上传任务不存在或已被清空", 404)


def _json(data, status=200):
    return JsonResponse(data, status=status,
                        json_dumps_params={"ensure_ascii": False})


def _handle(fn):
    """统一的错误翻译：业务错误给 JSON，意外错误记日志后给 500。"""
    def wrapper(request, *args, **kwargs):
        try:
            return _json(fn(request, *args, **kwargs))
        except UploadError as exc:
            return _json({"ok": False, "error": str(exc)}, status=exc.code)
        except Exception as exc:  # pragma: no cover - 兜底，避免页面拿到 HTML 报错
            logger.exception("上传接口异常")
            return _json({"ok": False, "error": f"服务端异常：{exc}"}, status=500)
    return wrapper


@ensure_csrf_cookie
def console(request):
    """演示看板：HTML 只是骨架，数据全部由 Vue 通过 /api/state/ 拉取。"""
    return render(request, "console.html", {
        "default_chunk_size": service.limits()["chunk_size"],
        "demo_chunk_size": service.limits()["demo_chunk_size"],
    })


@require_GET
@_handle
def api_state(request):
    return {"ok": True, **service.build_state()}


@require_POST
@_handle
def api_check(request):
    body = _body(request)
    result = service.check_upload(
        file_hash=body.get("file_hash"),
        filename=body.get("filename", "upload.bin"),
        size=body.get("size"),
        chunk_size=body.get("chunk_size"),
        source="web",
    )
    return {"ok": True, **result}


@require_POST
@_handle
def api_chunk(request):
    task = _task(request.POST.get("task_id"))
    upload = request.FILES.get("file")
    if upload is None:
        raise UploadError("缺少分片文件字段 file")
    if upload.size > 16 * 1024 * 1024:
        raise UploadError("单个分片超过 16MB 上限", 413)
    result = service.receive_chunk(
        task=task,
        index=request.POST.get("index"),
        chunk_hash=request.POST.get("chunk_hash", ""),
        data=upload.read(),
    )
    return {"ok": True, **result}


@require_POST
@_handle
def api_merge(request):
    task = _task(_body(request).get("task_id"))
    return {"ok": True, **service.merge_task(task)}


@require_POST
@_handle
def api_demo(request):
    return {"ok": True, **service.run_demo_scenario(_body(request).get("scenario", "all"))}


@require_POST
@_handle
def api_reset(request):
    return {"ok": True, **service.reset_demo()}
