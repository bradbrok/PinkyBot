# Daemon tmux server

The daemon runs its panes on a dedicated tmux server. Use `tmux -L pinkybot ls`
to list them and `tmux -L pinkybot attach -t <session>` to attach. Set
`PINKY_TMUX_SOCKET` to use another label; an empty value selects the legacy shared
server. On the first start after upgrading, the daemon closes its exact registered
`pinky-*` sessions on the shared server once and relaunches active agents on the
dedicated server. Other sessions and preserved login sessions are left alone.
Managed launches, including standalone dreams, require that startup pass or its
durable completion marker. Start the daemon once before using standalone tmux
entries after upgrading; standalone entries never perform the migration.

`PINKY_TMUX_PANE_PATH` overrides the pane PATH with an ordered, colon-separated
list of absolute directories. Otherwise the daemon PATH is extended with standard
Homebrew, local, and system bin/sbin directories. Managed servers ignore tmux
configuration files. Isolated clean launches refuse a polluted server; other
launches warn without changing its global environment. The shared-server option
does not support isolated clean launches.

Local managed launches set pane HOME and PATH from the daemon's constructed
client environment, including when the server was started by an earlier daemon.
Warnings report only the unexpected-name count and a command for inspecting the
server; parsed names can contain fragments of multiline values and are not logged.

Don't start the dedicated server by hand: a server started outside the daemon
inherits that shell's environment, and isolated launches refuse it until it is
restarted.
