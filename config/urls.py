from django.contrib import admin
from django.urls import include, path

from web.views.health import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", healthz, name="health"),
    path("", include("web.urls")),
]
