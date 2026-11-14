#!/usr/bin/env python3
"""Apply English defaults to an ARTEX source checkout + emit a DB migration.

Run ON THE RUNNER, after `Checkout ARTEX` and `Checkout deployment assets`, and
BEFORE `go build`. It does three things:

  1. Replaces the 11 built-in Go prompt constants with the full English bodies
     from prompts_en.py, and appends LANGUAGE_POLICY to every system prompt.
  2. Replaces ~100 code-owned Chinese prompt fragments (Go string literals that
     are appended to the system prompt AFTER the DB body) using content hashes
     from strings_en.py. Translating only agent_prompts is NOT enough: those
     fragments are not editable in the UI and would still reach the LLM in
     Chinese.
  3. Rewrites the embedded db/task_templates_seed.json (name/description/goal),
     keeping nkey byte-identical, and writes en_defaults.sql for the already
     persisted PostgreSQL rows (see the workflow's post-health step).

The DB migration is one-shot, gated by settings.artex_i18n_en_v1, so user edits
made after the migration are never clobbered by later self-renewing runs.

Usage:
  python3 apply_en.py --src <artex-checkout> --sql-out <path> [--report-out <path>] [--force]
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys

ASSETS = os.path.dirname(os.path.abspath(__file__))
ZH = re.compile(r'[\u4e00-\u9fff]')

# Prompt bodies that are complete system prompts get the language policy.
POLICY_TARGETS = {
    "goalsDefaultTmpl",
    "plannerDefaultTmpl",
    "mainAgentDefaultTmpl",
    "workerDefaultTmpl",
    "autoDefaultTmpl",
    "pentestDefaultTmpl",
    "redteamDefaultTmpl",
    "DefaultAssistantPrompt",
    "ReporterDefaultPrompt",
}

# English UI labels for the built-in agents seeded by db/db.go. Purely cosmetic,
# they never reach the LLM, but "all built-in templates" should read English.
AGENT_LABELS = {
    "goals": ("Goal Decomposition",
              "Splits a penetration-test objective into independent, verifiable sub-goals."),
    "planner": ("Planner",
                "Reads the situation, judges goals and adds exploration intents only when a genuinely uncovered direction exists (one planning loop per task)."),
    "mainagent": ("Main Agent",
                  "Human interface: observes progress and turns human intent into hints or high-priority intents."),
    "worker": ("Worker",
               "Takes one intent, executes it, writes facts/findings back to the knowledge graph, then stops."),
    "auto": ("Auto",
             "Platform operator assistant: manages tasks and assets with tools, and can create/modify skills, custom tools and MCP servers."),
    "pentest": ("Penetration Testing",
                "Solo pentest agent: reconnaissance to attack surface to exploitation to verification to wrap-up; plans, executes and self-verifies."),
    "redteam": ("Adversarial Exercise",
                "Red-team chain agent: expands assets and maps each one (including JS reverse engineering), then chains verified single points into one reproducible attack chain."),
}

SETTING_KEY = "artex_i18n_en_v1"
SQL_TAG = "$artex_en$"


def load(name):
    path = os.path.join(ASSETS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def digest(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Go source scanning. A small state machine is used instead of a regex so that
# string-looking text inside // and /* */ comments is never treated as a literal.
# ---------------------------------------------------------------------------
def go_literals(src):
    """Yield (start, end, decoded_value) for real Go string literals only."""
    i, n, state = 0, len(src), "normal"
    while i < n:
        c = src[i]
        if state == "normal":
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                state = "linecomment"
                i += 2
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                state = "blockcomment"
                i += 2
                continue
            if c == "`":
                state, start, kind = "raw", i, "raw"
                i += 1
                continue
            if c == '"':
                state, start, kind = "str", i, "str"
                i += 1
                continue
            if c == "'":
                state = "rune"
                i += 1
                continue
            i += 1
            continue
        if state == "linecomment":
            if c == "\n":
                state = "normal"
            i += 1
            continue
        if state == "blockcomment":
            if c == "*" and i + 1 < n and src[i + 1] == "/":
                state = "normal"
                i += 2
                continue
            i += 1
            continue
        if state == "rune":
            if c == "\\":
                i += 2
                continue
            if c == "'":
                state = "normal"
            i += 1
            continue
        # raw / interpreted string
        if state == "raw":
            if c == "`":
                yield start, i + 1, src[start + 1:i]
                state = "normal"
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == '"':
            tok = src[start:i + 1]
            try:
                val = json.loads(tok)
            except Exception:
                val = tok[1:-1]
            yield start, i + 1, val
            state = "normal"
        i += 1


def go_scan_state(src):
    """Return the lexical state at EOF. Anything but 'normal' means a broken
    string/comment was introduced by a replacement (i.e. Go would not compile)."""
    i, n, state = 0, len(src), "normal"
    while i < n:
        c = src[i]
        if state == "normal":
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                state, i = "linecomment", i + 2
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                state, i = "blockcomment", i + 2
                continue
            if c == "`":
                state = "raw"
            elif c == '"':
                state = "str"
            elif c == "'":
                state = "rune"
        elif state == "linecomment":
            if c == "\n":
                state = "normal"
        elif state == "blockcomment":
            if c == "*" and i + 1 < n and src[i + 1] == "/":
                state, i = "normal", i + 2
                continue
        elif state == "rune":
            if c == "\\":
                i += 2
                continue
            if c == "'":
                state = "normal"
        elif state == "raw":
            if c == "`":
                state = "normal"
        elif state == "str":
            if c == "\\":
                i += 2
                continue
            if c == '"':
                state = "normal"
            elif c == "\n":
                return "str"  # unterminated interpreted string
        i += 1
    return state


def go_quote(s):
    """json.dumps output is a valid Go double-quoted literal (\\n, \\", \\uXXXX)."""
    return json.dumps(s, ensure_ascii=False)


def raw_quote(s):
    """Prefer a Go raw string; fall back to an interpreted literal if needed."""
    if "`" not in s and "\r" not in s:
        return "`" + s + "`"
    return go_quote(s)


def const_value_span(src, start):
    """End offset of a Go `const NAME = <expr>` value (handles raw multiline)."""
    i, n, state = start, len(src), "normal"
    while i < n:
        c = src[i]
        if state == "normal":
            if c == "`":
                state = "raw"
            elif c == '"':
                state = "str"
            elif c == "'":
                state = "rune"
            elif c == "/" and i + 1 < n and src[i + 1] == "/":
                return i
            elif c == "/" and i + 1 < n and src[i + 1] == "*":
                state = "block"
                i += 2
                continue
            elif c == "\n":
                return i
        elif state == "raw":
            if c == "`":
                state = "normal"
        elif state == "str":
            if c == "\\":
                i += 2
                continue
            if c == '"':
                state = "normal"
            elif c == "\n":
                return i
        elif state == "rune":
            if c == "\\":
                i += 2
                continue
            if c == "'":
                state = "normal"
        elif state == "block":
            if c == "*" and i + 1 < n and src[i + 1] == "/":
                state = "normal"
                i += 2
                continue
        i += 1
    return n


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Step 1 + 2: Go source
# ---------------------------------------------------------------------------
def go_files(src):
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "web", "dist")]
        for fn in files:
            if fn.endswith(".go"):
                out.append(os.path.join(root, fn))
    return sorted(out)


