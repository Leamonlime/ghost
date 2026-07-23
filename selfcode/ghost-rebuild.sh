#!/bin/bash
# Ghost rebuild wrapper — the ONLY privileged action the self-code system can take
# for bot/scheduler. Install root-owned at /usr/local/sbin/ghost-rebuild and grant
# NOPASSWD sudo on exactly this script (see selfcode/README-sudo.md).
#
# Takes ONE argument: bot | scheduler. Anything else is refused. It runs the fixed
# documented rebuild for that one service from its fixed directory — no argument is
# interpolated into a shell, no other command is reachable. Because the script is
# root-owned it cannot be altered by the (unprivileged) user the runner runs as, so
# what "apply" is allowed to do stays fixed in advance.
set -euo pipefail

SERVICE="${1:-}"
case "$SERVICE" in
  bot)       DIR=/home/admin_hivequeen/ghost/bot;       IMAGE=ghost-bot ;;
  scheduler) DIR=/home/admin_hivequeen/ghost/scheduler; IMAGE=ghost-scheduler ;;
  *) echo "ghost-rebuild: refusing '$SERVICE' — only bot or scheduler" >&2; exit 2 ;;
esac

cd "$DIR"
docker build -t "${IMAGE}:latest" .
docker save "${IMAGE}:latest" | k3s ctr images import -
# rollout restart does not need root; run it as the invoking user's kubectl if present,
# else fall back to the k3s admin kubeconfig.
if [ -n "${SUDO_USER:-}" ] && sudo -u "$SUDO_USER" kubectl rollout restart "deployment/${IMAGE}" -n ghost; then
  :
else
  KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout restart "deployment/${IMAGE}" -n ghost
fi
echo "ghost-rebuild: ${SERVICE} rebuilt and rolled out"
