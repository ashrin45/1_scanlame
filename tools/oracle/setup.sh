#!/usr/bin/env bash
# Installation du serveur de capture sur une VM Oracle Cloud Always Free (Ubuntu 22.04/24.04, ARM A1).
# A lancer une seule fois, connecte en SSH sur la VM :
#   curl -fsSL https://raw.githubusercontent.com/ashrin45/1_scanlame/main/tools/oracle/setup.sh | sudo bash -s -- NOM.duckdns.org JETON_DUCKDNS JETON_PAGE
#
# Arguments : 1) domaine DuckDNS complet  2) jeton DuckDNS (pour mettre a jour l'IP)  3) jeton d'acces de la page
# Resultat : https://NOM.duckdns.org -> serveur de reception, series dans /data/series,
#            https://NOM.duckdns.org/lames/ -> pyramides de tuiles publiees (dossier /data/lames)
set -euo pipefail
DOMAIN="${1:?domaine duckdns}"; DUCK="${2:?jeton duckdns}"; TOKEN="${3:?jeton page}"

echo "== paquets"
apt-get update -q
apt-get install -y -q python3-venv python3-pip ffmpeg git debian-keyring debian-archive-keyring apt-transport-https curl
# Caddy (HTTPS automatique Let's Encrypt)
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -q && apt-get install -y -q caddy

echo "== DuckDNS : IP mise a jour toutes les 5 minutes"
mkdir -p /opt/duckdns
cat > /opt/duckdns/update.sh <<EOF
#!/bin/sh
curl -s "https://www.duckdns.org/update?domains=${DOMAIN%%.duckdns.org}&token=${DUCK}&ip=" > /opt/duckdns/last.log
EOF
chmod +x /opt/duckdns/update.sh && /opt/duckdns/update.sh
( crontab -l 2>/dev/null | grep -v duckdns; echo "*/5 * * * * /opt/duckdns/update.sh" ) | crontab -

echo "== pare-feu de la VM (le pare-feu Oracle se regle dans la console : ports 80 et 443)"
iptables -I INPUT -p tcp --dport 80 -j ACCEPT
iptables -I INPUT -p tcp --dport 443 -j ACCEPT
command -v netfilter-persistent >/dev/null && netfilter-persistent save || true

echo "== code et environnement Python"
mkdir -p /data/series /data/lames /opt/scanlame
if [ ! -d /opt/scanlame/.git ]; then git clone -q https://github.com/ashrin45/1_scanlame /opt/scanlame; else git -C /opt/scanlame pull -q; fi
python3 -m venv /opt/scanlame/venv
/opt/scanlame/venv/bin/pip install -q --upgrade pip
/opt/scanlame/venv/bin/pip install -q fastapi uvicorn python-multipart opencv-python-headless numpy
echo "SCANLAME_TOKEN=${TOKEN}" > /etc/scanlame.env; chmod 600 /etc/scanlame.env

echo "== service systemd"
cat > /etc/systemd/system/scanlame.service <<'EOF'
[Unit]
Description=Serveur de reception des prises (scanlame)
After=network.target
[Service]
EnvironmentFile=/etc/scanlame.env
ExecStart=/opt/scanlame/venv/bin/python /opt/scanlame/tools/server.py --root /data/series --port 8000 --host 127.0.0.1
Restart=always
User=root
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now scanlame

echo "== Caddy : HTTPS automatique, upload sans limite de taille, /lames en statique"
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
  encode gzip
  request_body { max_size 0 }
  handle_path /lames/* {
    root * /data/lames
    file_server browse
  }
  handle {
    reverse_proxy 127.0.0.1:8000 {
      transport http { read_timeout 10m }
    }
  }
}
EOF
systemctl restart caddy

echo
echo "Termine. Teste : curl https://${DOMAIN}/   (doit repondre {\"ok\":true,...})"
echo "Dans la page : URL du serveur = https://${DOMAIN}, jeton = ${TOKEN}"
echo "Les pyramides deposees dans /data/lames/<nom>/ sont visibles sur https://${DOMAIN}/lames/<nom>/"
