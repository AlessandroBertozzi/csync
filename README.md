# csync

Sync Claude Code conversations across machines through any [rclone](https://rclone.org) remote, and pull them into whichever local folder holds the project.

Claude Code stores sessions in `~/.claude/projects/<encoded-path>/`, keyed by the project's absolute path. `csync pull --to DIR` places them under the encoding of `DIR`, so `claude --resume` run from `DIR` finds them.

## Install

Requires Python 3.11+ and rclone.

```sh
uv tool install git+https://github.com/AlessandroBertozzi/csync.git   # or pipx install ...
```

Configure an rclone remote, then point csync at it:

```sh
rclone config create b2 b2 account=<keyID> key=<applicationKey>      # any rclone backend works

# optional, recommended: client-side encryption
rclone config create b2-crypt crypt remote=b2:<bucket>
rclone config password b2-crypt password '<password>' password2 '<salt>'

mkdir -p ~/.config/csync
echo 'remote = "b2-crypt:"' > ~/.config/csync/config.toml
csync doctor
```

`--remote` and `CSYNC_REMOTE` override the config file.

## Commands

```sh
csync push [DIR]                 # upload sessions + memory of DIR (default: cwd)
csync push --all --since 90      # every local project touched in the last 90 days
csync list                       # projects on the remote, with their original path
csync pull <project> --to DIR    # download into DIR; <project> = name, original path or substring
csync pull <project> --to DIR --rewrite-paths   # also rewrite the old absolute path inside the sessions
csync doctor                     # check rclone, remote and local layout
```

`push` and `pull` never delete anything. Typical flow on a second machine:

```sh
csync pull hack --to ~/dev/hack --rewrite-paths
cd ~/dev/hack && claude --resume
```

## Automatic push

Add this to `~/.claude/settings.json` (or print it with `csync hook-snippet`) to push each session when it ends:

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "csync push --hook" }] }
    ]
  }
}
```

The push runs in the background; its log is in `~/.local/state/csync/hook.log`.

## License

MIT
