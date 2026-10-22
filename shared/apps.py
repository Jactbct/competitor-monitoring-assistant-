from django.apps import AppConfig


class SharedConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "shared"
    label = "shared"
    verbose_name = "共享层（L1 公开快照）"
