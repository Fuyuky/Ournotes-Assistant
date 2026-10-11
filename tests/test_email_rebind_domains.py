import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from ournotes.vision.ocr import TextBox
from ournotes.rebind.flow import Flow


def dropdown(monkeypatch, pages, *, first_ocr_empty=False, binding=True):
    clock = [0.0]
    monkeypatch.setattr('ournotes.rebind.flow.time.monotonic', lambda: clock[0])
    flow = Flow.__new__(Flow)
    expected = 'bind_form' if binding else 'email_form'
    state = {'open': False, 'offset': 0, 'selected': 'gmail.com'}
    inputs = []
    flow.task = {'id': 'test'}
    flow.service = SimpleNamespace(mutex=threading.RLock(), check=lambda: None,
                                   wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    flow.policy = SimpleNamespace(point=lambda point, obs: point, interval=lambda: .1)

    def read():
        value = 30 + 50 * state['offset'] if state['open'] else 240
        flow.image = Image.new('RGB', (1600, 900), (value,) * 3)
        flow.observation = SimpleNamespace(image=flow.image)
        return expected

    def tap(x, y):
        inputs.append(('tap', x, y))
        if x == 974:
            state['open'] = not state['open']
        else:
            assert state['open']
            state['selected'] = pages[state['offset']]
            state['open'] = False

    def swipe(start, end, duration):
        assert state['open']
        inputs.append(('swipe', start, end))
        step = 1 if start[1] > end[1] else -1
        state['offset'] = max(0, min(len(pages) - 1, state['offset'] + step))

    def texts(frame, name, regions, *args, **kwargs):
        assert state['open']
        if first_ocr_empty and state['offset'] == 0:
            return []
        return [TextBox(' @' + pages[state['offset']] + ' ', 1, (636, 445, 772, 469))]

    flow.read = read
    flow.fields = SimpleNamespace(read=texts)
    flow.device = SimpleNamespace(tap=tap, swipe=swipe)
    flow.domain_menu_open = lambda: state['open']
    monkeypatch.setattr('ournotes.rebind.flow.field_text', lambda *args: '@' + state['selected'])
    read()
    return flow, inputs, state, expected


@pytest.mark.parametrize('domain', ['qq.com', '163.com', '126.com'])
@pytest.mark.parametrize('first_ocr_empty', [False, True])
def test_hidden_domain_scrolls_despite_sparse_ocr(monkeypatch, domain, first_ocr_empty):
    flow, inputs, state, expected = dropdown(monkeypatch, ['gmail.com', 'icloud.com', domain],
                                             first_ocr_empty=first_ocr_empty)
    assert flow.domain(expected, 'target@' + domain, binding=True) == 'target'
    assert state['selected'] == domain
    assert len([x for x in inputs if x[0] == 'swipe']) == 2
    assert len([x for x in inputs if x[0] == 'tap']) == 2


def test_default_gmail_does_not_open_or_scroll(monkeypatch):
    flow, inputs, _, expected = dropdown(monkeypatch, ['gmail.com'])
    assert flow.domain(expected, 'target@gmail.com', binding=True) == 'target'
    assert not inputs


def test_missing_domain_searches_both_directions_and_stops(monkeypatch):
    flow, inputs, _, expected = dropdown(monkeypatch, ['gmail.com', 'icloud.com'])
    with pytest.raises(ValueError, match='不含目标'):
        flow.domain(expected, 'target@qq.com', binding=True)
    swipes = [x for x in inputs if x[0] == 'swipe']
    assert any(x[1][1] < x[2][1] for x in swipes)
    assert len(inputs) < 12
    assert len([x for x in inputs if x[0] == 'tap']) == 1


def test_dropdown_closes_before_click_revalidation(monkeypatch):
    flow, inputs, state, expected = dropdown(monkeypatch, ['qq.com'])
    original = flow.domain_options

    def close(image, box):
        options = original(image, box)
        state['open'] = False
        return options

    flow.domain_options = close
    with pytest.raises(ValueError, match='已关闭'):
        flow.domain(expected, 'target@qq.com', binding=True)
    assert len(inputs) == 1


def test_disappearing_toast_restabilizes_without_extra_gesture(monkeypatch):
    flow, inputs, state, expected = dropdown(monkeypatch, ['qq.com'])
    original = flow.domain_options
    count = [0]

    def changing(image, box):
        result = original(image, box)
        if not count[0]:
            state['offset'] = 1
        count[0] += 1
        return result

    # Same label, a changed list background after OCR; only a read-only retry is allowed.
    original_read = flow.read

    def read():
        page = original_read()
        state['offset'] = 0
        return page

    flow.domain_options, flow.read = changing, read
    assert flow.domain(expected, 'target@qq.com', binding=True) == 'target'
    assert not any(x[0] == 'swipe' for x in inputs)
    assert count[0] >= 3


def test_clipped_row_is_not_clicked(monkeypatch):
    flow, inputs, state, expected = dropdown(monkeypatch, ['qq.com'])
    flow.fields.read = lambda *args, **kwargs: [TextBox('@qq.com', 1, (636, 650, 730, 675))]
    with pytest.raises(ValueError, match='不含目标'):
        flow.domain(expected, 'target@qq.com', binding=True)
    assert state['selected'] == 'gmail.com'
    assert len([x for x in inputs if x[0] == 'tap']) == 1


def test_sdk_loading_blocks_background_title(monkeypatch):
    flow = Flow.__new__(Flow)
    flow.service = SimpleNamespace(check=lambda: None)
    flow.device = SimpleNamespace(capture=lambda: SimpleNamespace(image=Image.new('RGB', (1600, 900))))
    flow.observer = SimpleNamespace(read=lambda *args, **kwargs: pytest.fail('background title must not be read'))
    monkeypatch.setattr('ournotes.rebind.flow.scene', lambda image: 'loading')
    assert flow.read() == 'loading'
    assert flow.observation.scene == 'loading'


@pytest.mark.parametrize('binding', [False, True])
def test_dropdown_uses_local_roi_and_requires_exact_label(monkeypatch, binding):
    flow, _, _, expected = dropdown(monkeypatch, ['qq.com'], binding=binding)
    assert flow.domain(expected, 'target@qq.com', binding=binding) == 'target'
    assert Flow.domain_value(' @QQ.COM ') == 'qq.com'
    assert Flow.domain_value('| @163.com') == '163.com'
    assert Flow.domain_value('@qq.com.evil') != 'qq.com'
