"""Observe each sidebar gesture before deciding whether a pool is absent.

Only geometry is compared: the card/banner movies are intentionally excluded.
Timeouts bound an unacknowledged gesture; no game step blocks on a fixed sleep.
"""
from __future__ import annotations


def viewport(current, positions):
    return {'current': current,
            'positions': {k: list(v['center']) for k, v in sorted(positions.items())}}


def same_view(a, b):
    if not a or not b or a['current'] != b['current'] or a['positions'].keys() != b['positions'].keys():
        return False
    return all(max(abs(x-y) for x, y in zip(point, b['positions'][key])) <= 3
               for key, point in a['positions'].items())


class PoolNavigator:
    # Two stable observations spanning this interval let a moving sidebar settle.
    settle_seconds = .3
    # A still frame immediately after input is not evidence of a list boundary.
    feedback_seconds = 1.5
    boundary_probes = 2
    max_swipes = 24

    def __init__(self):
        self.draw_id = None
        self.invalidate()

    def invalidate(self):
        self.last_view = None
        self.stable_since = None
        self.observations = 0

    def observe(self, context, draw_id, target, current, positions, now):
        state = context.get('scan')
        if not state or state.get('version') != 2 or state.get('draw_id') != draw_id:
            state = {'version': 2, 'draw_id': draw_id, 'direction': 'down', 'swipes': 0,
                     'boundary_hits': 0, 'boundaries': []}
            context['scan'] = state
        if self.draw_id != draw_id:
            self.draw_id = draw_id
            self.invalidate()
            # After restarting, elapsed wall time cannot prove what a gesture did.
            # Re-observe and repeat the reversible scan, preserving its attempt bound.
            if state.pop('pending', None):
                state.update(boundary_hits=0, boundaries=[], direction='down')
        if not positions:
            self.invalidate()
            return 'wait'
        view = viewport(current, positions)
        if not same_view(view, self.last_view):
            self.last_view, self.stable_since, self.observations = view, now, 1
            return 'wait'
        self.observations += 1
        if self.observations < 2 or now-self.stable_since < self.settle_seconds:
            return 'wait'

        pending = state.get('pending')
        if pending:
            if same_view(view, pending['before']):
                if now-pending['sent_at'] < self.feedback_seconds:
                    return 'wait'
                state['boundary_hits'] += 1
                if state['boundary_hits'] >= self.boundary_probes:
                    state['boundaries'].append(state['direction'])
                    state['direction'] = 'up' if state['direction'] == 'down' else 'down'
                    state['boundary_hits'] = 0
            else:
                state['boundary_hits'] = 0
            state.pop('pending')

        if current == target:
            context.pop('scan', None)
            return 'ready'
        if target in positions:
            return 'select'
        if {'up', 'down'} <= set(state['boundaries']):
            return 'missing'
        if state['swipes'] >= self.max_swipes:
            return 'uncertain'
        return 'swipe'

    def sent(self, context, current, positions, now):
        """Called only after the SDK successfully sends a sidebar swipe."""
        state = context['scan']
        state['swipes'] += 1
        state['pending'] = {'before': viewport(current, positions), 'sent_at': now}
        self.invalidate()
