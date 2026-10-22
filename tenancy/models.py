"""租户层（L2）：所有业务数据，``tenant_id`` + RLS 强制隔离。

除 :class:`Tenant` 外，每张表都有 ``tenant_id`` 且默认管理器自动按当前上下文过滤
（:mod:`core.tenant`）。生产环境另有 PG RLS 作为最后一道防线。
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from core.db import quote_table
from core.tenant import TenantScopedManager


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField()
    plan = models.TextField(default="basic")
    quota_json = models.JSONField(default=dict)
    status = models.TextField(default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = quote_table("app", "tenant")

    def __str__(self) -> str:
        return self.name

    @property
    def target_quota(self) -> int:
        return int(self.quota_json.get("targets", 100))


class AppUserManager(BaseUserManager):
    """用户查询不按租户过滤：登录发生在租户上下文建立之前。"""

    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra):
        if not email:
            raise ValueError("email 必填")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("role", AppUser.ROLE_PLATFORM_OPS)
        extra.setdefault("tenant", None)
        return self.create_user(email, password, **extra)


class AppUser(AbstractBaseUser, PermissionsMixin):
    ROLE_ADMIN = "admin"
    ROLE_OPERATOR = "operator"
    ROLE_DESIGNER = "designer"
    ROLE_VIEWER = "viewer"
    ROLE_PLATFORM_OPS = "platform_ops"
    ROLE_CHOICES = (
        (ROLE_ADMIN, "管理员"),
        (ROLE_OPERATOR, "运营"),
        (ROLE_DESIGNER, "美工"),
        (ROLE_VIEWER, "只读"),
        (ROLE_PLATFORM_OPS, "平台运营"),
    )
    #: 可写建议/任务的角色
    WRITE_ROLES = {ROLE_ADMIN, ROLE_OPERATOR}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, db_column="tenant_id")
    email = models.TextField(unique=True)
    password = models.TextField(db_column="password_hash")
    role = models.TextField(default=ROLE_OPERATOR, choices=ROLE_CHOICES)
    status = models.TextField(default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = quote_table("app", "app_user")

    def __str__(self) -> str:
        return self.email

    # --- Django admin / 权限接口所需 ---
    @property
    def is_staff(self) -> bool:
        # 平台运营走 /ops/health，不该进 Django admin：admin 是给「开发者自查」的，
        # 一旦开放就等于绕过全部业务视图与审计。
        return False

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.status == "active"

    @property
    def is_platform_ops(self) -> bool:
        return self.role == self.ROLE_PLATFORM_OPS

    @property
    def can_write(self) -> bool:
        return self.role in self.WRITE_ROLES

    @property
    def tenant_id_value(self) -> str | None:
        return str(self.tenant_id) if self.tenant_id else None


class SelfProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    sku = models.TextField()
    title = models.TextField(null=True, blank=True)
    price_display = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    margin_floor = models.DecimalField(max_digits=4, decimal_places=3, default=0.22)
    category_id = models.TextField(null=True, blank=True)
    attrs = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "self_product")
        constraints = [models.UniqueConstraint(fields=["tenant", "sku"], name="uq_sp")]

    def __str__(self) -> str:
        return f"{self.sku} {self.title or ''}"


class TenantTarget(models.Model):
    """对标关系：一个自有 SKU 可绑 1~10 个竞品，故为多对多。"""

    PRIORITY_CORE = "core"
    PRIORITY_NORMAL = "normal"
    PRIORITY_LONG_TAIL = "long_tail"
    PRIORITY_CHOICES = (
        (PRIORITY_CORE, "核心"),
        (PRIORITY_NORMAL, "一般"),
        (PRIORITY_LONG_TAIL, "长尾"),
    )
    PRIORITY_BY_INTERVAL = {PRIORITY_CORE: 120, PRIORITY_NORMAL: 720, PRIORITY_LONG_TAIL: 1440}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    target_id = models.BigIntegerField()
    self_product = models.ForeignKey(
        SelfProduct, null=True, blank=True, on_delete=models.CASCADE, db_column="self_product_id"
    )
    priority = models.TextField(default=PRIORITY_NORMAL)
    enabled = models.BooleanField(default=True)
    created_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "tenant_target")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "target_id", "self_product"], name="uq_tt", nulls_distinct=False
            )
        ]
        indexes = [models.Index(fields=["target_id"], name="ix_tt_target")]

    @property
    def interval_min(self) -> int:
        return self.PRIORITY_BY_INTERVAL.get(self.priority, 720)


class EventPriority(models.Model):
    """优先级是租户投影，不是共享层事实（补充文档 C-1）。"""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    event_id = models.BigIntegerField()
    priority = models.TextField()
    self_product = models.ForeignKey(
        SelfProduct, null=True, blank=True, on_delete=models.CASCADE, db_column="self_product_id"
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "event_priority")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "event_id", "self_product"], name="uq_ep", nulls_distinct=False
            )
        ]


class Analysis(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    event_id = models.BigIntegerField()
    self_product = models.ForeignKey(
        SelfProduct, null=True, blank=True, on_delete=models.CASCADE, db_column="self_product_id"
    )
    attribution = models.TextField(null=True, blank=True)
    threat_level = models.TextField(null=True, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    evidence = models.JSONField(default=list)
    model_meta = models.JSONField(default=dict)
    degraded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "analysis")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "event_id", "self_product"],
                name="uq_analysis_event",
                nulls_distinct=False,
            )
        ]


class Suggestion(models.Model):
    KIND_A_INFO = "A_info"
    KIND_B_PROMO = "B_promo"
    KIND_C_CREATIVE = "C_creative"
    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_IGNORED = "ignored"
    STATUS_FALSE_POSITIVE = "false_positive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    analysis = models.ForeignKey(Analysis, null=True, blank=True, on_delete=models.CASCADE, db_column="analysis_id")
    event_id = models.BigIntegerField(null=True, blank=True)
    target_id = models.BigIntegerField(null=True, blank=True)
    kind = models.TextField()  # A_info|B_promo|C_creative
    priority = models.TextField(default="P2")
    card = models.JSONField(default=dict)  # 8 字段，见 domain/suggest/card.py
    status = models.TextField(default=STATUS_PENDING)
    quality_gate = models.TextField(default="pass")  # pass|manual_required
    degraded = models.BooleanField(default=False)
    push_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "suggestion")
        constraints = [models.UniqueConstraint(fields=["analysis", "kind"], name="uq_sug_analysis_kind")]
        indexes = [models.Index(fields=["priority", "status", "-created_at"], name="ix_sug_feed")]


class Task(models.Model):
    STATUS_TODO = "todo"
    STATUS_DOING = "doing"
    STATUS_DONE = "done"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    suggestion = models.ForeignKey(
        Suggestion, null=True, blank=True, on_delete=models.SET_NULL, db_column="suggestion_id"
    )
    title = models.TextField()
    detail = models.TextField(blank=True, default="")
    owner = models.ForeignKey(AppUser, null=True, blank=True, on_delete=models.SET_NULL, db_column="owner_id")
    status = models.TextField(default=STATUS_TODO)
    due_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "task")
        indexes = [models.Index(fields=["status", "due_at"], name="ix_task_status_due")]


class Feedback(models.Model):
    VERDICT_ACCEPTED = "accepted"
    VERDICT_IGNORED = "ignored"
    VERDICT_FALSE_POSITIVE = "false_positive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    suggestion = models.ForeignKey(Suggestion, on_delete=models.CASCADE, db_column="suggestion_id")
    verdict = models.TextField()
    reason_tag = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    user = models.ForeignKey(AppUser, null=True, blank=True, on_delete=models.SET_NULL, db_column="user_id")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "feedback")


class Notification(models.Model):
    """推送记录。幂等键 (suggestion, channel, bucket) —— FR-52「1 小时不重复」。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    suggestion = models.ForeignKey(Suggestion, null=True, blank=True, on_delete=models.CASCADE, db_column="suggestion_id")
    channel = models.TextField()  # wecom|email|site
    bucket = models.TextField()
    status = models.TextField(default="sent")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "notification")
        constraints = [
            models.UniqueConstraint(
                fields=["suggestion", "channel", "bucket"], name="uq_notify_once", nulls_distinct=False
            )
        ]


