#!/usr/bin/env bash

set -Eeuo pipefail

readonly TABLE_FAMILY="inet"
readonly TABLE_NAME="ai_routing_panel_firewall"

IMAGE_REFERENCE="${XRAY_FIREWALL_IMAGE:-ghcr.io/xtls/xray-core:26.5.3}"
INTERVAL="${XRAY_FIREWALL_INTERVAL:-5}"
DRY_RUN=0
WATCH=0

DOCKER_BIN="${DOCKER_BIN:-docker}"
NFT_BIN="${NFT_BIN:-nft}"
SS_BIN="${SS_BIN:-ss}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
XRAY_CONFIG_PATH="${XRAY_FIREWALL_CONFIG:-/root/xray-routing-panel/app/xray/runtime/config.json}"

NFT_CONFIG=""
declare -A TCP_PORTS=()
declare -A UDP_PORTS=()
declare -a CONTAINER_NAMES=()

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

usage() {
  cat <<'EOF'
Synchronize inbound nftables rules with ports used by the Xray Docker image.

Usage:
  sync-ai-routing-panel-firewall.sh [options]

Options:
  --dry-run                 Discover ports and validate/print nftables rules only.
  --watch                   Synchronize, then repeat every --interval seconds.
  --interval SECONDS        Watch interval; default: 5.
  --image IMAGE              Docker image reference; default: ghcr.io/xtls/xray-core:26.5.3.
  -h, --help                Show this help.

Environment:
  XRAY_FIREWALL_IMAGE        Default Docker image reference.
  XRAY_FIREWALL_INTERVAL     Default watch interval.
  DOCKER_BIN                 Docker command path.
  NFT_BIN                    nft command path.
  SS_BIN                     ss command path.
  XRAY_FIREWALL_CONFIG      Unified-entry Xray config path (optional).
  SYSTEMCTL_BIN             systemctl command path.
  PYTHON_BIN                Python command used to read unified-entry tags.

The managed nftables table is replaced atomically. If no matching running
  container or no externally listening/published TCP/UDP port is found, existing rules
are left unchanged.
EOF
}

