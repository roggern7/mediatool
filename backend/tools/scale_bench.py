"""Bench de escala do Media Tool (offline): roda tests/scale/scale_harness.py, um cenario por processo, e grava JSON.

Exemplos:
  python tools/scale_bench.py --label depois --out scale_depois.json
  python tools/scale_bench.py --sizes 1000 2000 --scenarios clean restart --jobs 1
  python tools/scale_bench.py --backend <copia do backend> --label baseline   (mede outra copia da arvore)

Nao toca em data/secrets nem na porta 8765; os dados de cada execucao ficam em um diretorio temporario.
Passada de memoria (tracemalloc) separada da de tempo: --mem-scenarios (padrao: clean).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_SIZES = (1000, 2000, 5000, 10000)
ALL_SCENARIOS = ("clean", "expired", "restart", "cancel", "unproductive")
# Falhas em escala (tests/scale/scale_faults.py): so rodam quando pedidas em --scenarios.
FAULT_SCENARIOS = ("list_outage", "disk_full", "refresh_refused", "auth_failed", "slow_processor")


def run_child(backend: Path, scenario: str, size: int, tmp: Path, mem: bool, timeout: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"{scenario}{size}{'m' if mem else 't'}-", dir=tmp))
    script = "scale_faults.py" if scenario in FAULT_SCENARIOS else "scale_harness.py"
    cmd = [sys.executable, str(backend / "tests" / "scale" / script), scenario, str(size), str(work)]
    if mem:
        cmd.append("mem")
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=str(backend))
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("@@JSON@@")), None)
        if line is None:
            return {"scenario": scenario, "size": size, "error": "sem JSON", "stderr_tail": proc.stderr[-1500:],
                    "stdout_tail": proc.stdout[-500:]}
        return json.loads(line[len("@@JSON@@"):])
    except subprocess.TimeoutExpired:
        return {"scenario": scenario, "size": size, "error": f"timeout {timeout}s", "wall_s": time.time() - started}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    here = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
    ap.add_argument("--scenarios", nargs="+", default=list(ALL_SCENARIOS), choices=ALL_SCENARIOS + FAULT_SCENARIOS)
    ap.add_argument("--mem-scenarios", nargs="*", default=["clean"], help="cenarios que ganham passada tracemalloc")
    ap.add_argument("--skip-big", nargs="*", default=["cancel", "unproductive"],
                    help="cenarios que nao rodam em 10000 (custo)")
    ap.add_argument("--backend", type=Path, default=here, help="raiz do backend a medir")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", type=Path, default=Path("scale_report.json"))
    ap.add_argument("--jobs", type=int, default=2, help="processos em paralelo")
    ap.add_argument("--timeout", type=int, default=3000)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="scale-bench-"))
    tasks = []
    for scenario in args.scenarios:
        for size in args.sizes:
            if size >= 10000 and scenario in args.skip_big:
                continue
            tasks.append((scenario, size, False))
            if scenario in args.mem_scenarios:
                tasks.append((scenario, size, True))
    tasks.sort(key=lambda t: -t[1])  # maiores primeiro (melhor balanceamento)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda t: (t, run_child(args.backend, t[0], t[1], tmp, t[2], args.timeout)), tasks))
    shutil.rmtree(tmp, ignore_errors=True)

    merged: dict[tuple, dict] = {}
    for (scenario, size, mem), res in results:
        entry = merged.setdefault((scenario, size), {})
        if mem:
            entry["mem_pass"] = {k: res.get(k) for k in ("traced_peak_mb", "traced_peak_api_probe_mb", "rss_peak_before_api_mb", "traced_current_end_mb", "elapsed_s",
                                                         "error")}
            entry["mem_pass"]["progress"] = [
                {k: p.get(k) for k in ("completed", "traced_current_mb", "traced_peak_mb")}
                for p in res.get("progress", [])]
        else:
            entry.update(res)
    report = {"label": args.label, "backend": str(args.backend), "python": sys.version.split()[0],
              "generated_wall_s": round(time.time() - t0, 1), "runs": [merged[k] for k in sorted(merged)]}
    args.out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"relatorio: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
