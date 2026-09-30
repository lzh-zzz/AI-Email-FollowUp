"""Real model acceptance checks. Uses fictional inputs; never sends emails."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import BailianClient, EmailAgent
from app.config import ROOT, Settings
from app.main import SAMPLES

REPLIES = {
    "高": "We are interested in testing your recyclable packaging for our next launch. Could you share sample options and arrange a call next week?",
    "中": "Could you send more information about the materials and customization options? We need to review internally before deciding.",
    "低": "Thanks for the information. We are not evaluating new packaging suppliers this quarter, but may revisit this later.",
    "拒绝": "We are not interested in your offer. Please do not contact us again.",
    "退订": "Please unsubscribe me and remove this email address from your mailing list.",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replies-only", action="store_true", help="Only verify five reply intents")
    options = parser.parse_args()
    settings = Settings.load()
    client = BailianClient(settings)
    agent = EmailAgent(client)
    report = {"profiles": [], "replies": []}
    try:
        for lead in ([] if options.replies_only else SAMPLES):
            output, usage = agent.run("first", {"lead": lead})
            report["profiles"].append({"company": lead["company"], "output": output, "usage": usage})
            print(
                json.dumps(
                    {"company": lead["company"], "subject": output["subject"], "usage": usage},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        for expected, text in REPLIES.items():
            output, usage = agent.run("reply", {"lead": SAMPLES[0], "reply": text})
            passed = output["intent"] == ("拒绝" if expected == "退订" else expected)
            passed = passed and output["stop"] == (expected in ["拒绝", "退订"])
            report["replies"].append(
                {"expected": expected, "passed": passed, "output": output, "usage": usage}
            )
            print(
                json.dumps(
                    {
                        "expected": expected,
                        "intent": output["intent"],
                        "stop": output["stop"],
                        "passed": passed,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        client.close()
        folder = ROOT / "artifacts"
        folder.mkdir(exist_ok=True)
        (folder / ("real-replies.json" if options.replies_only else "real-ai.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if all(item["passed"] for item in report["replies"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