class AuditLog(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.UUIDField(null=True, blank=True, db_column="tenant_id")
    actor = models.TextField(blank=True, default="")
    action = models.TextField()
    target_type = models.TextField(blank=True, default="")
    target_id = models.TextField(blank=True, default="")
    detail = models.JSONField(default=dict)
    ip = models.TextField(blank=True, default="")
    request_id = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "audit_log")
        indexes = [models.Index(fields=["action", "-created_at"], name="ix_audit_action")]


class MetricDaily(models.Model):
    """单人版可观测：worker 每小时把关键计数写入，/ops/health 直接读表画图。"""

    id = models.BigAutoField(primary_key=True)
    day = models.DateField()
    hour = models.SmallIntegerField(default=0)
    metric = models.TextField()
    value = models.FloatField(default=0)
    dim = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "metric_daily")
        constraints = [models.UniqueConstraint(fields=["day", "hour", "metric", "dim"], name="uq_metric")]


class LlmUsage(models.Model):
    """按租户/天/节点的模型用量。

    为什么单独一张表而不是塞进 ``metric_daily``：``metric_daily`` 是全局运维指标（无租户列、
    不开 RLS），而账单必须能按租户切分，且必须受 RLS 约束 —— 商家之间不能互相看到用量。
    配额口径也随之从「全局」改为「每租户/日」。
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    day = models.DateField()
    node = models.TextField(default="all")  # n1|n2|all
    calls = models.IntegerField(default=0)
    tokens_in = models.BigIntegerField(default=0)
    tokens_out = models.BigIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "llm_usage")
        constraints = [models.UniqueConstraint(fields=["tenant", "day", "node"], name="uq_llm_usage")]

    @property
    def tokens(self) -> int:
        return (self.tokens_in or 0) + (self.tokens_out or 0)


class Outcome(models.Model):
    """建议采纳后的效果回看（T+7）。

    为什么要有这张表：没有它，「采纳」只是一个点击，产品无法回答
    「这条建议到底有没有用」。回看数据同时是评测集的天然来源（人工确认过的真实结果）。

    判定口径刻意保守：
    * 竞品价格**回涨** ≥1% → ``price_reverted``（原降价是短期活动，我方可回调）
    * 变动在 ±1% 内 → ``price_kept``（价格维持，建议仍然有效）
    * 继续下探 ≥1% → ``price_further_down``（我方需要再评估）
    * 取不到价 → ``unavailable``（不猜、不当成 0 变化）
    """

    PENDING = "pending"
    PRICE_REVERTED = "price_reverted"
    PRICE_KEPT = "price_kept"
    PRICE_FURTHER_DOWN = "price_further_down"
    UNAVAILABLE = "unavailable"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    suggestion = models.OneToOneField(
        Suggestion, on_delete=models.CASCADE, db_column="suggestion_id", related_name="outcome"
    )
    target_id = models.BigIntegerField(null=True, blank=True)
    accepted_at = models.DateTimeField()
    check_due_at = models.DateTimeField()
    measured_at = models.DateTimeField(null=True, blank=True)
    price_at_accept = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_after = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    change_pct = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    outcome = models.TextField(default=PENDING)
    note = models.TextField(blank=True, default="")

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "outcome")
        indexes = [models.Index(fields=["outcome", "-accepted_at"], name="ix_outcome_state")]


class LoginThrottle(models.Model):
    """登录失败计数（落库，多进程有效；不用进程内缓存）。"""

    email = models.TextField(primary_key=True)
    fail_count = models.SmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "login_throttle")


class AnalyticsEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.UUIDField(null=True, blank=True, db_column="tenant_id")
    name = models.TextField()
    props = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()

    # 有 tenant_id 就必须走作用域管理器，否则 SQLite 开发库下 `AnalyticsEvent.objects.all()`
    # 会跨租户（PG 有 RLS 兜底，SQLite 没有）
    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        db_table = quote_table("app", "analytics_event")
