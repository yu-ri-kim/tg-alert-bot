"""Schedule availability checker with Telegram notification.

사용법:
  python watcher.py --once         # 1회 확인 (cron / systemd timer / GitHub Actions 용)
  python watcher.py --loop         # 상주하며 INTERVAL_SEC 간격으로 확인
  python watcher.py --once --dry-run   # 텔레그램 대신 콘솔 출력
  python watcher.py --test-telegram    # 텔레그램 연결 테스트
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
GRAPHQL_URL = "https://m.booking.naver.com/graphql"
WEEKDAY_KO = "월화수목금토일"
WEEKDAY_EN = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

log = logging.getLogger("watcher")

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """의존성 없이 .env 읽기 (이미 설정된 환경변수는 덮어쓰지 않음)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0]  # 인라인 주석 제거
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    biz_id: str
    place_name: str
    days_ahead: int
    time_from: str | None
    time_to: str | None
    weekdays: set[int] | None
    telegram_token: str | None
    telegram_chat_id: str | None
    state_file: Path
    interval_sec: int
    notify_close: bool
    error_alert_threshold: int

    @classmethod
    def from_env(cls) -> "Config":
        wd = os.getenv("WEEKDAYS", "").strip()
        return cls(
            biz_id=os.environ["BIZ_ID"],
            place_name=os.getenv("PLACE_NAME", "target"),
            days_ahead=int(os.getenv("DAYS_AHEAD", "14")),
            time_from=os.getenv("TIME_FROM") or None,  # "09:00"
            time_to=os.getenv("TIME_TO") or None,  # "21:00"
            weekdays={WEEKDAY_EN.index(x.strip().upper()) for x in wd.split(",") if x.strip()} or None,
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            state_file=Path(os.getenv("STATE_FILE", "state.json")),
            interval_sec=int(os.getenv("INTERVAL_SEC", "180")),
            notify_close=os.getenv("NOTIFY_CLOSE", "true").lower() == "true",
            error_alert_threshold=int(os.getenv("ERROR_ALERT_THRESHOLD", "5")),
        )

    @property
    def booking_url(self) -> str:
        return f"https://m.booking.naver.com/booking/13/bizes/{self.biz_id}"


# ---------------------------------------------------------------------------
# 네이버 예약 GraphQL 클라이언트
# ---------------------------------------------------------------------------

BIZ_ITEMS_QUERY = """
query bizItems($input: BizItemsParams) {
  bizItems(input: $input) {
    bizItemId
    name
    isImp
    isClosedBooking
    bookableSettingJson
    __typename
  }
}
"""

