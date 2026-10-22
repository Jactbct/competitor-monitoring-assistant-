"""页面路由。没有独立前端，所以不存在跨端接口契约（架构文档 §6.2）。"""

from django.urls import path

from web.views import auth, ops, products, settings, suggestions, targets, tasks, workbench

urlpatterns = [
    # 认证
    path("login", auth.login_view, name="login"),
    path("logout", auth.logout_view, name="logout"),
    # 工作台
    path("", workbench.index, name="workbench"),
    path("workbench/alerts", workbench.alerts, name="workbench_alerts"),
    # 监控对象
    path("targets", targets.index, name="targets"),
    path("targets/add", targets.create, name="target_add"),
    path("targets/<uuid:pk>", targets.detail, name="target_detail"),
    path("targets/<uuid:pk>/priority", targets.update_priority, name="target_priority"),
    path("targets/<uuid:pk>/delete", targets.remove, name="target_delete"),
    # 建议
    path("suggestions", suggestions.index, name="suggestions"),
    path("suggestions/export.csv", suggestions.export_csv, name="suggestions_export"),
    path("suggestions/<uuid:pk>/panel", suggestions.detail_panel, name="suggestion_panel"),
    path("suggestions/<uuid:pk>/accept", suggestions.accept, name="suggestion_accept"),
    path("suggestions/<uuid:pk>/ignore", suggestions.ignore, name="suggestion_ignore"),
    path(
        "suggestions/<uuid:pk>/false-positive",
        suggestions.false_positive,
        name="suggestion_false_positive",
    ),
    # 任务
    path("tasks", tasks.index, name="tasks"),
    path("tasks/<uuid:pk>/status", tasks.update_status, name="task_status"),
    # 自有商品
    path("products", products.index, name="products"),
    path("products/add", products.create, name="product_add"),
    path("products/<uuid:pk>/update", products.update, name="product_update"),
    # 设置 / 平台运营
    path("settings", settings.index, name="settings"),
    path("settings/members/add", settings.add_member, name="member_add"),
    path("ops/health", ops.health_page, name="ops_health"),
]
