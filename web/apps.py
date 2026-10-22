from django.apps import AppConfig


class WebConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "web"
    label = "web"
    verbose_name = "Web 界面"
