#!/usr/bin/env bash
# 把 PACE 推到 GitHub（lluxingzhe7/pace）
# 用法： bash deploy.sh "提交说明" [tag]
# 需要以「完全访问」运行（沙箱默认禁外网）。
set -euo pipefail
cd "$(dirname "$0")"
PY=$(cygpath -u "$USERPROFILE")/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe
export PYTHONUTF8=1

# 1. 先跑全量测试，红了就不发
if [ "${SKIP_TEST:-0}" != "1" ]; then
  mkdir -p .selftest-tmp
  bash tools/run_all_tests.sh http://127.0.0.1:8765 >.selftest-tmp/deploy-test.log 2>&1 || {
    tail -20 .selftest-tmp/deploy-test.log; echo "测试没过，已中止发布"; exit 1; }
  tail -2 .selftest-tmp/deploy-test.log
fi

# 2. 提交
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "${1:-常规更新}"
fi

# 3. 可选打 tag
if [ -n "${2:-}" ]; then
  git tag -a "$2" -m "$2" 2>/dev/null || true
fi

# 4. 推送
git push -u origin main
[ -n "${2:-}" ] && git push origin "$2" || true
git log --oneline -3
