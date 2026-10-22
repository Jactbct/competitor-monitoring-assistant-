#!/usr/bin/env bash
# 恢复：从一个备份文件恢复数据库，并核对行数清单。
#
# 安全约定（都是踩过坑之后加的）：
#   1. 默认**不**恢复到生产库，要求显式给 DATABASE_URL 或 RESTORE_DB；
#   2. 需要输入 yes 二次确认；CI 里用 FORCE=1 跳过；
#   3. 恢复后自动对比 manifest 里的行数，不一致就以非 0 退出 —— 让脚本失败，而不是让人去猜。
set -euo pipefail

FILE=${1:-}
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "用法：scripts/restore.sh <备份文件.sql.gz>" >&2
  exit 2
fi

MANIFEST="${FILE%.sql.gz}.manifest"

if [ -z "${FORCE:-}" ]; then
  echo "即将把 $FILE 恢复到：${DATABASE_URL:-${PG_CONTAINER:-未指定}}"
  echo "这会覆盖同名 schema 下的对象。输入 yes 继续："
  read -r answer
  [ "$answer" = "yes" ] || { echo "已取消"; exit 1; }
fi

echo "==> 恢复"
if [ -n "${PG_CONTAINER:-}" ]; then
  ${DOCKER_BIN:-docker} exec -i "$PG_CONTAINER" \
    psql -U "${PG_SUPERUSER:-postgres}" -d "${PG_DB:-competitor}" -v ON_ERROR_STOP=1 \
    < <(gunzip -c "$FILE")
else
  : "${DATABASE_URL:?需要 PG_CONTAINER 或 DATABASE_URL}"
  gunzip -c "$FILE" | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
fi

if [ ! -f "$MANIFEST" ]; then
  echo "==> 没有找到 $MANIFEST，跳过行数核对"
  exit 0
fi

COUNT_SQL="SELECT 'snapshot=' || count(*) FROM shared.snapshot
           UNION ALL SELECT 'change_event=' || count(*) FROM shared.change_event
           UNION ALL SELECT 'suggestion=' || count(*) FROM app.suggestion
           UNION ALL SELECT 'target=' || count(*) FROM shared.target
           UNION ALL SELECT 'app_user=' || count(*) FROM app.app_user;"

if [ -n "${PG_CONTAINER:-}" ]; then
  ACTUAL=$(${DOCKER_BIN:-docker} exec "$PG_CONTAINER" psql -U "${PG_SUPERUSER:-postgres}" -d "${PG_DB:-competitor}" -tAc "$COUNT_SQL")
else
  ACTUAL=$(psql "$DATABASE_URL" -tAc "$COUNT_SQL")
fi

echo "==> 行数核对（期望 ← 备份清单 / 实际 ← 恢复后）"
fail=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  key=${line%%=*}
  want=${line#*=}
  got=$(echo "$ACTUAL" | grep "^$key=" | head -1 | cut -d= -f2)
  if [ "$want" = "$got" ]; then
    echo "    OK   $key  $want"
  else
    echo "    FAIL $key  期望 $want / 实际 ${got:-0}"
    fail=1
  fi
done < "$MANIFEST"

if [ "$fail" -ne 0 ]; then
  echo "==> 恢复后数据不一致，请检查（迁移版本不同、schema 未建、或备份本身不完整）" >&2
  exit 1
fi

echo "==> 恢复完成，行数一致"
