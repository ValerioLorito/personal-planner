"""
cli.py — command-line interface for Planner.
"""
from __future__ import annotations

import argparse
import json
import sys

from .app import App
from .config import load


def main():
    parser = argparse.ArgumentParser(prog="planner", description="Planner — personal agentic planner")
    sub = parser.add_subparsers(dest="cmd")

    web_p = sub.add_parser("web", help="Start local web app")
    web_p.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")

    status_p = sub.add_parser("status", help="Show system status and LLM budget")
    sub.add_parser("resync-habits", help="Sync habits with Apple Calendar")

    llm_p = sub.add_parser("config-llm", help="Configure LLM provider, model, or API key")
    llm_p.add_argument("--provider", choices=["gemini", "gemini-2.5", "gemini-2.0", "gemini-1.5", "openrouter", "custom"],
                       help="Preset to apply (default: gemini)")
    llm_p.add_argument("--model", help="Model name (e.g. gemini-2.5-flash)")
    llm_p.add_argument("--key", help="API key to save to data/llm_key")
    llm_p.add_argument("--test", action="store_true", help="Test LLM connection immediately")

    sub.add_parser("test-llm", help="Send a test ping to the configured LLM")

    args = parser.parse_args()
    cfg = load()
    app = App(cfg)

    if args.cmd == "web" or not args.cmd:
        from .web import serve
        port = getattr(args, "port", 8765) or 8765
        serve(port=port, app=app)
    elif args.cmd == "status":
        print(f"Calendar auth: {app.cal.status()}")
        print("Calendars:")
        for c in app.cal.calendars():
            print(f"  - {c['name']} ({c['source']})")
        print("Categories:")
        for c in app.categories():
            print(f"  - [{c['id']}] {c['label']} ({'OK' if c['exists'] else 'MISSING'}) -> {c['root']}")
        b = app.llm.budget()
        print(f"LLM: {b['provider']} / {b['model']} — {b['used']}/{b['limit']} used today (ready: {b['ready']}, key: {b['has_key']})")
    elif args.cmd == "resync-habits":
        from .habits_mgr import resync_habits
        res = resync_habits(app)
        print(json.dumps(res, indent=2))
    elif args.cmd == "config-llm":
        import os
        from .config import DATA_DIR, LLM_PRESETS, apply_preset, save
        if args.provider:
            apply_preset(cfg, args.provider)
            print(f"Applied preset: {args.provider}")
        if args.model:
            cfg.setdefault("llm", {})["model"] = args.model
            print(f"Set model: {args.model}")
        if args.key:
            key_path = os.path.join(DATA_DIR, "llm_key")
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(args.key.strip())
            print(f"Saved API key to {key_path}")
        save(cfg)
        app = App(cfg)
        b = app.llm.budget()
        print(f"LLM Configured: {b['provider']} / {b['model']} (limit: {b['limit']}/day, ready: {b['ready']})")
        if args.test:
            print("Testing connection...")
            res = app.llm.test_connection()
            if res.get("ok"):
                print(f"Success! Model responded: {res.get('reply')}")
            else:
                print(f"Connection failed: {res.get('error')}")
    elif args.cmd == "test-llm":
        print(f"Testing LLM ({app.llm.budget()['model']})...")
        res = app.llm.test_connection()
        if res.get("ok"):
            print(f"Success! Response: {res.get('reply')}")
        else:
            print(f"Failed: {res.get('error')}")


if __name__ == "__main__":
    main()
