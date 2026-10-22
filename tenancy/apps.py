from django.apps import AppConfig


class TenancyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenancy"
    label = "tenancy"
    verbose_name = "租户层（L2 隔离业务数据）"
