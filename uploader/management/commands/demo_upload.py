"""命令行一键演示：python manage.py demo_upload --scenario all"""
from django.core.management.base import BaseCommand

from uploader import service


class Command(BaseCommand):
    help = "在服务端真实跑一遍分片上传链路（不打开浏览器也能验证）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario", default="all",
            choices=["normal", "duplicate", "resume", "instant", "all", "reset"],
            help="演示场景，默认 all",
        )

    def handle(self, *args, **options):
        name = options["scenario"]
        if name == "reset":
            result = service.reset_demo()
            self.stdout.write(self.style.WARNING(result["message"]))
            return
        result = service.run_demo_scenario(name)
        self.stdout.write(self.style.SUCCESS(result["message"]))
        state = service.build_state()["stats"]
        self.stdout.write(
            "生效分片 {chunks} · 重复拦下 {duplicates} · 秒传 {instant} · "
            "断点续传 {resumes} · 文件档案 {files}".format(**state)
        )
