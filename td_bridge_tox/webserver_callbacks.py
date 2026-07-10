import json
import io
import sys
import traceback
from pathlib import Path

# Set to a non-empty string to force a specific token.
# Leave empty to use the same-machine token file below (recommended):
# td_connect creates ~/.cache/td-mcp/bridge_token on first use and sends
# its content with every message. Once that file exists, messages without
# the matching token are rejected — the WebServer DAT listens on the
# network, and without this anyone on the LAN could reach eval/exec.
SHARED_SECRET = ''
TOKEN_FILE = Path.home() / '.cache' / 'td-mcp' / 'bridge_token'


def _shared_secret():
    if SHARED_SECRET:
        return SHARED_SECRET
    try:
        return TOKEN_FILE.read_text().strip()
    except Exception:
        # No token file yet (the MCP server never connected on this
        # machine) — local dev fallback, enforcement starts with the file.
        return ''


def onWebSocketReceiveText(webServerDAT, client, data):
    try:
        msg = json.loads(data)
    except Exception as e:
        _send(webServerDAT, client, None, False, error=f'Invalid JSON: {e}')
        return

    msg_id = msg.get('id')
    action = msg.get('action', '')
    payload = msg.get('data', {})
    token = msg.get('token', '')

    secret = _shared_secret()
    if secret and token != secret:
        _send(webServerDAT, client, msg_id, False, error='Unauthorized')
        return

    try:
        result = _dispatch(action, payload)
        _send(webServerDAT, client, msg_id, True, result=result)
    except Exception as e:
        _send(webServerDAT, client, msg_id, False,
              error=str(e), tb=traceback.format_exc())


def _send(webServerDAT, client, msg_id, ok, result=None, error=None, tb=None):
    resp = {'id': msg_id, 'ok': ok}
    if ok:
        resp['result'] = result
    else:
        resp['error'] = {'message': error}
        if tb:
            resp['error']['traceback'] = tb
    # default=str is a safety net: TD param values can include COMP refs and
    # other non-primitive Python objects that would otherwise crash json.dumps
    # mid-response (e.g. op_info on a CHOP whose params reference the parent).
    # Typed validation against the operators KB will land in Phase 3.
    webServerDAT.webSocketSendText(client, json.dumps(resp, default=str))


