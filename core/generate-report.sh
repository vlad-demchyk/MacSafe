#!/bin/bash
# A local macOS auditor. Does not send data to the network (except --check-updates).
# Step 1 (this file): collects facts, assesses risks, builds the JSON.
# Step 2 (template.html): JavaScript draws the sections, dropdowns, graphs and zones.
#
#   bash generate-report.sh                   quick snapshot
#   bash generate-report.sh --deep            + caches by folder, large files, Homebrew
#   bash generate-report.sh --check-updates   + macOS updates (needs internet)

set -uo pipefail
PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH LC_ALL=C

DEEP=0
CHECK_UPDATES=0
for arg in "$@"; do
  case "$arg" in
    --deep) DEEP=1 ;;
    --check-updates) CHECK_UPDATES=1 ;;
    --regen) break ;;   # handled below, together with the snapshot identifier
    *) echo "Unknown parameter: $arg" >&2; exit 2 ;;
  esac
done

# ---------- Settings ----------
MAX_REPORTS=60                  # how many HTML reports to keep (one per day)
MAX_SNAPSHOTS=60                # how many JSON snapshots to keep (the calendar shows these days)
MAX_HISTORY=52                  # how many history entries to keep (a year of weekly entries)
REPORT_PERIOD_DAYS=7            # window for crash reports
# Background processes needed for work. Their average CPU time is not considered a problem:
# only current spikes, restart loops and errors in the logs are checked.
EXPECTED_PROCESS_RE='^(brain-daemon|codebase-memory-mcp)$'
XBRAIN_LOG="$HOME/.xbrain/daemon.log"
XBRAIN_DB="$HOME/brain/.brain/index.db"

# ---------- Paths ----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
# Report data lives outside the repository. The path can be set with the AUDIT_DATA_DIR variable.
REPORTS_DIR="${AUDIT_DATA_DIR:-$HOME/Library/Application Support/MacSafe}"
DATA_DIR="$REPORTS_DIR/дані"
SYSTEMS_DIR="$REPORTS_DIR/системи"
TEMPLATE="$SCRIPT_DIR/template.html"
# One report per day: the ID is the date. Running again on the same day replaces that day's report.
TS="$(date '+%Y-%m-%d')"
REPORT_NAME="report-${TS}.html"
REPORT_FILE="$REPORTS_DIR/$REPORT_NAME"
LATEST_LINK="$REPORTS_DIR/latest.html"
SNAPSHOT_FILE="$DATA_DIR/report-${TS}.json"
HISTORY_FILE="$DATA_DIR/history.json"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sysreport.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$REPORTS_DIR" "$DATA_DIR" "$SYSTEMS_DIR"
[ -f "$TEMPLATE" ] || { echo "Error: template not found: $TEMPLATE" >&2; exit 1; }

# Mode --regen <id>: regenerate the HTML report from a saved snapshot, without a new check.
# This way an old day is shown with the current template. No new data is collected.
if [ "${1:-}" = "--regen" ]; then
  RID="${2:-}"
  [[ "$RID" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}(_[0-9]{2}-[0-9]{2}-[0-9]{2})?$ ]] || { echo "Error: invalid snapshot identifier: $RID" >&2; exit 2; }
  SRC="$DATA_DIR/report-$RID.json"
  [ -f "$SRC" ] || { echo "Error: there is no snapshot for this day: $SRC" >&2; exit 1; }
  OUT="$REPORTS_DIR/report-$RID.html"
  cat > "$WORK/render.pl" <<'PERL'
use strict;
use warnings;
use JSON::PP;

my ($snap_path, $hist_path, $sys_dir, $tpl_path, $out_path) = @ARGV;
my $json = JSON::PP->new->utf8->canonical;

sub slurp {
  my ($file) = @_;
  open my $fh, '<:raw', $file or return undef;
  local $/;
  my $text = <$fh>;
  close $fh;
  return $text;
}

my $core = $json->decode(slurp($snap_path));
my $hist_text = slurp($hist_path);
my $hist = defined $hist_text ? eval { $json->decode($hist_text) } : undef;
$core->{history} = ref $hist eq 'ARRAY' ? $hist : [];

my @systems;
opendir(my $sh, $sys_dir) or die "No folder $sys_dir: $!\n";
for my $name (sort readdir($sh)) {
  next unless $name =~ /\.json$/i;
  my $d = eval { $json->decode(slurp("$sys_dir/$name")) };
  push @systems, $d if ref $d eq 'HASH' && ref $d->{meta} eq 'HASH';
}
closedir $sh;
$core->{systems} = \@systems;

my $data = $json->encode($core);
$data =~ s/</\\u003c/g;
my $html = slurp($tpl_path);
$html =~ s/__REPORT_JSON__/$data/ or die "The template has no place for data\n";
my $tmp = "$out_path.tmp.$$";
open my $ofh, '>:raw', $tmp or die "Could not write $tmp: $!\n";
print $ofh $html or die "Write error for $tmp: $!\n";
close $ofh or die "Close error for $tmp: $!\n";
rename $tmp, $out_path or die "Could not replace $out_path: $!\n";
PERL
  if ! perl "$WORK/render.pl" "$SRC" "$HISTORY_FILE" "$SYSTEMS_DIR" "$TEMPLATE" "$OUT"; then
    echo "Error: could not regenerate the report for $RID" >&2
    exit 1
  fi
  echo "Regenerated: $OUT"
  exit 0
fi

# ---------- Helper functions ----------
note() { printf '%s\n' "$*"; }
is_number() { [[ "${1:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]; }
num() { if [[ "${1:-}" =~ ^(0|[1-9][0-9]*)([.][0-9]+)?$ ]]; then printf '%s' "$1"; else printf 'null'; fi; }
qpath() { printf '%q' "$1"; }
integer_part() { printf '%s' "${1%%.*}"; }
div() { awk -v n="$1" -v d="$2" -v f="$3" 'BEGIN { printf f, n / d }'; }
sp_field() { printf '%s\n' "$1" | awk -F': ' -v f="$2" '$1 ~ f { print $2; exit }'; }
ioreg_value() { printf '%s\n' "$IOREG_BATT" | awk -v key="$1" '$0 ~ "\"" key "\" = " { sub(/^.*= /, ""); gsub(/[[:space:]"]/, ""); print; exit }'; }
size_mb() { [ -e "$1" ] || return 0; du -s -k -x "$1" 2>/dev/null | awk '{ printf "%d", $1 / 1024 }'; }

# JSON helpers. The string is escaped by perl; an empty string gives “”.
jstr() {
  if [ -z "${1:-}" ]; then printf '""'; return; fi
  printf '%s' "$1" | perl -CS -0777 -pe 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\r/\\r/g; s/\n/\\n/g; s/([\x00-\x08\x0b\x0c\x0e-\x1f])/sprintf("\\u%04x", ord($1))/ge; $_ = "\"$_\""'
}
kv() { printf '%s:%s' "$(jstr "$1")" "$(jstr "${2:-}")"; }
kn() { printf '%s:%s' "$(jstr "$1")" "$(num "${2:-}")"; }
kr() { printf '"%s":%s' "$1" "${2:-null}"; }
kb() { printf '"%s":%s' "$1" "${2:-false}"; }
arr() { local IFS=,; printf '[%s]' "$*"; }
obj() { local IFS=,; printf '{%s}' "$*"; }

# Security commands: we accept only a successful call with no diagnostics in stderr.
read_security() {
  local out
  command -v "$1" >/dev/null 2>&1 || return 1
  out="$("$@" 2>"$WORK/security-command.err")" || return 1
  [ ! -s "$WORK/security-command.err" ] || return 1
  printf '%s' "$out"
}
add_security() { # name kind value command advice
  local kind="$2" value="$3" advice="$5" status
  case "$kind" in
    good) status="ок" ;;
    info) status="до відома" ;;
    warning) status="увага" ;;
    critical) status="критично" ;;
    *) kind="unknown"; status="немає даних"; value="немає даних"
       advice="Перевірте стан у налаштуваннях macOS; дані недоступні або не розібрані." ;;
  esac
  SECURITY_ARR+=("$(obj "$(kv label "$1")" "$(kv kind "$kind")" "$(kv status "$status")" "$(kv value "$value")" "$(kv command "$4")" "$(kv advice "$advice")")")
}

# ---------- Collecting: hardware and system ----------
note "Collecting: hardware and system"
HW_DATA="$(system_profiler SPHardwareDataType 2>/dev/null || true)"
MODEL_NAME="$(sp_field "$HW_DATA" 'Model Name')"
CHIP="$(sp_field "$HW_DATA" '^ *Chip')"
CORES="$(sp_field "$HW_DATA" 'Total Number of Cores')"
MEMORY_TOTAL="$(sp_field "$HW_DATA" '^ *Memory')"
OS_NAME="$(sw_vers -productName 2>/dev/null || true)"
OS_VERSION="$(sw_vers -productVersion 2>/dev/null || true)"
OS_BUILD="$(sw_vers -buildVersion 2>/dev/null || true)"
KERNEL="$(uname -r 2>/dev/null || true)"
COMPUTER_NAME="$(scutil --get ComputerName 2>/dev/null || hostname 2>/dev/null || true)"
NCPU="$(sysctl -n hw.ncpu 2>/dev/null || true)"
MEM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || true)"
BOOT_EPOCH="$(sysctl -n kern.boottime 2>/dev/null | sed -nE 's/^[{] sec = ([0-9]+),.*/\1/p')"
NOW_EPOCH="$(date +%s)"
UPTIME_DAYS=""; UPTIME_TEXT=""; BOOT_DATE=""
if is_number "$BOOT_EPOCH"; then
  UP_SEC=$((NOW_EPOCH - BOOT_EPOCH))
  UPTIME_DAYS=$((UP_SEC / 86400))
  UPTIME_TEXT="${UPTIME_DAYS} дн. $(((UP_SEC % 86400) / 3600)) год."
  BOOT_DATE="$(date -r "$BOOT_EPOCH" '+%d.%m.%Y %H:%M')"
fi
DATE_TEXT="$(date '+%d.%m.%Y о %H:%M')"

# ---------- Collecting: battery ----------
BATTERY_RAW="$(pmset -g batt 2>/dev/null || true)"
BATT_PCT="$(printf '%s\n' "$BATTERY_RAW" | sed -nE 's/.*\t([0-9]+)%;.*/\1/p' | head -1)"
BATT_STATE_RAW="$(printf '%s\n' "$BATTERY_RAW" | sed -nE 's/.*;[[:space:]]*([^;]+);.*/\1/p' | head -1)"
case "$BATT_STATE_RAW" in
  charging) BATT_STATE="Заряджається" ;;
  discharging) BATT_STATE="Розряджається" ;;
  charged) BATT_STATE="Заряджено" ;;
  "AC attached") BATT_STATE="Підключено до живлення" ;;
  *) BATT_STATE="$BATT_STATE_RAW" ;;
esac
BATT_REMAIN="$(printf '%s\n' "$BATTERY_RAW" | sed -nE 's/.*;[[:space:]]*([0-9]+:[0-9]+) remaining.*/\1/p' | head -1)"
IOREG_BATT="$(ioreg -r -c AppleSmartBattery 2>/dev/null || true)"
CYCLE_COUNT="$(ioreg_value CycleCount)"
# On modern Macs the capacity is inside the BatteryData string: FullChargeCapacity and DesignCapacity in mAh
BATT_DATA_LINE="$(printf '%s\n' "$IOREG_BATT" | grep -m1 '"BatteryData"' || true)"
batt_data_value() { printf '%s' "$BATT_DATA_LINE" | sed -nE "s/.*\"$1\"=([0-9]+).*/\1/p" | head -1; }
MAX_CAP="$(batt_data_value FullChargeCapacity)"
[ -n "$MAX_CAP" ] || MAX_CAP="$(ioreg_value AppleRawMaxCapacity)"
DESIGN_CAP="$(batt_data_value DesignCapacity)"
[ -n "$DESIGN_CAP" ] || DESIGN_CAP="$(ioreg_value DesignCapacity)"
BATT_TEMP_RAW="$(ioreg_value Temperature)"
BATT_VOLT_RAW="$(ioreg_value Voltage)"
BATT_CONDITION="$(ioreg_value BatteryHealth)"
# Health: the "Maximum Capacity" that macOS itself shows (System Settings). FullChargeCapacity jumps with temperature
# and recalibration and reads lower than that figure, so it is only the last fallback.
BATT_HEALTH_PCT=""; BATT_HEALTH_SRC=""
if [ -n "$BATT_PCT" ]; then
  SP_CAP="$(LC_ALL=C system_profiler SPPowerDataType 2>/dev/null | sed -nE 's/.*Maximum Capacity:[[:space:]]*([0-9]+)%.*/\1/p' | head -1)"
  if is_number "$SP_CAP"; then BATT_HEALTH_PCT="$SP_CAP"; BATT_HEALTH_SRC="macOS Maximum Capacity"; fi
fi
if [ -z "$BATT_HEALTH_PCT" ] && is_number "$DESIGN_CAP" && [ "$(integer_part "$DESIGN_CAP")" -gt 0 ]; then
  NOMINAL_CAP="$(batt_data_value NominalChargeCapacity)"
  if is_number "$NOMINAL_CAP"; then
    BATT_HEALTH_PCT="$(awk -v a="$NOMINAL_CAP" -v b="$DESIGN_CAP" 'BEGIN { printf "%.0f", a / b * 100 }')"; BATT_HEALTH_SRC="NominalChargeCapacity / DesignCapacity"
  elif is_number "$MAX_CAP"; then
    BATT_HEALTH_PCT="$(awk -v a="$MAX_CAP" -v b="$DESIGN_CAP" 'BEGIN { printf "%.0f", a / b * 100 }')"; BATT_HEALTH_SRC="FullChargeCapacity / DesignCapacity"
  fi
