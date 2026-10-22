from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jobs"
    label = "jobs"
    verbose_name = "任务与调度"