HOURLY_SCHEDULE_QUERY = """
query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        unitStartTime
        unitBookingCount
        unitStock
        isUnitSaleDay
        isUnitBusinessDay
        isHoliday
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


class BookingClient:
    def __init__(self, biz_id: str, session: requests.Session | None = None):
        self.biz_id = biz_id
        self.s = session or requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Origin": "https://m.booking.naver.com",
                "Referer": f"https://m.booking.naver.com/booking/13/bizes/{biz_id}",
                "x-booking-client-device": "pc",
                "x-booking-service-target": "map-pc",
            }
        )

    def _post(self, op: str, variables: dict, query: str) -> dict:
        r = self.s.post(
            GRAPHQL_URL,
            params={"opName": op},
            json={"operationName": op, "variables": variables, "query": query},
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"{op} GraphQL errors: {body['errors']}")
        return body["data"]

    def biz_items(self) -> list[dict]:
        data = self._post(
            "bizItems",
            {"input": {"businessId": self.biz_id, "lang": "ko", "projections": "RESOURCE"}},
            BIZ_ITEMS_QUERY,
        )
        return data.get("bizItems") or []

    def hourly_schedule(self, biz_item_id: str, start: date, end: date) -> list[dict]:
        data = self._post(
            "hourlySchedule",
            {
                "scheduleParams": {
                    "businessTypeId": 13,
                    "businessId": self.biz_id,
                    "bizItemId": biz_item_id,
                    "startDateTime": f"{start.isoformat()}T00:00:00",
                    "endDateTime": f"{end.isoformat()}T23:59:59",
                    "fixedTime": True,
                    "includesHolidaySchedules": True,
                }
            },
            HOURLY_SCHEDULE_QUERY,
        )
        sched = (data.get("schedule") or {}).get("bizItemSchedule") or {}
        return sched.get("hourly") or []


# ---------------------------------------------------------------------------
# 판정 로직
# ---------------------------------------------------------------------------


def is_item_open(item: dict) -> bool:
    setting = item.get("bookableSettingJson") or {}
    if isinstance(setting, str):
        try:
            setting = json.loads(setting)
        except ValueError:
            setting = {}
    return (
        bool(item.get("isImp", True))
        and not item.get("isClosedBooking", False)
        and setting.get("isOpened", True) is not False
        and not setting.get("isPaused", False)
    )


def available_slots(hourly: list[dict], cfg: Config, now: datetime) -> list[datetime]:
    out = []
    for s in hourly:
        if not (s.get("isUnitBusinessDay") and s.get("isUnitSaleDay")) or s.get("isHoliday"):
            continue
        if (s.get("unitBookingCount") or 0) >= (s.get("unitStock") or 0):
            continue
        t = datetime.strptime(s["unitStartTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        if t <= now:
            continue
        hm = t.strftime("%H:%M")
        if cfg.time_from and hm < cfg.time_from:
            continue
        if cfg.time_to and hm > cfg.time_to:
            continue
        if cfg.weekdays is not None and t.weekday() not in cfg.weekdays:
            continue
        out.append(t)
    return sorted(set(out))


@dataclass
class CheckResult:
    open_items: list[dict] = field(default_factory=list)
    slots: dict[str, list[datetime]] = field(default_factory=dict)  # bizItemId -> slots

    @property
    def is_open(self) -> bool:
        return bool(self.open_items)

    @property
    def has_slots(self) -> bool:
        return any(self.slots.values())

    def slot_keys(self) -> set[str]:
        return {f"{i}|{t.isoformat()}" for i, ts in self.slots.items() for t in ts}


def check(client: BookingClient, cfg: Config, now: datetime | None = None) -> CheckResult:
    now = now or datetime.now(KST)
    res = CheckResult(open_items=[it for it in client.biz_items() if is_item_open(it)])
    today = now.date()
    last = today + timedelta(days=cfg.days_ahead - 1)
    for item in res.open_items:
        iid = str(item["bizItemId"])
        slots: list[datetime] = []
        start = today
        while start <= last:  # 웹과 동일하게 5일 단위로 조회
            end = min(start + timedelta(days=4), last)
            slots += available_slots(client.hourly_schedule(iid, start, end), cfg, now)
            start = end + timedelta(days=1)
        res.slots[iid] = sorted(set(slots))
    return res


# ---------------------------------------------------------------------------
# 상태 & 알림
# ---------------------------------------------------------------------------


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"available": False, "slots": [], "errors": 0}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def format_slots(res: CheckResult, only: set[str] | None = None, limit_days: int = 10) -> str:
    names = {str(i["bizItemId"]): i.get("name") or str(i["bizItemId"]) for i in res.open_items}
    lines = []
    for iid, ts in res.slots.items():
        ts = [t for t in ts if only is None or f"{iid}|{t.isoformat()}" in only]
        if not ts:
            continue
        if len(res.slots) > 1:
            lines.append(f"\n<b>[{names.get(iid, iid)}]</b>")
        by_day: dict[date, list[str]] = {}
        for t in ts:
            by_day.setdefault(t.date(), []).append(t.strftime("%H:%M"))
        for i, (d, hms) in enumerate(by_day.items()):
            if i >= limit_days:
                lines.append(f"… 외 {len(by_day) - limit_days}일")
                break
            lines.append(f"• {d.month}/{d.day}({WEEKDAY_KO[d.weekday()]}) {', '.join(hms)}")
    return "\n".join(lines)


def decide_message(cfg: Config, prev: dict, res: CheckResult) -> str | None:
    now_available = res.is_open and res.has_slots
    prev_keys = set(prev.get("slots", []))
    link = f'\n\n👉 <a href="{cfg.booking_url}">예약하러 가기</a>'
    if now_available and not prev.get("available"):
        return f"🟢 <b>{cfg.place_name} 예약 열림!</b>\n빈 시간:\n{format_slots(res)}{link}"
    if now_available:
        new = res.slot_keys() - prev_keys
        if new:
            return f"🆕 <b>{cfg.place_name} 새 빈 시간</b>\n{format_slots(res, only=new)}{link}"
        return None
    if prev.get("available") and cfg.notify_close:
        return f"🔴 {cfg.place_name} 예약 마감/닫힘"
    return None


def send_telegram(cfg: Config, text: str, dry_run: bool = False) -> None:
    text = text[:4000]
    if dry_run or not (cfg.telegram_token and cfg.telegram_chat_id):
        print("----- [알림] -----\n" + text + "\n------------------")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
        json={
            "chat_id": cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    r.raise_for_status()


def run_once(cfg: Config, client: BookingClient, dry_run: bool = False) -> int:
    state = load_state(cfg.state_file)
    try:
        res = check(client, cfg)
    except Exception as e:  # 네트워크/차단/스키마 변경
        state["errors"] = state.get("errors", 0) + 1
        log.error("확인 실패 (%d회 연속): %s", state["errors"], e)
        if state["errors"] == cfg.error_alert_threshold:
            send_telegram(cfg, f"⚠️ check failed {state['errors']}x: {e}", dry_run)
        save_state(cfg.state_file, state)
        return 1

    log.info(
        "open_items=%d slots=%d",
        len(res.open_items),
        sum(len(v) for v in res.slots.values()),
    )
    msg = decide_message(cfg, state, res)
    if msg:
        send_telegram(cfg, msg, dry_run)
    save_state(
        cfg.state_file,
        {
            "available": res.is_open and res.has_slots,
            "slots": sorted(res.slot_keys()),
            "errors": 0,
            "checked_at": datetime.now(KST).isoformat(timespec="seconds"),
        },
    )
    return 0


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = Config.from_env()
    if args.test_telegram:
        send_telegram(cfg, "✅ test")
        return 0
    client = BookingClient(cfg.biz_id)
    if args.once:
        return run_once(cfg, client, args.dry_run)
    while True:
        run_once(cfg, client, args.dry_run)
        time.sleep(cfg.interval_sec + random.uniform(0, cfg.interval_sec * 0.2))


if __name__ == "__main__":
    sys.exit(main())
