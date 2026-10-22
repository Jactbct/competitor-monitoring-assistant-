#!/usr/bin/env bash
# 备份：数据库 + 原始页 + 校验和 + 清单（含行数），并按份数保留。
#
# 两种取库方式，按环境自动选：
#   PG_CONTAINER=pg-competitor  → 走 docker exec（本机/私有化最常用）
#   DATABASE_URL=postgresql://… → 走本机 pg_dump（CI 或有客户端的环境）
#
# 为什么备份里要写行数清单：恢复后「有没有真的恢复成功」不能靠肉眼看日志，
# 要么对比行数，要么对比校验和。清单就是这个对照物。
set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-./.backups}
KEEP=${KEEP:-7}
PG_DB=${PG_DB:-competitor}
PG_SUPERUSER=${PG_SUPERUSER:-postgres}
DOCKER_BIN=${DOCKER_BIN:-docker}
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/db-$STAMP.sql.gz"
MANIFEST="$BACKUP_DIR/db-$STAMP.manifest"

mkdir -p "$BACKUP_DIR"

COUNT_SQL="SELECT 'snapshot=' || count(*) FROM shared.snapshot
           UNION ALL SELECT 'change_event=' || count(*) FROM shared.change_event
           UNION ALL SELECT 'suggestion=' || count(*) FROM app.suggestion
           UNION ALL SELECT 'target=' || count(*) FROM shared.target
           UNION ALL SELECT 'app_user=' || count(*) FROM app.app_user;"

echo "==> 备份数据库到 $OUT"
if [ -n "${PG_CONTAINER:-}" ]; then
  "$DOCKER_BIN" exec "$PG_CONTAINER" pg_dump -U "$PG_SUPERUSER" -d "$PG_DB" \
    --clean --if-exists --no-owner --no-privileges | gzip -9 > "$OUT"
  "$DOCKER_BIN" exec "$PG_CONTAINER" psql -U "$PG_SUPERUSER" -d "$PG_DB" -tAc "$COUNT_SQL" > "$MANIFEST"
else
  : "${DATABASE_URL:?需要 PG_CONTAINER 或 DATABASE_URL}"
  pg_dump "$DATABASE_URL" --clean --if-exists --no-owner --no-privileges | gzip -9 > "$OUT"
  psql "$DATABASE_URL" -tAc "$COUNT_SQL" > "$MANIFEST"
fi

# 原始页（仅排障用，可丢；单独打包，不进数据库备份）
if [ -d ./.data/raw ]; then
  echo "==> 打包原始页（7 天保留期内的快照页）"
  tar -czf "$BACKUP_DIR/raw-$STAMP.tar.gz" -C .data raw
fi

echo "==> 写入校验和"
( cd "$BACKUP_DIR" && sha256sum "$(basename "$OUT")" >> "db-$STAMP.sha256" )

echo "==> 备份清单"
cat "$MANIFEST"

echo "==> 清理超过 $KEEP 份的旧备份"
ls -1t "$BACKUP_DIR"/db-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  base=$(basename "$old" .sql.gz)
  rm -f "$old" "$BACKUP_DIR/$base.manifest" "$BACKUP_DIR/$base.sha256"
  echo "    已删除 $old"
done

echo "==> 完成：$OUT"
