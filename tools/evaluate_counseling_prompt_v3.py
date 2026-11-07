"""Sequential prompt A/B on synthetic cases through an isolated Responses relay."""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import random
import re
from statistics import mean
import subprocess
import sys
from time import perf_counter
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOLDOUT = ROOT / "evals/prompt-tuning-holdout-20261107.json"
MODEL = "gpt-5-mini-2025-08-07"
BASELINE = "8fdb78e8305db57c726a6f81ae80d819803627f0"
AXES = ("empathy", "context", "autonomy", "grounding", "usefulness")
PROMPT_FILES = ("api/adapters/counseling_openai.py", "api/adapters/counseling_strategy.py")
FILES = (*PROMPT_FILES, "shared/counseling-runtime.json", "shared/data-catalog.json")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if re.search(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{25,}", encoded):
        raise SystemExit("A secret-shaped string was found; the report was not written.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def dataset():
    cases = read_json(HOLDOUT)["cases"]
    ids = {case["id"] for case in cases}
    groups = Counter(case["family"] for case in cases)
    if len(cases) != 50 or len(ids) != 50 or sorted(groups.values()) != [10] * 5:
        raise SystemExit("The frozen holdout must have 50 unique cases in five equal groups.")
    return cases


def plan(cases):
    analysis = sum(len(case["turns"]) for case in cases)
    return {
        "holdout_sha256": sha256(HOLDOUT),
        "cases": len(cases),
        "groups": dict(Counter(case["family"] for case in cases)),
        "model": MODEL,
        "max_calls_per_arm": analysis + len(cases),
        "max_calls_both_arms": 2 * (analysis + len(cases)),
        "concurrency": 1,
        "provider_calls_in_plan": 0,
        "target_mean": 90,
        "target_share_at_least_90": 0.90,
        "target_critical_errors": 0,
    }


def source_revision(root):
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "api", *FILES], cwd=root).returncode != 0:
        raise SystemExit("Commit the evaluation inputs before capturing a result.")
    return revision


def endpoint(value):
    uri = urlsplit(value)
    if (uri.scheme != "http" or uri.hostname not in ("127.0.0.1", "localhost", "::1")
            or uri.username or uri.password or uri.query or uri.fragment
            or uri.path != "/v1/responses"):
        raise SystemExit("Use a configured loopback Responses relay ending in /v1/responses.")
    return value


class ResponsesRelay:
    """Pass synthetic request bodies to the relay. The relay owns provider authentication."""

    def __init__(self, client, url, call_limit):
        self.client = client
        self.url = url
        self.call_limit = call_limit
        self.attempts = 0
        self.receipts = []

    def post(self, url, *, json, headers, timeout):
        if url != "https://api.openai.com/v1/responses" or self.attempts >= self.call_limit:
            raise SystemExit("Evaluation route or call limit violated.")
        self.attempts += 1
        started = perf_counter()
        response = self.client.post(self.url, json=json, timeout=timeout)
        if response.is_success:
            body = response.json()
            if body.get("model") != MODEL or not str(body.get("id", "")).startswith("resp_"):
                raise SystemExit("The relay did not return the requested model and a Responses receipt.")
            self.receipts.append({
                "id": body["id"], "model": body["model"], "usage": body.get("usage", {}),
                "milliseconds": round((perf_counter() - started) * 1000, 2),
            })
        return response


