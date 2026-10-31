---
name: docker-management
description: Inspect and manage Docker containers, images, networks and volumes on the connected Docker host using the "Docker (MCP over HTTP)" tools. Use when the user asks to run, deploy, start, stop, restart, update, remove, list, debug or check the logs of containers, or to pull, build, push or clean up images, networks or volumes.
---

# Docker management

You have tools that act on a real Docker host. Every call changes or reads live
state, so look before you act, and confirm before you destroy anything.

## When to use these tools

Use them when the user wants something done to, or found out about, the Docker
host: "start an nginx container", "why is my app container down?", "what's
taking up space?", "update postgres to version 16".

Do not use them when the user only wants Docker knowledge or files, such as
explaining a command, or writing a Dockerfile or compose file they will run
themselves. If you're unsure whether they want you to act, ask.

## What you can see and change

The server only manages containers and volumes that were created through these
tools. It labels them `mcp-server-docker.managed=true` automatically, so you
never need to add that label yourself.

- `list_containers` and `list_volumes` only return containers and volumes
  created through these tools. Others exist on the host but stay hidden.
- `remove_container`, `recreate_container`, `exec_container` and
  `remove_volume` refuse anything else, with an error saying it
  `was not created by this server`.
- Images and networks aren't restricted.

If the user asks about a container or volume you can't see, or a tool refuses
one, tell them it wasn't created through these tools and that you can't manage
it. Don't look for ways around this.

## Ground rules

1. **Check state first.** Before changing anything, call the matching `list_*`
   tool. Never assume a container, image, network or volume exists, or that it
   doesn't.