fi
BATT_TEMP_C=""; BATT_VOLT_V=""
is_number "$BATT_TEMP_RAW" && BATT_TEMP_C="$(div "$BATT_TEMP_RAW" 100 '%.1f')"
is_number "$BATT_VOLT_RAW" && BATT_VOLT_V="$(div "$BATT_VOLT_RAW" 1000 '%.2f')"
BATT_PRESENT=false
[ -n "$BATT_PCT" ] && BATT_PRESENT=true

# ---------- Collecting: storage ----------
DISK_INFO="$(df -k /System/Volumes/Data 2>/dev/null | tail -1)"
[ -n "$DISK_INFO" ] || DISK_INFO="$(df -k / 2>/dev/null | tail -1)"
DISK_TOTAL_KB="$(printf '%s\n' "$DISK_INFO" | awk '{print $2}')"
DISK_USED_KB="$(printf '%s\n' "$DISK_INFO" | awk '{print $3}')"
DISK_AVAIL_KB="$(printf '%s\n' "$DISK_INFO" | awk '{print $4}')"
DISK_PCT="$(printf '%s\n' "$DISK_INFO" | awk '{gsub(/%/, "", $5); print $5}')"
DISK_TOTAL_GB=""; DISK_USED_GB=""; DISK_AVAIL_GB=""
is_number "$DISK_TOTAL_KB" && DISK_TOTAL_GB="$(div "$DISK_TOTAL_KB" 1048576 '%.0f')"
is_number "$DISK_USED_KB" && DISK_USED_GB="$(div "$DISK_USED_KB" 1048576 '%.0f')"
is_number "$DISK_AVAIL_KB" && DISK_AVAIL_GB="$(div "$DISK_AVAIL_KB" 1048576 '%.0f')"
STORAGE_DATA="$(system_profiler SPStorageDataType 2>/dev/null || true)"
STORAGE_DEVICE="$(sp_field "$STORAGE_DATA" 'Device Name')"
SMART_STATUS="$(sp_field "$STORAGE_DATA" 'S.M.A.R.T.')"
LOCAL_SNAPS="$(tmutil listlocalsnapshots / 2>/dev/null | grep -c 'com.apple.TimeMachine' || true)"

# ---------- Collecting: load and memory ----------
note "Collecting: load, memory, processes"
TOP_OUTPUT="$(top -l 2 -s 1 -n 0 2>/dev/null || true)"
CPU_LINE="$(printf '%s\n' "$TOP_OUTPUT" | awk '/CPU usage:/ { l = $0 } END { print l }')"
CPU_USER="$(printf '%s\n' "$CPU_LINE" | sed -nE 's/.*: *([0-9.]+)% user.*/\1/p')"
CPU_SYS="$(printf '%s\n' "$CPU_LINE" | sed -nE 's/.*user, *([0-9.]+)% sys.*/\1/p')"
CPU_TOTAL=""
if is_number "$CPU_USER" && is_number "$CPU_SYS"; then
  CPU_TOTAL="$(awk -v a="$CPU_USER" -v b="$CPU_SYS" 'BEGIN { printf "%.1f", a + b }')"
fi
PHYS_LINE="$(printf '%s\n' "$TOP_OUTPUT" | awk '/PhysMem:/ { l = $0 } END { print l }')"
MEM_USED_TEXT="$(printf '%s\n' "$PHYS_LINE" | sed -nE 's/^PhysMem: *([^,]+) used.*/\1/p')"
# Raw inactive memory (PhysMem: ... unused). Different from memory_pressure, which accounts for what can be freed.
UNUSED_RAW="$(printf '%s\n' "$PHYS_LINE" | sed -nE 's/.*,[[:space:]]*([0-9]+([.][0-9]+)?)([MGK])[[:space:]]+unused.*/\1\3/p')"
UNUSED_MB=""
case "$UNUSED_RAW" in
  *G) UNUSED_MB="$(awk -v n="${UNUSED_RAW%G}" 'BEGIN { printf "%.0f", n * 1024 }')" ;;
  *M) UNUSED_MB="${UNUSED_RAW%M}"; UNUSED_MB="${UNUSED_MB%%.*}" ;;
  *K) UNUSED_MB=0 ;;
esac
# Active swap traffic: two vm_stat samples 5 s apart. Cumulative counters since boot do not show the current state.
VM_A="$(vm_stat 2>/dev/null || true)"
[ -n "$VM_A" ] && sleep 5
VM_B="$(vm_stat 2>/dev/null || true)"
vm_count() { printf '%s\n' "$1" | awk -F': *' -v k="$2" '$1 == k { gsub(/\./, "", $2); print $2; exit }'; }
SWAPOUT_5S=""; PAGEOUT_5S=""
SO_A="$(vm_count "$VM_A" Swapouts)"; SO_B="$(vm_count "$VM_B" Swapouts)"
PO_A="$(vm_count "$VM_A" Pageouts)"; PO_B="$(vm_count "$VM_B" Pageouts)"
is_number "$SO_A" && is_number "$SO_B" && SWAPOUT_5S=$((SO_B - SO_A))
is_number "$PO_A" && is_number "$PO_B" && PAGEOUT_5S=$((PO_B - PO_A))
PROCESS_COUNT="$(printf '%s\n' "$TOP_OUTPUT" | awk '/^Processes:/ { n = $2 } END { print n }')"
LOAD_RAW="$(sysctl -n vm.loadavg 2>/dev/null || true)"
LOAD_1="$(printf '%s' "$LOAD_RAW" | awk '{ print $2 }')"
LOAD_5="$(printf '%s' "$LOAD_RAW" | awk '{ print $3 }')"
LOAD_15="$(printf '%s' "$LOAD_RAW" | awk '{ print $4 }')"
LOAD_5_PER_CORE=""
if is_number "$LOAD_5" && is_number "$NCPU" && [ "$NCPU" -gt 0 ]; then
  LOAD_5_PER_CORE="$(awk -v l="$LOAD_5" -v c="$NCPU" 'BEGIN { printf "%.2f", l / c }')"
fi
MEM_FREE_PCT="$(memory_pressure 2>/dev/null | sed -nE 's/.*System-wide memory free percentage: *([0-9]+)%.*/\1/p' | head -1)"
if ! is_number "$MEM_FREE_PCT"; then
  MEM_FREE_PCT="$(vm_stat 2>/dev/null | awk -v tot="$MEM_BYTES" '/page size of/ { ps = $8 } /^Pages free:/ { f = $3 } /^Pages speculative:/ { s = $3 } END { if (tot > 0 && ps > 0) printf "%.0f", (f + s) * ps / tot * 100 }')"
fi
COMPRESSOR_MB="$(vm_stat 2>/dev/null | awk '/page size of/ { ps = $8 } /occupied by compressor/ { n = $5; gsub(/\./, "", n) } END { if (ps > 0 && n != "") printf "%.0f", n * ps / 1048576 }')"
SWAP_MB="$(sysctl vm.swapusage 2>/dev/null | awk '{ for (i = 1; i <= NF; i++) if ($i == "used") { v = $(i + 2); u = substr(v, length(v)); n = substr(v, 1, length(v) - 1); if (u == "G") n *= 1024; else if (u == "K") n /= 1024; printf "%.0f", n; exit } }')"
MEM_PRESSURE_LEVEL="unknown"
if is_number "$MEM_FREE_PCT"; then
  MEM_PRESSURE_LEVEL="ok"
  if [ "$MEM_FREE_PCT" -lt 5 ]; then MEM_PRESSURE_LEVEL="critical"
  elif [ "$MEM_FREE_PCT" -lt 10 ]; then MEM_PRESSURE_LEVEL="warning"; fi
fi

# Process table: pid, lifetime in seconds, CPU seconds, %CPU, RSS KB, name (the last path component)
PS_TABLE="$WORK/ps.tsv"
ps -Ao pid=,etime=,time=,%cpu=,rss=,comm= 2>/dev/null | awk '
  function secs(s,   parts, n, i, r, d, a) {
    d = 0
    if (index(s, "-")) { split(s, a, "-"); d = a[1]; s = a[2] }
    n = split(s, parts, ":"); r = 0
    for (i = 1; i <= n; i++) r = r * 60 + parts[i]
    return d * 86400 + r
  }
  {
    name = $6
    for (i = 7; i <= NF; i++) name = name " " $i
    n = split(name, p, "/")
    printf "%s\t%d\t%d\t%s\t%s\t%s\n", $1, secs($2), secs($3), $4, $5, p[n]
  }' > "$PS_TABLE"

category_of() {
  case "$1" in
    brain-daemon|codebase-memory-mcp|mcp-relay) echo "XBrain / індекс коду" ;;
    node|*vite*|*esbuild*|*webpack*|*turbo*|tsc|npm*) echo "Збірка / дев-сервер" ;;
    *Chrome*|*Safari*|*Firefox*|*WebKit*|Arc|Brave*) echo "Браузер" ;;
    Claude*|Codex*|codex|ChatGPT*|Code|"Code Helper"*|Cursor*|Slack*|Discord*|Notion*|Teams*|Spotify*|"YouTube Music"*) echo "Electron / AI / чат" ;;
    [0-9]*.[0-9]*.[0-9]*) echo "Claude Code (CLI)" ;;
    Finder|WindowServer|SkyLight*) echo "Графіка і Finder" ;;
    *openAndSavePanel*) echo "Діалог відкриття/збереження" ;;
    mds|mds_stores|mdworker*|Spotlight*) echo "Індексація Spotlight" ;;
    backupd*|TimeMachine*) echo "Time Machine" ;;
    kernel_task) echo "Ядро macOS (тротлінг)" ;;
    Xcode*|xcodebuild|swift*|clang*) echo "Xcode / компіляція" ;;
    *) echo "Інше" ;;
  esac
}

pretty_name() {
  case "$1" in
    com.apple.appkit.xpc.openAndSavePanelService) echo "Панель відкриття/збереження (Apple)" ;;
    *) echo "$1" ;;
  esac
}

proc_json() { # pid name %CPU RSS_MB life% expected
  obj "$(kn pid "$1")" "$(kv name "$(pretty_name "$2")")" "$(kn cpu "$3")" "$(kn rssMb "$4")" "$(kn life "$5")" "$(kv category "$(category_of "$2")")" "$(kb expected "$6")"
}

PROC_CPU_JSON=(); PROC_MEM_JSON=(); MEM_GROUP_JSON=()
while IFS=$'\t' read -r pid et ct cpu rss name; do
  [ -n "$pid" ] || continue
  life="$(awk -v c="$ct" -v e="$et" 'BEGIN { if (e > 0) printf "%.1f", c * 100 / e; else print 0 }')"
  rss_mb="$(div "$rss" 1024 '%.0f')"
  exp=false
  [[ "$name" =~ $EXPECTED_PROCESS_RE ]] && exp=true
  PROC_CPU_JSON+=("$(proc_json "$pid" "$name" "$cpu" "$rss_mb" "$life" "$exp")")
done < <(sort -t$'\t' -k4,4gr "$PS_TABLE" | head -12)

while IFS=$'\t' read -r pid et ct cpu rss name; do
  [ -n "$pid" ] || continue
  life="$(awk -v c="$ct" -v e="$et" 'BEGIN { if (e > 0) printf "%.1f", c * 100 / e; else print 0 }')"
  rss_mb="$(div "$rss" 1024 '%.0f')"
  exp=false
  [[ "$name" =~ $EXPECTED_PROCESS_RE ]] && exp=true
  PROC_MEM_JSON+=("$(proc_json "$pid" "$name" "$cpu" "$rss_mb" "$life" "$exp")")
done < <(sort -t$'\t' -k5,5nr "$PS_TABLE" | head -12)

while IFS=$'\t' read -r gname grss gcount; do
  [ -n "$gname" ] || continue
  MEM_GROUP_JSON+=("$(obj "$(kv name "$(pretty_name "$gname")")" "$(kn rssMb "$grss")" "$(kn count "$gcount")" "$(kv category "$(category_of "$gname")")")")
