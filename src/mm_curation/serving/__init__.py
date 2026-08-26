"""serving 模块：检索服务的 HTTP 层。"""

from .api import app, create_app

__all__ = ["app", "create_app"]
