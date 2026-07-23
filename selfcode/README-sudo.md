# Self-code rebuild grant (bot / scheduler)

The dashboard **Apply** button lets the operator apply an already-approved bot/scheduler
self-code proposal without SSHing in. The dashboard pod itself stays powerless: it
only flips a Postgres status (`approved` -> `apply_queued`). The privileged work —
`docker build` + `k3s ctr images import` + `kubectl rollout restart` — runs on the
host in the existing `ghost-selfcode-runner` user service, which needs root for the
build/import steps.

## The grant (run once, by the operator, with sudo)

Everything the grant can do is fixed in a root-owned wrapper that only rebuilds bot
or scheduler from their fixed directories. Nothing else is reachable through it.

```bash
# 1. install the wrapper root-owned (not user-writable) so its behaviour is fixed
sudo install -o root -g root -m 755 ~/ghost/selfcode/ghost-rebuild.sh /usr/local/sbin/ghost-rebuild

# 2. grant NOPASSWD on ONLY that wrapper, only for the two service args
sudo tee /etc/sudoers.d/ghost-selfcode >/dev/null <<'EOF'
admin_hivequeen ALL=(root) NOPASSWD: /usr/local/sbin/ghost-rebuild bot, /usr/local/sbin/ghost-rebuild scheduler
EOF
sudo chmod 440 /etc/sudoers.d/ghost-selfcode
sudo visudo -c   # validate syntax
```

## Why this shape

- **Tighter than granting `docker`/`sudo` broadly.** The runner can run exactly
  `sudo /usr/local/sbin/ghost-rebuild bot` (or `scheduler`) and nothing else. It
  cannot run arbitrary docker/k3s commands, and cannot pass any other argument (the
  wrapper refuses anything but `bot`/`scheduler`).
- **Wrapper is root-owned**, so the unprivileged runner user can't rewrite what the
  privileged action does.
- **Build input is already gated.** Self-code only ever changes an approved `.py`
  file; Dockerfiles are not self-codable, so what runs as root at build time stays
  the fixed `pip install` + `COPY` steps.

## Honest residual risk

`docker build` executes the Dockerfile's build steps as root. That is inherent to
building images and is why this is a real privilege grant, not a reused pattern.
It is bounded here to rebuilding two specific services from fixed Dockerfiles, with
the source change gated by the Telegram approval + dashboard button (two explicit
human steps). If you'd rather not grant it, leave it uninstalled: the button will
write the file + backup and report `applied_no_rebuild`, and you finish with the
manual `python3 ~/ghost/selfcode/selfcode.py apply <id>` under your own sudo, exactly
as before. `undo last change <service>` works either way.