done < <(awk -F'\t' '{ g = $6; sub(/ Helper.*$/, "", g); sub(/ \(.*\)$/, "", g); rss[g] += $5; cnt[g]++ }
  END { for (k in rss) printf "%s\t%d\t%d\n", k, rss[k] / 1024, cnt[k] }' "$PS_TABLE" | sort -t$'\t' -k2,2nr | head -15)

# Sums for individual rules
FINDER_RSS_MB="$(awk -F'\t' '$6 == "Finder" { if ($5 > m) m = $5 } END { if (m > 0) printf "%.0f", m / 1024 }' "$PS_TABLE")"
PANEL_RSS_MB="$(awk -F'\t' '$6 ~ /openAndSavePanel/ { s += $5 } END { if (s > 0) printf "%.0f", s / 1024 }' "$PS_TABLE")"
PANEL_CPU="$(awk -F'\t' '$6 ~ /openAndSavePanel/ { s += $4 } END { if (s > 0) printf "%.1f", s }' "$PS_TABLE")"
ELECTRON_MB="$(awk -F'\t' '$6 ~ /^(Claude|Codex|codex|ChatGPT|Code|Cursor|Slack|Discord|Notion|Teams|Spotify|YouTube Music)/ { s += $5 } END { if (s > 0) printf "%.0f", s / 1024 }' "$PS_TABLE")"
XB_RSS_MB="$(awk -F'\t' '$6 == "brain-daemon" { if ($5 > m) m = $5 } END { if (m > 0) printf "%.0f", m / 1024 }' "$PS_TABLE")"
FINDERSYNC_LIST="$(pluginkit -m -p com.apple.FinderSync 2>/dev/null | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^[a-zA-Z][a-zA-Z0-9_-]*(\.[a-zA-Z0-9_-]+)+/) { sub(/\(.*/, "", $i); print $i; break } }' | sort -u | tr '\n' ' ' | sed 's/ *$//')"

# Spikes right now (CPU snapshot; 100% = one core)
SPIKE_ROWS="$(awk -F'\t' '$4 >= 100 { print }' "$PS_TABLE" | sort -t$'\t' -k4,4gr | head -3)"

# ---------- Collecting: temperatures and graphs ----------
IOREG_ALL="$(ioreg -l 2>/dev/null || true)"
CPU_TEMP_RAW="$(printf '%s\n' "$IOREG_ALL" | awk '/"sensor.*cpu.*temp|"cpu.*temp.*"|"TC[0-9][PCEF]"/ && /Temperature/ { gsub(/.*= /,""); gsub(/[^0-9]/,""); if ($1+0 > 0 && $1+0 < 20000) { print; exit } }')"
GPU_TEMP_RAW="$(printf '%s\n' "$IOREG_ALL" | awk '/"sensor.*gpu.*temp|"gpu.*temp.*"|"TG[0-9][PCEF]"/ && /Temperature/ { gsub(/.*= /,""); gsub(/[^0-9]/,""); if ($1+0 > 0 && $1+0 < 20000) { print; exit } }')"
SSD_TEMP_RAW="$(printf '%s\n' "$IOREG_ALL" | awk '/"sensor.*nand.*temp|"nand.*temp.*"|"TN[0-9][PCEF]"/ && /Temperature/ { gsub(/.*= /,""); gsub(/[^0-9]/,""); if ($1+0 > 0 && $1+0 < 20000) { print; exit } }')"
CPU_TEMP_C=""; GPU_TEMP_C=""; SSD_TEMP_C=""
is_number "$CPU_TEMP_RAW" && CPU_TEMP_C="$(div "$CPU_TEMP_RAW" 100 '%.1f')"
is_number "$GPU_TEMP_RAW" && GPU_TEMP_C="$(div "$GPU_TEMP_RAW" 100 '%.1f')"
is_number "$SSD_TEMP_RAW" && SSD_TEMP_C="$(div "$SSD_TEMP_RAW" 100 '%.1f')"
FAN_COUNT="$(printf '%s\n' "$IOREG_ALL" | awk -F= '/"Fan.*Count"/ { gsub(/[^0-9]/,"",$2); print $2; exit }')"
FAN_SPEED="$(printf '%s\n' "$IOREG_ALL" | awk -F= '/"Fan.*CurrentSpeed"/ { gsub(/[^0-9]/,"",$2); printf "%s ", $2 }' | sed 's/ *$//')"
THROTTLE_STATUS="Не виявлено"; THROTTLE_KIND="good"
THERMAL_CHARGE_LIMIT="$(ioreg_value TimeChargingThermallyLimited)"
if is_number "$THERMAL_CHARGE_LIMIT" && [ "$THERMAL_CHARGE_LIMIT" -gt 0 ]; then
  THROTTLE_STATUS="Заряджання обмежене теплом (${THERMAL_CHARGE_LIMIT} с)"; THROTTLE_KIND="warning"
fi
HEAT_LEVEL="$(printf '%s\n' "$IOREG_ALL" | awk -F= '/"AccumulatedHeatLevel"|"HeatLevel"/ { gsub(/[^0-9]/,"",$2); print $2; exit }')"
if is_number "$HEAT_LEVEL" && [ "$HEAT_LEVEL" -gt 0 ]; then
  THROTTLE_STATUS="Рівень тепла: ${HEAT_LEVEL} з 16"
  if [ "$HEAT_LEVEL" -ge 12 ]; then THROTTLE_KIND="critical"
  elif [ "$HEAT_LEVEL" -ge 8 ]; then THROTTLE_KIND="warning"; fi
fi
GPU_DATA="$(system_profiler SPDisplaysDataType 2>/dev/null || true)"
GPU_MODEL="$(sp_field "$GPU_DATA" 'Chipset Model')"
[ -n "$GPU_MODEL" ] || GPU_MODEL="$(sp_field "$GPU_DATA" 'Chip Model')"
GPU_VRAM="$(sp_field "$GPU_DATA" 'VRAM')"
[ -n "$GPU_VRAM" ] || GPU_VRAM="$(sp_field "$GPU_DATA" 'Total Number of Cores')"
GPU_METAL="$(sp_field "$GPU_DATA" 'Metal Support')"
[ -n "$GPU_METAL" ] || GPU_METAL="$(sp_field "$GPU_DATA" 'Metal')"
GPU_RESOLUTION="$(sp_field "$GPU_DATA" 'Resolution')"
GPU_DISPLAY_TYPE="$(sp_field "$GPU_DATA" 'Display Type')"

# ---------- Collecting: security and backups ----------
note "Collecting: security and network"
FILEVAULT_RAW="$(read_security fdesetup status || true)"
case "$FILEVAULT_RAW" in
  "FileVault is On.") FILEVAULT_KIND="good"; FILEVAULT_TEXT="Увімкнено" ;;
  "FileVault is Off.") FILEVAULT_KIND="critical"; FILEVAULT_TEXT="Вимкнено" ;;
  *) FILEVAULT_KIND="info"; FILEVAULT_TEXT="" ;;
esac
FW_RAW="$(read_security /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate || true)"
case "$FW_RAW" in
  "Firewall is enabled. (State = 1)"|"Firewall is enabled. (State = 2)") FW_KIND="good"; FW_TEXT="Увімкнено" ;;
  "Firewall is disabled. (State = 0)") FW_KIND="warning"; FW_TEXT="Вимкнено" ;;
  *) FW_KIND="info"; FW_TEXT="" ;;
esac
STEALTH_RAW="$(read_security /usr/libexec/ApplicationFirewall/socketfilterfw --getstealthmode || true)"
case "$STEALTH_RAW" in
  "Firewall stealth mode is on") STEALTH_KIND="good"; STEALTH_TEXT="Увімкнено" ;;
  "Firewall stealth mode is off") STEALTH_KIND="info"; STEALTH_TEXT="Вимкнено" ;;
  *) STEALTH_KIND="unknown"; STEALTH_TEXT="" ;;
esac
SIP_RAW="$(read_security csrutil status || true)"
case "$SIP_RAW" in
  "System Integrity Protection status: enabled.") SIP_KIND="good"; SIP_TEXT="Увімкнено" ;;
  "System Integrity Protection status: disabled.") SIP_KIND="critical"; SIP_TEXT="Вимкнено" ;;
  *"System Integrity Protection status: unknown (Custom Configuration)."*) SIP_KIND="warning"; SIP_TEXT="Нестандартна конфігурація" ;;
  *) SIP_KIND="info"; SIP_TEXT="" ;;
esac
GK_RAW="$(read_security spctl --status || true)"
case "$GK_RAW" in
  "assessments enabled") GK_KIND="good"; GK_TEXT="Увімкнено" ;;
  "assessments disabled") GK_KIND="critical"; GK_TEXT="Вимкнено" ;;
  *) GK_KIND="info"; GK_TEXT="" ;;
esac
# The inventory is not an antivirus scan. The com.apple prefix does not prove a signature.
EXT_COUNT=""; EXT_THIRD_JSON=(); EXT_KIND="unknown"; EXT_TEXT=""
EXT_RAW="$(read_security systemextensionsctl list || true)"
if printf '%s\n' "$EXT_RAW" | awk '
  NR == 1 && /^[0-9]+ extension(s|[(]s[)])?$/ { expected=$1; header=1; next }
  /^--- / || /^enabled[[:space:]]+active[[:space:]]+teamID/ || /^[[:space:]]*$/ { next }
  { found=0; for (i=1; i<NF; i++) if ($i ~ /^[A-Za-z0-9_-]+$/ && $(i+1) ~ /^[A-Za-z0-9_-]+[.][A-Za-z0-9_.-]+$/) { found=1; break }
    if (!found) bad=1; rows++ }
  END { exit !(header && !bad && rows == expected) }'; then
  EXT_COUNT="${EXT_RAW%% *}"
  while IFS= read -r ext; do
    [ -n "$ext" ] || continue
    EXT_THIRD_JSON+=("$(jstr "$ext")")
  done < <(printf '%s\n' "$EXT_RAW" | awk '{ for(i=1; i<NF; i++) if ($i ~ /^[A-Za-z0-9_-]+$/ && $(i+1) ~ /^[A-Za-z0-9_-]+[.][A-Za-z0-9_.-]+$/) { if ($(i+1) !~ /^com[.]apple[.]/) print; break } }')
  EXT_KIND="good"; EXT_TEXT="Записів: $EXT_COUNT; без префікса com.apple: ${#EXT_THIRD_JSON[@]}"
  [ "${#EXT_THIRD_JSON[@]}" -eq 0 ] || EXT_KIND="info"   # third-party entries are facts to review, not warnings
fi
XPROTECT_PLIST="/Library/Apple/System/Library/CoreServices/XProtect.bundle/Contents/Info.plist"
XPROTECT_VERSION=""; XPROTECT_KIND="unknown"
if [ -r "$XPROTECT_PLIST" ]; then
  XPROTECT_VERSION="$(read_security /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$XPROTECT_PLIST" || true)"
  [[ "$XPROTECT_VERSION" =~ ^[0-9]+([.][0-9]+)*$ ]] && XPROTECT_KIND="good"
fi
PROFILES_INCLUDED=true; PROFILES_KIND="unknown"; PROFILES_TEXT=""
PROFILES_RAW="$(profiles status -type enrollment 2>&1)"; PROFILES_RC=$?
# We do not include the check if this macOS version directly requires privileges.
if printf '%s\n' "$PROFILES_RAW" | grep -qiE 'requires? root|must be (run as )?root|root privileges|must be run as (an? )?admin'; then
  PROFILES_INCLUDED=false
elif [ "$PROFILES_RC" -eq 0 ] && printf '%s\n' "$PROFILES_RAW" | awk '
  /^Enrolled via DEP: (Yes|No)$/ { dep++; next }
  /^MDM enrollment: (Yes( [(]User Approved[)])?|No)$/ { mdm++; next }
  { bad=1 } END { exit !(dep==1 && mdm==1 && !bad) }'; then
  PROFILES_KIND="good"; PROFILES_TEXT="DEP: ні; MDM: ні"
  if printf '%s\n' "$PROFILES_RAW" | grep -q ': Yes'; then
    PROFILES_KIND="warning"; PROFILES_TEXT="$PROFILES_RAW"
  fi
fi
SPOT_RAW="$(mdutil -s / 2>/dev/null || true)"
case "$SPOT_RAW" in
  *"Indexing enabled"*) SPOT_KIND="good"; SPOT_TEXT="Увімкнено" ;;
  *"disabled"*) SPOT_KIND="info"; SPOT_TEXT="Вимкнено" ;;
  *) SPOT_KIND="info"; SPOT_TEXT="" ;;
esac
TM_DEST_RAW="$(tmutil destinationinfo 2>&1 || true)"
TM_LATEST="$(tmutil latestbackup 2>/dev/null || true)"
if [[ "$TM_DEST_RAW" == *"No destinations"* ]]; then
  BACKUP_KIND="warning"; BACKUP_TEXT="Не налаштовано"; BACKUP_DEST=0
elif [ -z "$TM_LATEST" ]; then
  BACKUP_KIND="warning"; BACKUP_TEXT="Налаштовано, але копій немає"; BACKUP_DEST=1
else
  BACKUP_KIND="good"; BACKUP_TEXT="Є копія: $(basename "$TM_LATEST")"; BACKUP_DEST=1
fi
UPDATES_CHECKED=false; UPDATES_PENDING=""
if [ "$CHECK_UPDATES" -eq 1 ]; then
  if UPDATE_LIST="$(softwareupdate -l 2>&1)"; then
    UPDATES_PENDING="$(printf '%s\n' "$UPDATE_LIST" | grep -cE '^[[:space:]]*[*] Label:' || true)"
    UPDATES_CHECKED=true
  fi
fi

# One lsof call for all visible TCP listeners. Without root the list may be incomplete.
EXPOSED_FILE="$WORK/exposed.tsv"
LISTEN_FILE="$WORK/listening.tsv"
: > "$EXPOSED_FILE"; : > "$LISTEN_FILE"
LISTEN_KIND="unknown"; LISTEN_TEXT=""; EXPOSED_COUNT=""
LISTEN_RAW="$(read_security lsof -iTCP -sTCP:LISTEN -nP -FpcPn || true)"
if [ -n "$LISTEN_RAW" ] && printf '%s\n' "$LISTEN_RAW" | awk '
  /^p[0-9]+$/ { pid=substr($0,2); proc=""; next }
  /^c/ { proc=substr($0,2); gsub(/\t/, " ", proc); next }
  /^f[0-9]+$/ || /^PTCP$/ { next }
  /^n/ { name=substr($0,2); port=name; sub(/.*:/,"",port); addr=name; sub(/:[^:]*$/,"",addr)
    if (!pid || !proc || port !~ /^[0-9]+$/ || (port+0)<1 || (port+0)>65535 || !addr) { bad=1; next }
    printf "%s\t%s\t%s\t%s\n", proc,pid,port,addr; rows++; next }
  { bad=1 } END { exit (bad || !rows) }
