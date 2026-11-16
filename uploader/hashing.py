"""指纹（哈希）与分片的小工具层。

「哈希」用大白话说：给一堆字节算出一串固定长度的身份证号。
内容只要改一个字节，身份证号就完全不同；同样的内容，永远算出同一串。
"""
import hashlib

from django.conf import settings


def sha256_bytes(data: bytes) -> str:
    """整块字节的 SHA-256 十六进制字符串。"""
    return hashlib.sha256(data).hexdigest()


def sha256_stream(fp, chunk_bytes: int = 1024 * 1024) -> str:
    """边读边算，适合合并后的大文件，不把整个文件一次性塞进内存。"""
    digest = hashlib.sha256()
    for block in iter(lambda: fp.read(chunk_bytes), b""):
        digest.update(block)
    return digest.hexdigest()


def expected_total(size: int, chunk_size: int) -> int:
    """按文件大小和分片大小算应该有几片（服务端自己算，不信前端）。"""
    if size <= 0 or chunk_size <= 0:
        raise ValueError("文件大小和分片大小必须为正数")
    return (size + chunk_size - 1) // chunk_size


def expected_chunk_size(size: int, chunk_size: int, index: int, total: int) -> int:
    """第 index 片应该有多少字节：只有最后一片允许变小。"""
    if index != total - 1:
        return chunk_size
    return size - chunk_size * (total - 1)


def split_chunks(data: bytes, chunk_size: int):
    """把字节流切成 (序号, 内容) 生成器，供服务端演示复用真实链路。"""
    total = expected_total(len(data), chunk_size)
    for index in range(total):
        yield index, data[index * chunk_size : (index + 1) * chunk_size]


def human_size(num) -> str:
    """把字节数变成人能读的样子：1536 → 1.5 KB。"""
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def safe_extension(filename: str) -> str:
    """只取文件名末尾的字母数字扩展名，防止 ../ 之类的路径穿越。"""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    cleaned = "".join(c for c in suffix if c.isalnum())
    return ("." + cleaned[:8]) if cleaned else ""


def clean_filename(filename: str) -> str:
    """去掉客户端可能带来的目录部分，只留文件名。"""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    return name[:200] or "upload.bin"


def image_extensions():
    return {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def limits():
    return {
        "chunk_size": settings.UPLOAD_CHUNK_SIZE,
        "concurrency": settings.UPLOAD_CONCURRENCY,
        "max_size": settings.MAX_UPLOAD_SIZE,
        "demo_chunk_size": settings.DEMO_CHUNK_SIZE,
    }