def capture(args, cases):
    if not args.allow_paid:
        raise SystemExit("The live capture requires --allow-paid and a real isolated provider relay.")
    url = endpoint(args.relay)
    root = args.source.resolve()
    revision = source_revision(root)
    if (args.arm == "baseline") != (revision == BASELINE):
        raise SystemExit("The baseline arm must use the frozen TDD revision.")
    if args.out.exists():
        raise SystemExit("Capture output already exists.")

    import httpx
    sys.path.insert(0, str(root))
    from api.adapters.counseling_openai import OpenAICounselingModel
    from api.adapters.counseling_strategy import PROMPT_VERSION
    from api.application.counseling import bounded_history, public_definition
    from api.domain.counseling import (
        Analysis, CounselingError, draw_cards, grounded_result, personal_definitions, retrieve_definitions,
    )

    config = read_json(root / "shared/counseling-runtime.json")
    catalog = read_json(root / "shared/data-catalog.json")["cards"]
    rows = []
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        relay = ResponsesRelay(client, url, plan(cases)["max_calls_per_arm"])
        model = OpenAICounselingModel(
            relay, MODEL, lambda: "relay", policy=config, reasoning_effort=config["reasoning_effort"],
        )
        for case in cases:
            selected = draw_cards(catalog, 3, random.Random(case["id"]).sample)
            row = {"case_id": case["id"], "draw_ids": [c["card_id"] for c in selected], "turns": [], "result": None}
            history = []
            definitions = {d["term"]: {**d, "version": 1} for d in case.get("dictionary", [])}
            try:
                for user_text in case["turns"]:
                    relevant = retrieve_definitions(user_text, definitions.values())
                    analysis = Analysis.model_validate(model.analyze({
                        "text": user_text, "history": bounded_history(history),
                        "dictionary": [public_definition(d) for d in relevant],
                    }))
                    row["turns"].append(analysis.model_dump())
                    if analysis.needs_immediate_support:
                        row["status"] = "support_before_confirmation"
                        break
                    for proposal in personal_definitions(analysis, user_text):
                        term = proposal["term"]
                        old = definitions.get(term, {})
                        definitions[term] = {
                            **proposal, "id": old.get("id", "turn-" + str(len(history)) + "-" + term),
                            "version": old.get("version", 0) + 1,
                        }
                    history.extend((
                        {"role": "user", "text": user_text},
                        {"role": "assistant", "text": " ".join(filter(None, (
                            analysis.acknowledgement, analysis.understanding, analysis.clarification_question,
                        )))},
                    ))
                else:
                    if analysis.clarification_question:
                        row["status"] = "needs_clarification"
                    else:
                        mode = analysis.response_mode
                        cards = selected if mode == "advice" else []
                        sources = [
                            {"id": "personal:" + d["id"], "kind": "user_definition", "title": d["term"],
                             "url": None, "text": d["definition"]}
                            for d in retrieve_definitions(user_text, definitions.values())
                        ]
                        sources.extend({
                            "id": "tarot:" + str(card["card_id"]), "kind": "tarot_reference",
                            "title": card["card"]["name_ko"], "url": None,
                            "text": card["card"].get("basic_meaning", ""),
                        } for card in cards)
                        row["result"] = grounded_result(model.compose({
                            "understanding": analysis.understanding, "perspective": analysis.perspective,
                            "response_mode": mode, "history": bounded_history(history),
                            "cards": cards, "sources": sources, "search_status": "not_requested",
                        }), cards, sources, mode)
                        row["status"] = "completed"
            except CounselingError as error:
                if error.code in {"AI_UNAVAILABLE", "AI_RATE_LIMITED", "UNKNOWN_PROVIDER_OUTCOME", "AI_PROVIDER_ERROR"}:
                    write_new(args.out, {"status": "stopped", "error": error.code, "rows": rows, "attempts": relay.attempts})
                    raise SystemExit(f"Provider run stopped at {case['id']}: {error.code}") from None
                row["status"] = error.code
            rows.append(row)
        write_new(args.out, {
            **plan(cases), "status": "captured", "arm": args.arm, "source_commit": revision,
            "prompt_version": PROMPT_VERSION, "inputs_sha256": {name: sha256(root / name) for name in FILES},
            "attempts": relay.attempts, "receipts": relay.receipts, "rows": rows,
        })