' > "$LISTEN_FILE"; then
  # lsof escapes non-ASCII bytes as \xNN: we restore the real characters in process names
  perl -pe 's/\\x([0-9a-fA-F]{2})/pack("C", hex($1))/ge' "$LISTEN_FILE" > "$LISTEN_FILE.dec" && mv "$LISTEN_FILE.dec" "$LISTEN_FILE"
  sort -u "$LISTEN_FILE" -o "$LISTEN_FILE"
  awk -F'\t' '$4 !~ /^127[.]/ && $4 != "[::1]" && $4 != "localhost" { print $1 "\t" $3 "\t" $4 }' "$LISTEN_FILE" | sort -u > "$EXPOSED_FILE"
  EXPOSED_COUNT="$(wc -l < "$EXPOSED_FILE" | tr -d ' ')"
  LISTEN_KIND="good"; LISTEN_TEXT="Видимих слухачів: $(wc -l < "$LISTEN_FILE" | tr -d ' '); поза localhost: $EXPOSED_COUNT"
  [ "$EXPOSED_COUNT" -eq 0 ] || LISTEN_KIND="info"
else
  : > "$LISTEN_FILE"
fi
EXPOSED_JSON=(); LISTEN_JSON=()
while IFS=$'\t' read -r ep epid eport eaddr; do
  [ -n "$ep" ] || continue
  LISTEN_JSON+=("$(obj "$(kv proc "$ep")" "$(kn pid "$epid")" "$(kn port "$eport")" "$(kv addr "$eaddr")")")
done < "$LISTEN_FILE"
while IFS=$'\t' read -r ep eport eaddr; do
  [ -n "$ep" ] || continue
  EXPOSED_JSON+=("$(obj "$(kv proc "$ep")" "$(kv port "$eport")" "$(kv addr "$eaddr")")")
done < "$EXPOSED_FILE"

# Crash reports (only for REPORT_PERIOD_DAYS) and kernel panics
CRASH_DIR="$HOME/Library/Logs/DiagnosticReports"
CRASH_IPS="$(find "$CRASH_DIR" -maxdepth 1 -type f -name '*.ips' -mtime "-$REPORT_PERIOD_DAYS" 2>/dev/null | wc -l | tr -d ' ')"
CRASH_DIAG="$(find /Library/Logs/DiagnosticReports -maxdepth 1 -type f -name '*.diag' -mtime "-$REPORT_PERIOD_DAYS" 2>/dev/null | wc -l | tr -d ' ')"
PANIC_30="$(find /Library/Logs/DiagnosticReports "$CRASH_DIR" -maxdepth 1 -type f -name '*.panic' -mtime -30 2>/dev/null | wc -l | tr -d ' ')"
CRASH_BY_APP_JSON=()
while read -r ccount capp; do
  [ -n "$capp" ] || continue
  CRASH_BY_APP_JSON+=("$(obj "$(kv app "$capp")" "$(kn count "$ccount")")")
done < <(find "$CRASH_DIR" -maxdepth 1 -type f -name '*.ips' -mtime "-$REPORT_PERIOD_DAYS" 2>/dev/null \
  | while IFS= read -r f; do basename "$f" | sed -E 's/-[0-9][0-9-]*.*$//; s/_.*$//'; done \
  | sort | uniq -c | sort -nr | head -8)
CRASH_RECENT_JSON=()
while IFS='|' read -r cepoch cfile; do
  [ -n "$cfile" ] || continue
  capp="$(basename "$cfile" .ips | sed -E 's/-[0-9][0-9-]*.*$//; s/_.*$//')"
  CRASH_RECENT_JSON+=("$(obj "$(kv app "$capp")" "$(kv date "$(date -r "$cepoch" '+%d.%m.%Y %H:%M')")")")
done < <(find "$CRASH_DIR" -maxdepth 1 -type f -name '*.ips' -mtime "-$REPORT_PERIOD_DAYS" -exec stat -f '%m|%N' {} \; 2>/dev/null | sort -rn | head -12)

# Login items: broken path, a secret in a plist readable by everyone, number of third-party agents
LAUNCH_THIRD=0
LAUNCH_TOTAL=0; LAUNCH_COMPLETE=true; LAUNCH_COUNTS_COMPLETE=true
LAUNCH_ITEMS=(); LAUNCH_DIRS=()
BROKEN_JSON=(); SECRET_JSON=(); SECRET_PATHS=""
for ldir in "$HOME/Library/LaunchAgents" "/Library/LaunchAgents" "/Library/LaunchDaemons"; do
  dir_count=""; dir_kind="unknown"
  if [ -d "$ldir" ] && find "$ldir" -maxdepth 1 \( -type f -o -type l \) -name '*.plist' -print > "$WORK/launch-files" 2> "$WORK/launch.err" && [ ! -s "$WORK/launch.err" ]; then
    dir_count="$(wc -l < "$WORK/launch-files" | tr -d ' ')"; dir_kind="good"
  elif [ ! -e "$ldir" ] && [ -r "$(dirname "$ldir")" ] && [ -x "$(dirname "$ldir")" ]; then
    dir_count=0; dir_kind="good"; : > "$WORK/launch-files"
  else
    LAUNCH_COMPLETE=false; LAUNCH_COUNTS_COMPLETE=false; : > "$WORK/launch-files"
  fi
  LAUNCH_DIRS+=("$(obj "$(kv path "$ldir")" "$(kn count "$dir_count")" "$(kv kind "$dir_kind")")")
  while IFS= read -r plist; do
    [ -n "$plist" ] || continue
    LAUNCH_TOTAL=$((LAUNCH_TOTAL + 1))
    label=""; origin="немає даних"
    if [ -r "$plist" ]; then
      label="$(read_security /usr/libexec/PlistBuddy -c 'Print :Label' "$plist" || true)"
    fi
    # Some plists (for example, Google Keystone) have no Label key: then we take the file name as the label
    if [ -z "$label" ] && [ -r "$plist" ] && plutil -lint "$plist" >/dev/null 2>&1; then
      label="$(basename "$plist" .plist)"
    fi
    if [ -z "$label" ] || [[ "$label" = *$'\n'* ]] || [[ "$label" = *$'\t'* ]]; then
      label=""; LAUNCH_COMPLETE=false
    else
      case "$label" in com.apple.*) origin="префікс com.apple" ;; *) origin="без префікса com.apple"; LAUNCH_THIRD=$((LAUNCH_THIRD + 1)) ;; esac
    fi
    LAUNCH_ITEMS+=("$(obj "$(kv label "$label")" "$(kv path "$plist")" "$(kv origin "$origin")")")
    case "$origin" in "префікс com.apple"|"немає даних") continue ;; esac
    prog="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$plist" 2>/dev/null || /usr/libexec/PlistBuddy -c 'Print :Program' "$plist" 2>/dev/null || true)"
    if [ -n "$prog" ] && [ ! -e "$prog" ]; then
      BROKEN_JSON+=("$(obj "$(kv label "$label")" "$(kv program "$prog")")")
    fi
    if grep -qiE 'TOKEN|SECRET|PASSWORD|API_?KEY' "$plist" 2>/dev/null; then
      perm="$(stat -f '%Lp' "$plist" 2>/dev/null || true)"
      case "$perm" in
        600|400) ;;
        *)
          SECRET_JSON+=("$(obj "$(kv label "$label")" "$(kv perm "$perm")" "$(kv path "$plist")")")
          SECRET_PATHS="${SECRET_PATHS}${plist} (права ${perm}); "
          ;;
      esac
    fi
  done < "$WORK/launch-files"
done
LAUNCH_KIND="unknown"; LAUNCH_TEXT=""
if [ "$LAUNCH_COMPLETE" = true ]; then
  LAUNCH_KIND="good"; LAUNCH_TEXT="Файлів: $LAUNCH_TOTAL; без префікса com.apple: $LAUNCH_THIRD"
  [ "$LAUNCH_THIRD" -eq 0 ] || LAUNCH_KIND="info"
fi
LAUNCH_THIRD_KNOWN="$LAUNCH_THIRD"
[ "$LAUNCH_COMPLETE" = true ] || LAUNCH_THIRD_KNOWN=""
[ "$LAUNCH_COUNTS_COMPLETE" = true ] || LAUNCH_TOTAL=""

# XBrain daemon state: only actual signals from the log and database; token values are not read
XB_ADDR_IN_USE=0; XB_BANNERS=0; XB_DB_ERRORS=0; XB_LOG_KB=""; XB_DB_CHECK="none"
if [ -f "$XBRAIN_LOG" ]; then
  XB_ADDR_IN_USE="$(grep -c 'Address already in use' "$XBRAIN_LOG" 2>/dev/null || true)"
  XB_BANNERS="$(grep -cE 'listening|слухає' "$XBRAIN_LOG" 2>/dev/null || true)"
  XB_DB_ERRORS="$(grep -cE 'disk I/O error|readonly database' "$XBRAIN_LOG" 2>/dev/null || true)"
  XB_LOG_KB="$(du -k "$XBRAIN_LOG" 2>/dev/null | awk '{ print $1 }')"
