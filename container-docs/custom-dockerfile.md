# Creating a `Dockerfile.yolo` (custom container image)

You're running inside a yolo container. If the user asks you to customize the
container image — add a system package, a language toolchain, a CLI tool that
should be present in every session — the mechanism is a **`Dockerfile.yolo`** in
the project that layers on top of yolo's built-in image. This is the file to
create when there isn't one already; don't hand-write an image from scratch or go
hunting for how yolo builds things — everything you need is here.

## Understand this first

**yolo runs on the host, not in this container, and it is not installed here.** So:

- You *can* write and edit `Dockerfile.yolo` from here — the working directory is
  bind-mounted, so the file persists to the host.
- You *cannot* build or apply it from inside the container (there's no `yolo`
  command here). Building happens on the host, on the next launch.
- The current session keeps the image it started with. Your change takes effect
  only when the user exits and starts a new session.

So the flow is: **you write the file, then tell the user the one host command to
wire it up and that they'll need to restart the session** for it to take effect.

## The template

Write this to `Dockerfile.yolo` in the project root and edit the marked block.
This is exactly what `yolo dockerfile --custom` prints on the host:

```dockerfile
# Custom yolo Dockerfile — layers your own build steps on top of yolo's built-in
# image. Save it as `Dockerfile.yolo` in your project, then point yolo at it:
#
#   yolo config --dockerfile ./Dockerfile.yolo   # persist it for this project
#   yolo --dockerfile ./Dockerfile.yolo          # ...or just for one run
#
# yolo builds the image on the HOST (not inside the container), so an edit here
# takes effect on the NEXT `yolo` launch — leave the current session and start a
# new one. `yolo dockerfile --custom` reprints this template; `yolo dockerfile`
# prints the full default image if you want to see exactly what you inherit.
#
# --- how it works -----------------------------------------------------------
# The two lines just below are load-bearing: referencing YOLO_BASE is what makes
# yolo build its default image first and pass that tag in as the YOLO_BASE build
# arg. That's how you inherit everything the default provides — the `claude` user
# (with passwordless sudo), the native Claude install, PATH, and the ENTRYPOINT —
# without repeating any of it. Keep both lines.

ARG YOLO_BASE
FROM ${YOLO_BASE}

# --- add your customizations below ------------------------------------------
# You start as the `claude` user, which has passwordless sudo, so install system
# packages with `RUN sudo apt-get ...`. Bake in cross-cutting tools you want in
# every session; leave heavy or project-specific ones to install on demand inside
# the container. The build context is EMPTY by design (a security property: it
# stops a Dockerfile from copying host files into the image), so you can't `COPY`
# files out of your project — fetch what you need during the build (curl/git) or
# install it at runtime instead. For example:
#
#   RUN sudo apt-get update && sudo apt-get install -y postgresql-client
#   RUN curl -fsSL https://example.com/tool -o /tmp/tool \
#       && sudo install /tmp/tool /usr/local/bin/tool



# --- keep this last ---------------------------------------------------------
# yolo passes no `-u` to `docker run`, so the image's final USER is the container's
# runtime user, and yolo refuses to launch an image whose user isn't `claude` (a
# root image would write your bind-mounted files as root). If you `USER root` to do
# work above, switch back here.
USER claude
```

## Wiring it up — host commands to give the user

- Persist it for the project: `yolo config --dockerfile ./Dockerfile.yolo`
- Or use it for a single run: `yolo --dockerfile ./Dockerfile.yolo`

Then the user exits this session and starts a new one; the new image builds on
that launch. If they save the file but run neither command, yolo ignores it and
warns on the next launch that a `Dockerfile.yolo` is present but not configured.

## Rules that must hold

- **Keep the `ARG YOLO_BASE` / `FROM ${YOLO_BASE}` lines.** That's what layers on
  yolo's default and inherits the `claude` user, sudo, Claude, PATH, and the
  ENTRYPOINT. A Dockerfile that omits YOLO_BASE is treated as a full replacement
  and built as-is — rarely what you want.
- **End with `USER claude`.** yolo refuses to launch an image that runs as any
  other user (it would write bind-mounted files as root).
- **Install with `RUN sudo apt-get update && sudo apt-get install -y ...`** — the
  `claude` user has passwordless sudo.
- **You can't `COPY` project files in** — the build context is empty by design.
  Fetch during the build (curl/git) or install at runtime instead.
- **Bake in cross-cutting tools only.** Heavy or one-off project-specific things
  are better installed on demand inside the container.
