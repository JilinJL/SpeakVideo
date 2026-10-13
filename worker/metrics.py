"""Bounded resource snapshot. Missing sensors stay null, never invented."""

import csv
import io
import shutil
import subprocess
from datetime import UTC, datetime


def snapshot(queue_depth, current_job_id, logs):
    result = {
        "timestamp": datetime.now(UTC).isoformat(),
        "queue_depth": queue_depth,
        "current_job_id": current_job_id,
        "cpu_percent": None,
        "memory_used_mb": None,
        "memory_total_mb": None,
        "gpus": [],
        "logs": logs,
    }
    try:
        import psutil

        memory = psutil.virtual_memory()
        result.update(
            cpu_percent=psutil.cpu_percent(interval=0.15),
            memory_used_mb=round(memory.used / 1048576),
            memory_total_mb=round(memory.total / 1048576),
        )
    except ImportError:
        result["cpu_memory_status"] = "psutil not installed"
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            output = subprocess.run(
                [
                    executable,
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            ).stdout
            for row in csv.reader(io.StringIO(output)):
                result["gpus"].append(
                    {
                        "name": row[0].strip(),
                        "utilization_percent": int(row[1]),
                        "memory_used_mb": int(row[2]),
                        "memory_total_mb": int(row[3]),
                        "temperature_c": int(row[4]),
                    }
                )
        except (subprocess.SubprocessError, ValueError, IndexError):
            result["gpu_status"] = "nvidia-smi unavailable"
    else:
        result["gpu_status"] = "nvidia-smi not found"
    return result