def patch_go(src, prompts, strings, report):
    table = {}
    for h, en in strings.TRANSLATIONS:
        table.setdefault(h, en)

    files = go_files(src)
    # Only agent/*.go contains LLM-bound code-owned fragments. db/*.go and
    # server/*.go Chinese literals are UI labels / log messages and are left
    # alone (they never become part of an agent <-> LLM message).
    agent_dir = os.path.join(src, "agent")
    agent_files = [p for p in files
                   if os.path.dirname(os.path.abspath(p)) == os.path.abspath(agent_dir)]

    # Hashes of the literals that belong to the 11 big constant declarations:
    # they are replaced wholesale in 2b, so they are not "unmatched fragments".
    const_hashes = set()
    for name in prompts.PROMPTS:
        pat = re.compile(r"(?m)^([ \t]*)const[ \t]+" + re.escape(name) + r"[ \t]*=[ \t]*")
        for path in files:
            text = read(path)
            m = pat.search(text)
            if not m:
                continue
            end = const_value_span(text, m.end())
            for _s, _e, val in go_literals(text[m.end():end]):
                const_hashes.add(digest(val))
            break

    inline_done = 0
    inline_unmatched = []
    # 2a) inline code-owned fragments (hash based -> immune to line moves)
    for path in agent_files:
        text = read(path)
        hits = []
        for start, end, val in go_literals(text):
            if not ZH.search(val):
                continue
            h = digest(val)
            if h in const_hashes:
                continue
            en = table.get(h)
            if en is None:
                inline_unmatched.append(
                    (os.path.relpath(path, src).replace("\\", "/"), val[:60]))
                continue
            hits.append((start, end, go_quote(en)))
        if hits:
            for start, end, rep in reversed(hits):
                text = text[:start] + rep + text[end:]
            write(path, text)
            inline_done += len(hits)

    const_done = {}
    # 2b) whole prompt constants (by identifier -> immune to wording changes)
    for name, body in prompts.PROMPTS.items():
        final = body + (prompts.LANGUAGE_POLICY if name in POLICY_TARGETS else "")
        pat = re.compile(r"(?m)^([ \t]*)const[ \t]+" + re.escape(name) + r"[ \t]*=[ \t]*")
        found = False
        for path in files:
            text = read(path)
            m = pat.search(text)
            if not m:
                continue
            end = const_value_span(text, m.end())
            new = text[:m.end()] + raw_quote(final) + text[end:]
            write(path, new)
            const_done[name] = os.path.relpath(path, src).replace("\\", "/")
            found = True
            break
        if not found:
            report["errors"].append("const %s not found in source tree" % name)

    # 2c) verification: Chinese literals left in the agent package must be ZERO.
    left_agent, left_other, broken = [], [], []
    for path in files:
        text = read(path)
        rel = os.path.relpath(path, src).replace("\\", "/")
        if go_scan_state(text) != "normal":
            broken.append(rel)
        for _start, _end, val in go_literals(text):
            if ZH.search(val):
                (left_agent if path in agent_files else left_other).append((rel, val[:70]))
    if broken:
        report["errors"].append("unterminated Go string/comment after patch: %s" % ", ".join(broken))

    report["inline_replaced"] = inline_done
    report["inline_unmatched"] = inline_unmatched[:40]
    report["inline_unmatched_total"] = len(inline_unmatched)
    report["constants_replaced"] = const_done
    report["chinese_literals_left_agent"] = left_agent[:20]
    report["chinese_literals_left_agent_total"] = len(left_agent)
    report["chinese_literals_left_other_total"] = len(left_other)
    report["go_syntax_sane"] = not broken
    report["go_files_scanned"] = len(files)
    report["agent_files_scanned"] = len(agent_files)