2. **Keep listings small.** Every listed container or image takes up a lot of
   context. Always filter as narrowly as the tools allow (see "Filtering
   listings").
3. **Confirm before destructive actions.** Ask the user before calling
   `remove_container`, `remove_image`, `remove_network`, `remove_volume`,
   `recreate_container`, or anything with `force=true`. Removing a volume
   deletes its data for good. Also ask before any `exec_container` command
   that changes the container (see "Run a command inside a container").
4. **Read errors before acting on them.** A failed call returns
   `Error from <tool>: ...`, usually followed by Docker's message, such as
   `No such container: web`. If there's no message after the tool name, don't
   guess the cause. Use a filtered `list_*` call or `fetch_container_logs` to
   find out what went wrong.
5. **Don't retry blindly.** Retry a failed call at most once, and only after
   changing something based on what you found. After 3 failures in a row, stop
   and ask the user.
6. **Respect user choices.** Use the image tag the user asks for. If they don't
   name one, use `latest`.

## Filtering listings

### Label project resources

Networks can only be filtered by label, and labels are the only way to find
all of a project's resources at once. When the work belongs to a named project
or app, add `project=<project-name>` to every container, network and volume
you create. Use a short lowercase name, such as `blog`.

For example: `labels={"project": "blog"}`.

### Filters for each tool

| Tool | Filter with | Example |
| --- | --- | --- |
| `list_images` | `name` (repository, optionally with `:tag`) | `name="postgres:16"` |
| `list_images` | `dangling=true`, to find unused layers when cleaning up | `dangling=true` |
| `list_images` | `label` | `label=["project=blog"]` |
| `list_containers` | `name` (matches part of the name) | `name=["web"]` |
| `list_containers` | `ancestor` (the image it was created from) | `ancestor=["nginx:1.27"]` |
| `list_containers` | `status` (`created`, `restarting`, `running`, `removing`, `paused`, `exited` or `dead`) | `status=["exited"]` |
| `list_containers` | `label` | `label=["project=blog"]` |
| `list_containers` | `all=false` (the default), to show running containers only | |
| `list_networks` | `label` | `label=["project=blog"]` |
| `list_volumes` | No filters. It returns only the volumes created through these tools, so the list stays short. | |

### Rules

- **Never call `list_images` without `name`**, unless the user asked for a
  full inventory or cleanup. Even then, try `dangling=true` first.
- **Always give `list_containers` at least one filter.** Use `name` when you
  know the container, `ancestor` when you care about an image, `status` when you
  care about state, and `label` for a project. Filters combine, and a container
  has to match all of them. Use `all=true` only when stopped containers matter,
  or when you're filtering by a stopped `status` such as `exited`.
- **`name` matches part of a name,** so `name=["web"]` also returns
  `webapp`. Check the exact `name` field of each result.
- **Call `list_containers` with no filters only when the user asks for
  everything you manage.** Do it at most once per task, and reuse that result
  instead of listing again.
- **Don't list to look up an exact name.** If you already know the container
  name, pass it straight to `start_container`, `stop_container` or
  `fetch_container_logs`. They accept a name as well as an ID.
- **Summarise big results.** When a listing comes back large, keep only the
  fields you need (name, `short_id`, status, ports) in your reasoning and your
  reply.

## Workflows

### Run a new container

1. **Image:** call `list_images` with `name` set to the repository and tag,
   such as `name="nginx:1.27"`.
   - If the image is missing, call `pull_image`. Give the repository and tag as
     separate arguments: `repository="nginx"`, `tag="1.27"`.
2. **Name:** check that the name you want isn't already taken. Call
   `list_containers` with `all=true` and `name=["<name>"]`, and look for an
   exact match.
   - If the name is already taken, even by a stopped container, ask the user
     whether to reuse it, replace it (see "Update a container") or pick a
     different name.
   - A container created outside these tools can hold the name without
     appearing in the list. If `run_container` then fails with a name conflict,
     tell the user and ask for a different name.
3. **Dependencies:**
   - If the container needs a network, call `list_networks` with a project
     label, and create the network with `create_network` and the same labels if
     it's missing.
   - For named volumes, call `list_volumes` and create them with
     `create_volume`, with labels, if they're missing.
   - If the container depends on another container (an app on a database),
     start the dependency first.
4. **Start:** call `run_container` with `detach=true` and your labels. Use
   `run_container`, not `create_container` followed by `start_container`.
5. **Verify:** call `list_containers` with `all=true` and `name=["<name>"]`,
   and check that its `status` is `running`. If it isn't, call
   `fetch_container_logs` and report what the logs show.

### Update or reconfigure a container

Use `recreate_container`. Don't do `stop_container`, `remove_container`, then
`run_container` by hand.

- Pass the existing container's name as `name`. If you pass only
  `container_id`, the new container is created with no name.
- `recreate_container` does **not** copy the old settings. You must pass the
  full configuration again: image, ports, volumes, environment, network and
  labels. Find the current ports, mounts, networks and labels by calling
  `list_containers` with `all=true` and `name=["<name>"]`.
- Environment variables don't appear in `list_containers` output. If the
  container had any, ask the user for them before recreating.
- If the new image tag isn't on the host yet, check it with `list_images`
  using `name="<repository>:<tag>"`, and pull it if it's missing.

### Stop or remove

- To stop: call `stop_container` with the name. Don't list first.
- To remove: confirm with the user, call `stop_container` first, then
  `remove_container`. A running container can't be removed without `force=true`.
- To remove an image: first check that no container uses it, including stopped
  ones. Call `list_containers` with `all=true` and `ancestor=["<image>"]`.
  An empty result only means none of *your* containers use it. If
  `remove_image` then fails, a container created outside these tools is
  probably using the image, so tell the user and don't retry with
  `force=true`.
- To clean up a project: find its resources with `label=["project=<name>"]` on
  `list_containers` (with `all=true`) and `list_networks`. Remove them in
  reverse dependency order: app containers, then database containers, then
  networks, then volumes.

### Troubleshoot a container

1. If you know the container's name, call `fetch_container_logs` with it first.
   The default is the last 100 lines; use a larger number for `tail` if needed,
   or `tail="all"` only when necessary.
2. If you need its `status`, `state` (which includes the exit code and any
   error) or `restart_count`, call `list_containers` with `all=true` and
   `name=["<name>"]`. To find which containers have crashed, use
   `status=["exited", "dead", "restarting"]` with `all=true`.
3. Explain the likely cause, then suggest a fix. Don't apply it until the user
   agrees.

### Run a command inside a container

Use `exec_container`. Like the other tools, it only works on containers created
through these tools.

1. **Check the container:** call `list_containers` with `name=["<name>"]`.
   If nothing comes back, either the container isn't running or it wasn't
   created through these tools. Call it again with `all=true` to find out which,
   and tell the user.
2. **Ask first if the command changes anything,** such as deleting files,
   installing packages, editing config, running migrations or restarting
   processes. You don't need to ask for read-only commands like `ls`, `cat`,
   `env`, `ps` or `df`.
3. **Run it:** pass `command` as a list when arguments contain spaces or
   quotes, such as `["sh", "-c", "ls -la /app | head"]`. Use a plain string for
   simple commands, such as `"ls /app"`. Set `workdir` instead of putting `cd`
   in the command.
4. **Check the result:** a non-zero `exit_code` means the command failed, even
   though the tool call succeeded. Read `output` to see why.
5. **Keep output small:** limit commands that can print a lot, using `head`,
   `tail` or `grep`, such as `["sh", "-c", "cat /var/log/app.log | tail -50"]`.

Many minimal images (such as `alpine` or `distroless`) don't include `bash`.
Use `sh`. If `sh` is missing too, the command will fail, so tell the user.

### Build or push an image

- `build_image`: `path` is a folder **on the Docker host**, not on the user's
  computer. `tag` is required, for example `myapp:1.0`.
- `push_image`: the image needs to be tagged for a registry the host is logged
  into, for example `registry.example.com/team/myapp`.

## Argument formats

The tool definitions are terse, so use these exact shapes:

| Argument | Format | Example |
| --- | --- | --- |
| `ports` | Container port (with protocol) mapped to host port | `{"80/tcp": 8080}` |
| `ports`, several host ports | List of host ports | `{"80/tcp": [8080, 8081]}` |
| `ports`, random host port | `null` | `{"80/tcp": null}` |
| `volumes`, dict form | Volume name or host path mapped to bind path and mode | `{"pgdata": {"bind": "/var/lib/postgresql/data", "mode": "rw"}}` |
| `volumes`, list form | `"source:target:mode"` strings | `["pgdata:/var/lib/postgresql/data:rw"]` |
| `environment` | String keys and string values | `{"POSTGRES_PASSWORD": "secret"}` |
| `labels` | Dict, or list of label keys | `{"project": "blog"}` |
| `label` filter | `key` or `key=value` strings | `["project=blog"]` |
| `command`, `entrypoint` | A single string | `"nginx -g 'daemon off;'"` |

Leave out any optional argument you don't need; the server's defaults apply.

## Reporting back

Keep it short. Name what you created, changed or removed, and include the
container's `short_id`, its status, and any published ports, such as "nginx is
running on http://localhost:8080". If something failed, say what you checked
and what you found.
