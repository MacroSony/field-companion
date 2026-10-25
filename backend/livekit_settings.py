"""Local LiveKit configuration. Never return secrets in status or errors."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / '.env.livekit.local'


@dataclass(frozen=True)
class LiveKitSettings:
    url: str = ''
    api_key: str = field(default='', repr=False)
    api_secret: str = field(default='', repr=False)

    @classmethod
    def load(cls):
        local = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
        return cls(*(str(os.environ.get(n) or local.get(n) or '').strip()
                     for n in ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET')))

    @property
    def configured(self):
        return bool(self.url and self.api_key and self.api_secret)

    def validate(self, *, allow_local=False):
        if not self.configured:
            raise ValueError('Configure a LiveKit Cloud project on this computer first.')
        parsed = urlsplit(self.url)
        local = allow_local and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.scheme == 'ws'
        cloud = (parsed.scheme == 'wss' and parsed.hostname
                 and parsed.hostname.endswith('.livekit.cloud') and parsed.port in (None, 443))
        if not (local or cloud) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Use the wss:// project URL from LiveKit Cloud.')
        if any('\n' in v or '\r' in v or len(v) > 1024 for v in (self.api_key, self.api_secret)):
            raise ValueError('Invalid LiveKit credentials.')
        if len(self.api_secret) < 32:
            raise ValueError('The LiveKit API secret is incomplete.')

    def bind_inference_environment(self):
        # The SDK's adaptive detector reads these standard variables itself.
        # Configuration changes are refused while calls are active.
        for name, value in zip(('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET'),
                               (self.url, self.api_key, self.api_secret)):
            os.environ[name] = value


def save_local(settings, path=ENV_FILE):
    import json
    settings.validate()
    data = '\n'.join(name + '=' + json.dumps(value) for name, value in zip(
        ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET'),
        (settings.url, settings.api_key, settings.api_secret))) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError('Refusing a linked configuration file')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(data)
    os.replace(temporary, path)
