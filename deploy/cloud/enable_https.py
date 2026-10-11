"""Add HTTPS API to the existing static site and install certificate renewal units."""
from __future__ import annotations

import argparse
import ipaddress
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    address = str(ipaddress.ip_address(args.ip))
    certificate = Path("/etc/letsencrypt/live") / address
    subprocess.run(["openssl", "x509", "-in", str(certificate / "fullchain.pem"),
                    "-checkend", "129600", "-noout"], check=True)
    site = Path("/etc/nginx/sites-available/ournotes-cards")
    original = site.read_text()
    if "listen 443 ssl;" not in original:
        _, separator, body = original.partition("server {")
        if not separator or "server {" in body or "    listen 80;" not in body:
            raise ValueError("Expected one existing OurNotes HTTP server block")
        tls = "\n".join([
            "    listen 443 ssl;",
            f"    ssl_certificate {certificate}/fullchain.pem;",
            f"    ssl_certificate_key {certificate}/privkey.pem;",
            "    ssl_protocols TLSv1.2 TLSv1.3;",
            "    client_max_body_size 2m;",
            "    access_log off;",
        ])
        https = (separator + body).replace("    listen 80;", tls, 1)
        api = """    location = /api/cloud/v1/events {
        proxy_pass http://127.0.0.1:8770;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_read_timeout 75s;
        add_header Cache-Control "no-store" always;
    }
    location ^~ /api/cloud/v1/ {
        proxy_pass http://127.0.0.1:8770;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_read_timeout 45s;
        add_header Cache-Control "no-store" always;
    }
"""
        anchor = "    location = / {"
        if anchor not in https:
            raise ValueError("Existing public root route is missing")
        https = https.replace(anchor, api + anchor, 1)
        backup = Path("/etc/ournotes-cloud/nginx-before-https.conf")
        if not backup.exists():
            backup.write_text(original)
        site.write_text(original + "\n" + https)
        try:
            subprocess.run(["nginx", "-t"], check=True)
        except subprocess.CalledProcessError:
            site.write_text(original)
            raise
    else:
        subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True)

    source = Path(__file__).resolve().parent
    systemd = Path("/etc/systemd/system")
    for name in ("certbot-renew.service", "certbot-renew.timer",
                 "certificate-check.service", "certificate-check.timer"):
        (systemd / ("ournotes-" + name)).write_text(
            (source / name).read_text().replace("PUBLIC_IP", address))
    hooks = Path("/etc/letsencrypt/renewal-hooks/deploy")
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "ournotes-nginx.sh"
    hook.write_text((source / "renewal-hook.sh").read_text())
    hook.chmod(0o755)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "ournotes-certbot-renew.timer",
                    "ournotes-certificate-check.timer"], check=True)
    subprocess.run(["systemctl", "start", "ournotes-certificate-check.service"], check=True)
    print("HTTPS API and certificate renewal enabled; existing public content preserved")


if __name__ == "__main__":
    main()