fi
if [ -f "$XBRAIN_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  if [ "$(sqlite3 -readonly "$XBRAIN_DB" 'PRAGMA quick_check;' 2>/dev/null | head -1)" = "ok" ]; then
    XB_DB_CHECK="ok"
  else
    XB_DB_CHECK="error"
  fi
fi
XB_ADDR_IN_USE="${XB_ADDR_IN_USE:-0}"; XB_BANNERS="${XB_BANNERS:-0}"; XB_DB_ERRORS="${XB_DB_ERRORS:-0}"

# ---------- Cleaning: candidates (display only, nothing is deleted) ----------
note "Collecting: caches and cleaning candidates"
CLEANUP=(); SAFE_TOTAL_MB=0
add_cleanup() { # name path risk explanation command
  [ -n "$2" ] || return 0
  local mb
  mb="$(size_mb "$2")"
  # The path exists, but the size could not be read (for example, no access): we show the item with an unknown size
  if [ -z "$mb" ]; then [ -e "$2" ] || return 0; mb="null"; fi
  CLEANUP+=("$(obj "$(kv label "$1")" "$(kv path "$2")" "$(kn sizeMb "$mb")" "$(kv risk "$3")" "$(kv note "$4")" "$(kv cmd "$5")")")
  if [ "$3" = "safe" ] && is_number "$mb"; then SAFE_TOTAL_MB=$((SAFE_TOTAL_MB + mb)); fi
}
BREW_CACHE=""
if command -v brew >/dev/null 2>&1; then BREW_CACHE="$(HOMEBREW_NO_AUTO_UPDATE=1 brew --cache 2>/dev/null || true)"; fi
add_cleanup "Кошик" "$HOME/.Trash" "review" "Файли в кошику вже видалені для системи, але ще займають місце." "osascript -e 'tell application \"Finder\" to empty the trash'"
add_cleanup "Кеші користувача (усі)" "$HOME/Library/Caches" "review" "Загальний розмір. Він включає pip і Homebrew, тому в суму безпечних кешів не входить. Очищайте по одній папці, коли застосунок закрито." "# видалити одну папку: rm -rf ~/Library/Caches/<назва>"
add_cleanup "Xcode DerivedData" "$HOME/Library/Developer/Xcode/DerivedData" "safe" "Результати збірок. Xcode створить їх знову." "rm -rf ~/Library/Developer/Xcode/DerivedData/*"
add_cleanup "Xcode Archives" "$HOME/Library/Developer/Xcode/Archives" "review" "Архіви для публікації застосунків. Видаляйте лише старі, які вам не потрібні." "open ~/Library/Developer/Xcode/Archives"
add_cleanup "iOS DeviceSupport" "$HOME/Library/Developer/Xcode/iOS DeviceSupport" "review" "Символи для старих версій iOS. Потрібні лише для налагодження на старих пристроях." "open $(qpath "$HOME/Library/Developer/Xcode/iOS DeviceSupport")"
add_cleanup "Симулятори CoreSimulator" "$HOME/Library/Developer/CoreSimulator" "review" "Симулятори iOS. Видаляйте лише недоступні." "xcrun simctl delete unavailable"
add_cleanup "Кеш npm" "$HOME/.npm" "safe" "Кеш пакетів npm. Завантажується заново за потреби." "npm cache clean --force"
add_cleanup "Кеш pip" "$HOME/Library/Caches/pip" "safe" "Кеш пакетів Python." "pip cache purge"
add_cleanup "Кеш Homebrew" "$BREW_CACHE" "safe" "Завантажені архіви формул Homebrew." "brew cleanup -s"
add_cleanup "Образ Docker" "$HOME/Library/Containers/com.docker.docker" "keep" "Віртуальний диск Docker. Видаляйте образи через Docker, а не файл напряму." "docker system df && docker system prune"
add_cleanup "Резервні копії iOS" "$HOME/Library/Application Support/MobileSync/Backup" "review" "Локальні копії iPhone або iPad. Видаляйте лише, якщо вони вам не потрібні." "open $(qpath "$HOME/Library/Application Support/MobileSync/Backup")"
add_cleanup "Логи користувача" "$HOME/Library/Logs" "review" "Журнали застосунків. Корисні для діагностики, тому видаляйте не все підряд." "open ~/Library/Logs"
add_cleanup "XBrain: резервні копії" "$HOME/.xbrain/backups" "review" "Резервні копії XBrain. Не видаляйте без перевірки." "open ~/.xbrain/backups"
add_cleanup "XBrain: кошик" "$HOME/.xbrain/trash" "review" "Тимчасові файли XBrain у кошику." "open ~/.xbrain/trash"
add_cleanup "XBrain: лог daemon" "$XBRAIN_LOG" "review" "Лог росте без обмежень. Обрізайте, коли історія не потрібна." "# обрізати після зупинки daemon, потім запустити знову"
if [ "${LOCAL_SNAPS:-0}" -ge 1 ]; then
  CLEANUP+=("$(obj "$(kv label "Локальні знімки Time Machine")" "$(kv path "APFS snapshots")" "$(kn sizeMb "")" "$(kv risk "review")" "$(kv note "Знімків: ${LOCAL_SNAPS}. macOS звільняє їх сама за потреби.")" "$(kv cmd "# за потреби: tmutil thinlocalsnapshots / 10000000000 4")")")
fi

if [ "$DEEP" -eq 1 ]; then
  note "Deep mode: caches by folder, large files, Homebrew"
  while IFS=$'\t' read -r ckb cpath; do
    [ -n "$cpath" ] || continue
    if [ "$cpath" -ef "$HOME/Library/Caches/pip" ] || { [ -n "$BREW_CACHE" ] && [ "$cpath" -ef "$BREW_CACHE" ]; }; then
      continue
    fi
    add_cleanup "Кеш застосунку" "$cpath" "safe" "Кеш відновлюється сам. Закрийте застосунок перед очищенням." "rm -rf $(qpath "$cpath")"
  done < <(du -s -k "$HOME"/Library/Caches/* 2>/dev/null | sort -nr | head -10)
  while IFS= read -r bigf; do
    [ -n "$bigf" ] || continue
    add_cleanup "Великий файл" "$bigf" "review" "Файл понад 500 МБ. Перевірте, чи він потрібен." "open -R $(qpath "$bigf")"
  done < <(find "$HOME/Downloads" "$HOME/Desktop" "$HOME/Movies" "$HOME/Documents" -maxdepth 4 -type f -size +500M 2>/dev/null | head -12)
  while IFS= read -r inst; do
    [ -n "$inst" ] || continue
    add_cleanup "Інсталятор" "$inst" "review" "Образ або пакет встановлення. Якщо програму вже встановлено, файл можна прибрати." "rm -i $(qpath "$inst")"
  done < <(find "$HOME/Downloads" "$HOME/Desktop" -maxdepth 3 -type f \( -name '*.dmg' -o -name '*.pkg' -o -name '*.iso' \) -size +50M 2>/dev/null | head -12)
  BREW_OUTDATED="null"
  if command -v brew >/dev/null 2>&1; then
    BREW_OUTDATED="$(HOMEBREW_NO_AUTO_UPDATE=1 brew outdated --quiet 2>/dev/null | wc -l | tr -d ' ')"
  fi
else
  BREW_OUTDATED="null"
fi

# ---------- Issues and recommendations ----------
# add_issue <critical|warning|info> <category> <title> <details> <evidence> <why it matters> <check> <action risk> [steps...]
ISSUES=()
N_CRIT=0; N_WARN=0; N_INFO=0; PENALTY=0
# Score model (version 2): a critical problem costs 15 (at most 45 in total), a warning costs 4 (at most 8 per category and
# 20 in total), a recommendation (info) costs nothing. Issues of the XBrain daemon concern that tool, not the health of the Mac,
# so they do not change the score. The score is computed once, after all issues are known.
PEN_LINES=""
add_issue() {
  local sev="$1" cat="$2" title="$3" detail="$4" evidence="$5" why="$6" verify="$7" risk="$8" step fix=""
  shift 8
  for step in "$@"; do fix="${fix:+$fix,}$(jstr "$step")"; done
  ISSUES+=("$(obj "$(kv sev "$sev")" "$(kv cat "$cat")" "$(kv title "$title")" "$(kv detail "$detail")" "$(kv evidence "$evidence")" "$(kv why "$why")" "$(kv verify "$verify")" "$(kv risk "$risk")" "$(kr fix "[$fix]")")")
  case "$sev" in
    critical) N_CRIT=$((N_CRIT + 1)) ;;
    warning)  N_WARN=$((N_WARN + 1)) ;;
    *)        N_INFO=$((N_INFO + 1)) ;;
  esac
  [ "$cat" = "XBrain daemon" ] || PEN_LINES="${PEN_LINES}${sev}|${cat}"$'\n'
}
compute_penalty() {
  printf '%s' "$PEN_LINES" | awk -F'|' '
    $1 == "critical" { c += 15 }
    $1 == "warning"  { w[$2] += 4 }
    END {
      if (c > 45) c = 45
      t = 0; for (k in w) { t += (w[k] > 8 ? 8 : w[k]) }
      if (t > 20) t = 20
      print c + t
    }'
}

# Security
if [ "$FILEVAULT_KIND" = critical ]; then
  add_issue critical "Безпека" "FileVault вимкнено" "Дані на диску не зашифровані на рівні macOS." \
    "fdesetup status: FileVault is Off" \
    "Якщо ноутбук втратять або вкрадуть, дані будуть доступні без пароля." \
    "fdesetup status" "низький" \
    "Увімкніть: Системні налаштування → Конфіденційність і безпека → FileVault." \
    "Збережіть ключ відновлення в менеджері паролів, а не на цьому Mac."
fi
if [ "$SIP_KIND" = critical ]; then
  add_issue critical "Безпека" "System Integrity Protection вимкнено" "Захист системних файлів macOS вимкнено." \
    "csrutil status: disabled" \
    "Без SIP шкідливий код може змінювати системні файли." \
    "csrutil status" "середній" \
    "Перезавантажте в режим відновлення і виконайте: csrutil enable" \
    "Вимкніть SIP лише за конкретною технічною потребою."
fi
if [ "$GK_KIND" = critical ]; then
  add_issue critical "Безпека" "Gatekeeper вимкнено" "Перевірка підпису застосунків вимкнена." \
    "spctl --status: assessments disabled" \
    "Без Gatekeeper можна запустити непідписаний або підроблений застосунок без попередження." \
    "spctl --status" "низький" \
    "Увімкніть: sudo spctl --master-enable" \
    "Не вимикайте Gatekeeper для обходу попереджень. Перевіряйте джерело застосунку."
fi
if [ "$FW_KIND" = warning ]; then
  add_issue info "Безпека" "Брандмауер macOS вимкнено" "Вхідні з'єднання не фільтруються системним брандмауером." \
    "socketfilterfw: disabled" \
    "Служби, що слухають мережу, доступні з локальної мережі без фільтрації." \
    "/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate" "низький" \
    "Увімкніть: Системні налаштування → Мережа → Брандмауер." \
    "За потреби увімкніть «Блокувати всі вхідні з’єднання» в параметрах брандмауера."
fi
if [ "${EXPOSED_COUNT:-0}" -gt 0 ]; then
  EXPOSED_PREVIEW="$(head -8 "$EXPOSED_FILE" | awk -F'\t' '{ printf "%s:%s (%s); ", $1, $2, $3 }')"
  add_issue info "Безпека" "Служби слухають адреси поза localhost" "Видимих користувачу TCP-слухачів поза localhost: ${EXPOSED_COUNT}. Доступність з мережі не перевірена." \
    "$EXPOSED_PREVIEW" \
    "Перегляньте призначення служб і правила брандмауера; прив’язка до адреси сама не доводить загрозу." \
    "lsof -nP -iTCP -sTCP:LISTEN" "низький" \
    "Перевірте список. Непотрібні служби вимкніть або закрийте в їхніх налаштуваннях." \
    "Перевіряйте список після встановлення нових застосунків."
fi
if [ "${#SECRET_JSON[@]}" -gt 0 ]; then
  add_issue warning "Безпека" "Секрет у LaunchAgent доступний іншим користувачам" \
    "Plist з ключовими словами TOKEN, SECRET або PASSWORD має права, ширші за 600." \
    "$SECRET_PATHS" \
    "Будь-який користувач або процес на цьому Mac може прочитати секрет." \
    "stat -f '%Lp' ПЛІСТ (очікується 600)" "низький" \
    "Обмежте доступ: chmod 600 ШЛЯХ_ДО_PLIST" \
    "Перенесіть значення в Keychain або у файл з правами 600, а в plist залиште лише шлях до нього." \
    "Значення секрету в звіті не показується. Ротуйте його, якщо plist читався іншими."
fi
if [ "$BACKUP_KIND" = warning ]; then
  add_issue info "Резервні копії" "Резервні копії не налаштовано або копій немає" "$BACKUP_TEXT" \
    "tmutil destinationinfo: $([ "$BACKUP_DEST" -eq 0 ] && echo 'немає призначення' || echo 'призначення є, копій немає')" \
    "Без копії втрата диска означає втрату даних." \
    "tmutil latestbackup" "низький" \
    "Підключіть зовнішній диск і увімкніть Time Machine." \
    "Додайте другу копію у хмару або на інший диск (правило 3-2-1)."
fi

# Stability
if is_number "$PANIC_30" && [ "$PANIC_30" -ge 1 ]; then
  add_issue critical "Стабільність" "Kernel panic за 30 днів: ${PANIC_30}" "Система аварійно перезавантажувалась через збій ядра." \
    "Файлів .panic: ${PANIC_30}" \
    "Паніки ядра часто вказують на драйвер, периферію або пам'ять." \
    "ls -lt /Library/Logs/DiagnosticReports | head" "низький" \
    "Відкрийте останній .panic і знайдіть рядок panic string або Process name." \
    "Оновіть macOS і драйвери, від'єднайте нову периферію, запустіть Діагностику Apple." \
    "Якщо паніки повторюються, зверніться в сервіс Apple."
fi
if is_number "$CRASH_IPS" && [ "$CRASH_IPS" -ge 20 ]; then
  add_issue warning "Стабільність" "Багато аварійних звітів застосунків за тиждень" "Знайдено ${CRASH_IPS} звітів .ips за ${REPORT_PERIOD_DAYS} днів." \
    "Розподіл за застосунками є в розділі «Збої» звіту." \
    "Часті збої псують роботу й можуть вказувати на несумісну версію." \
    "ls ~/Library/Logs/DiagnosticReports" "низький" \
    "Оновіть застосунки з найбільшою кількістю звітів." \
    "Якщо збій повторюється, перевстановіть застосунок або перевірте його журнал."
elif is_number "$CRASH_IPS" && [ "$CRASH_IPS" -ge 8 ]; then
  add_issue info "Стабільність" "Повторювані аварійні звіти" "Знайдено ${CRASH_IPS} звітів .ips за ${REPORT_PERIOD_DAYS} днів." \
    "Перевірте, чи повторюється назва застосунку." \
    "Одиничні збої є нормою, повторювані — ні." \
    "ls ~/Library/Logs/DiagnosticReports" "низький" \
    "Оновіть застосунок і стежте за наступним звітом."
fi
if [ -n "$UPTIME_DAYS" ] && [ "$UPTIME_DAYS" -ge 30 ]; then
  add_issue info "Стабільність" "Mac давно не перезавантажувався" "Безперервна робота: ${UPTIME_TEXT}." \
    "kern.boottime: ${BOOT_DATE}" \
    "Довга робота накопичує пам'ять, кеші й незастосовані оновлення." \
    "uptime" "низький" \
    "Перезавантажте Mac у зручний момент після збереження роботи." \
    "Перезавантажуйте Mac раз на тиждень або два."
fi
if [ "$MEM_PRESSURE_LEVEL" = critical ] || [ "$MEM_PRESSURE_LEVEL" = warning ]; then
  SEV="warning"; [ "$MEM_PRESSURE_LEVEL" = critical ] && SEV="critical"
  add_issue "$SEV" "Памʼять" "Мало вільної системної памʼяті" "Вільна памʼять: ${MEM_FREE_PCT}%." \
    "memory_pressure: вільно ${MEM_FREE_PCT}%; стиснення ${COMPRESSOR_MB:-?} МБ; swap ${SWAP_MB:-?} МБ" \
    "Коли памʼяті бракує, macOS стискає і виводить дані в swap, і вся система сповільнюється." \
    "memory_pressure" "низький" \
    "Закрийте невикористані застосунки і вкладки браузера." \
    "Перезапустіть найбільші Electron-застосунки (Claude, Codex, Code, ChatGPT)." \
    "Якщо нестача повторюється щодня, розгляньте більше памʼяті для цього Mac."
fi
if is_number "${SWAP_MB:-}" && [ "$SWAP_MB" -ge 4096 ] && [ "$MEM_PRESSURE_LEVEL" != ok ]; then
  add_issue warning "Памʼять" "Великий swap: ${SWAP_MB} МБ" "Система активно використовує диск як памʼять." \
    "vm.swapusage: used ${SWAP_MB} МБ" \
    "Swap на SSD повільніший за памʼять. Велике використання означає, що памʼяті не вистачає." \
    "sysctl vm.swapusage" "низький" \
    "Закрийте важкі застосунки. Перезавантаження очищає swap." \
    "Оцініть, що саме займає памʼять (таблиця «Памʼять» нижче)."
elif is_number "${SWAP_MB:-}" && [ "$SWAP_MB" -ge 2048 ]; then
  add_issue info "Памʼять" "Помітний swap: ${SWAP_MB} МБ" "Система частково використовує swap." \
    "vm.swapusage: used ${SWAP_MB} МБ" \
    "macOS користується swap і за вільної памʼяті. Це рекомендація: варто стежити, лише якщо значення росте з дня в день." \
    "sysctl vm.swapusage" "низький" \
    "Стежте за значенням у наступних звітах."
fi
if is_number "${SWAPOUT_5S:-}" && [ "$SWAPOUT_5S" -ge 200 ]; then
  add_issue warning "Памʼять" "Активний обмін з swap: ${SWAPOUT_5S} сторінок за 5 секунд" "Система зараз витісняє сторінки памʼяті на диск." \
    "vm_stat, дві вибірки з інтервалом 5 с: Swapouts +${SWAPOUT_5S}, Pageouts +${PAGEOUT_5S:-невідомо}" \
    "Активний обмін сповільнює всі застосунки. Одиничний пік не страшний, стійкий обмін означає нестачу памʼяті." \
    "vm_stat 5" "низький" \
    "Закрийте важкі застосунки, зокрема Електрон-вікна і вкладки браузера." \
    "Порівняйте з наступним звітом: якщо обмін повторюється, потрібно менше одночасних задач або більше памʼяті."
elif is_number "${SWAPOUT_5S:-}" && [ "$SWAPOUT_5S" -ge 20 ]; then
  add_issue info "Памʼять" "Помітний обмін з swap: ${SWAPOUT_5S} сторінок за 5 секунд" "Короткочасний обмін." \
    "vm_stat, дві вибірки з інтервалом 5 с" "Разовий обмін нормальний, його варто лише відстежувати." \
    "vm_stat 5" "низький" \
    "Стежте за значенням у наступних звітах."
fi
if is_number "${FINDER_RSS_MB:-}" && [ "$FINDER_RSS_MB" -ge 800 ]; then
  add_issue info "Графіка і Finder" "Finder займає ${FINDER_RSS_MB} МБ памʼяті" \
    "Finder зазвичай тримає 100–300 МБ. Велике значення означає роздуття, часто через FinderSync-розширення." \
    "RSS Finder: ${FINDER_RSS_MB} МБ; FinderSync: ${FINDERSYNC_LIST:-не визначено}" \
    "Роздутий Finder гальмує відкриття папок і діалогів збереження." \
    "ps -o rss,comm -p \$(pgrep -x Finder)" "низький" \
    "Перезапустіть Finder: killall Finder (відкриті вікна відновляться)." \
    "Вимкніть FinderSync-розширення, які не потрібні: Системні налаштування → Загальні → Розширення."
fi
if is_number "${PANEL_RSS_MB:-}" && { [ "$PANEL_RSS_MB" -ge 300 ] || [ "${PANEL_CPU%.*}" -ge 50 ] 2>/dev/null; }; then
  add_issue info "Графіка і Finder" "Панель відкриття/збереження споживає ресурси" \
    "Процес Open and Save Panel Service: ${PANEL_RSS_MB} МБ памʼяті, CPU ${PANEL_CPU}%." \
    "com.apple.appkit.xpc.openAndSavePanelService" \
    "Діалог відкриття файлів сканує папку і метадані хмарних дисків. У папках з тисячами файлів або на Google Drive і iCloud він може довго працювати на повну потужність." \
    "ps -Ao pid,%cpu,rss,comm | grep openAndSavePanel" "низький" \
    "Закрийте відкриті діалоги Відкрити і Зберегти. Якщо він зависає, перезапустіть застосунок, що його відкрив." \
    "Не переходьте в діалозі до папок з тисячами файлів або на хмарні диски."
fi
if is_number "${ELECTRON_MB:-}" && [ "$ELECTRON_MB" -ge 2500 ]; then
  add_issue info "Памʼять" "Електрон-застосунки разом займають ${ELECTRON_MB} МБ" \
    "Claude, Codex, VS Code, ChatGPT та інші застосунки на Chromium (Electron)." \
    "Сума RSS: ${ELECTRON_MB} МБ" \
    "Кожен Electron-застосунок несе власний рушій браузера, тому їх число швидко росте." \
    "Таблиця «Памʼять» нижче" "низький" \
    "Закривайте невикористані вікна і застосунки." \
    "Перезапускайте застосунки, коли вони роздуваються."
fi

# Processor
if is_number "${LOAD_5_PER_CORE:-}"; then
  LOAD_INT="$(integer_part "$LOAD_5_PER_CORE")"
  if [ "$LOAD_INT" -ge 1 ] && [ "$(awk -v x="$LOAD_5_PER_CORE" 'BEGIN { print (x >= 1.5) ? 1 : 0 }')" -eq 1 ]; then
    add_issue warning "Процесор" "Тривале навантаження CPU" "Середнє за 5 хвилин: ${LOAD_5} на ${NCPU} ядер." \
      "load average: ${LOAD_1} / ${LOAD_5} / ${LOAD_15}" \
      "Стійке навантаження вище 1.5 на ядро сповільнює систему і нагріває Mac." \
      "top -l 2 -s 1 -n 10 -o cpu" "низький" \
      "Перегляньте процеси в таблиці CPU нижче і закрийте зайві." \
      "Якщо навантаження повторюється щодня, знайдіть процес, що його створює."
  elif [ "$(awk -v x="$LOAD_5_PER_CORE" 'BEGIN { print (x >= 1.0) ? 1 : 0 }')" -eq 1 ]; then
    add_issue info "Процесор" "Помітне навантаження CPU" "Середнє за 5 хвилин: ${LOAD_5} на ${NCPU} ядер." \
      "load average: ${LOAD_1} / ${LOAD_5} / ${LOAD_15}" \
      "Одиничний пік нормальний, стійке навантаження — ні." \
      "top -l 2 -s 1 -n 10 -o cpu" "низький" \
      "Порівняйте з наступним звітом."
  fi
fi
SPIKE_COUNT=0
while IFS=$'\t' read -r spid set_ sct scpu srss sname; do
  [ -n "$spid" ] || continue
  SPIKE_COUNT=$((SPIKE_COUNT + 1))
  [ "$SPIKE_COUNT" -le 3 ] || break
  if [[ "$sname" =~ $EXPECTED_PROCESS_RE ]]; then
    add_issue info "Процесор" "Очікуваний процес навантажує CPU: ${sname}" "Зараз ${scpu}% (100% — одне ядро)." \
      "pid ${spid}, RSS $(div "$srss" 1024 '%.0f') МБ" \
      "Очікуваний фоновий процес. Перевіряємо лише сплески, а не середнє за життя." \
      "ps -o pid,%cpu,rss,etime,comm -p ${spid}" "низький" \
      "Повторіть звіт за кілька хвилин. Якщо сплеск повторюється, дивіться лог daemon." \
      "Не змінюйте процес, поки не побачите повторення."
  else
    add_issue info "Процесор" "Процес навантажує CPU: ${sname}" "Зараз ${scpu}% (100% — одне ядро)." \
      "pid ${spid}, RSS $(div "$srss" 1024 '%.0f') МБ, категорія $(category_of "$sname")" \
      "Це знімок одного моменту: сплеск може бути короткочасним. Тривалий сплеск сповільнює систему і нагріває Mac." \
      "ps -o pid,%cpu,rss,etime,comm -p ${spid}" "низький" \
      "Перевірте процес в Activity Monitor (CPU, сортування за %CPU)." \
      "Якщо процес не потрібен, закрийте його. Якщо він фоновий, перевірте його автозапуск."
  fi
done < <(printf '%s\n' "$SPIKE_ROWS" | awk -F'\t' 'NF >= 6')

while IFS=$'\t' read -r lpid let lct lcpu lrss lname; do
  [ -n "$lpid" ] || continue
  [[ "$lname" =~ $EXPECTED_PROCESS_RE ]] && continue
  lfrac="$(awk -v c="$lct" -v e="$let" 'BEGIN { if (e > 0) printf "%.0f", c * 100 / e; else print 0 }')"
  add_issue info "Процесор" "Процес весь час щось рахує: ${lname}" \
    "За час життя процес використав близько ${lfrac}% часу одного ядра (щонайменше годину)." \
    "pid ${lpid}, час роботи ${let} с, CPU-час ${lct} с" \
    "Постійне фонове навантаження виснажує батарею і грає роль у нагріванні." \
    "ps -o pid,etime,time,comm -p ${lpid}" "низький" \
    "Перевірте, чи процес потрібен. Вимкніть його автозапуск або видаліть розширення." \
    "Стежте за показником у наступних звітах."
done < <(awk -F'\t' '$2 >= 3600 && $3 / $2 >= 0.25 { print }' "$PS_TABLE" | sort -t$'\t' -k3,3gr | head -3)

# Temperatures and graphs
if is_number "${CPU_TEMP_C:-}"; then
  CPU_TEMP_INT="$(integer_part "$CPU_TEMP_C")"
  if [ "$CPU_TEMP_INT" -ge 95 ]; then
    add_issue critical "Температура" "Температура CPU критична: ${CPU_TEMP_C} °C" "Mac може знижувати швидкість для захисту від перегріву." \
      "CPU: ${CPU_TEMP_C} °C" "Перегрів скорочує строк служби і спричиняє тротлінг." "ioreg -l | grep -i temp" "низький" \
      "Негайно зменшіть навантаження: закрийте важкі процеси." "Перевірте вентиляцію і не тримайте Mac на м'яких поверхнях."
  elif [ "$CPU_TEMP_INT" -ge 80 ]; then
    add_issue warning "Температура" "Температура CPU підвищена: ${CPU_TEMP_C} °C" "Вище звичайного." \
      "CPU: ${CPU_TEMP_C} °C" "Тривалий жар погіршує продуктивність і батарею." "ioreg -l | grep -i temp" "низький" \
      "Знайдіть процеси з великим CPU в таблиці нижче." "Не тримайте Mac на ковдрі чи подушці."
  elif [ "$CPU_TEMP_INT" -ge 70 ]; then
    add_issue info "Температура" "Температура CPU помітно підвищена: ${CPU_TEMP_C} °C" "Під навантаженням це нормально." \
      "CPU: ${CPU_TEMP_C} °C" "Одиничні значення нормальні." "ioreg -l | grep -i temp" "низький" \
      "Стежте за трендом у наступних звітах."
  fi
fi
# macOS thermal pressure through pmset -g therm (no root). Threshold: text without the phrase “No thermal warning level has been recorded”.
THERM_RAW="$(pmset -g therm 2>&1 || true)"
THERM_LEVEL="Немає даних"
if printf '%s\n' "$THERM_RAW" | grep -qiE '^[[:space:]]*(Note: )?No thermal warning level has been recorded[.]?[[:space:]]*$'; then
  THERM_LEVEL="не зафіксовано"
fi
if printf '%s\n' "$THERM_RAW" | grep -qiE '^[[:space:]]*Thermal warning level[[:space:]]*[:=][[:space:]]*[1-9][0-9]*[[:space:]]*$'; then
  THERM_LEVEL="зафіксовано"
  add_issue warning "Температура" "macOS зафіксувала теплове попередження" "pmset -g therm повідомляє про тепловий рівень." \
    "$(printf '%s\n' "$THERM_RAW" | grep -i 'thermal' | head -2 | tr '\n' ' ')" \
    "Система обмежує продуктивність, щоб не перегрітися." \
    "pmset -g therm" "низький" \
    "Дайте Mac охолонути і зменшіть навантаження."
fi
if [ "$THROTTLE_KIND" = critical ]; then
  add_issue critical "Температура" "Термальний тротлінг активний" "$THROTTLE_STATUS" \
    "AccumulatedHeatLevel з IOKit" "Mac знижує швидкість, щоб не перегрітися." "ioreg -l | grep -i heat" "низький" \
    "Зменшіть навантаження і покращіть охолодження."
elif [ "$THROTTLE_KIND" = warning ]; then
  add_issue warning "Температура" "Термальне обмеження виявлено" "$THROTTLE_STATUS" \
    "IOKit: тепло або обмеження заряджання" "Заряджання або продуктивність обмежені через температуру." "ioreg -l | grep -i heat" "низький" \
    "Дайте Mac охолонути і не заряджайте його на спеці."
fi

# Battery
if is_number "${BATT_HEALTH_PCT:-}"; then
  BH="$(integer_part "$BATT_HEALTH_PCT")"
  if [ "$BH" -lt 80 ]; then
    add_issue critical "Батарея" "Ємність батареї суттєво знижена: ${BATT_HEALTH_PCT}%" "Поточна ємність від проєктної." \
      "AppleRawMaxCapacity / DesignCapacity" "Батарея тримає значно менше заряду, ніж задумано." "system_profiler SPPowerDataType" "низький" \
      "Заплануйте сервісну діагностику або заміну батареї."
  elif [ "$BH" -lt 85 ]; then
    add_issue warning "Батарея" "Ємність батареї нижча за рекомендовану: ${BATT_HEALTH_PCT}%" "Поточна ємність від проєктної." \
      "AppleRawMaxCapacity / DesignCapacity" "Автономність гірша за очікувану." "system_profiler SPPowerDataType" "низький" \
      "Стежте за автономністю. Розгляньте сервіс під час наступного огляду."
  elif [ "$BH" -lt 90 ]; then
    add_issue info "Батарея" "Ємність батареї зменшується: ${BATT_HEALTH_PCT}%" "Ще не критично." \
      "AppleRawMaxCapacity / DesignCapacity" "Важливий тренд, а не одиничне значення." "system_profiler SPPowerDataType" "низький" \
      "Порівнюйте з наступними звітами."
  fi
fi
if is_number "${CYCLE_COUNT:-}"; then
  if [ "$CYCLE_COUNT" -ge 1000 ]; then
    add_issue warning "Батарея" "Ліміт циклів батареї: ${CYCLE_COUNT}" "Батарея досягла 1000 циклів." \
      "CycleCount: ${CYCLE_COUNT}" "Після 1000 циклів ємність помітно падає." "system_profiler SPPowerDataType" "низький" \
      "Плануйте заміну, якщо автономність відчутно гірша."
  elif [ "$CYCLE_COUNT" -ge 800 ]; then
    add_issue info "Батарея" "Батарея наближається до 1000 циклів: ${CYCLE_COUNT}" "Поки достатньо спостерігати." \
      "CycleCount: ${CYCLE_COUNT}" "Ресурс батареї поступово спадає." "system_profiler SPPowerDataType" "низький" \
      "Увімкніть оптимізоване заряджання в налаштуваннях батареї."
  fi
fi

# Storage
if is_number "${DISK_PCT:-}"; then
  if [ "$DISK_PCT" -ge 90 ]; then
    add_issue critical "Сховище" "Системний диск майже заповнений: ${DISK_PCT}%" "Вільно близько ${DISK_AVAIL_GB:-?} ГБ." \
      "df: ${DISK_PCT}% зайнято" "Без вільного місця macOS не може оновлюватися, кешувати і робити swap." "df -h /System/Volumes/Data" "низький" \
      "Звільніть щонайменше 15% диска: кошик, кеші, великі файли (див. таблицю очищення)." \
      "Перевірте Налаштування → Основні → Сховище."
  elif [ "$DISK_PCT" -ge 85 ]; then
    add_issue warning "Сховище" "Вільного місця мало: ${DISK_PCT}%" "Вільно близько ${DISK_AVAIL_GB:-?} ГБ." \
      "df: ${DISK_PCT}% зайнято" "Місця стає замало для оновлень і кешів." "df -h /System/Volumes/Data" "низький" \
      "Почніть з безпечних кандидатів у таблиці очищення."
  elif [ "$DISK_PCT" -ge 75 ]; then
    add_issue info "Сховище" "Сховище варто почистити: ${DISK_PCT}%" "Вільно близько ${DISK_AVAIL_GB:-?} ГБ." \
      "df: ${DISK_PCT}% зайнято" "Запас тане непомітно." "df -h /System/Volumes/Data" "низький" \
      "Перегляньте таблицю очищення."
  fi
fi
if [ "${LOCAL_SNAPS:-0}" -ge 1 ] && is_number "${DISK_PCT:-}" && [ "$DISK_PCT" -ge 80 ]; then
  add_issue info "Сховище" "Локальні знімки Time Machine займають місце" "Знімків: ${LOCAL_SNAPS}." \
    "tmutil listlocalsnapshots /" "Знімки тримають видалені файли, поки macOS не звільнить місце." "tmutil listlocalsnapshots /" "середній" \
    "Звільніть місце через tmutil thinlocalsnapshots або дочекайтеся автоматичного очищення."
fi
if [ "$SAFE_TOTAL_MB" -ge 1024 ]; then
  add_issue info "Сховище" "Безпечних кешів на ${SAFE_TOTAL_MB} МБ" "Їх можна видалити без втрати даних." \
    "Сума позицій з позначкою «безпечно» в таблиці очищення" \
    "Кеші відновлюються самі, тож їх видалення звільняє місце без ризику." \
    "Таблиця очищення нижче" "низький" \
    "Очистіть кеші по одному, як у команді в таблиці."
fi

# Login items and updates
if [ "${#BROKEN_JSON[@]}" -gt 0 ]; then
  add_issue warning "Автозапуск" "Битий автозапуск: ${#BROKEN_JSON[@]} агент(и)" "Plist вказує на файл, якого немає." \
    "Агенти з відсутніми програмами, див. розділ «Автозапуск»" \
    "launchd намагатиметься запускати відсутній файл, це шум і зайві помилки." \
    "ls ~/Library/LaunchAgents" "середній" \
    "Переконайтеся, що агент не потрібен, і вимкніть його: launchctl bootout gui/<UID>/<мітка>" \
    "Видаліть відповідний plist з ~/Library/LaunchAgents."
fi
if [ "$LAUNCH_THIRD" -ge 20 ]; then
  add_issue info "Автозапуск" "Багато сторонніх агентів автозапуску: ${LAUNCH_THIRD}" "Кожен агент виконує код у фоні." \
    "Підрахунок plist без префікса com.apple" "Чим більше агентів, тим довше завантаження і більше фонових процесів." \
    "ls ~/Library/LaunchAgents /Library/LaunchAgents /Library/LaunchDaemons" "низький" \
    "Перегляньте список і вимкніть непотрібні агенти."
fi
if [ "$UPDATES_CHECKED" = true ] && is_number "${UPDATES_PENDING:-}" && [ "$UPDATES_PENDING" -ge 1 ]; then
  add_issue info "Оновлення" "Доступні оновлення macOS: ${UPDATES_PENDING}" "Знайдено через softwareupdate -l." \
    "softwareupdate -l" "Оновлення містять виправлення безпеки." "softwareupdate -l" "низький" \
    "Системні налаштування → Загальні → Оновлення ПЗ."
fi
if is_number "${BREW_OUTDATED:-}" && [ "$BREW_OUTDATED" -ge 10 ]; then
  add_issue info "Оновлення" "Застарілі пакети Homebrew: ${BREW_OUTDATED}" "Знайдено через brew outdated." \
    "brew outdated --quiet" "Застарілі пакети накопичують вразливості." "brew outdated" "низький" \
    "brew upgrade і brew cleanup -s."
fi

# XBrain daemon: only actual signals from the log and database
if [ "$XB_ADDR_IN_USE" -ge 5 ]; then
  add_issue warning "XBrain daemon" "Конфлікт порту 7717: ${XB_ADDR_IN_USE} помилок" \
    "У лозі ${XB_ADDR_IN_USE} разів «Address already in use» при ${XB_BANNERS} банерах запуску." \
    "daemon.log: лог без дат, тому видно лише суму за весь час" \
    "Повторні спроби запуску конфліктують за порт, launchd перезапускає процес без паузи." \
    "lsof -nP -iTCP:7717 -sTCP:LISTEN" "низький" \
    "Перевірте, що працює один екземпляр: lsof -nP -iTCP:7717 -sTCP:LISTEN" \
    "У net.xbrain.daemon.plist додайте ThrottleInterval (наприклад 30)." \
    "Додайте часові мітки в лог daemon, щоб бачити, коли саме були збої."
fi
if [ "$XB_DB_ERRORS" -ge 1 ]; then
  add_issue warning "XBrain daemon" "Помилки запису в базу: ${XB_DB_ERRORS}" \
    "У лозі є «disk I/O error» або «readonly database»." \
    "daemon.log, рядки з disk I/O error і readonly database" \
    "Неможливість запису означає, що частина змін індексу може не зберегтися." \
    "sqlite3 -readonly \"$XBRAIN_DB\" 'PRAGMA quick_check;'" "низький" \
    "Перевірте вільне місце і права на файл бази." \
    "Перевірте quick_check (див. verify)."
fi
if [ "$XB_DB_CHECK" = error ]; then
  add_issue critical "XBrain daemon" "База індексу не проходить quick_check" "PRAGMA quick_check повернув не ok." \
    "$XBRAIN_DB" "Пошкоджена база може втратити дані індексу." \
    "sqlite3 -readonly \"$XBRAIN_DB\" 'PRAGMA quick_check;'" "середній" \
    "Зробіть копію файлу бази перед будь-якими змінами." \
    "Не відновлюйте базу без перевірки, спочатку зрозумійте причину."
fi
if is_number "${XB_LOG_KB:-}" && [ "$XB_LOG_KB" -ge 51200 ]; then
  add_issue info "XBrain daemon" "Лог daemon завеликий: ${XB_LOG_KB} КБ" "Лог росте без ротації." \
    "du -k daemon.log" "Великий лог важко читати і він займає місце." "du -k ~/.xbrain/daemon.log" "низький" \
    "Додайте ротацію логу або обріжте його після зупинки daemon."
fi
if is_number "${XB_RSS_MB:-}" && [ "$XB_RSS_MB" -ge 1024 ]; then
  add_issue info "XBrain daemon" "Памʼять daemon: ${XB_RSS_MB} МБ" "Значення в момент звіту." \
    "RSS brain-daemon: ${XB_RSS_MB} МБ" "Одиничні піки бувають, стійке зростання — ні." "ps -o rss,etime -p \$(pgrep -f brain-daemon)" "низький" \
    "Стежте за значенням у наступних звітах. Зростання без спаду означає витік."
fi

if [ "${#ISSUES[@]}" -eq 0 ]; then
  ISSUES=()
fi
PENALTY="$(compute_penalty)"; PENALTY="${PENALTY:-0}"
SCORE=$((100 - PENALTY)); [ "$SCORE" -lt 0 ] && SCORE=0
if [ "$SCORE" -ge 80 ]; then HEALTH_KIND="good"; HEALTH_TEXT="Добрий"; HEALTH_SUMMARY="Добрий загальний стан. Рекомендацій, що не знімають бал: ${N_INFO}."
elif [ "$SCORE" -ge 60 ]; then HEALTH_KIND="info"; HEALTH_TEXT="Є що покращити"; HEALTH_SUMMARY="Є кілька попереджень, але нічого термінового. Рекомендацій, що не знімають бал: ${N_INFO}."
else HEALTH_KIND="warning"; HEALTH_TEXT="Потребує уваги"; HEALTH_SUMMARY="Є кілька попереджень, почніть з розділу «Проблеми»."; fi
if [ "$N_CRIT" -gt 0 ]; then HEALTH_KIND="critical"; HEALTH_TEXT="Потребує дій"; HEALTH_SUMMARY="Є критичні проблеми. Почніть з розділу «Проблеми»."; fi

# ---------- Assembling JSON ----------
note "Building JSON and HTML"
SYSTEM_OBJ="$(obj "$(kv computerName "$COMPUTER_NAME")" "$(kv model "$MODEL_NAME")" "$(kv chip "$CHIP")" \
  "$(kv cores "$CORES")" "$(kv memory "$MEMORY_TOTAL")" "$(kv os "$OS_NAME $OS_VERSION")" "$(kv build "$OS_BUILD")" \
  "$(kv kernel "$KERNEL")" "$(kv uptime "$UPTIME_TEXT")" "$(kv bootDate "$BOOT_DATE")" "$(kn uptimeDays "$UPTIME_DAYS")")"
HEALTH_OBJ="$(obj "$(kn score "$SCORE")" "$(kn penalty "$PENALTY")" "$(kv kind "$HEALTH_KIND")" "$(kv text "$HEALTH_TEXT")" \
  "$(kv summary "$HEALTH_SUMMARY")" "$(kr counts "$(obj "$(kn critical "$N_CRIT")" "$(kn warning "$N_WARN")" "$(kn info "$N_INFO")")")")"
LOAD_OBJ="$(obj "$(kn l1 "$LOAD_1")" "$(kn l5 "$LOAD_5")" "$(kn l15 "$LOAD_15")" "$(kn perCore5 "$LOAD_5_PER_CORE")" \
  "$(kn ncpu "$NCPU")" "$(kn cpuTotal "$CPU_TOTAL")" "$(kn cpuUser "$CPU_USER")" "$(kn cpuSys "$CPU_SYS")" "$(kn processCount "$PROCESS_COUNT")")"
MEMORY_OBJ="$(obj "$(kv usedText "$MEM_USED_TEXT")" "$(kn freePct "$MEM_FREE_PCT")" "$(kn swapMb "$SWAP_MB")" \
  "$(kn compressorMb "$COMPRESSOR_MB")" "$(kv pressure "$MEM_PRESSURE_LEVEL")" "$(kn unusedMb "$UNUSED_MB")" \
  "$(kn swapoutsIn5s "$SWAPOUT_5S")" "$(kn pageoutsIn5s "$PAGEOUT_5S")" "$(kn finderRssMb "$FINDER_RSS_MB")" \
  "$(kn panelRssMb "$PANEL_RSS_MB")" "$(kn electronMb "$ELECTRON_MB")")"
BATTERY_OBJ="$(obj "$(kb present "$BATT_PRESENT")" "$(kn pct "$BATT_PCT")" "$(kv state "$BATT_STATE")" "$(kv remain "$BATT_REMAIN")" \
  "$(kn healthPct "$BATT_HEALTH_PCT")" "$(kv healthSource "$BATT_HEALTH_SRC")" "$(kn cycles "$CYCLE_COUNT")" "$(kn maxCapacity "$MAX_CAP")" "$(kn designCapacity "$DESIGN_CAP")" \
  "$(kn tempC "$BATT_TEMP_C")" "$(kn voltageV "$BATT_VOLT_V")" "$(kv condition "$BATT_CONDITION")")"
STORAGE_OBJ="$(obj "$(kv device "$STORAGE_DEVICE")" "$(kv smart "$SMART_STATUS")" "$(kn usedGb "$DISK_USED_GB")" \
  "$(kn totalGb "$DISK_TOTAL_GB")" "$(kn availGb "$DISK_AVAIL_GB")" "$(kn pct "$DISK_PCT")" "$(kn localSnapshots "$LOCAL_SNAPS")")"
THERMAL_OBJ="$(obj "$(kv pmsetPressure "$THERM_LEVEL")" "$(kn cpuTemp "$CPU_TEMP_C")" "$(kn gpuTemp "$GPU_TEMP_C")" "$(kn ssdTemp "$SSD_TEMP_C")" \
  "$(kn batteryTemp "$BATT_TEMP_C")" "$(kn fans "$FAN_COUNT")" "$(kv fanSpeed "$FAN_SPEED")" "$(kv throttle "$THROTTLE_STATUS")" "$(kv throttleKind "$THROTTLE_KIND")")"
GPU_OBJ="$(obj "$(kv model "$GPU_MODEL")" "$(kv vram "$GPU_VRAM")" "$(kv metal "$GPU_METAL")" "$(kv resolution "$GPU_RESOLUTION")" "$(kv displayType "$GPU_DISPLAY_TYPE")")"
SECURITY_ARR=()
add_security "FileVault" "$FILEVAULT_KIND" "$FILEVAULT_TEXT" "fdesetup status" "Перевірте шифрування диска в налаштуваннях FileVault."
add_security "Брандмауер" "$FW_KIND" "$FW_TEXT" "/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate" "Перегляньте брандмауер і дозволені вхідні з’єднання."
add_security "Режим невидимості" "$STEALTH_KIND" "$STEALTH_TEXT" "/usr/libexec/ApplicationFirewall/socketfilterfw --getstealthmode" "Перегляньте режим невидимості в параметрах брандмауера."
add_security "System Integrity Protection" "$SIP_KIND" "$SIP_TEXT" "csrutil status" "Залишайте SIP увімкненим; перевірте причину нестандартного стану."
add_security "Gatekeeper" "$GK_KIND" "$GK_TEXT" "spctl --status" "Залишайте перевірку завантажених застосунків увімкненою."
add_security "TCP-порти" "$LISTEN_KIND" "$LISTEN_TEXT" "lsof -iTCP -sTCP:LISTEN -nP -FpcPn" "Перегляньте служби поза localhost; список обмежений доступом користувача."
add_security "Системні розширення" "$EXT_KIND" "$EXT_TEXT" "systemextensionsctl list" "Зіставте записи зі встановленими програмами; префікс не підтверджує підпис."
add_security "Автозапуск" "$LAUNCH_KIND" "$LAUNCH_TEXT" "find <LaunchAgents/LaunchDaemons> -maxdepth 1; PlistBuddy -c 'Print :Label' <plist>" "Перегляньте мітки й походження файлів; сторонній запис не означає загрозу."
add_security "Версія XProtect" "$XPROTECT_KIND" "$XPROTECT_VERSION" "PlistBuddy -c 'Print :CFBundleShortVersionString' $XPROTECT_PLIST" "Версія прочитана локально; актуальність бази й відсутність загроз не перевірені."
if [ "$PROFILES_INCLUDED" = true ]; then
  add_security "Керування DEP / MDM" "$PROFILES_KIND" "$PROFILES_TEXT" "profiles status -type enrollment" "Переконайтеся, що керування Mac очікуване; це не список усіх профілів."
fi
SECURITY_OBJ="$(arr "${SECURITY_ARR[@]}")"
NETWORK_OBJ="$(obj "$(kn listeningExposed "$EXPOSED_COUNT")" "$(kv kind "$LISTEN_KIND")" "$(kr listening "$(arr ${LISTEN_JSON[@]+"${LISTEN_JSON[@]}"})")" "$(kr exposed "$(arr ${EXPOSED_JSON[@]+"${EXPOSED_JSON[@]}"})")")"
EXTENSIONS_OBJ="$(obj "$(kn count "$EXT_COUNT")" "$(kv kind "$EXT_KIND")" "$(kr nonApple "$(arr ${EXT_THIRD_JSON[@]+"${EXT_THIRD_JSON[@]}"})")")"
BACKUP_OBJ="$(obj "$(kv status "$BACKUP_TEXT")" "$(kv kind "$BACKUP_KIND")" "$(kv latest "$TM_LATEST")")"
LAUNCH_OBJ="$(obj "$(kn thirdParty "$LAUNCH_THIRD_KNOWN")" "$(kn total "$LAUNCH_TOTAL")" "$(kv kind "$LAUNCH_KIND")" "$(kr directories "$(arr ${LAUNCH_DIRS[@]+"${LAUNCH_DIRS[@]}"})")" "$(kr items "$(arr ${LAUNCH_ITEMS[@]+"${LAUNCH_ITEMS[@]}"})")" "$(kr broken "$(arr ${BROKEN_JSON[@]+"${BROKEN_JSON[@]}"})")" "$(kr secrets "$(arr ${SECRET_JSON[@]+"${SECRET_JSON[@]}"})")")"
CRASH_OBJ="$(obj "$(kn ips7d "$CRASH_IPS")" "$(kn diag7d "$CRASH_DIAG")" "$(kn panics30d "$PANIC_30")" \
  "$(kr byApp "$(arr ${CRASH_BY_APP_JSON[@]+"${CRASH_BY_APP_JSON[@]}"})")" "$(kr recent "$(arr ${CRASH_RECENT_JSON[@]+"${CRASH_RECENT_JSON[@]}"})")")"
DAEMON_OBJ="$(obj "$(kv name "XBrain daemon")" "$(kn rssMb "$XB_RSS_MB")" "$(kn logKb "$XB_LOG_KB")" "$(kn addrInUse "$XB_ADDR_IN_USE")" \
  "$(kn banners "$XB_BANNERS")" "$(kn dbErrors "$XB_DB_ERRORS")" "$(kv dbCheck "$XB_DB_CHECK")" "$(kv dbPath "$XBRAIN_DB")" "$(kb expected true)")"
PROCESSES_OBJ="$(obj "$(kr cpu "$(arr ${PROC_CPU_JSON[@]+"${PROC_CPU_JSON[@]}"})")" "$(kr mem "$(arr ${PROC_MEM_JSON[@]+"${PROC_MEM_JSON[@]}"})")" "$(kr groups "$(arr ${MEM_GROUP_JSON[@]+"${MEM_GROUP_JSON[@]}"})")")"
CLEANUP_OBJ="$(arr ${CLEANUP[@]+"${CLEANUP[@]}"})"
ISSUES_OBJ="$(arr ${ISSUES[@]+"${ISSUES[@]}"})"
UPDATES_OBJ="$(obj "$(kb checked "$UPDATES_CHECKED")" "$(kn pending "$UPDATES_PENDING")")"
META_OBJ="$(obj "$(kv computer "$COMPUTER_NAME")" "$(kv generatedAt "$DATE_TEXT")" "$(kn generatedEpoch "$NOW_EPOCH")" \
  "$(kv mode "$([ "$DEEP" -eq 1 ] && echo deep || echo quick)")" "$(kv file "$REPORT_NAME")" "$(kv tool "local-auditor v2")")"
CORE_FILE="$WORK/core.json"
obj "$(kr meta "$META_OBJ")" "$(kr system "$SYSTEM_OBJ")" "$(kr health "$HEALTH_OBJ")" "$(kr issues "$ISSUES_OBJ")" \
  "$(kr load "$LOAD_OBJ")" "$(kr processes "$PROCESSES_OBJ")" "$(kr memory "$MEMORY_OBJ")" "$(kr battery "$BATTERY_OBJ")" \
  "$(kr storage "$STORAGE_OBJ")" "$(kr thermal "$THERMAL_OBJ")" "$(kr gpu "$GPU_OBJ")" "$(kr security "$SECURITY_OBJ")" \
  "$(kr network "$NETWORK_OBJ")" "$(kr extensions "$EXTENSIONS_OBJ")" "$(kr backup "$BACKUP_OBJ")" "$(kr launch "$LAUNCH_OBJ")" "$(kr crashes "$CRASH_OBJ")" \
  "$(kr daemon "$DAEMON_OBJ")" "$(kr cleanup "$CLEANUP_OBJ")" "$(kr updates "$UPDATES_OBJ")" \
  "$(kr deep "$(obj "$(kb enabled "$([ "$DEEP" -eq 1 ] && echo true || echo false)")" "$(kr brewOutdated "$BREW_OUTDATED")")")" > "$CORE_FILE"

ENTRY_FILE="$WORK/entry.json"
obj "$(kv date "$DATE_TEXT")" "$(kn score "$SCORE")" "$(kv kind "$HEALTH_KIND")" "$(kn critical "$N_CRIT")" \
  "$(kn warning "$N_WARN")" "$(kv computer "$COMPUTER_NAME")" "$(kv file "$REPORT_NAME")" > "$ENTRY_FILE"

cat > "$WORK/assemble.pl" <<'PERL'
use strict;
use warnings;
use JSON::PP;

my ($core_path, $hist_path, $sys_dir, $snap_path, $entry_path, $max_hist, $tpl_path, $out_path) = @ARGV;
my $json = JSON::PP->new->utf8->canonical;

sub slurp {
  my ($file) = @_;
  open my $fh, '<:raw', $file or die "Could not open $file: $!\n";
  local $/;
  my $text = <$fh>;
  close $fh;
  return $text;
}

my $core  = $json->decode(slurp($core_path));
my $entry = $json->decode(slurp($entry_path));

# History: the new entry is on top, old ones are trimmed.
my $history = [];
if (-s $hist_path) {
  my $old = eval { $json->decode(slurp($hist_path)) };
  $history = $old if ref $old eq 'ARRAY';
}
unshift @$history, $entry;
splice(@$history, $max_hist) if @$history > $max_hist;
open my $hfh, '>:raw', $hist_path or die "Could not write history: $!\n";
print $hfh $json->encode($history);
close $hfh;

# A snapshot of the current system without history (it can be moved to another Mac).
open my $sfh, '>:raw', $snap_path or die "Could not write snapshot: $!\n";
print $sfh $json->encode($core);
close $sfh;

# Other systems from the “systems” folder: each valid JSON snapshot becomes a card.
my @systems;
opendir(my $dh, $sys_dir) or die "No folder $sys_dir: $!\n";
for my $name (sort readdir($dh)) {
  next unless $name =~ /\.json$/i;
  my $data = eval { $json->decode(slurp("$sys_dir/$name")) };
  push @systems, $data if ref $data eq 'HASH' && ref $data->{meta} eq 'HASH';
}
closedir $dh;

$core->{history} = $history;
$core->{systems} = \@systems;

my $data = $json->encode($core);
$data =~ s/</\\u003c/g;   # "<" in JSON appears only inside strings, so < is safe for <script>
my $html = slurp($tpl_path);
$html =~ s/__REPORT_JSON__/$data/ or die "The template has no place for data (__REPORT_JSON__)\n";
open my $ofh, '>:raw', $out_path or die "Could not write the report: $!\n";
print $ofh $html;
close $ofh;
PERL

if ! perl "$WORK/assemble.pl" "$CORE_FILE" "$HISTORY_FILE" "$SYSTEMS_DIR" "$SNAPSHOT_FILE" "$ENTRY_FILE" "$MAX_HISTORY" "$TEMPLATE" "$REPORT_FILE"; then
  echo "Error: could not build the report" >&2
  exit 1
fi

# Central page: one HTML with all snapshots (up to 12) and date switching. Files are not loaded into the browser.
DASHBOARD_FILE="$REPORTS_DIR/dashboard.html"
cat > "$WORK/dashboard.pl" <<'PERL'
use strict;
use warnings;
use JSON::PP;

my ($data_dir, $hist_path, $sys_dir, $tpl_path, $out_path, $max) = @ARGV;
my $json = JSON::PP->new->utf8->canonical;

sub slurp {
  my ($file) = @_;
  open my $fh, '<:raw', $file or return undef;
  local $/;
  my $text = <$fh>;
  close $fh;
  return $text;
}

opendir(my $dh, $data_dir) or die "No folder $data_dir: $!\n";
my @names = grep { /^report-\d{4}-\d{2}-\d{2}\.json$/ } readdir($dh);
closedir $dh;

my @snaps;
for my $name (@names) {
  my $text = slurp("$data_dir/$name");
  next unless defined $text;
  my $d = eval { $json->decode($text) };
  next unless ref $d eq 'HASH' && ref $d->{meta} eq 'HASH';
  (my $id = $name) =~ s/^report-//;
  $id =~ s/\.json$//;
  push @snaps, [$id, $d];
}
@snaps = sort { $b->[0] cmp $a->[0] } @snaps;
splice(@snaps, $max) if @snaps > $max;
die "No snapshot for the central page\n" unless @snaps;

my %by = map { $_->[0] => $_->[1] } @snaps;
my $hist_text = slurp($hist_path);
my $hist = defined $hist_text ? eval { $json->decode($hist_text) } : undef;
$hist = [] unless ref $hist eq 'ARRAY';

my @systems;
opendir(my $sh, $sys_dir) or die "No folder $sys_dir: $!\n";
for my $name (sort readdir($sh)) {
  next unless $name =~ /\.json$/i;
  my $d = eval { $json->decode(slurp("$sys_dir/$name")) };
  push @systems, $d if ref $d eq 'HASH' && ref $d->{meta} eq 'HASH';
}
closedir $sh;

my $bundle = {
  schemaVersion => 1,
  latestId      => $snaps[0][0],
  snapshotIds   => [map { $_->[0] } @snaps],
  snapshotsById => \%by,
  history       => $hist,
  systems       => \@systems,
};
my $data = $json->encode($bundle);
$data =~ s/</\\u003c/g;
my $html = slurp($tpl_path);
$html =~ s/__REPORT_JSON__/$data/ or die "The template has no place for data\n";
my $tmp = "$out_path.tmp.$$";
open my $ofh, '>:raw', $tmp or die "Could not write $tmp: $!\n";
print $ofh $html or die "Write error for $tmp: $!\n";
close $ofh or die "Close error for $tmp: $!\n";
rename $tmp, $out_path or die "Could not replace $out_path: $!\n";
PERL
if ! perl "$WORK/dashboard.pl" "$DATA_DIR" "$HISTORY_FILE" "$SYSTEMS_DIR" "$TEMPLATE" "$DASHBOARD_FILE" "$MAX_SNAPSHOTS" >/dev/null; then
  echo "Error: could not build the central page" >&2
  exit 1
fi

ln -sfn "$REPORT_NAME" "$LATEST_LINK"
find "$REPORTS_DIR" -maxdepth 1 -type f -name 'report-*.html' 2>/dev/null | sort -r | tail -n +$((MAX_REPORTS + 1)) | while IFS= read -r old; do rm -f "$old"; done
find "$DATA_DIR" -maxdepth 1 -type f -name 'report-*.json' 2>/dev/null | sort -r | tail -n +$((MAX_SNAPSHOTS + 1)) | while IFS= read -r old; do rm -f "$old"; done

note "Report created: $REPORT_FILE"
note "Latest report: $LATEST_LINK"
note "Central page: $DASHBOARD_FILE"
note "Score: $SCORE (critical: $N_CRIT, warnings: $N_WARN, info: $N_INFO)"
