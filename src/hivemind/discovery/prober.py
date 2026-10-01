import subprocess
from typing import List, Dict, Any, Optional


class SystemProber:
    """Probes local or remote machines over SSH to detect accurate hardware, OS, and tool tags."""

    # Shell script executed on remote worker to extract all system info in one fast command (<0.5s)
    PROBE_SCRIPT = r"""
python3 -c "
import os, sys, platform, subprocess, shutil

tags = []

# OS & Architecture
tags.append(f'os-{platform.system().lower()}')
tags.append(f'arch-{platform.machine().lower()}')

# CPU Info
cpu_model = ''
try:
    if os.path.exists('/proc/cpuinfo'):
        with open('/proc/cpuinfo') as f:
            for line in f:
                if 'model name' in line:
                    cpu_model = line.split(':')[1].strip()
                    break
except Exception:
    pass

cores = os.cpu_count() or 1
tags.append(f'{cores}-cores')
if cores >= 16:
    tags.append('high-cpu')
elif cores <= 4:
    tags.append('standard-cpu')

if 'i3' in cpu_model.lower():
    tags.append('intel-i3')
elif 'i5' in cpu_model.lower():
    tags.append('intel-i5')
elif 'i7' in cpu_model.lower():
    tags.append('intel-i7')
elif 'i9' in cpu_model.lower():
    tags.append('intel-i9')
elif 'ryzen' in cpu_model.lower():
    tags.append('amd-ryzen')
elif 'm1' in cpu_model.lower() or 'm2' in cpu_model.lower() or 'm3' in cpu_model.lower():
    tags.append('apple-silicon')

# RAM Check
try:
    if os.path.exists('/proc/meminfo'):
        with open('/proc/meminfo') as f:
            for line in f:
                if 'MemTotal' in line:
                    kb = int(line.split()[1])
                    gb = round(kb / (1024 * 1024))
                    tags.append(f'{gb}gb-ram')
                    break
except Exception:
    pass

# GPU / CUDA Check
if shutil.which('nvidia-smi'):
    try:
        gpu_out = subprocess.check_output(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], stderr=subprocess.DEVNULL).decode().strip()
        if gpu_out:
            tags.append('gpu')
            tags.append('cuda')
    except Exception:
        pass

# Tooling Checks
tools_to_check = [
    ('docker', 'docker'),
    ('podman', 'podman'),
    ('rustc', 'rust'),
    ('cargo', 'cargo'),
    ('node', 'node'),
    ('bun', 'bun'),
    ('claude', 'engine:claude-code'),
    ('agy', 'engine:agy'),
    ('aider', 'engine:aider'),
    ('uv', 'uv')
]

for cmd, tag in tools_to_check:
    if shutil.which(cmd):
        tags.append(tag)

print(','.join(tags))
" 2>/dev/null || (
    # Fallback POSIX bash probe if python3 is missing
    TAGS="os-$(uname -s | tr '[:upper:]' '[:lower:]')"
    command -v docker >/dev/null && TAGS="$TAGS,docker"
    command -v claude >/dev/null && TAGS="$TAGS,engine:claude-code"
    command -v agy >/dev/null && TAGS="$TAGS,engine:agy"
    command -v nvidia-smi >/dev/null && TAGS="$TAGS,gpu,cuda"
    echo "$TAGS"
)
"""

    @classmethod
    def probe_remote(cls, host: str, user: str, port: int = 22, timeout: float = 4.0) -> List[str]:
        """Runs a fast non-interactive SSH command to extract real hardware & tool tags from the worker."""
        try:
            cmd = [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", f"ConnectTimeout={int(timeout)}",
                "-p", str(port),
                f"{user}@{host}",
                cls.PROBE_SCRIPT.strip()
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode == 0 and res.stdout.strip():
                raw_tags = res.stdout.strip().splitlines()[-1]
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
                return tags
        except Exception:
            pass

        # Fallback default if probe fails / SSH key not configured yet
        return ["os-linux", "standard-cpu"]
