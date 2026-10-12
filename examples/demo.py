"""Self-contained demo: runs the full vertical slice in temp directories.

Touches nothing real: it builds a tiny fixture workspace, runs a file_report
task (with an injected crash + resume to prove recovery), and prints evidence.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nextclaw import agent
from nextclaw.config import Config, ensure_data_dir


def write(ws, rel, content):
    real = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    ws = tempfile.mkdtemp(prefix="nextclaw-demo-ws-")
    data = tempfile.mkdtemp(prefix="nextclaw-demo-data-")
    cfg = Config(data_dir=data, workspace_root=ws)
    ensure_data_dir(cfg)

    write(ws, "src/main.py", "print('hi')\n" * 6)
    write(ws, "src/util.py", "x = 1\n" * 4)

    task_id = agent.create_task(
        "Demo: count Python files and write a verified report",
        "file_report",
        {"root": "src", "pattern": "**/*.py", "output": "reports/demo.md"},
        cfg,
    )
    print(f"task: {task_id}")

    try:
        agent.run_task(task_id, cfg, fail_after_step=0)
    except RuntimeError as exc:
        print(f"simulated crash (expected): {exc}")

    task = agent.run_task(task_id, cfg)  # resume
    print(f"status: {task['status']}")
    print("evidence:")
    print(json.dumps(task["result"], indent=2))

    report = os.path.join(ws, "reports/demo.md")
    with open(report) as f:
        print("--- report ---")
        print(f.read())


if __name__ == "__main__":
    main()