def _dispatch(action, data):
    if action == 'bridge_version':
        # Hash of this very script — lets the MCP server detect drift between
        # the repo's bridge script and what TD actually loaded (a project
        # reload silently reverts the DAT to the last-saved version).
        import hashlib
        return {'script_hash': hashlib.sha256(me.text.strip().encode('utf-8')).hexdigest()}

    if action == 'get_status':
        # /perform.par.rate is the project's actual render rate. The original
        # code used .fpsop which doesn't exist, silently falling back to 0.
        try:
            fps = float(op('/perform').par.rate)
        except Exception:
            fps = float(project.cookRate)
        return {
            'version': app.version,
            'build': getattr(app, 'build', ''),
            'project': project.name,
            'fps': fps,
            'frame': absTime.frame,
        }

    elif action == 'eval':
        expression = data.get('expression', '')
        value = eval(expression)
        return {'value': str(value)}

    elif action == 'run_script':
        code = data.get('code', '')
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        # Exec in a COPY of our globals: a bare exec(code) uses _dispatch's
        # locals, so functions the script defines can't see the script's own
        # top-level names (NameError on any helper call). The copy keeps
        # op/root/project visible while protecting the bridge's namespace
        # from whatever the script rebinds.
        ns = dict(globals())
        try:
            exec(code, ns)
        except Exception as e:
            partial = buf.getvalue()
            if partial:
                # The output up to the failure says how far the script got —
                # discarding it turns "failed at op 13/15" into a blind error.
                raise RuntimeError(
                    '{}: {} | partial output before failure:\n{}'.format(
                        type(e).__name__, e, partial[-2000:]
                    )
                )
            raise
        finally:
            sys.stdout = old_stdout
        return {'output': buf.getvalue()}

    elif action == 'create_op':
        parent_path = data.get('parent', '/project1')
        op_type = data.get('type', '')
        name = data.get('name', '')
        x = data.get('x', 0)
        y = data.get('y', 0)
        parent_op = op(parent_path)
        if not parent_op:
            raise Exception(f'Parent not found: {parent_path}')
        new_op = parent_op.create(getattr(td, op_type), name)
        new_op.nodeX = x
        new_op.nodeY = y
        return {'path': new_op.path, 'type': new_op.type, 'name': new_op.name}

    elif action == 'load_tox':
        parent_path = data.get('parent', '/project1')
        file_path = data.get('file', '')
        name = data.get('name', '')
        x = data.get('x', 0)
        y = data.get('y', 0)
        parent_op = op(parent_path)
        if not parent_op:
            raise Exception(f'Parent not found: {parent_path}')
        import os
        if not os.path.isfile(file_path):
            raise Exception(f'.tox not found on disk: {file_path}')
        new_op = parent_op.loadTox(file_path)
        extracted = False
        if data.get('extract', True):
            # Derivative palette .tox files wrap the real component one level
            # down (wrapper: no connectors, an icon TOP, one child COMP).
            # Native palette drag&drop extracts it — mirror that.
            try:
                inner_comps = [c for c in new_op.children if c.isCOMP]
                if (len(new_op.inputConnectors) == 0
                        and len(new_op.outputConnectors) == 0
                        and len(inner_comps) == 1
                        and (inner_comps[0].inputConnectors
                             or inner_comps[0].outputConnectors
                             or inner_comps[0].customPars)):
                    inner = inner_comps[0]
                    pulled = parent_op.copy(inner, name=(name or inner.name))
                    new_op.destroy()
                    new_op = pulled
                    extracted = True
            except Exception:
                pass  # extraction is best-effort; the wrapper still works
        if name:
            new_op.name = name
        new_op.nodeX = x
        new_op.nodeY = y
        return {'path': new_op.path, 'type': new_op.type, 'name': new_op.name,
                'tox': file_path, 'extracted': extracted}

    elif action == 'delete_op':
        path = data.get('path', '')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        target.destroy()
        return {'deleted': path}

    elif action == 'connect_ops':
        out_path = data.get('out', '')
        into_path = data.get('into', '')
        out_index = data.get('out_index', 0)
        in_index = data.get('in_index', 0)
        out_op = op(out_path)
        into_op = op(into_path)
        if not out_op:
            raise Exception(f'Output operator not found: {out_path}')
        if not into_op:
            raise Exception(f'Input operator not found: {into_path}')
        if in_index >= len(into_op.inputConnectors):
            msg = (f'{into_path} has {len(into_op.inputConnectors)} data input '
                   f'connector(s); in_index {in_index} is invalid.')
            if into_op.isCOMP:
                msg += (' Most COMPs (geoCOMP, cameraCOMP...) take no wired data '
                        'inputs — create a selectPOP/inTOP INSIDE the COMP, point '
                        'it at the source, and set its display/render flags '
                        '(td_set_flags).')
            raise Exception(msg)
        if out_index >= len(out_op.outputConnectors):
            raise Exception(f'{out_path} has {len(out_op.outputConnectors)} output '
                            f'connector(s); out_index {out_index} is invalid.')
        out_op.outputConnectors[out_index].connect(into_op.inputConnectors[in_index])
        return {'connected': f'{out_path}[{out_index}] -> {into_path}[{in_index}]'}

    elif action == 'op_info':
        path = data.get('path', '')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        params = {}
        for par in target.pars():
            try:
                params[par.name] = {
                    'val': par.eval(),
                    'default': par.default,
                    'label': par.label,
                }
            except Exception:
                pass
        return {
            'path': target.path,
            'name': target.name,
            'type': target.type,
            'x': target.nodeX,
            'y': target.nodeY,
            'inputs': len(target.inputConnectors),
            'outputs': len(target.outputConnectors),
            'parameters': params,
        }

    elif action == 'get_network':
        # Accept either 'path' (layout orchestrator) or 'parent' (legacy) key.
        parent_path = data.get('path') or data.get('parent', '/project1')
        parent_op = op(parent_path)
        if not parent_op:
            raise Exception(f'Parent not found: {parent_path}')
        children = parent_op.children if hasattr(parent_op, 'children') else []
        # Build ops list with op_type + family for layout orchestrator.
        ops_out = []
        for c in children:
            # OPType = python class name (cameraCOMP, renderTOP) — the canonical
            # vocabulary shared with the operators catalog and cluster detection.
            # c.type is the short name (cam, render) and matches nothing.
            ops_out.append({
                'path': c.path,
                'name': c.name,
                'op_type': getattr(c, 'OPType', c.type),
                'family': _family_of(c),
                'x': int(c.nodeX),
                'y': int(c.nodeY),
            })
        conns = []
        for c in children:
            for i, inp in enumerate(c.inputs):
                if inp is not None:
                    conns.append({'src': inp.path, 'dst': c.path, 'in_index': i})
        # Param references (material, camera, geometry, top, pop, targetpop,
        # sop...) are dependencies too — without them, mats/cams/geos have no
        # wires and the layout piles them all into column 0 (a 2400px-tall
        # stack, observed live). Resolve OP-type params to sibling edges.
        # For COMPs, also scan their DIRECT children (the select-inside-geo
        # idiom: geo depends on what its inner selectPOP points at).
        _OP_STYLES = {'OP', 'TOP', 'CHOP', 'SOP', 'DAT', 'MAT', 'POP',
                      'COMP', 'Object', 'PanelCOMP'}
        sibling_paths = set(o['path'] for o in ops_out)
        ref_conns = []

        def _collect_refs(scan_op, attribute_to):
            for p in scan_op.pars():
                if p.style not in _OP_STYLES:
                    continue
                try:
                    tgt = p.eval()
                except Exception:
                    continue
                targets = tgt if isinstance(tgt, (list, tuple)) else [tgt]
                for t in targets:
                    if t is None or isinstance(t, str):
                        continue
                    tgt_path = getattr(t, 'path', None)
                    if (tgt_path in sibling_paths and tgt_path != attribute_to):
                        ref_conns.append({'src': tgt_path, 'dst': attribute_to,
                                          'via': p.name})

        for c in children:
            _collect_refs(c, c.path)
            if c.isCOMP:
                for sub in c.children:
                    _collect_refs(sub, c.path)
        # Keep legacy 'operators' key for backward compat.
        return {
            'ok': True,
            'parent': parent_path,
            'ops': ops_out,
            'connections': conns,
            'ref_connections': ref_conns,
            'operators': ops_out,
            'count': len(ops_out),
        }

    elif action == 'set_param':
        path = data.get('path', '')
        param = data.get('param', '')
        value = data.get('value')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        par = target.par[param]
        if par is None:
            raise Exception(
                f"Unknown parameter '{param}' on {path} "
                f'({type(target).__name__}). Param names are internal names, '
                f'case-sensitive.')
        par.val = value
        return {'path': path, 'param': param, 'value': value}

    elif action == 'pulse':
        path = data.get('path', '')
        param = data.get('param', '')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        par = target.par[param]
        if par is None:
            raise Exception(
                f"Unknown parameter '{param}' on {path} "
                f'({type(target).__name__}). Param names are internal names, '
                f'case-sensitive.')
        par.pulse()
        return {'path': path, 'param': param, 'pulsed': True}

    elif action == 'set_flags':
        path = data.get('path', '')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        applied = {}
        for flag in ('display', 'render', 'bypass', 'viewer', 'lock'):
            if flag in data:
                try:
                    setattr(target, flag, bool(data[flag]))
                    applied[flag] = bool(data[flag])
                except Exception:
                    raise Exception(f"{type(target).__name__} has no '{flag}' flag")
        if not applied:
            raise Exception('No flag given — pass display/render/bypass/viewer/lock.')
        return {'path': path, 'flags': applied}

    elif action == 'timeline_play':
        op('/perform').par.play = 1
        return {'playing': True}

    elif action == 'timeline_stop':
        op('/perform').par.play = 0
        return {'playing': False}

    elif action == 'save_project':
        file_path = data.get('file', None)
        project.save(file_path)
        return {'saved': file_path or project.name}

    elif action == 'snapshot':
        import base64
        top_path = data.get('op', '/project1/fractal3d')
        tmp = '/tmp/td_mcp_snapshot.png'
        target = op(top_path)
        if not target:
            raise Exception(f'Operator not found: {top_path}')
        target.cook(force=True)
        target.save(tmp)
        with open(tmp, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
        return {'base64': b64, 'width': target.width, 'height': target.height}

    elif action == 'get_project_folder':
        return {'folder': project.folder}

    elif action == 'perf_report':
        # Cook-cost report: the heaviest ops by last measured cook time.
        # Field names verified live on build 2025.32820 (cpuCookTime /
        # gpuCookTime / childrenCPUCookTime / totalCooks).
        parent_path = data.get('path', '/')
        top = int(data.get('top', 15))
        max_depth = int(data.get('max_depth', 10))
        parent_op = op(parent_path)
        if not parent_op:
            raise Exception(f'Parent not found: {parent_path}')
        rows = []
        for o in parent_op.findChildren(maxDepth=max_depth):
            try:
                rows.append({
                    'path': o.path,
                    'op_type': getattr(o, 'OPType', o.type),
                    'cpu_ms': float(getattr(o, 'cpuCookTime', 0.0) or 0.0),
                    'gpu_ms': float(getattr(o, 'gpuCookTime', 0.0) or 0.0),
                    'children_cpu_ms': float(getattr(o, 'childrenCPUCookTime', 0.0) or 0.0),
                    'children_gpu_ms': float(getattr(o, 'childrenGPUCookTime', 0.0) or 0.0),
                    'total_cooks': int(getattr(o, 'totalCooks', 0) or 0),
                })
            except Exception:
                pass
        rows.sort(key=lambda r: r['cpu_ms'] + r['gpu_ms'], reverse=True)
        try:
            rate = float(project.cookRate)
        except Exception:
            rate = 60.0
        total_cpu = 0.0
        total_gpu = 0.0
        for r in rows:
            total_cpu += r['cpu_ms']
            total_gpu += r['gpu_ms']
        return {
            'path': parent_path,
            'measured_ops': len(rows),
            'frame_budget_ms': (1000.0 / rate) if rate else 0.0,
            'total_cpu_ms': total_cpu,
            'total_gpu_ms': total_gpu,
            'top': rows[:top],
        }

    elif action == 'checkpoint':
        # Comp-scoped .tox export (td_checkpoint and td_layout_network):
        #   data = {"comp_path": "/project1/mycomp", "file_path": "/path/x.tox"}
        #   Returns {"comp_path": ..., "file_path": ...}
        comp_path = data['comp_path']
        file_path = data['file_path']
        target = op(comp_path)
        if not target:
            raise Exception(f'Operator not found: {comp_path}')
        if not target.isCOMP:
            raise Exception(f'Checkpoint target must be a COMP: {comp_path} is {target.type}')
        # comp.save() exports a .tox of just this COMP (children + params)
        target.save(file_path)
        return {'comp_path': comp_path, 'file_path': file_path}

    elif action == 'rollback':
        comp_path = data['comp_path']
        file_path = data['file_path']
        target = op(comp_path)
        if not target:
            raise Exception(f'Operator not found: {comp_path} (was deleted since checkpoint)')
        parent = target.parent()
        if parent is None:
            raise Exception(f'Cannot rollback root COMP: {comp_path}')
        # Preserve identity before destroying
        name = target.name
        x, y = target.nodeX, target.nodeY
        target.destroy()
        # loadTox re-imports the saved component at the same parent
        restored = parent.loadTox(file_path)
        if restored is None:
            raise Exception(f'loadTox returned None for {file_path}')
        restored.name = name
        restored.nodeX = x
        restored.nodeY = y
        return {'restored_path': restored.path, 'file_path': file_path}

    elif action == 'apply_layout':
        # Apply computed layout: move ops, rename ops, add annotate COMPs.
        # Called by td_layout_network in server.py.
        path = data.get('path', '/project1')
        moves = data.get('moves', [])
        renames = data.get('renames', [])
        annotations = data.get('annotations', [])

        for m in moves:
            target = op(m['path'])
            if target is None:
                continue
            target.nodeX = m['x']
            target.nodeY = m['y']

        for r in renames:
            target = op(r['old_path'])
            if target is None:
                continue
            new_name = r['new_path'].rsplit('/', 1)[-1]
            target.name = new_name

        for a in annotations:
            member_paths = a.get('member_paths', [])
            parent_path_ann = member_paths[0].rsplit('/', 1)[0] if member_paths else path
            parent_op_ann = op(parent_path_ann)
            if parent_op_ann is None:
                continue
            safe_name = f"ann_{a['cluster_name'].replace(' ', '_')}"
            existing = parent_op_ann.op(safe_name)
            ann = existing if existing is not None else parent_op_ann.create(annotateCOMP, safe_name)
            if ann is not None:
                # TD may ignore the requested name on create — enforce it so
                # re-running layout updates this annotate instead of stacking
                # annotate1, annotate2, ... duplicates.
                if ann.name != safe_name:
                    ann.name = safe_name
                # annotateCOMP label lives in the custom par 'Titletext'
                # (capitalized) — there is no builtin 'title' par.
                ann.par.Titletext = a['cluster_name']
                ann.nodeX = a['bbox_x']
                ann.nodeY = a['bbox_y']
                ann.nodeWidth = a['bbox_w']
                ann.nodeHeight = a['bbox_h']

        return {
            'ok': True,
            'applied': {
                'moves': len(moves),
                'renames': len(renames),
                'annotations': len(annotations),
            },
        }

    else:
        raise Exception(f'Unknown action: {action}')


def _family_of(o):
    """Return the operator family string for a TD operator."""
    name = type(o).__name__
    for fam in ('CHOP', 'TOP', 'SOP', 'DAT', 'COMP', 'MAT', 'POP'):
        if name.endswith(fam):
            return fam
    return 'COMP'


def onWebSocketOpen(webServerDAT, client, data):
    print('[td-bridge] Client connected')


def onWebSocketClose(webServerDAT, client):
    print('[td-bridge] Client disconnected')
