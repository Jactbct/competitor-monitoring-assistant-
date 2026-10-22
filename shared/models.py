"""共享层（L1）：URL 级公开快照，跨租户复用。

**硬规则（CI 检查）**：本 app 任何表的列名都不得出现
``tenant_id / user_id / owner_id / creator_id``（架构文档 §5.4 规则 1）。
"""

from __future__ import annotations

from django.db import models

from core.db import quote_table


class Target(models.Model):
    """监控目标：URL 级、全局唯一、不含任何租户信息。"""

    STATUS_ACTIVE = "active"
    STATUS_STALE = "stale"
    STATUS_INVALID = "invalid"
    CIRCUIT_CLOSED = "closed"
    CIRCUIT_OPEN = "open"
    CIRCUIT_HALF_OPEN = "half_open"

    id = models.BigAutoField(primary_key=True)
    platform = models.TextField(default="taobao")
    url = models.TextField()
    url_hash = models.CharField(max_length=64)
    item_id = models.TextField(null=True, blank=True)
    shop_id = models.TextField(null=True, blank=True)
    category_id = models.TextField(null=True, blank=True)
    source = models.TextField(default="public_sample")  # official_api|data_vendor|public_sample
    status = models.TextField(default=STATUS_ACTIVE)
    circuit_state = models.TextField(default=CIRCUIT_CLOSED)
    circuit_until = models.DateTimeField(null=True, blank=True)
    fail_streak = models.SmallIntegerField(default=0)
    interval_min = models.IntegerField(default=120)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    next_due_at = models.DateTimeField()
    # 下列字段是从最近一次快照投影出来的，仅用于列表展示，不参与判定
    last_captured_at = models.DateTimeField(null=True, blank=True)
    last_price_display = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_price_final = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_price_confidence = models.TextField(null=True, blank=True)
    last_title = models.TextField(null=True, blank=True)
    last_status = models.TextField(null=True, blank=True)
    # 条件请求：带上 ETag / Last-Modified，未改动时服务端回 304，几乎零成本
    # （同类开源项目的一致结论：这是「最省预算的礼貌」，也直接降低被风控的概率）
    http_etag = models.TextField(null=True, blank=True)
    http_last_modified = models.TextField(null=True, blank=True)
    last_304_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = quote_table("shared", "target")
        constraints = [models.UniqueConstraint(fields=["platform", "url_hash"], name="uq_target_url")]
        indexes = [models.Index(fields=["next_due_at"], name="ix_target_due")]

    def __str__(self) -> str:
        return f"{self.platform}:{self.url_hash[:8]}"


class TargetDemand(models.Model):
    """需求投影，由 L2 派生。**故意不含租户标识与计数**（否则可被反推成监控热度榜）。"""

    target = models.OneToOneField(Target, on_delete=models.CASCADE, primary_key=True, db_column="target_id")
    has_monitor = models.BooleanField(default=False)
    min_interval_min = models.IntegerField(default=1440)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = quote_table("shared", "target_demand")


class Snapshot(models.Model):
    """不可变快照。生产环境中 ``app_rw`` 对该表只有 SELECT/INSERT（无 UPDATE/DELETE）。"""

    STATUS_VALID = "valid"
    STATUS_ABNORMAL = "abnormal"

    id = models.BigAutoField(primary_key=True)
    target = models.ForeignKey(Target, on_delete=models.DO_NOTHING, db_column="target_id")
    captured_at = models.DateTimeField()
    http_status = models.SmallIntegerField(default=200)
    content_sha256 = models.CharField(max_length=64)
    raw_object_key = models.TextField(null=True, blank=True)
    raw_bytes = models.IntegerField(null=True, blank=True)
    extract_version = models.TextField(default="rules-1")
    fields = models.JSONField(default=dict)
    field_completeness = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    price_display = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_final = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_confidence = models.TextField(default="certain")  # certain|uncertain
    promo_detail = models.JSONField(default=list)
    evidence_id = models.CharField(max_length=32)
    status = models.TextField(default=STATUS_VALID)
    abnormal_type = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = quote_table("shared", "snapshot")
        indexes = [
            models.Index(fields=["target", "-captured_at"], name="ix_snap_target_time"),
            models.Index(fields=["evidence_id"], name="ix_snap_evidence"),
        ]

    def __str__(self) -> str:
        return self.evidence_id


class ChangeEvent(models.Model):
    """变化事件：只承载客观事实（变了什么、变了多少、什么时候），不含优先级。

    优先级是租户投影，放 ``app.event_priority``（补充文档 C-1）。
    """

    id = models.BigAutoField(primary_key=True)
    target = models.ForeignKey(Target, on_delete=models.DO_NOTHING, db_column="target_id")
    prev_snapshot_id = models.BigIntegerField(null=True, blank=True)
    snapshot = models.ForeignKey(
        Snapshot, on_delete=models.DO_NOTHING, db_column="snapshot_id", related_name="events"
    )
    changed_fields = models.JSONField(default=list)
    event_types = models.JSONField(default=list)
    deltas = models.JSONField(default=dict)
    magnitude = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    merge_key = models.TextField(null=True, blank=True)
    merged_count = models.SmallIntegerField(default=1)
    detected_at = models.DateTimeField()

    class Meta:
        db_table = quote_table("shared", "change_event")
        constraints = [models.UniqueConstraint(fields=["target", "snapshot"], name="uq_event_snapshot")]
        indexes = [models.Index(fields=["target", "-detected_at"], name="ix_event_target_time")]


class RateBucket(models.Model):
    """采集出口令牌桶（全局单行）。存库而不是存内存：多进程共享 + 重启不丢预算。"""

    id = models.SmallIntegerField(primary_key=True, default=1)
    tokens = models.FloatField(default=0.0)
    updated_at = models.DateTimeField()

    class Meta:
        db_table = quote_table("shared", "rate_bucket")


class LlmCache(models.Model):
    """LLM 结果缓存：key = sha256(content_sha256 + prompt_version + model_id)。"""

    cache_key = models.CharField(max_length=64, primary_key=True)
    node = models.TextField()
    payload = models.JSONField()
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = quote_table("shared", "llm_cache")


class Job(models.Model):
    """任务表 = 队列（架构文档 §6.4：PG + SKIP LOCKED，不引入 Redis/Celery）。

    刻意放在 shared 层：worker 必须能跨租户取任务，而 payload 只放 ID 引用，
    不含租户业务数据；若放进 app 层并开 RLS，worker 在无租户上下文时将一行也取不到。
    """

    STATE_PENDING = "pending"
    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"
    STATE_DEAD = "dead"

    id = models.BigAutoField(primary_key=True)
    kind = models.TextField()
    payload = models.JSONField(default=dict)
    state = models.TextField(default=STATE_PENDING)
    priority = models.SmallIntegerField(default=100)  # 数字越小越优先
    run_after = models.DateTimeField()
    attempts = models.SmallIntegerField(default=0)
    max_attempts = models.SmallIntegerField(default=3)
    locked_by = models.TextField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    # 租约到期时间。只靠 locked_at + 固定阈值，长任务（日报清理百万行）会被误判死亡 → 重复执行
    lease_until = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)
    last_error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = quote_table("shared", "job")
        indexes = [
            models.Index(fields=["state", "run_after", "priority"], name="ix_job_claim"),
            models.Index(fields=["kind", "state"], name="ix_job_kind_state"),
        ]

    def __str__(self) -> str:
        return f"job#{self.id} {self.kind} {self.state}"
