#!/bin/sh
# Tests for toff. Everything runs inside a throwaway HOME; launchctl is never called.
# Single-quoted `sh -c` scripts and "~" in test names are intended.
# shellcheck disable=SC2016,SC2088
set -u

here=$(cd "$(dirname "$0")/.." && pwd)
TOFF="$here/toff"
fails=0
passes=0

check() { # name command...
  name=$1
  shift
  if "$@" >/dev/null 2>&1; then
    passes=$((passes + 1))
    echo "ok   $name"
  else
    fails=$((fails + 1))
    echo "FAIL $name"
  fi
}

setup() {
  T=$(mktemp -d "${TMPDIR:-/tmp}/toff-test.XXXXXX")
  HOME="$T/home"
  XDG_CONFIG_HOME="$HOME/.config"
  XDG_DATA_HOME="$HOME/.local/share"
  T3CODE_HOME="$HOME/.t3"
  TOFF_NO_LAUNCHCTL=1
  NO_COLOR=1
  SHELL=/bin/bash
  export HOME XDG_CONFIG_HOME XDG_DATA_HOME T3CODE_HOME TOFF_NO_LAUNCHCTL NO_COLOR SHELL
  unset ZDOTDIR || true
  mkdir -p "$HOME"
}

blocks() { grep -c '^# >>> t-off >>>' "$1" 2>/dev/null || true; }
has_one_block() { [ "$(blocks "$1")" = 1 ]; }
no_block() { [ "$(blocks "$1")" = 0 ]; }
sources_false() { [ "$(bash -c ". '$1'; printf %s \"\${T3CODE_TELEMETRY_ENABLED:-unset}\"")" = false ]; }
dotenv_false() { grep -qx 'T3CODE_TELEMETRY_ENABLED=false' "$1"; }
state_is() { grep -qx "mode=$1" "$XDG_CONFIG_HOME/t-off/state"; }
login_shell_false() { bash -ilc env </dev/null 2>/dev/null | grep -qx 'T3CODE_TELEMETRY_ENABLED=false'; }

# 1. fresh home, bash user ---------------------------------------------------
setup
"$TOFF" optout >/dev/null
check "optout writes ~/.profile for a bash user without bash_profile" has_one_block "$HOME/.profile"
check "the profile block exports the opt-out" sources_false "$HOME/.profile"
check "a bash login shell (what the desktop app runs) sees it" login_shell_false
check "optout writes ~/.t3/service.env" has_one_block "$T3CODE_HOME/service.env"
check "service.env holds a plain KEY=VALUE line" dotenv_false "$T3CODE_HOME/service.env"
check "state records optout" state_is optout
if command -v zsh >/dev/null 2>&1; then
  check "optout writes ~/.zshenv when zsh is installed" has_one_block "$HOME/.zshenv"
  check "zsh -ilc sees it" sh -c 'zsh -ilc env </dev/null 2>/dev/null | grep -qx T3CODE_TELEMETRY_ENABLED=false'
fi
if [ "$(uname -s)" = Darwin ]; then
  check "LaunchAgent is written" test -f "$HOME/Library/LaunchAgents/io.github.t-off.env.plist"
  check "LaunchAgent is a valid plist" plutil -lint "$HOME/Library/LaunchAgents/io.github.t-off.env.plist"
else
  check "environment.d file is written" grep -qx 'T3CODE_TELEMETRY_ENABLED=false' "$XDG_CONFIG_HOME/environment.d/60-t-off.conf"
fi
"$TOFF" optout >/dev/null
check "optout is idempotent (one block in ~/.profile)" has_one_block "$HOME/.profile"
check "optout is idempotent (one block in service.env)" has_one_block "$T3CODE_HOME/service.env"
check "status exits 0" "$TOFF" status
check "status reports telemetry off" sh -c '"$1" status | grep -q "telemetry off"' _ "$TOFF"
check "help works" "$TOFF" help
check "unknown commands fail" sh -c '! "$1" bogus' _ "$TOFF"

# 2. optin needs explicit consent and restores files exactly -------------------
check "optin without a terminal refuses" sh -c '! "$1" optin </dev/null' _ "$TOFF"
check "refused optin keeps the opt-out" has_one_block "$HOME/.profile"
"$TOFF" optin --yes-i-understand >/dev/null
check "optin removes the profile block" no_block "$HOME/.profile"
check "optin removes an otherwise empty service.env" test ! -e "$T3CODE_HOME/service.env"
check "state records optin" state_is optin
if [ "$(uname -s)" = Darwin ]; then
  check "optin removes the LaunchAgent" test ! -e "$HOME/Library/LaunchAgents/io.github.t-off.env.plist"
fi

# 3. existing dotfiles: content preserved, symlinks kept, missing newline handled ----
setup
mkdir -p "$HOME/dotfiles" "$T3CODE_HOME"
printf 'export PATH="$HOME/bin:$PATH"' >"$HOME/dotfiles/bash_profile" # no trailing newline
ln -s "$HOME/dotfiles/bash_profile" "$HOME/.bash_profile"
printf "alias ll='ls -l'\n" >"$HOME/.bashrc"
printf 'T3CODE_PORT=3773\n' >"$T3CODE_HOME/service.env"
cp "$HOME/dotfiles/bash_profile" "$T/bash_profile.orig"
cp "$HOME/.bashrc" "$T/bashrc.orig"
cp "$T3CODE_HOME/service.env" "$T/service.env.orig"
"$TOFF" optout >/dev/null
check "a symlinked ~/.bash_profile stays a symlink" test -h "$HOME/.bash_profile"
check "the block lands in the symlink target" has_one_block "$HOME/dotfiles/bash_profile"
check "user content before the block is intact" sh -c 'head -n 1 "$1" | grep -qx "export PATH=\"\$HOME/bin:\$PATH\""' _ "$HOME/dotfiles/bash_profile"
check "~/.bashrc also gets the block" has_one_block "$HOME/.bashrc"
check "~/.profile is left alone when ~/.bash_profile exists" test ! -e "$HOME/.profile"
check "existing service.env entries are kept" grep -qx 'T3CODE_PORT=3773' "$T3CODE_HOME/service.env"
"$TOFF" optin --yes-i-understand >/dev/null
check "optin restores ~/.bashrc byte for byte" cmp -s "$HOME/.bashrc" "$T/bashrc.orig"
check "optin restores service.env byte for byte" cmp -s "$T3CODE_HOME/service.env" "$T/service.env.orig"
check "optin restores the symlinked profile (plus its newline)" sh -c 'printf "\n" | cat "$1" - | cmp -s - "$2"' _ "$T/bash_profile.orig" "$HOME/dotfiles/bash_profile"

# 4. audit reads T3 Code's trace logs ------------------------------------------
setup
"$TOFF" optout >/dev/null
since=$(sed -n 's/^since=//p' "$XDG_CONFIG_HOME/t-off/state")
logs="$T3CODE_HOME/userdata/logs"
mkdir -p "$logs"
span() { # epoch outcome
  printf '{"type":"effect-span","name":"POST","startTimeUnixNano":"%s000000000","attributes":{"url.full":"https://ingest.t3.codes/batch/","server.address":"ingest.t3.codes"},"exit":{"_tag":"%s"}}\n' "$1" "$2"
}
span $((since - 7200)) Success >"$logs/server.trace.ndjson.1"
span $((since - 3600)) Failure >>"$logs/server.trace.ndjson.1"
check "audit passes when uploads stopped at opt-out" "$TOFF" audit
check "audit counts uploads before the opt-out" sh -c '"$1" audit | grep -q "2 upload attempt"' _ "$TOFF"
span $((since + 60)) Success >"$logs/server.trace.ndjson"
check "audit fails when an upload happened after opt-out" sh -c '! "$1" audit' _ "$TOFF"

# 5. uninstall -----------------------------------------------------------------
"$TOFF" uninstall --yes >/dev/null
check "uninstall removes the state directory" test ! -e "$XDG_CONFIG_HOME/t-off"
check "uninstall removes the profile block" no_block "$HOME/.profile"

echo
echo "$passes passed, $fails failed"
[ "$fails" = 0 ]
