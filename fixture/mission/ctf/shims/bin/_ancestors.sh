# Prints the PID of this process and of each of its ancestors, one per line.
ancestors() {
  local pid=$$
  while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    echo "$pid"
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
  done
}