error() {
  log "ERROR: $*"
  return 1
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --watch)
        WATCH=1
        shift
        ;;
      --interval)
        if (($# < 2)); then
          log "ERROR: --interval requires a positive integer"
          exit 2
        fi
        INTERVAL="$2"
        shift 2
        ;;
      --image)
        if (($# < 2)); then
          log "ERROR: --image requires a value"
          exit 2
        fi
        IMAGE_REFERENCE="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      *)
        log "ERROR: unknown argument: $1"
        usage >&2
        exit 2
        ;;
    esac
  done

  if (($# > 0)); then
    log "ERROR: unexpected positional argument: $1"
    usage >&2
    exit 2
  fi

  if [[ ! "$IMAGE_REFERENCE" =~ ^[[:alnum:]][[:alnum:]_.@/:-]*$ ]]; then
    log "ERROR: --image is not a valid Docker image reference: $IMAGE_REFERENCE"
    exit 2
  fi

  if [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
    log "ERROR: --interval must be a positive integer"
    exit 2
  fi
}

require_dependencies() {
  if ((EUID != 0)); then
    error "run this script as root"
    return 1
  fi

  if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
    error "Docker command not found: $DOCKER_BIN"
    return 1
  fi

  if ! command -v "$NFT_BIN" >/dev/null 2>&1; then
    error "nft command not found: $NFT_BIN"
    return 1
  fi

  if ! command -v sort >/dev/null 2>&1; then
    error "required command not found: sort"
    return 1
  fi

  if ! command -v "$SS_BIN" >/dev/null 2>&1; then
    error "ss command not found: $SS_BIN"
    return 1
  fi
}

is_external_binding() {
  local host_ip="$1"

  case "$host_ip" in
    ""|0.0.0.0|::)
      return 0
      ;;
    127.*|::1|::ffff:127.*|localhost)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

is_external_endpoint() {
  local endpoint="$1"
  local address="${endpoint%:*}"

  address="${address#[}"
  address="${address%]}"
  case "$address" in
    127.*|::1|::ffff:127.*|localhost)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

endpoint_port() {
  local endpoint="$1"
  printf '%s' "${endpoint##*:}"
}

discover_unified_entry_ports() {
  local config_path="$XRAY_CONFIG_PATH"
  local port_output

  # In unified-entry mode Xray listens on Unix sockets. The public listeners
  # belong to HAProxy and the legacy forwarder, so container socket discovery
  # alone cannot populate the firewall allow-list.
  if [[ ! -r "$config_path" ]]; then
    return 0
  fi
  if ! "$SYSTEMCTL_BIN" is-active --quiet xray-entry.service 2>/dev/null; then
    return 0
  fi
  if ! port_output="$("$PYTHON_BIN" - "$config_path" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)

for inbound in config.get("inbounds", []):
    tag = str(inbound.get("tag", ""))
    if not (tag.startswith("unified-") or tag.startswith("panel-")):
        continue
    try:
        port = int(tag.split("-", 1)[1])
    except (IndexError, ValueError):
        continue
    if 1 <= port <= 65535:
        print(f"tcp|{port}")
PY
)"; then
    error "unable to read unified-entry ports from config: $config_path"
    return 1
  fi

  while IFS='|' read -r protocol port; do
    [[ -z "$port" ]] && continue
    add_port "$protocol" "$port"
  done <<< "$port_output"
  CONTAINER_NAMES+=("unified-entry-gateway")
}

add_port() {
  local protocol="$1"
  local host_port="$2"
  local port_number

  if [[ ! "$host_port" =~ ^[0-9]+$ ]]; then
    log "WARN: ignoring invalid Docker host port: $host_port"
    return 0
  fi

  port_number=$((10#$host_port))
  if ((port_number < 1 || port_number > 65535)); then
    log "WARN: ignoring Docker host port outside 1-65535: $host_port"
    return 0
  fi

  case "$protocol" in
    tcp)
      TCP_PORTS["$port_number"]=1
      ;;
    udp)
      UDP_PORTS["$port_number"]=1
      ;;
    *)
      log "WARN: ignoring unsupported published protocol: $protocol"
      ;;
  esac
}

discover_published_ports() {
  local container_id="$1"
  local container_name="$2"
  local inspect_output
  local container_port
  local host_ip
  local host_port
  local protocol
  local inspect_format

  inspect_format='{{range $container_port, $bindings := .NetworkSettings.Ports}}{{range $bindings}}{{printf "%s|%s|%s\n" $container_port .HostIp .HostPort}}{{end}}{{end}}'
  if ! inspect_output="$("$DOCKER_BIN" inspect --format "$inspect_format" "$container_id")"; then
    error "unable to inspect Docker container: $container_name ($container_id)"
    return 1
  fi

  while IFS='|' read -r container_port host_ip host_port; do
    [[ -z "$container_port" ]] && continue
    is_external_binding "$host_ip" || continue

    protocol="${container_port##*/}"
    add_port "$protocol" "$host_port"
  done <<< "$inspect_output"
}

discover_host_network_ports() {
  local container_id="$1"
  local container_name="$2"
  local process_list
  local socket_list
  local pid
  local socket_line
  local protocol
  local local_endpoint
  local host_port
  local matched
  local -a fields=()
  declare -A target_pids=()

  if ! process_list="$("$DOCKER_BIN" top "$container_id" -eo pid,comm)"; then
    error "unable to list processes for host-network Docker container: $container_name"
    return 1
  fi

  while read -r pid _; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    target_pids["$pid"]=1
  done <<< "$process_list"

  if ((${#target_pids[@]} == 0)); then
    error "unable to find processes for host-network Docker container: $container_name"
    return 1
  fi

  if ! socket_list="$("$SS_BIN" -H -ltnupn)"; then
    error "unable to query listening sockets with: $SS_BIN"
    return 1
  fi

  while IFS= read -r socket_line; do
    [[ -z "$socket_line" ]] && continue

    fields=()
    read -r -a fields <<< "$socket_line"
    ((${#fields[@]} >= 6)) || continue

    protocol="${fields[0]}"
    case "$protocol" in
      tcp|udp)
        ;;
      *)
        continue
        ;;
    esac

    matched=0
    for pid in "${!target_pids[@]}"; do
      case "$socket_line" in
        *"pid=${pid},"*)
          matched=1
          break
          ;;
      esac
    done
    ((matched == 1)) || continue

    local_endpoint="${fields[4]}"
    is_external_endpoint "$local_endpoint" || continue
    host_port="$(endpoint_port "$local_endpoint")"
    add_port "$protocol" "$host_port"
  done <<< "$socket_list"
}

discover_ports() {
  local container_list
  local container_id
  local container_name
  local container_image
  local network_mode
  local inspect_summary
  local inspect_format

  TCP_PORTS=()
  UDP_PORTS=()
  CONTAINER_NAMES=()

  inspect_format='{{.Config.Image}}|{{.HostConfig.NetworkMode}}'

  if ! container_list="$("$DOCKER_BIN" ps --filter "ancestor=$IMAGE_REFERENCE" --format '{{.ID}}\t{{.Names}}')"; then
    error "unable to query running Docker containers"
    return 1
  fi

  if [[ -z "$container_list" ]]; then
    error "no running Docker container uses image: $IMAGE_REFERENCE"
    return 1
  fi

  while IFS=$'\t' read -r container_id container_name; do
    [[ -z "$container_id" ]] && continue

    if ! inspect_summary="$("$DOCKER_BIN" inspect --format "$inspect_format" "$container_id")"; then
      error "unable to inspect Docker container: $container_name ($container_id)"
      return 1
    fi

    IFS='|' read -r container_image network_mode <<< "$inspect_summary"
    if [[ "$container_image" != "$IMAGE_REFERENCE" ]]; then
      log "WARN: skipping descendant image $container_image for target $IMAGE_REFERENCE"
      continue
    fi

    CONTAINER_NAMES+=("$container_name")
    case "$network_mode" in
      host)
        discover_host_network_ports "$container_id" "$container_name" || return 1
        ;;
      *)
        discover_published_ports "$container_id" "$container_name" || return 1
        ;;
    esac
  done <<< "$container_list"

  if ((${#CONTAINER_NAMES[@]} == 0)); then
    error "no running Docker container has the exact target image: $IMAGE_REFERENCE"
    return 1
  fi

  discover_unified_entry_ports || return 1

  if ((${#TCP_PORTS[@]} + ${#UDP_PORTS[@]} == 0)); then
    error "matching container(s) have no externally listening or published TCP/UDP port"
    return 1
  fi
}

port_elements() {
  local -n ports_ref="$1"
  local -a sorted_ports=()
  local port
  local joined=""

  while IFS= read -r port; do
    [[ -n "$port" ]] && sorted_ports+=("$port")
  done < <(
    for port in "${!ports_ref[@]}"; do
      printf '%s\n' "$port"
    done | sort -n
  )

  for port in "${sorted_ports[@]}"; do
    if [[ -n "$joined" ]]; then
      joined+=", "
    fi
    joined+="$port"
  done

  printf '%s' "$joined"
}

render_nft_config() {
  local tcp_elements
  local udp_elements

  if [[ -n "$NFT_CONFIG" && -f "$NFT_CONFIG" ]]; then
    rm -f -- "$NFT_CONFIG"
    NFT_CONFIG=""
  fi

  tcp_elements="$(port_elements TCP_PORTS)"
  udp_elements="$(port_elements UDP_PORTS)"
  NFT_CONFIG="$(mktemp /tmp/xray-firewall.XXXXXX)"

  {
    printf 'destroy table %s %s\n\n' "$TABLE_FAMILY" "$TABLE_NAME"
    printf 'table %s %s {\n' "$TABLE_FAMILY" "$TABLE_NAME"
    printf '  set internal_ipv4 {\n'
    printf '    type ipv4_addr\n'
    printf '    flags interval\n'
    printf '    elements = { 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16 }\n'
    printf '  }\n'
    printf '  set internal_ipv6 {\n'
    printf '    type ipv6_addr\n'
    printf '    flags interval\n'
    printf '    elements = { ::1/128, fc00::/7, fe80::/10 }\n'
    printf '  }\n'

    if ((${#TCP_PORTS[@]} > 0)); then
    printf '  set xray_tcp_ports {\n'
      printf '    type inet_service\n'
      printf '    flags interval\n'
      printf '    elements = { %s }\n' "$tcp_elements"
      printf '  }\n'
    fi

    if ((${#UDP_PORTS[@]} > 0)); then
    printf '  set xray_udp_ports {\n'
      printf '    type inet_service\n'
      printf '    flags interval\n'
      printf '    elements = { %s }\n' "$udp_elements"
      printf '  }\n'
    fi

    printf '  chain input {\n'
    printf '    type filter hook input priority -100; policy drop;\n'
    printf '    ct state invalid drop\n'
    printf '    ct state established,related accept\n'
    printf '    iifname "lo" accept\n'
    printf '    ip saddr @internal_ipv4 accept\n'
    printf '    ip6 saddr @internal_ipv6 accept\n'
    if ((${#TCP_PORTS[@]} > 0)); then
      printf '    tcp dport @xray_tcp_ports accept\n'
    fi
    if ((${#UDP_PORTS[@]} > 0)); then
      printf '    udp dport @xray_udp_ports accept\n'
    fi
    printf '  }\n'

    printf '  chain forward {\n'
    printf '    type filter hook forward priority -100; policy drop;\n'
    printf '    ct state invalid drop\n'
    printf '    ct state established,related accept\n'
    printf '    ip saddr @internal_ipv4 accept\n'
    printf '    ip6 saddr @internal_ipv6 accept\n'
    if ((${#TCP_PORTS[@]} > 0)); then
      printf '    ct original protocol tcp ct original proto-dst @xray_tcp_ports accept\n'
    fi
    if ((${#UDP_PORTS[@]} > 0)); then
      printf '    ct original protocol udp ct original proto-dst @xray_udp_ports accept\n'
    fi
    printf '  }\n'
    printf '}\n'
  } > "$NFT_CONFIG"
}

cleanup() {
  if [[ -n "$NFT_CONFIG" && -f "$NFT_CONFIG" ]]; then
    rm -f -- "$NFT_CONFIG"
  fi
}

sync_once() {
  local container_summary
  local tcp_elements
  local udp_elements

  if ! discover_ports; then
    return 1
  fi

  # Allow the subscription HTTPS endpoint through the managed Xray firewall.
  TCP_PORTS["443"]=1
  render_nft_config
  trap cleanup RETURN

  container_summary="${CONTAINER_NAMES[*]}"
  tcp_elements="$(port_elements TCP_PORTS)"
  udp_elements="$(port_elements UDP_PORTS)"
  log "containers: $container_summary"
  log "public TCP ports: ${tcp_elements:-none}; public UDP ports: ${udp_elements:-none}"

  if ! "$NFT_BIN" --check -f "$NFT_CONFIG"; then
    error "generated nftables configuration failed validation"
    return 1
  fi

  if ((DRY_RUN)); then
    printf '%s\n' '--- nftables configuration (dry-run) ---'
    cat "$NFT_CONFIG"
    return 0
  fi

  if ! "$NFT_BIN" -f "$NFT_CONFIG"; then
    error "failed to apply nftables configuration; existing managed rules should remain unchanged"
    return 1
  fi

  log "nftables rules synchronized successfully"
}

main() {
  parse_args "$@"
  require_dependencies
  trap cleanup EXIT

  if ((WATCH == 0)); then
    sync_once
    return
  fi

  trap 'exit 0' INT TERM
  while true; do
    if ! sync_once; then
      log "sync failed; will retry in ${INTERVAL}s without changing the managed rules"
    fi
    sleep "$INTERVAL"
  done
}

main "$@"
