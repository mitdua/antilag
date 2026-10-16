#!/usr/bin/env bash
# Prepares a Linux VPS (Debian/Ubuntu) to run 'antilag serve'.
#
#   sudo ./setup-vps.sh            # installs, configures NAT and starts the service
#
# Idempotent: can be run again after updating the code.
# With 'sh setup-vps.sh' (dash on Ubuntu) there is no pipefail nor arrays: relaunch with bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

PORT="${ANTILAG_PORT:-51888}"
TUN_NET="${ANTILAG_NET:-10.66.0.0/24}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/antilag
SECRET=/etc/antilag/secret          # only to migrate from the previous version
CLIENTS=/etc/antilag/clients.json

[ "$(id -u)" -eq 0 ] || { echo "correr con sudo" >&2; exit 1; }

WAN_IF="$(ip -4 route show default | awk '{print $5; exit}')"
[ -n "$WAN_IF" ] || { echo "no hay ruta por defecto" >&2; exit 1; }
echo "interfaz de salida: $WAN_IF"

apt-get update -qq
# iptables-persistent without its dialog: without it, a VPS reboot wipes the
# NAT and the port rule.
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv iptables \
    iptables-persistent tcpdump curl >/dev/null

python3 -m venv "$PREFIX"
"$PREFIX/bin/pip" install -q "$SRC_DIR"

install -d -m 700 /etc/antilag
ln -sf "$PREFIX/bin/antilag" /usr/local/bin/antilag   # 'antilag clients ...' by hand

# Players. The first time: if there is a secret from the previous version, it
# becomes player 1's ("principal"), so that panel keeps working; otherwise
# "principal" is created with a new one. The endpoint (what goes inside every
# access code) is taken from the public IP; with a reserved IP, change it with
#   antilag clients endpoint RESERVED-IP:PORT
NEW_CODE=""
if [ ! -s "$CLIENTS" ]; then
    ENDPOINT="${ANTILAG_ENDPOINT:-}"
    if [ -z "$ENDPOINT" ]; then
        PUBLIC_IP="$(curl -4 -s --max-time 10 https://api.ipify.org || true)"
        [ -n "$PUBLIC_IP" ] && ENDPOINT="$PUBLIC_IP:$PORT"
    fi
    INIT=(antilag clients --clients "$CLIENTS" init)
    [ -n "$ENDPOINT" ] && INIT+=(--endpoint "$ENDPOINT")
    if [ -s "$SECRET" ]; then
        "${INIT[@]}" --secret-file "$SECRET"
    else
        "${INIT[@]}"
        antilag clients --clients "$CLIENTS" add principal >/dev/null
        NEW_CODE=1
    fi
    [ -z "$ENDPOINT" ] && echo "aviso: no se detecto la IP publica; fijala con 'antilag clients endpoint IP:$PORT'"
fi

# Forwarding and NAT: whatever leaves the tunnel goes out with the VPS public IP.
cat > /etc/sysctl.d/90-antilag.conf <<SYSCTL
net.ipv4.ip_forward = 1
SYSCTL
sysctl -q --system

rule() { iptables -C "$@" 2>/dev/null || iptables -A "$@"; }
rule_t() { local t="$1"; shift; iptables -t "$t" -C "$@" 2>/dev/null || iptables -t "$t" -A "$@"; }

rule_t nat POSTROUTING -s "$TUN_NET" -o "$WAN_IF" -j MASQUERADE
rule FORWARD -i antilag0 -o "$WAN_IF" -s "$TUN_NET" -j ACCEPT
rule FORWARD -i "$WAN_IF" -o antilag0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
# The TUN has a 1400 MTU: without this, TCP connections negotiate a 1460 MSS
# and large segments are silently lost if some hop filters ICMP.
rule_t mangle FORWARD -o antilag0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
rule_t mangle FORWARD -i antilag0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
rule INPUT -p udp --dport "$PORT" -j ACCEPT

# ufw (active on some images) evaluates its chains before the rules above and
# by default drops everything it did not allow.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT"/udp >/dev/null
    ufw route allow in on antilag0 out on "$WAN_IF" >/dev/null
    echo "ufw activo: abierto udp/$PORT y permitido el reenvio desde antilag0"
fi

if command -v netfilter-persistent >/dev/null; then
    netfilter-persistent save >/dev/null
else
    echo "aviso: instala iptables-persistent para que las reglas sobrevivan un reinicio"
fi

install -m 644 "$SRC_DIR/deploy/antilag.service" /etc/systemd/system/antilag.service
sed -i "s/--listen 0.0.0.0:[0-9]*/--listen 0.0.0.0:$PORT/" /etc/systemd/system/antilag.service
systemctl daemon-reload
systemctl enable --now antilag
systemctl restart antilag
systemctl --no-pager status antilag | head -5
echo
antilag clients --clients "$CLIENTS" list
if [ -n "$NEW_CODE" ]; then
    echo
    echo "codigo de acceso de 'principal' (pegarlo en el panel):"
    antilag clients --clients "$CLIENTS" code principal || true
fi
echo
echo "agregar un jugador:  antilag clients add NOMBRE"
echo
echo "abre udp/$PORT tambien en el firewall del proveedor (security group en AWS)"
