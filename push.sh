#!/usr/bin/env bash
# 推送当前分支到 GitHub。本机到 github.com:443 间歇性超时，失败自动重试。
#
# 用法：
#   ./push.sh              # 推当前分支，默认重试 5 次
#   ./push.sh main         # 推指定分支
#   PUSH_RETRIES=10 ./push.sh
#
# 环境变量：
#   PUSH_RETRIES  重试次数，默认 5
#   PUSH_DELAY    每次重试间隔秒数，默认 5
#   PUSH_TIMEOUT  单次推送超时秒数，默认 120
#   REMOTE        远程名，默认 origin

set -uo pipefail

REMOTE="${REMOTE:-origin}"
BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null)}"
MAX="${PUSH_RETRIES:-5}"
DELAY="${PUSH_DELAY:-5}"
LIMIT="${PUSH_TIMEOUT:-120}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "✗ 当前目录不是 git 仓库" >&2
  exit 2
fi

if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "✗ 当前处于游离 HEAD 状态，请先切到具体分支" >&2
  exit 2
fi

if ! git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null; then
  echo "✗ 本地没有分支 $BRANCH" >&2
  exit 2
fi

# 工作区不干净时提醒，但照常推送（推送的是已提交内容）
if [ -n "$(git status --porcelain)" ]; then
  echo "! 工作区有未提交的改动，本次只推送已有提交" >&2
fi

# Windows 的 timeout.exe 参数完全不同，只有 coreutils 版本才用
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1 && timeout --version >/dev/null 2>&1; then
  TIMEOUT_BIN="timeout"
fi

push_once() {
  if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "$LIMIT" git push "$REMOTE" "$BRANCH" 2>&1
  else
    git push "$REMOTE" "$BRANCH" 2>&1
  fi
}

for i in $(seq 1 "$MAX"); do
  echo "[$i/$MAX] git push $REMOTE $BRANCH"
  out="$(push_once)"
  code=$?

  if [ $code -eq 0 ]; then
    printf '%s\n' "$out" | sed 's/^/    /'
    echo "✓ 推送成功"
    exit 0
  fi

  printf '%s\n' "$out" | sed 's/^/    /'

  # 网络问题重试没意义的情况：被拒绝、鉴权失败
  if printf '%s' "$out" | grep -qE "non-fast-forward|\[rejected\]|Authentication failed|could not read Username|Repository not found|src refspec|does not appear to be a git repository|No such remote"; then
    echo "✗ 不是网络抖动，重试没用：远程名/分支不对、远程有新提交，或凭据失效" >&2
    echo "  检查：git remote -v；git fetch $REMOTE && git log --oneline HEAD..$REMOTE/$BRANCH" >&2
    exit 3
  fi

  if [ "$i" -lt "$MAX" ]; then
    echo "    $DELAY 秒后重试…"
    sleep "$DELAY"
  fi
done

echo "✗ 连续 $MAX 次失败：github.com:443 对本机持续不通" >&2
echo "  排查：curl -s -o /dev/null -w '%{http_code}' https://api.github.com" >&2
echo "  API 通但 git 不通时，可改用 SSH（github.com:22 实测可达）" >&2
exit 1
