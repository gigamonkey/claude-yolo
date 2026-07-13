---
name: verify
description: Drive yolo's CLI verbs and the wip dashboard end-to-end without docker, against throwaway repos under an isolated HOME.
---

# Verifying yolo changes at the real surface

The host-side verbs (`config`, `list`, `dir`, `rebase`, `merge`, `diff`, `wip`)
never need docker — only `start`/`resume`/`shell`/`stop`/`finish` touch it. So
most changes can be driven for real against throwaway git repos.

## Isolated environment

Everything yolo persists lives under `$HOME`, so an isolated `HOME` is full
isolation:

```bash
export HOME=$SCRATCH/verify/home YOLO_CREDENTIAL_STORE=file
mkdir -p $HOME
```

Run yolo from the repo's venv from anywhere:

```bash
uv run --project <repo-root> yolo <verb> ...
```

## Worktrees without `start`

`start` launches docker, but the topic verbs only need the worktree layout it
creates. Fabricate it (slug via `_repo_root_of`, loaded through importlib):

```python
spec = importlib.util.spec_from_file_location("cy", "<repo-root>/yolo.py")
cy = importlib.util.module_from_spec(spec); spec.loader.exec_module(cy)
slug = cy._repo_root_of(repo_root)[2]
wt = pathlib.Path.home() / ".claude-yolo" / "worktrees" / slug / topic
# then: git -C <repo> worktree add -b <topic> <wt> main
```

Multi-repo: register the project first (`yolo config --add-repo ../lib` from
the primary), then fabricate a same-topic worktree per repo.

## Driving the `wip` dashboard

Needs tmux (`sudo apt-get install -y tmux`) and a `docker` on PATH — a stub is
fine, `wip` only uses it to list sessions:

```bash
printf '#!/bin/sh\nexit 0\n' > $SCRATCH/verify/bin/docker && chmod +x $SCRATCH/verify/bin/docker
```

**Gotcha: `yolo wip` respawns itself as `yolo wip --_dashboard` in a new tmux
window, which inherits the tmux *server's* environment — not the launching
pane's.** Exporting HOME only around the `yolo wip` command silently gives you
an empty dashboard drawn under the real HOME. Export HOME/PATH **before
starting the tmux server** so the server (and every window it spawns) inherits
them:

```bash
export HOME=$SCRATCH/verify/home YOLO_CREDENTIAL_STORE=file PATH=$SCRATCH/verify/bin:$PATH
tmux -L verify new-session -d -x 180 -y 40 "uv run --project <repo-root> yolo wip; sleep 120"
sleep 6   # uv resolve + dashboard respawn take a few seconds
tmux -L verify capture-pane -t yolo-wip -p          # the dashboard window is named yolo-wip
tmux -L verify send-keys -t yolo-wip j              # navigate; m/r/d/f act; y confirms
```

Windows the dashboard spawns are named (`diff-<topic>`, `<project>-<topic>`) —
capture them with `-t <name>`. Kill the server when done:
`tmux -L verify kill-server`.
