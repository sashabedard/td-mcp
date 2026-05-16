import json
import io
import sys
import traceback

# Set to a non-empty string to require token authentication.
# Leave empty to allow all local connections (default for local dev).
SHARED_SECRET = ''


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

    if SHARED_SECRET and token != SHARED_SECRET:
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
        try:
            exec(code)
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
        parent_path = data.get('parent', '/project1')
        parent_op = op(parent_path)
        if not parent_op:
            raise Exception(f'Parent not found: {parent_path}')
        operators = [
            {'path': child.path, 'name': child.name, 'type': child.type,
             'x': child.nodeX, 'y': child.nodeY}
            for child in parent_op.children
        ]
        return {'parent': parent_path, 'operators': operators, 'count': len(operators)}

    elif action == 'set_param':
        path = data.get('path', '')
        param = data.get('param', '')
        value = data.get('value')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        target.par[param].val = value
        return {'path': path, 'param': param, 'value': value}

    elif action == 'pulse':
        path = data.get('path', '')
        param = data.get('param', '')
        target = op(path)
        if not target:
            raise Exception(f'Operator not found: {path}')
        target.par[param].pulse()
        return {'path': path, 'param': param, 'pulsed': True}

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

    elif action == 'checkpoint':
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

    else:
        raise Exception(f'Unknown action: {action}')


def onWebSocketOpen(webServerDAT, client, data):
    print(f'[td-bridge] Client connected')


def onWebSocketClose(webServerDAT, client):
    print(f'[td-bridge] Client disconnected')
