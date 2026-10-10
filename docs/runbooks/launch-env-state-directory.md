# Launch environment state directory

The service user must own `~`, `~/.local`, and `~/.local/state`, and those
directories must not be group- or other-writable. The same ownership and write
restriction applies to an existing `~/.local/state/pinkybot`. The daemon creates
`pinkybot` and its `tmux-launch-env` child with mode 0700; `tmux-launch-env` and
its scope directories must allow no group or other access.

An unsafe component can leave every agent pane in `boot_failed` while the daemon
is still active. At startup, the daemon checks in the background and writes at
most one warning naming the home-relative component, its four-digit octal mode,
owner uid, effective uid,
and the failed rule. Launch refusals retain the `unsafe launch environment
directory` or `unsafe launch environment home` prefix. In the startup warning,
unexpected filesystem errors report only an errno symbol or UNKNOWN. Check for
a symlink when the symbol is `ELOOP` or `ENOTDIR`.

Run the warning's repair command in the service user's shell. For example,
remove group and other write access from writable ancestors:

```sh
chmod g-w,o-w ~/.local ~/.local/state
```

For excess access on `~/.local/state/pinkybot/tmux-launch-env`, use
`chmod g-rwx,o-rwx ~/.local/state/pinkybot/tmux-launch-env`. An owner refusal
names the uid that must own the component; have an operator correct that
component's ownership for the service user before retrying the launch. The
daemon never changes directory permissions or ownership automatically.

For systemd installs with `ProtectSystem=strict`, the unit must be able to write
to the service user's `~/.local/state`, for example by including that directory
in `ReadWritePaths`.
