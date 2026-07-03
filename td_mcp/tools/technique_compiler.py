"""Technique → pattern compiler: the curation loop, industrialized.

Takes one vision-extracted segment (techniques.json), rebuilds it inside a
live TD wrapper COMP, and reads the *actual* parameters back from the built
network. What the VLM guessed gets corrected by what TD accepted — the same
mechanism that caught RayReflect vs "ReflectedRay" on the first manual pass.

Pure logic lives here (instance-name resolution, param-label matching,
build planning) so it is unit-testable without TD; the server tool owns the
bridge round-trips.

Known limits, by design:
- Extraction params use display LABELS ("Black Level"), not internal names —
  matching is fuzzy and unmatched labels are REPORTED, never guessed.
- Vector params share one label across components ("Displace Weight" covers
  displaceweightx/y): whitespace-separated values are distributed in order.
- Instance names resolve to classes by digit-stripped stem match against the
  extraction's own operator list; ambiguous stems are reported unresolved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class BuildPlan:
    creates: list[tuple[str, str]] = field(default_factory=list)   # (instance, op_type)
    connections: list[tuple[str, str]] = field(default_factory=list)
    params: list[tuple[str, str, str]] = field(default_factory=list)  # (instance, label, value)
    unresolved_instances: list[str] = field(default_factory=list)
    dropped_connections: list[tuple[str, str]] = field(default_factory=list)


_TRAILING_DIGITS = re.compile(r"[\d_]+$")
_FAMILY_SUFFIX = re.compile(r"(COMP|CHOP|DAT|MAT|POP|SOP|TOP)$")


def _stem(name: str) -> str:
    """noise3 → noise, Pseudo_Liquid_V01 → pseudo_liquid_v, mathcombine2 → mathcombine."""
    return _TRAILING_DIGITS.sub("", name).lower()


def resolve_instance(instance: str, class_names: list[str]) -> str | None:
    """Map an instance name (noise1) to a class from the extraction's own
    operator list (noisePOP). None when ambiguous or unknown — the caller
    reports, never guesses."""
    if instance in class_names:
        return instance
    stem = _stem(instance)
    if not stem:
        return None
    candidates = [c for c in class_names if _FAMILY_SUFFIX.sub("", c).lower() == stem]
    if not candidates:
        candidates = [c for c in class_names if c.lower().startswith(stem)]
    return candidates[0] if len(candidates) == 1 else None


def build_plan(extraction: dict, catalog_classes: set[str]) -> BuildPlan:
    """Turn one techniques.json extraction into an executable build plan.
    Only catalog-validated classes are created; connections whose endpoints
    can't be resolved are dropped (and reported)."""
    plan = BuildPlan()
    classes = [c for c in extraction.get("operators", []) if c in catalog_classes]

    instances: dict[str, str] = {}

    def _register(instance: str) -> bool:
        if instance in instances:
            return True
        cls = resolve_instance(instance, classes)
        if cls is None:
            if instance not in plan.unresolved_instances:
                plan.unresolved_instances.append(instance)
            return False
        instances[instance] = cls
        return True

    for conn in extraction.get("connections", []):
        src, dst = conn.get("source", ""), conn.get("target", "")
        if _register(src) and _register(dst):
            plan.connections.append((src, dst))
        else:
            plan.dropped_connections.append((src, dst))

    for p in extraction.get("parameters", []):
        inst = p.get("operator", "")
        if _register(inst):
            plan.params.append((inst, p.get("parameter", ""), str(p.get("value", ""))))

    # classes named in the extraction but never referenced by wiring/params
    # still get one instance each — they were on screen for a reason
    used_classes = set(instances.values())
    for cls in classes:
        if cls not in used_classes:
            inst = _FAMILY_SUFFIX.sub("", cls).lower() + "1"
            if inst not in instances:
                instances[inst] = cls

    plan.creates = sorted(instances.items())
    return plan


def normalize_label(label: str) -> str:
    return "".join(label.lower().split())


def compile_script(plan: BuildPlan, parent: str, comp_name: str) -> str:
    """Generate the flat run_script payload that builds the plan in TD and
    returns a JSON report: what was created/wired, which param labels
    matched which internal names (with the value TD actually holds), and
    which labels found no match. Flat code only — the bridge exec has
    split namespaces, nested functions can't see outer locals."""
    lines = [
        "import json, re",
        f"_parent = op({parent!r})",
        f"_old = _parent.op({comp_name!r})",
        "if _old: _old.destroy()",
        f"_w = _parent.create(baseCOMP, {comp_name!r})",
        "_report = {'created': [], 'create_failed': [], 'wired': [], 'wire_failed': [],",
        "           'params_applied': [], 'params_unmatched': []}",
    ]
    for inst, cls in plan.creates:
        lines += [
            "try:",
            f"    _o = _w.create({cls}, {inst!r})",
            f"    _report['created'].append([{inst!r}, {cls!r}])",
            "except Exception as _e:",
            f"    _report['create_failed'].append([{inst!r}, {cls!r}, str(_e)])",
        ]
    for src, dst in plan.connections:
        lines += [
            "try:",
            f"    _w.op({dst!r}).inputConnectors[len([i for i in _w.op({dst!r}).inputs if i is not None])].connect(_w.op({src!r}))",
            f"    _report['wired'].append([{src!r}, {dst!r}])",
            "except Exception as _e:",
            f"    _report['wire_failed'].append([{src!r}, {dst!r}, str(_e)])",
        ]
    for inst, label, value in plan.params:
        norm = normalize_label(label)
        lines += [
            f"_o = _w.op({inst!r})",
            "if _o is not None:",
            f"    _targets = [p for p in _o.pars() if ''.join(p.label.lower().split()) == {norm!r}]",
            "    if _targets:",
            f"        _raw = {value!r}",
            "        _parts = _raw.split()",
            "        _vals = _parts if len(_parts) == len(_targets) else [_raw] * len(_targets)",
            "        for _p, _v in zip(_targets, _vals):",
            "            try:",
            "                _p.val = _v",
            f"                _report['params_applied'].append([{inst!r}, _p.name, str(_p.eval())])",
            "            except Exception as _e:",
            f"                _report['params_unmatched'].append([{inst!r}, {label!r}, {value!r}, str(_e)])",
            "    else:",
            f"        _report['params_unmatched'].append([{inst!r}, {label!r}, {value!r}, 'no par with this label'])",
        ]
    # read back non-default constant params of every created op — the
    # live-verified values that feed the draft pattern
    lines += [
        "_verified = {}",
        "for _c in _w.children:",
        "    _nd = {}",
        "    for _p in _c.pars():",
        "        try:",
        "            if _p.mode == ParMode.CONSTANT and not _p.isDefault and _p.name != 'pageindex':",
        "                _nd[_p.name] = str(_p.eval())",
        "        except Exception:",
        "            pass",
        "    _verified[_c.name] = {'op_type': _c.OPType, 'params': _nd}",
        "_report['verified_ops'] = _verified",
        "print(json.dumps(_report, default=str))",
    ]
    return "\n".join(lines)