# ---------------------------------------------------------------------------
# Step 3: embedded task-template seed
# ---------------------------------------------------------------------------
def patch_seed(src, templates, report):
    path = None
    for cand in (os.path.join(src, "db", "task_templates_seed.json"),
                 os.path.join(src, "task_templates_seed.json")):
        if os.path.isfile(cand):
            path = cand
            break
    if path is None:
        report["errors"].append("db/task_templates_seed.json not found")
        return
    data = json.loads(read(path))
    by_key = {t["nkey"]: t for t in templates.TASK_TEMPLATES}
    changed = 0
    for item in data.get("templates", []):
        en = by_key.get(item.get("nkey"))
        if not en:
            report["errors"].append("no English template for nkey %r" % item.get("nkey"))
            continue
        # nkey, agent_key, intercept_rules and all other fields stay untouched
        item["name"] = en["name"]
        item["description"] = en["description"]
        item["goal"] = en["goal"]
        changed += 1
    if "comment" in data and ZH.search(data["comment"]):
        data["comment"] = ("Built-in task template seed: idempotently inserted on first "
                           "startup; later user edits/deletions are never overwritten. "
                           "Added templates are backfilled by the v2 gate in db.go. "
                           "nkey is an internal identifier and must stay byte-identical.")
    write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    report["seed_templates"] = changed
    report["seed_path"] = os.path.relpath(path, src).replace("\\", "/")


# ---------------------------------------------------------------------------
# Step 4: SQL for the persisted DB
# ---------------------------------------------------------------------------
def q(text):
    if SQL_TAG in text:
        raise ValueError("SQL dollar-quote tag collides with prompt text")
    return SQL_TAG + text + SQL_TAG


def lit(text):
    return "'" + text.replace("'", "''") + "'"