def blind(args, cases):
    reports = [read_json(args.baseline), read_json(args.candidate)]
    expected_ids = [case["id"] for case in cases]
    for arm, report in zip(("baseline", "candidate"), reports):
        if (report["status"] != "captured" or report["arm"] != arm or report["model"] != MODEL
                or report["holdout_sha256"] != sha256(HOLDOUT)
                or [row["case_id"] for row in report["rows"]] != expected_ids):
            raise SystemExit("Each arm must cover all frozen cases on the same requested model.")
    for name in FILES[2:]:
        if reports[0]["inputs_sha256"][name] != reports[1]["inputs_sha256"][name]:
            raise SystemExit("Runtime settings or tarot catalog differ between the arms.")
    pairs, mapping = [], {}
    for case, before, after in zip(cases, reports[0]["rows"], reports[1]["rows"]):
        if before["draw_ids"] != after["draw_ids"]:
            raise SystemExit("The cards differ between the arms.")
        labels = ["baseline", "candidate"]
        random.SystemRandom().shuffle(labels)
        by_arm = {"baseline": before, "candidate": after}
        mapping[case["id"]] = dict(zip(("X", "Y"), labels))
        pairs.append({"case": case, **{letter: by_arm[arm] for letter, arm in mapping[case["id"]].items()}})
    write_new(args.out, {"holdout_sha256": sha256(HOLDOUT), "axes": AXES, "pairs": pairs})
    write_new(args.mapping, {"review_sha256": sha256(args.out), "arms": mapping})


def score(args, cases):
    review, key = read_json(args.review), read_json(args.mapping)
    if key["review_sha256"] != sha256(args.review) or review["holdout_sha256"] != sha256(HOLDOUT):
        raise SystemExit("Review content changed after blinding.")
    with args.ratings.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    grades = {}
    for row in rows:
        identity = row["case_id"], row["candidate"], row["reviewer"].strip()
        values = [int(row[axis]) for axis in AXES]
        if (identity in grades or not identity[2] or any(not 0 <= value <= 4 for value in values)
                or row["critical_error"] not in ("0", "1")):
            raise SystemExit("Invalid or duplicate review.")
        grades[identity] = sum(values) * 5, row["critical_error"] == "1"
    scores = {"baseline": [], "candidate": []}
    reviewers = {item[2] for item in grades}
    expected = {(case["id"], label, reviewer) for case in cases for label in ("X", "Y") for reviewer in reviewers}
    if len(reviewers) != 2 or set(grades) != expected:
        raise SystemExit("Both reviewers must grade X and Y for all 50 cases.")
    for pair in review["pairs"]:
        case_id = pair["case"]["id"]
        for label in ("X", "Y"):
            judged = [grades[case_id, label, reviewer] for reviewer in reviewers]
            delivered = pair[label]["result"] is not None
            arm = key["arms"][case_id][label]
            scores[arm].append((mean(g[0] for g in judged) if delivered else 0, any(g[1] for g in judged)))
    result = {}
    for arm, values in scores.items():
        avg = mean(value for value, _ in values)
        count_at_least_90 = sum(value >= 90 for value, _ in values)
        critical = sum(flag for _, flag in values)
        result[arm] = {
            "mean": round(avg, 2), "count_at_least_90": count_at_least_90, "total": len(values),
            "critical_errors": critical, "passed": avg >= 90 and count_at_least_90 >= 45 and critical == 0,
        }
    write_new(args.out, {"holdout_sha256": sha256(HOLDOUT), "review_sha256": sha256(args.review), "scores": result})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    run = sub.add_parser("capture")
    run.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--relay", required=True)
    run.add_argument("--allow-paid", action="store_true")
    run.add_argument("--out", type=Path, required=True)
    compare = sub.add_parser("blind")
    for name in ("baseline", "candidate", "out", "mapping"):
        compare.add_argument("--" + name, type=Path, required=True)
    grade = sub.add_parser("score")
    for name in ("review", "mapping", "ratings", "out"):
        grade.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    cases = dataset()
    if args.command == "plan":
        print(json.dumps(plan(cases), ensure_ascii=False, indent=2))
    else:
        {"capture": capture, "blind": blind, "score": score}[args.command](args, cases)


if __name__ == "__main__":
    main()
