"""Generate a public-route migration candidate; never reload or overwrite the live site."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER = '# OurNotes published snapshot proxy'
UPSTREAM = 'http://127.0.0.1:8770'
PUBLIC_RESOURCES = (r'^/(?:static/(?:app|data-source)\.[a-f0-9]{64}\.js|static/style\.[a-f0-9]{64}\.css|'
                    r'data/snapshot\.[a-f0-9]{64}\.json|art/(?:member|support)-[0-9]+\.[a-f0-9]{64}\.'
                    r'(?:png|webp|jpg|jpeg)|brand/(?:mygo|mujica|crychic)\.[a-f0-9]{64}\.(?:webp|png))$')


def proxy_body():
    return (f'        proxy_pass {UPSTREAM};\n'
            '        proxy_set_header Host $host;\n'
            '        proxy_set_header Authorization "";\n'
            '        proxy_hide_header Cache-Control;\n')


def render(config):
    """Migrate the known two-server static site without broadening its whitelist."""
    if MARKER in config:
        if config.count(MARKER) != 2:
            raise ValueError('Incomplete publication proxy migration')
        return config
    if len(re.findall(r'\bserver\s*\{', config)) != 2:
        raise ValueError('Expected the existing HTTP and HTTPS server blocks')
    routes = ((r'location = / \{ try_files /index\.html =404; \}', '/'),
              (r'location = /index\.html \{ try_files \$uri =404; \}', '/index.html'),
              (r'location = /data/current\.json \{ try_files \$uri =404; \}', '/data/current.json'))
    for pattern, route in routes:
        replacement = f'location = {route} {{\n{proxy_body()}    }}'
        config, count = re.subn(pattern, lambda _, replacement=replacement: replacement, config)
        if count != 2:
            raise ValueError(f'Expected two unchanged static routes: {route}')
    # Existing immutable assets remain accessible to previously opened pages.
    # New hashes fall through to the publisher, including its retained history.
    pattern = r'(location ~ "([^"\n]+)" \{)\s*try_files \$uri =404;\s*\}'

    def resources(match):
        if match[2] != PUBLIC_RESOURCES:
            raise ValueError('Unexpected public resource whitelist')
        return match[1] + '\n        try_files $uri @ournotes_published;\n    }'

    config, count = re.subn(pattern, resources, config)
    if count != 2:
        raise ValueError('Expected two existing public resource whitelists')
    fallback = ('    ' + MARKER + '\n    location @ournotes_published {\n'
                + proxy_body() + '    }\n    location / { return 404; }')
    config, count = re.subn(r'    location / \{ return 404; \}', lambda _: fallback, config)
    if count != 2:
        raise ValueError('Expected two private-path deny rules')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.input.resolve() or args.output.exists():
        parser.error('Output must be a new candidate file, separate from the live configuration')
    args.output.write_text(render(args.input.read_text('utf-8')), 'utf-8')


if __name__ == '__main__':
    main()
