"""판정/알림 로직 테스트 (익명화된 응답 샘플 사용)."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BIZ_ID", "999999")
import watcher as w  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 29, 18, 43, tzinfo=w.KST)


class FakeClient:
    def __init__(self, items):
        self.items = items
        self.hourly = json.loads((FIX / "hourlySchedule.json").read_text())["data"]["schedule"][
            "bizItemSchedule"
        ]["hourly"]
        self.calls = []

    def biz_items(self):
        return self.items

    def hourly_schedule(self, iid, start, end):
        self.calls.append((iid, start, end))
        return [h for h in self.hourly if start.isoformat() <= h["unitStartTime"][:10] <= end.isoformat()]


def cfg(tmp_path, **kw):
    c = w.Config.from_env()
    c.state_file = tmp_path / "state.json"
    c.days_ahead = 5
    c.time_from = c.time_to = None
    c.weekdays = None
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def open_items():
    return json.loads((FIX / "bizItems.json").read_text())["data"]["bizItems"][:1]


def test_open_item_detection():
    it = open_items()[0]
    assert w.is_item_open(it)
    assert not w.is_item_open({**it, "bookableSettingJson": {"isOpened": False}})
    assert not w.is_item_open({**it, "bookableSettingJson": {"isOpened": True, "isPaused": True}})
    assert not w.is_item_open({**it, "isClosedBooking": True})
    assert not w.is_item_open({**it, "isImp": False})


def test_slots_filter_past_and_full(tmp_path):
    res = w.check(FakeClient(open_items()), cfg(tmp_path), NOW)
    slots = res.slots["1001"]
    hm = {t.strftime("%m-%d %H:%M") for t in slots}
    assert all(t > NOW for t in slots)  # 오늘 지난 시간 제외
    assert "09-29 18:00" not in hm
    assert "10-03 13:30" not in hm  # 마감 슬롯 제외
    assert "10-03 13:00" in hm and "09-30 09:30" in hm


def test_time_and_weekday_filters(tmp_path):
    c = cfg(tmp_path, time_from="18:00", time_to="20:00", weekdays={2})  # 수요일
    res = w.check(FakeClient(open_items()), c, NOW)
    hm = [t.strftime("%m-%d %H:%M") for t in res.slots["1001"]]
    assert hm == ["09-30 18:00", "09-30 18:30", "09-30 19:00", "09-30 19:30", "09-30 20:00"]


def test_closed_business_no_schedule_calls(tmp_path):
    fc = FakeClient([])
    res = w.check(fc, cfg(tmp_path), NOW)
    assert not res.is_open and fc.calls == []


def test_notification_transitions(tmp_path):
    c = cfg(tmp_path)
    res = w.check(FakeClient(open_items()), c, NOW)
    closed = {"available": False, "slots": [], "errors": 0}
    msg = w.decide_message(c, closed, res)
    assert "예약 열림" in msg and "999999" in msg and "9/30(수)" in msg

    same = {"available": True, "slots": sorted(res.slot_keys())}
    assert w.decide_message(c, same, res) is None  # 변화 없으면 조용히

    one_missing = {"available": True, "slots": sorted(res.slot_keys())[1:]}
    assert "새 빈 시간" in w.decide_message(c, one_missing, res)

    assert "닫힘" in w.decide_message(c, same, w.CheckResult())


def test_run_once_dedup(tmp_path, capsys, monkeypatch):
    c = cfg(tmp_path)
    fc = FakeClient(open_items())
    monkeypatch.setattr(w, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: NOW)}))
    w.run_once(c, fc, dry_run=True)
    assert "예약 열림" in capsys.readouterr().out
    w.run_once(c, fc, dry_run=True)
    assert "[알림]" not in capsys.readouterr().out  # 두 번째 실행은 중복 알림 없음
