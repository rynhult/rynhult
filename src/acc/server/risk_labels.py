"""Static advice only. This module has no prompt-answering or session-control interfaces."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from threading import Lock

from .models import Setting

LOCK = Lock()  # the standalone guard's run-directory context is module-local
log = logging.getLogger(__name__)
DEFAULT = {"label": "Review", "reason": "details on screen"}
UNKNOWN = {"label": "Review", "reason": "unknown action; check the details"}
READ_TOOL = re.compile(r"^(?:[Rr]ead|[Vv]iew|[Gg]lob|[Gg]rep|[Ss]earch|[Ff]ind|[Ll]ist|ls|rg)(?:$|[_A-Z])")
WRITE_TOOL = re.compile(r"write|edit|patch|delete|remove|create|save|replace", re.I)
NETWORK = frozenset(("curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "netcat", "telnet", "http", "https"))
WRITERS = frozenset(("touch", "mkdir", "cp", "mv", "install", "tee", "truncate", "chmod", "chown", "ln",
                     "rm", "rmdir", "unlink", "dd", "sed"))
GIT_READ = frozenset(("status", "diff", "log", "show", "ls-files", "ls-tree", "rev-parse", "describe",
                      "grep", "blame", "shortlog"))


@lru_cache(maxsize=4)
def _guard(path: Path):
    loader = importlib.machinery.SourceFileLoader("acc_advisory_guard", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def enabled(db, username: str, *, project_id=None) -> bool:
    from .admin_features import current
    if not current('risk_labels'):
        return False
    from .security_settings import resolve
    if not resolve(db, owner=username, project_id=project_id)['switches']['risk_labels']:
        return False
    row = db.get(Setting, f"risk_labels.{username}")
    return row.value.get("enabled", True) if row else True


def classify(hook: dict, workspace: str, *, guard_path: Path, policy: dict | None = None,
             run_dir: str = "") -> dict[str, str]:
    with LOCK:
        try:
            guard = _guard(guard_path)
            guard.BRANCHES.clear()  # standalone hook observations belong to one classification
            try:
                return _classify(guard, hook, workspace, policy or {}, run_dir)
            finally:
                guard.BRANCHES.clear()
        except (OSError, ValueError, SyntaxError) as error:
            log.warning("advisory risk classification unavailable (%s)", type(error).__name__)
            return {"label": "Review", "reason": "classification unavailable; check the details"}


def _classify(g, hook: dict, workspace: str, policy: dict, run_dir: str) -> dict[str, str]:
    tool, args = g.tool_call(hook)
    commands = g.strings(args, g.COMMAND_KEYS)
    paths = g.strings(args, g.PATH_KEYS + g.SEARCH_KEYS + ("paths",)) + g.patch_paths(args)
    if not tool and not commands and not paths:
        return dict(DEFAULT)
    workspace = os.path.normpath(workspace)
    cwd = hook.get("cwd") if isinstance(hook.get("cwd"), str) else workspace
    for key in ("workdir", "Cwd", "cwd"):
        if isinstance(args.get(key), str) and args[key]:
            cwd = os.path.normpath(os.path.join(cwd, args[key]))
            break
    results = []

    def note(label, reason):
        results.append({"label": label, "reason": reason})

    rules = {key: True for key in ("force_push", "push_default", "rm_outside", "secrets")}
    # Check refusal rules separately from tool approval: an install must not mask a later dangerous command.
    verdict = g.check({**policy, "rules": rules, "workspace": workspace, "tool_approval": False},
                      tool, args, hook.get("cwd") if isinstance(hook.get("cwd"), str) else workspace, run_dir)
    if verdict:
        note("Risky", "accesses credential paths" if verdict[0] == "secrets"
             else "guardrails would refuse this action")
    if g.check({"rules": {}, "workspace": workspace, "tool_approval": True},
               tool, args, workspace, run_dir):
        note("Review", "installs or changes tools or dependencies")

    def path_advice(word, here, known, write=False, delete=False):
        path = g.word_path(word, here, known)
        if path is None:
            results.append(dict(UNKNOWN))
        elif g.secret_path(path):
            note("Risky", "accesses credential paths")
        elif not g.project_path(path, workspace):
            note("Risky" if write else "Review", "deletes outside the workspace" if delete else
                 "writes outside the workspace" if write else "reads outside the workspace")
        else:
            note("Review" if write else "Routine",
                 "writes files in the project" if write else "reads files in the project")

    def walk(items, here, known, incoming=False):
        previous = None
        network_pipe = incoming
        for index, (node, sep) in enumerate(items):
            piped = (incoming and index == 0) or (index > 0 and items[index - 1][1] in ("|", "|&"))
            if not piped:
                network_pipe = False
            isolated = piped or sep in ("|", "|&", "&")
            local = dict(known) if isolated else known
            if not isinstance(node, g.Command):
                after = walk(node[1], here, dict(local) if node[0] == "sub" else local, network_pipe)
                if node[0] != "sub" and not isolated:
                    here = after
                previous = node
                continue
            for part in node.assigns + node.words + [target for _, target in node.redirs]:
                for sub in part.subs:
                    walk(sub, here, dict(local))
            for assign in node.assigns:
                g.remember(assign, local)
            for op, target in node.redirs:
                if isinstance(target, g.Word) and op != "<<<" and not re.fullmatch(r"\d*-?", target.text):
                    path_advice(target, here, local, op in g.WRITE_REDIRECTS)
            if node.data:
                results.append(dict(UNKNOWN))
                local.clear()
                continue
            words, where, lookup = g.unwrap(node.words, here, local)
            effective = next((i for i, w in enumerate(node.words) if words and w is words[0]), len(node.words))
            prefix = node.words[:effective + 1]
            if any(os.path.basename(g.expand(w, local) or w.text) in ("sudo", "su", "doas") for w in prefix):
                note("Risky", "requests elevated privileges")
            if not words:
                previous = node
                continue
            name = os.path.basename(g.expand(words[0], local) or words[0].text)
            text = [w.text for w in words[1:]]
            is_network = name in NETWORK
            network_pipe |= is_network
            if name in g.SHELLS:
                if piped and network_pipe:
                    note("Risky", "runs a network download piped to a shell")
                scripts, rest = g.shell_scripts(words, node, previous if piped else None)
                for script in scripts:
                    walk(g.Parser(script).parse(), where, dict(local))
                if rest or not scripts and not piped:
                    results.append(dict(UNKNOWN))
            elif name == "eval":
                walk(g.Parser(" ".join(text)).parse(), where, dict(local))
                results.append(dict(UNKNOWN))
            elif name in ("cd", "pushd"):
                here = g.change_dir(words, where) if not isolated else here
            elif name == "popd":
                here = None
            elif name == "find":
                if any(t in text for t in ("-exec", "-execdir", "-ok", "-okdir")):
                    start = next(i for i, w in enumerate(words) if w.text in ("-exec", "-execdir", "-ok", "-okdir"))
                    end = next((i for i in range(start + 1, len(words)) if words[i].text in (";", "+")), len(words))
                    nested = g.Command()
                    nested.words = words[start + 1:end]
                    walk([[nested, None]], None if words[start].text.endswith("dir") else where, dict(local))
                if "-delete" in text:
                    for word in words[1:]:
                        if word.text.startswith("-"):
                            break
                        path_advice(word, where, local, True, True)
                elif not any(t in text for t in ("-exec", "-execdir", "-ok", "-okdir")):
                    for word in words[1:]:
                        if word.text.startswith("-"):
                            break
                        path_advice(word, where, local)
            elif is_network:
                note("Review", "accesses the network")
                for i, word in enumerate(words[1:], 1):
                    option, equals, _ = word.text.partition("=")
                    if option in ("-o", "--output", "--output-dir", "--output-document",
                                  "--directory-prefix", "-P") or name == "wget" and option == "-O":
                        target = word.tail(len(option) + 1) if equals else (
                            words[i + 1] if i + 1 < len(words) else g.Word("$", "$"))
                        if target.text != "-":
                            path_advice(target, where, local, True)
            elif name in g.TOOL_MANAGERS or name.startswith("python") and text[:2] == ["-m", "pip"]:
                note("Review", "installs or changes tools or dependencies")
            elif name == "git":
                sub, _, _ = g.git_subcommand(words, where)
                if sub in GIT_READ and not any(t in text for t in ("--output", "--ext-diff", "--textconv")) \
                        and not any(t.startswith(("--output=", "--exec-path", "--git-dir", "--work-tree")) for t in text):
                    path_advice(g.Word("."), g.git_subcommand(words, where)[2], local)
                    for word in g.operands(words):
                        path_advice(word, where, local)
                else:
                    note("Review", "accesses the network" if sub in ("push", "pull", "fetch", "clone")
                         else "unknown action; check the details")
            elif name in WRITERS:
                write = name != "sed" or any(t == "--in-place" or t.startswith(("-i", "--in-place=")) for t in text)
                operands = g.operands(words)
                destination = None
                if name in ("cp", "install") and "-d" not in text:
                    for i, word in enumerate(words[1:], 1):
                        option, equals, _ = word.text.partition("=")
                        if option in ("-t", "--target-directory"):
                            destination = word.tail(len(option) + 1) if equals else (
                                words[i + 1] if i + 1 < len(words) else g.Word("$", "$"))
                    if destination:
                        path_advice(destination, where, local, True)
                for i, word in enumerate(operands):
                    if name == "dd" and word.text.startswith(("if=", "of=")):
                        path_advice(word.tail(3), where, local, word.text.startswith("of="))
                    else:
                        source = name in ("cp", "install") and "-d" not in text and (
                            destination is not None or i < len(operands) - 1)
                        path_advice(word, where, local, write and not source, name in ("rm", "rmdir", "unlink"))
                if not operands:
                    results.append(dict(UNKNOWN))
            elif name in g.READERS or name in g.TEXT_COMMANDS or name == "pwd" or lookup:
                operands = g.operands(words)
                for word in operands:
                    path_advice(word, where, local)
                if not operands:
                    path_advice(g.Word("."), where, local)
            else:
                results.append(dict(UNKNOWN))
            previous = node
        return here

    try:
        for command in commands:
            walk(g.Parser(command).parse(), cwd, {})
    except RecursionError:
        note("Risky", "guardrails would refuse this action")
    if paths:
        write = bool(WRITE_TOOL.search(tool))
        if not write and not READ_TOOL.search(tool):
            results.append(dict(UNKNOWN))
        for path in paths:
            word = g.Word(path, "u" * len(path))
            if "$" in path:
                word = g.Word(path, "$" * len(path))
            path_advice(word, cwd, {}, write)
    elif not commands and READ_TOOL.search(tool):
        path_advice(g.Word("."), cwd, {})
    if re.search(r"web|fetch|http|browse", tool, re.I) or g.strings(args, ("url", "urls")):
        note("Review", "accesses the network")
    if not results:
        return dict(UNKNOWN)
    return max(results, key=lambda r: ({"Routine": 0, "Review": 1, "Risky": 2}[r["label"]],
                                      r["reason"] != "guardrails would refuse this action"))