def build_sql(prompts, templates, strings, force):
    gate = "" if force else (
        "\n   AND NOT EXISTS (SELECT 1 FROM settings s "
        "WHERE s.key = '" + SETTING_KEY + "' AND s.value = 'true')")
    out = [
        "-- Generated by deploy/i18n/en/apply_en.py -- DO NOT EDIT BY HAND.",
        "-- One-shot English-default migration for a persisted ARTEX database.",
        "-- Idempotent: gated by settings." + SETTING_KEY + " (or --force).",
        "BEGIN;",
        "",
        "-- 4.1 Built-in agent prompt bodies. The DB body WINS over the code default",
        "--     (agent.PromptOverride), so an old Chinese row must be rewritten or the",
        "--     source patch alone would have no effect on an existing deployment.",
    ]
    for const, key in prompts.AGENT_KEY_BY_CONST.items():
        body = prompts.PROMPTS[const] + prompts.LANGUAGE_POLICY
        out += [
            "UPDATE agent_prompts p",
            "   SET template_text = " + q(body) + ",",
            "       note = 'English default (i18n)'",
            "  FROM agents a",
            " WHERE p.id = a.current_prompt_id",
            "   AND a.key = " + lit(key) + gate + ";",
            "",
        ]

    out += ["-- 4.2 Built-in agent UI labels (cosmetic; not sent to the LLM)."]
    for key, (name, desc) in AGENT_LABELS.items():
        out += [
            "UPDATE agents SET name = " + lit(name) + ", description = " + lit(desc) + ",",
            "       updated_at = now()",
            " WHERE key = " + lit(key) + " AND builtin" + gate + ";",
            "",
        ]

    out += [
        "-- 4.3 Built-in task templates. nkey is the unique key: upstream uses",
        "--     ON CONFLICT (nkey) DO NOTHING, so it must stay byte-identical even",
        "--     when it contains Chinese. Only the LLM-facing text is translated.",
    ]
    for t in templates.TASK_TEMPLATES:
        out += [
            "UPDATE task_templates",
            "   SET name = " + q(t["name"]) + ",",
            "       description = " + q(t["description"]) + ",",
            "       goal = " + q(t["goal"]) + ",",
            "       updated_at = now()",
            " WHERE nkey = " + q(t["nkey"]) + gate + ";",
            "",
        ]

    out += [
        "-- 4.4 Custom agents are NOT built-in templates: keep their body, but make",
        "--     every reply English. Skipped once the policy marker is present.",
        "UPDATE agent_prompts p",
        "   SET template_text = p.template_text || " + q(prompts.LANGUAGE_POLICY),
        "  FROM agents a",
        " WHERE p.id = a.current_prompt_id",
        "   AND a.builtin = false",
        "   AND position('LANGUAGE POLICY (highest priority' in p.template_text) = 0" + gate + ";",
        "",
        "-- 4.5 Wrap-up / task-timeout prompts persisted in the agents table.",
        "--     Empty values are intentional: the (now English) code default applies.",
    ]
    for composite, body in strings.WRAPUP.items():
        key, column = composite.split(".", 1)
        if key == "generic":
            continue
        signature = re.split(r"[.,;:]", body.strip())[0][:48]
        out += [
            "UPDATE agents SET " + column + " = " + q(body) + ", updated_at = now()",
            " WHERE key = " + lit(key) + " AND " + column + " <> ''",
            "   AND position(" + lit(signature) + " in " + column + ") = 0" + gate + ";",
            "",
        ]

    out += [
        "-- 4.6 Mark the migration done. Persisted in db.sql.gz by the state snapshot.",
        "INSERT INTO settings(key, value, updated_at)",
        "VALUES (" + lit(SETTING_KEY) + ", 'true', now())",
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();",
        "COMMIT;",
        "",
    ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="ARTEX checkout root")
    ap.add_argument("--sql-out", required=True)
    ap.add_argument("--report-out", default="")
    ap.add_argument("--force", action="store_true",
                    help="drop the one-shot gate (re-apply English defaults)")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        print("ERROR: --src is not a directory: %s" % src, file=sys.stderr)
        return 2
    prompts = load("prompts_en")
    templates = load("templates_en")
    strings = load("strings_en")

    report = {"errors": []}
    patch_go(src, prompts, strings, report)
    patch_seed(src, templates, report)

    sql = build_sql(prompts, templates, strings, args.force)
    os.makedirs(os.path.dirname(os.path.abspath(args.sql_out)), exist_ok=True)
    write(args.sql_out, sql)
    report["sql_bytes"] = len(sql.encode("utf-8"))
    report["force"] = bool(args.force)

    if args.report_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.report_out)), exist_ok=True)
        write(args.report_out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    print("English defaults applied to %s" % src)
    print("  Go files scanned .............. %d (%d in agent/)" %
          (report["go_files_scanned"], report["agent_files_scanned"]))
    print("  inline fragments replaced .... %d" % report["inline_replaced"])
    print("  prompt constants replaced .... %d / %d" %
          (len(report["constants_replaced"]), len(prompts.PROMPTS)))
    print("  task templates rewritten ..... %d" % report.get("seed_templates", 0))
    print("  Chinese literals in agent/ ... %d  (must be 0)" %
          report["chinese_literals_left_agent_total"])
    print("  Chinese UI/log literals ...... %d  (not sent to the LLM)" %
          report["chinese_literals_left_other_total"])
    print("  Go lexical sanity ............ %s" % report["go_syntax_sane"])
    print("  unmatched known hashes ....... %d" % report["inline_unmatched_total"])
    print("  SQL migration ................ %d bytes -> %s" %
          (report["sql_bytes"], args.sql_out))
    if report["inline_unmatched_total"]:
        print("::warning::%d agent/ Chinese fragments did not match upstream (wording changed?)" %
              report["inline_unmatched_total"])
        for f, v in report["inline_unmatched"][:8]:
            print("  unmatched: %s | %s" % (f, v))
    if report["chinese_literals_left_agent_total"]:
        print("::error::%d Chinese string literals still reach the LLM from agent/" %
              report["chinese_literals_left_agent_total"])
        for f, v in report["chinese_literals_left_agent"][:8]:
            print("  left: %s | %s" % (f, v))
    for e in report["errors"]:
        print("::warning::%s" % e)
    if report["errors"] or report["chinese_literals_left_agent_total"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
