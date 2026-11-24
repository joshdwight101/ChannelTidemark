"""Optional foreground HTTPS publication through a configured Tailscale node."""
import json
import re
import shutil
import subprocess
import threading

FUNNEL_PORT = 8443
PUBLIC_URL = re.compile(r'https://[A-Za-z0-9.-]+\.ts\.net:8443(?=[/\s]|$)')


class PublicFunnel:
    def __init__(self, port, callback):
        self.port = port
        self.callback = callback
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.thread = threading.Thread(target=self.run, name='public-funnel', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()

    def run(self):
        executable = shutil.which('tailscale')
        if not executable:
            self.callback('', 'Install Tailscale and enable Funnel on this node, or configure an HTTPS origin.')
            return
        output = ''
        try:
            status = subprocess.run([executable, 'serve', 'status', '--json'], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=8, check=True)
            current = json.loads(status.stdout or '{}') or {}
            if (str(FUNNEL_PORT) in (current.get('TCP') or {}) or
                    any(host.endswith(f':{FUNNEL_PORT}') for host in (current.get('Web') or {}))):
                raise ValueError(f'Tailscale port {FUNNEL_PORT} is already in use. Configure a public HTTPS origin instead.')
            with self.lock:
                if self.stopping.is_set():
                    return
                self.process = subprocess.Popen(
                    [executable, 'funnel', f'--https={FUNNEL_PORT}', f'http://127.0.0.1:{self.port}'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
            with self.process.stdout as stream:
                for line in stream:
                    match = PUBLIC_URL.search(line)
                    if match and not self.stopping.is_set():
                        self.callback(match.group(), 'Public HTTPS sharing is active.')
                    output = line.strip() or output
            self.process.wait()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            output = str(error)
        if not self.stopping.is_set():
            self.callback('', output or 'Public sharing ended. Check Tailscale Funnel on this node.')
